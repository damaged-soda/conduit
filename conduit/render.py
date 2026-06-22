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
from .policy import DEFAULT_POLICY, policy_rules, rule_providers_block
from .tags import region_of

_HEALTH_URL = "http://www.gstatic.com/generate_204"

# 订阅兜底直连：私网 / loopback / link-local / CGNAT(含 tailscale 100.64/10)。
# 防"全代理"把本地 / 私有网流量也抓走（rule#0 基线）。调用方的 direct-list 再叠在其上。
_BASELINE_DIRECT = [
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10",
]
_RESERVED_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE", "PROXY"}
_CORE_KEYS = {"name", "type", "server", "port"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _short(aid_value: str) -> str:
    return hashlib.sha1(aid_value.encode()).hexdigest()[:6]


def _assign_names(nodes: list[Node], extra_reserved: set[str] = frozenset()) -> list[str]:
    """给每个节点一个去重、且不撞保留名/group 名的 proxy 名。v1 用 raw_name(+access_id 短哈希)。

    extra_reserved：额外要避开的名字（如动态生成的地区组名 / AUTO），防 proxy 名撞组名。
    """
    reserved = _RESERVED_NAMES | set(extra_reserved)
    used: set[str] = set()
    out: list[str] = []
    for n in nodes:
        base = n.raw_name or "node"
        name = base if base not in used and base not in reserved else f"{base}-{_short(n.access_id.value)}"
        i = 2
        while name in used or name in reserved:
            name = f"{base}-{_short(n.access_id.value)}-{i}"
            i += 1
        used.add(name)
        out.append(name)
    return out


def _fallback_group(name: str, proxies: list[str]) -> dict:
    """一个 fallback 组（用首个存活节点，仅故障时切换 → 人无感，目标 #3）。"""
    return {
        "name": name,
        "type": "fallback",
        "proxies": proxies,
        "url": _HEALTH_URL,
        "interval": 60,
        "timeout": 2000,
        "lazy": False,
        "expected-status": "204",
    }


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


def build_subscription(
    nodes: list[Node], direct: dict, full: bool = False, tags: dict | None = None, policy: dict | None = None
) -> dict:
    """订阅用配置：标准 clash 骨架 + 按 region 分组的 proxy-groups + 规则；`full=True` 再加 dns+tun。

    必须带标准顶层骨架（port/mode/log-level…）：clash-verge 等 GUI 的导入校验会**静默拒绝**只有
    proxies/groups/rules 的配置。external-controller 不放进来（客户端自管 + 安全）。

    分组结构（地区分组 + 顶层选择）：
      - `PROXY` (select)：顶层手动选 [AUTO, <各地区>]，默认 AUTO；规则只引用组名。
      - `AUTO` (fallback)：所有非隔离节点，全局故障转移。
      - 每个 region 一个 fallback 组。

    tags：`{access_id: {"region": override|None, "quarantined": bool}}`（service 传入）。隔离的剔除；
    region 优先用 override，否则 `region_of(raw_name)`。标签按 access_id 存 → 跟着节点走，不跟订阅。
    """
    tags = tags or {}
    policy = policy or DEFAULT_POLICY
    cfg: dict = {
        "port": 7890,
        "socks-port": 7891,
        "mixed-port": 7893,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "unified-delay": True,
        "ipv6": False,
    }
    if full:
        # rule#0 三处之二（fake-ip-filter + tun route-exclude）：从 policy 里 to==DIRECT 的 route 派生
        # 显式域名/IP（tailnet/DERP/控制面），让它们解析真 IP + 不被 TUN 抓走。
        d_domains: list[str] = []
        d_ips: list[str] = []
        for r in policy.get("routes", []):
            if r.get("to") == "DIRECT":
                d_domains += [f"+.{x}" for x in r.get("domain_suffix", [])] + list(r.get("domain", []))
                d_ips += list(r.get("ip_cidr", []))
        pdns = policy.get("dns", {})
        dns = {
            "enable": True,
            # ipv6:true + tun.inet6-address：必须让 TUN 同时接管 IPv6，否则系统 IPv6 默认路由仍在物理网卡上，
            # 浏览器优先走 IPv6/HTTP3 会**绕过代理直连**（IPv6 leak）→ 出口变成本地真实地区（如 CN）→ claude.ai 等按区域封。
            "ipv6": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            # default-nameserver（引导 DNS）必须有：否则连 DoH 服务器都没法解析 → 整个 DNS 瘫、出网断。
            # 含 system → 任何环境都能引导（用 OS 解析器）。可被 policy.dns.default_nameserver 覆盖。
            "default-nameserver": pdns.get("default_nameserver") or ["system", "223.5.5.5", "8.8.8.8"],
            "nameserver": pdns.get("nameserver") or ["https://1.1.1.1/dns-query"],
            "fake-ip-filter": ["*.lan", "*.local", "*.arpa", *_fake_ip_filter(direct), *d_domains],
        }
        if pdns.get("fallback"):
            dns["fallback"] = pdns["fallback"]
        nsp = pdns.get("nameserver_policy", {})
        if nsp:  # 如 {'+.ts.net': '100.100.100.100'} —— tailnet 走 MagicDNS
            dns["nameserver-policy"] = nsp
        cfg["dns"] = dns
        cfg["ipv6"] = True  # 全局开 IPv6，配合 tun.inet6-address 让 TUN 接管 IPv6，堵住 IPv6 leak
        cfg["tun"] = {
            "enable": True,
            "stack": "system",
            "auto-route": True,
            "auto-detect-interface": True,
            "strict-route": True,
            # 给 TUN 一个 IPv6 地址，auto-route 才会把 IPv6 默认路由 (::/0) 也指向 TUN → IPv6 流量进代理。
            "inet6-address": ["fdfe:dcba:9876::1/126"],
            "dns-hijack": ["any:53", "tcp://any:53"],
            # route-exclude 已含 ::1 / fc00::/7（含 tailscale ULA IPv6）/ fe80::/10 → 本地+tailnet IPv6 仍直连，SSH 不断。
            "route-exclude-address": _BASELINE_DIRECT + list(direct.get("ip_cidr", [])) + d_ips,
        }

    # 剔除隔离节点 + 算每个节点的 region（override 优先）
    active: list[tuple[Node, str]] = []
    for n in nodes:
        t = tags.get(n.access_id.value, {})
        if t.get("quarantined"):
            continue
        region = (t.get("region") or "").strip() or region_of(n.raw_name)
        active.append((n, region))

    if not active:  # 无可用节点：给个合法的全直连配置，别产出坏订阅
        cfg["proxies"] = []
        cfg["rules"] = ["MATCH,DIRECT"]
        return cfg

    region_order: list[str] = []  # 按出现顺序，稳定
    for _, r in active:
        if r not in region_order:
            region_order.append(r)

    nodes_only = [n for n, _ in active]
    names = _assign_names(nodes_only, extra_reserved={"AUTO", *region_order})
    cfg["proxies"] = [_node_to_proxy(n, nm) for n, nm in zip(nodes_only, names)]

    by_region: dict[str, list[str]] = {}
    for (_, r), nm in zip(active, names):
        by_region.setdefault(r, []).append(nm)

    groups: list[dict] = [{"name": "PROXY", "type": "select", "proxies": ["AUTO", *region_order]}]
    groups.append(_fallback_group("AUTO", names))
    groups += [_fallback_group(r, by_region[r]) for r in region_order]
    cfg["proxy-groups"] = groups

    # rule-providers（被 routes 引用的 .mrs）；指向「当前不存在的组」的 route / final 落到 PROXY，保证合法
    valid = {"DIRECT", "REJECT", "PROXY", "AUTO", *region_order}
    providers = rule_providers_block(policy)
    if providers:
        cfg["rule-providers"] = providers
    cfg["rules"] = subscription_rules(direct, policy, lambda to: to if to in valid else "PROXY")
    return cfg


def subscription_rules(direct: dict, policy: dict, resolve=None) -> list[str]:
    """订阅的完整规则序：私网/tailnet 兜底(rule#0) → 调用方 direct-list → 策略路由 → MATCH,final。

    规则不依赖具体节点（只依赖 direct-list + 策略），可单独给页面展示。resolve（render 传入）把指向
    不存在组的 route / final 落到 PROXY；默认 identity（页面只读视图展示意图目标）。
    """
    resolve = resolve or (lambda to: to)
    final = resolve(policy.get("final", "PROXY"))
    return (
        _direct_rules({"ip_cidr": _BASELINE_DIRECT})
        + _direct_rules(direct)
        + policy_rules(policy, resolve)
        + [f"MATCH,{final}"]
    )


def render_subscription(
    nodes: list[Node], direct_list: dict, full: bool = False, tags: dict | None = None, policy: dict | None = None
) -> str:
    return yaml.safe_dump(
        build_subscription(nodes, direct_list, full, tags, policy), sort_keys=False, allow_unicode=True
    )
