"""Golden 配置不变量：断言生成出来的 mihomo 配置满足 CONSTRAINTS。

现在跑在手写夹具上（fixtures/mihomo.min.yaml），它代表 render() 应当产出的形态；
等 render() 实现后，把夹具换成 render() 的真实输出即可。
零网络、最安全的一层（见 TESTING.md 第 1 层）。
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "mihomo.min.yaml"

# 调用方喂入的 direct-list（占位，对应夹具）
DIRECT = {
    "domain_suffix": ["example-internal"],
    "ip_cidr": ["10.0.0.0/8"],
}

TERMINALS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}


def load(path: pathlib.Path = FIXTURE) -> dict:
    return yaml.safe_load(path.read_text())


def _rule_parts(cfg: dict) -> list[list[str]]:
    return [[p.strip() for p in r.split(",")] for r in cfg.get("rules", [])]


def rule_targets(cfg: dict) -> set[str]:
    out: set[str] = set()
    for parts in _rule_parts(cfg):
        if parts[0] == "MATCH":
            out.add(parts[1])
        elif len(parts) >= 3:
            out.add(parts[2])
    return out


def group_names(cfg: dict) -> set[str]:
    return {g["name"] for g in cfg.get("proxy-groups", [])}


def proxy_names(cfg: dict) -> set[str]:
    return {p["name"] for p in cfg.get("proxies", [])}


def test_direct_list_lands_in_three_places():
    """每个 direct 目的地必须同时出现在：DIRECT 规则 / fake-ip 放行 / TUN route-exclude。"""
    cfg = load()
    violations: list[str] = []

    # ① DIRECT 规则
    suffix_direct = {p[1] for p in _rule_parts(cfg) if p[0] == "DOMAIN-SUFFIX" and p[2:3] == ["DIRECT"]}
    cidr_direct = {p[1] for p in _rule_parts(cfg) if p[0] == "IP-CIDR" and p[2:3] == ["DIRECT"]}
    for d in DIRECT["domain_suffix"]:
        if d not in suffix_direct:
            violations.append(f"domain_suffix {d} 缺 DIRECT 规则")
    for c in DIRECT["ip_cidr"]:
        if c not in cidr_direct:
            violations.append(f"ip_cidr {c} 缺 DIRECT 规则")

    # ② fake-ip 放行（用 fake-ip 时）
    dns = cfg.get("dns", {})
    if dns.get("enhanced-mode") == "fake-ip":
        joined = " ".join(dns.get("fake-ip-filter", []))
        for d in DIRECT["domain_suffix"]:
            if d not in joined:
                violations.append(f"domain_suffix {d} 不在 fake-ip-filter")

    # ③ TUN 路由排除（开 TUN 时）
    tun = cfg.get("tun", {})
    if tun.get("enable"):
        excl = set(tun.get("route-exclude-address", []))
        for c in DIRECT["ip_cidr"]:
            if c not in excl:
                violations.append(f"ip_cidr {c} 不在 tun.route-exclude-address")

    assert not violations, "direct-list 三处覆盖不一致:\n" + "\n".join(violations)


def test_rules_only_target_groups_or_terminals():
    """关键隔离不变量：规则只引用 group 名或终端动作，绝不直接指向具体节点。"""
    cfg = load()
    allowed = group_names(cfg) | TERMINALS
    bad = rule_targets(cfg) - allowed
    leaked = bad & proxy_names(cfg)
    assert not leaked, f"规则直接指向了具体节点（应只引用 group）：{leaked}"
    assert not bad, f"规则指向了未定义的 group/terminal：{bad}"


def test_groups_reference_defined_members():
    """proxy-group 的成员必须都已定义（无悬空引用）。"""
    cfg = load()
    names = proxy_names(cfg) | group_names(cfg)
    dangling = [
        f"{g['name']} -> {m}"
        for g in cfg.get("proxy-groups", [])
        for m in g.get("proxies", [])
        if m not in names
    ]
    assert not dangling, f"group 引用了不存在的成员：{dangling}"
