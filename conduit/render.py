"""render：把（已打标 / 剔除后的 **active**）节点 + 调用方输入，渲染成一份 mihomo 配置。

输入契约：`nodes` 必须是「允许进入配置」的 active 节点（隔离区 / 长期不健康的已在上游剔除）。
render 不做 quarantine 过滤。

v1（最小可用）：
- 单 `PROXY` fallback 组 over 所有 active 节点；
- direct-list 落三处：DIRECT 规则（最前）+ fake-ip 放行 + TUN route-exclude；
- overlay 驱动 listen / controller / tun / dns；默认不开 allow-lan（生产安全）；
- proxy 名去重 + 避开保留/group 名；空节点 fail-closed；params 不得覆盖核心身份字段；
  controller 非 loopback 且无 secret 时拒绝生成。

不变量见 CONSTRAINTS.md，由 tests/ 的 golden 不变量直接断言本函数产出。

TODO：tag 表达式分组（v1 单组）、各协议字段完整映射、proxy 名 ↔ access_id 稳定映射（v1 仅去重）、
domain_wildcard → fake-ip 语义对齐、IPv6 controller bind 解析、validate 里做输入校验。
"""

from __future__ import annotations

import hashlib

import yaml

from .models import Node

_HEALTH_URL = "http://www.gstatic.com/generate_204"
_RESERVED_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE", "PROXY"}
_CORE_KEYS = {"name", "type", "server", "port"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _short(aid_value: str) -> str:
    return hashlib.sha1(aid_value.encode()).hexdigest()[:6]


def _assign_names(nodes: list[Node]) -> list[str]:
    """给每个节点一个去重、且不撞保留名/group 名的 proxy 名。v1 用 raw_name(+access_id 短哈希)。"""
    used: set[str] = set()
    out: list[str] = []
    for n in nodes:
        base = n.raw_name or "node"
        name = base if base not in used and base not in _RESERVED_NAMES else f"{base}-{_short(n.access_id.value)}"
        i = 2
        while name in used or name in _RESERVED_NAMES:
            name = f"{base}-{_short(n.access_id.value)}-{i}"
            i += 1
        used.add(name)
        out.append(name)
    return out


def _node_to_proxy(n: Node, name: str) -> dict:
    ep = n.access_id.endpoint
    safe = {k: v for k, v in (n.params or {}).items() if k not in _CORE_KEYS}  # params 不得覆盖核心身份
    return {"name": name, "type": ep.type, "server": ep.server, "port": ep.port, **safe}


def _direct_rules(direct: dict) -> list[str]:
    rules: list[str] = []
    for d in direct.get("domain_exact", []):
        rules.append(f"DOMAIN,{d},DIRECT")
    for d in direct.get("domain_suffix", []):
        rules.append(f"DOMAIN-SUFFIX,{d},DIRECT")
    for d in direct.get("domain_wildcard", []):
        rules.append(f"DOMAIN-WILDCARD,{d},DIRECT")
    for c in direct.get("ip_cidr", []):
        rtype = "IP-CIDR6" if ":" in c else "IP-CIDR"
        rules.append(f"{rtype},{c},DIRECT,no-resolve")
    return rules


def _fake_ip_filter(direct: dict) -> list[str]:
    out: list[str] = list(direct.get("domain_exact", []))
    out += [f"+.{s}" for s in direct.get("domain_suffix", [])]
    out += [f"+.{w[2:]}" if w.startswith("*.") else w for w in direct.get("domain_wildcard", [])]
    return out


def build_config(nodes: list[Node], direct: dict, overlay: dict) -> dict:
    """渲染成 mihomo 配置 dict（rules 顺序关键：direct-list 必须在最前）。"""
    if not nodes:
        raise ValueError("render: 无 active 节点，fail-closed 拒绝生成空配置")

    names = _assign_names(nodes)
    proxies = [_node_to_proxy(n, nm) for n, nm in zip(nodes, names)]

    listen = overlay.get("listen", "127.0.0.1:7890")
    host, _, port = listen.rpartition(":")
    cfg: dict = {"mixed-port": int(port)}

    # 默认不开 allow-lan（生产安全）；仅当 overlay 明确要求 / listen 绑通配时才开
    if overlay.get("allow_lan") or host in ("0.0.0.0", "*", "::"):
        cfg["allow-lan"] = True
        cfg["bind-address"] = "*"
    cfg["mode"] = "rule"

    controller = overlay.get("controller", {})
    bind = controller.get("bind")
    if bind:
        chost, _, _ = bind.rpartition(":")
        if chost not in _LOOPBACK and not controller.get("secret"):
            raise ValueError(f"render: controller 绑定非 loopback({bind}) 但无 secret —— 拒绝生成（生产安全）")
        cfg["external-controller"] = bind
        if controller.get("secret"):  # 真实 secret 部署期注入；*_ref 不进配置
            cfg["secret"] = controller["secret"]

    if overlay.get("dns", {}).get("fake_ip"):
        cfg["dns"] = {
            "enable": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": _fake_ip_filter(direct),
            "nameserver": ["https://1.1.1.1/dns-query"],
        }

    tun = overlay.get("tun", {})
    if tun.get("enable"):
        excludes = list(direct.get("ip_cidr", []))
        for c in tun.get("route_exclude", []) or []:  # 合入 per-target 专属排除
            if c not in excludes:
                excludes.append(c)
        cfg["tun"] = {
            "enable": True,
            "stack": "system",
            "auto-route": True,
            "auto-detect-interface": True,
            "strict-route": True,
            "dns-hijack": ["any:53", "tcp://any:53"],
            "route-exclude-address": excludes,
        }

    cfg["proxies"] = proxies
    cfg["proxy-groups"] = [
        {
            "name": "PROXY",
            "type": "fallback",
            "proxies": names,
            "url": _HEALTH_URL,
            "interval": 60,
            "timeout": 2000,
            "lazy": False,
            "expected-status": "204",
        }
    ]
    cfg["rules"] = _direct_rules(direct) + ["MATCH,PROXY"]
    return cfg


def render(nodes: list[Node], target: str, direct_list: dict, overlay: dict) -> str:
    """渲染某个 target 的 mihomo 配置（YAML 字符串）。target 暂仅作标签。"""
    cfg = build_config(nodes, direct_list, overlay)
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


def build_subscription(nodes: list[Node], direct: dict, full: bool = False) -> dict:
    """订阅用配置：proxies + PROXY 组 + 规则；`full=True` 再加 dns(fake-ip) + tun。

    **不含** mixed-port / external-controller / allow-lan —— 那些是客户端实例设置，订阅只给「可移植」的部分
    （proxies/组/规则，full 时加 dns/tun），让 clash-verge / mihomo 无缝导入。
    """
    cfg: dict = {}
    if full:
        cfg["dns"] = {
            "enable": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": _fake_ip_filter(direct),
            "nameserver": ["https://1.1.1.1/dns-query"],
        }
        cfg["tun"] = {
            "enable": True,
            "stack": "system",
            "auto-route": True,
            "auto-detect-interface": True,
            "strict-route": True,
            "dns-hijack": ["any:53", "tcp://any:53"],
            "route-exclude-address": list(direct.get("ip_cidr", [])),
        }
    if nodes:
        names = _assign_names(nodes)
        cfg["proxies"] = [_node_to_proxy(n, nm) for n, nm in zip(nodes, names)]
        cfg["proxy-groups"] = [
            {
                "name": "PROXY",
                "type": "fallback",
                "proxies": names,
                "url": _HEALTH_URL,
                "interval": 60,
                "timeout": 2000,
                "lazy": False,
                "expected-status": "204",
            }
        ]
        cfg["rules"] = _direct_rules(direct) + ["MATCH,PROXY"]
    else:  # 无节点：给个合法的全直连配置，别产出坏订阅
        cfg["proxies"] = []
        cfg["rules"] = ["MATCH,DIRECT"]
    return cfg


def render_subscription(nodes: list[Node], direct_list: dict, full: bool = False) -> str:
    return yaml.safe_dump(build_subscription(nodes, direct_list, full), sort_keys=False, allow_unicode=True)
