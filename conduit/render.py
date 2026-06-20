"""render：把（已打标 / 剔除后的）节点 + 调用方输入，渲染成一份 mihomo 配置。

v1（最小可用）：
- 单 `PROXY` fallback 组 over 所有 active 节点；
- direct-list 落到三处：DIRECT 规则（最前）+ fake-ip 放行 + TUN route-exclude；
- overlay 驱动 listen / controller / tun / dns；默认不开 allow-lan（生产安全）。

不变量见 CONSTRAINTS.md，由 tests/ 的 golden 不变量直接断言本函数产出。

TODO：tag 表达式分组（v1 单组）、各协议字段完整映射、proxy 名 ↔ access_id 稳定映射、
空组兜底、controller secret 部署期注入、生产 controller 必须 loopback 的不变量。
"""

from __future__ import annotations

import yaml

from .models import Node

_HEALTH_URL = "http://www.gstatic.com/generate_204"


def _proxy_name(n: Node) -> str:
    # TODO: 稳定的 proxy 名 ↔ access_id 映射（健康回路要用）；v1 先用原始名。
    return n.raw_name


def _node_to_proxy(n: Node) -> dict:
    ep = n.access_id.endpoint
    proxy = {"name": _proxy_name(n), "type": ep.type, "server": ep.server, "port": ep.port}
    proxy.update(n.params or {})  # 协议字段（cipher/uuid/password/sni…）
    return proxy


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
    proxies = [_node_to_proxy(n) for n in nodes]
    names = [p["name"] for p in proxies]

    listen = overlay.get("listen", "127.0.0.1:7890")
    host, _, port = listen.rpartition(":")
    cfg: dict = {"mixed-port": int(port)}

    # 默认不开 allow-lan（生产安全）；仅当 overlay 明确要求 / listen 绑通配时才开
    if overlay.get("allow_lan") or host in ("0.0.0.0", "*", "::"):
        cfg["allow-lan"] = True
        cfg["bind-address"] = "*"
    cfg["mode"] = "rule"

    controller = overlay.get("controller", {})
    if controller.get("bind"):
        cfg["external-controller"] = controller["bind"]
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

    if overlay.get("tun", {}).get("enable"):
        cfg["tun"] = {
            "enable": True,
            "stack": "system",
            "auto-route": True,
            "auto-detect-interface": True,
            "strict-route": True,
            "dns-hijack": ["any:53"],
            "route-exclude-address": list(direct.get("ip_cidr", [])),
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
