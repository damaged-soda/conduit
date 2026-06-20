"""Golden 配置不变量：断言生成的 mihomo 配置满足 CONSTRAINTS（TESTING.md 第 1 层，零网络）。

每个不变量是一个返回「违规列表」的函数：good 夹具应当无违规，**坏夹具（mihomo.bad.yaml）必须被抓出违规**——
后者防「假绿」（检查写错了会让坏配置也过）。render() 实现后，把 good 夹具换成 render() 真实输出即可。

待补（见 Codex review）：SUB-RULE 递归校验、fake-ip-filter-mode: rule、fallback 健康检查频率预算、
controller 绑定/secret 生产不变量、按不变量拆的多负例语料。
"""

from __future__ import annotations

import ipaddress
import pathlib
import shutil
import subprocess

import pytest
import yaml  # 硬依赖：缺 PyYAML 直接失败，不静默跳过（装 `.[dev]`）

HERE = pathlib.Path(__file__).parent
GOOD = HERE / "fixtures" / "mihomo.min.yaml"
BAD = HERE / "fixtures" / "mihomo.bad.yaml"

# 调用方喂入的结构化 direct-list（占位，对应 good 夹具），覆盖各类型。
DIRECT = {
    "domain_exact": ["host.example-internal"],
    "domain_suffix": ["example-internal"],
    "domain_wildcard": ["*.corp.example"],
    "ip_cidr": ["10.0.0.0/8", "fd00::/8"],  # IPv4 + IPv6
}

TERMINALS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}
LOGICAL = {"AND", "OR", "NOT"}
# direct-list 各类型 → mihomo 规则类型（mihomo 确有 DOMAIN-WILDCARD，语义与 Clash 不同）
DOMAIN_RULE = {"domain_exact": "DOMAIN", "domain_suffix": "DOMAIN-SUFFIX", "domain_wildcard": "DOMAIN-WILDCARD"}


def load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _norm_cidr(s: str) -> str:
    try:
        return str(ipaddress.ip_network(s, strict=False))
    except ValueError:
        return s


def split_top(s: str) -> list[str]:
    """按顶层逗号切分（括号内逗号不切）。逻辑规则 AND/OR/NOT、SUB-RULE 的 payload 内含逗号。"""
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def rules(cfg: dict) -> list[list[str]]:
    return [split_top(r) for r in cfg.get("rules", [])]


def rule_outbound(parts: list[str]) -> str | None:
    """取一条规则的 outbound（group/terminal）。覆盖 MATCH / 逻辑规则 / 普通规则；SUB-RULE 暂不取。"""
    head = parts[0]
    if head == "MATCH":
        return parts[1] if len(parts) > 1 else None
    if head == "SUB-RULE":
        return None  # 子规则引用，单独校验（TODO）
    if head in LOGICAL:
        return parts[-1] if len(parts) > 1 else None
    return parts[2] if len(parts) >= 3 else None


def proxy_names(cfg: dict) -> set[str]:
    return {p["name"] for p in cfg.get("proxies", [])}


def group_names(cfg: dict) -> set[str]:
    return {g["name"] for g in cfg.get("proxy-groups", [])}


def _domain_covered(domain: str, patterns: list[str]) -> bool:
    """domain 是否被 fake-ip-filter 某模式覆盖（精确 / `+.x` / `*.x`）。"""
    for p in patterns:
        if p == domain:
            return True
        if p.startswith("+.") and (domain == p[2:] or domain.endswith("." + p[2:])):
            return True
        if p.startswith("*.") and domain.endswith("." + p[2:]):
            return True
    return False


def _direct_payloads_by_rule_type(cfg: dict, rule_type: str) -> set[str]:
    return {p[1] for p in rules(cfg) if p[0] == rule_type and rule_outbound(p) == "DIRECT"}


# ---- 不变量：返回违规列表（空 = 通过）----

def check_direct_first_and_match(cfg: dict, direct: dict) -> list[str]:
    """direct-list 的 DIRECT 规则必须在规则表最前；最后必须有 MATCH 兜底。"""
    rp = rules(cfg)
    if not rp:
        return ["规则为空"]
    v: list[str] = []
    if rp[-1][0] != "MATCH":
        v.append("最后一条规则不是 MATCH（缺兜底）")

    want = (
        set(direct.get("domain_exact", []))
        | set(direct.get("domain_suffix", []))
        | set(direct.get("domain_wildcard", []))
        | {_norm_cidr(c) for c in direct.get("ip_cidr", [])}
    )
    direct_idx = []
    for i, parts in enumerate(rp):
        if rule_outbound(parts) == "DIRECT" and len(parts) >= 2:
            payload = _norm_cidr(parts[1]) if parts[0].startswith("IP-CIDR") else parts[1]
            if payload in want:
                direct_idx.append(i)
    if not direct_idx:
        return v + ["没找到任何 direct-list 的 DIRECT 规则"]
    non_direct = [i for i in range(len(rp)) if i not in direct_idx]
    if non_direct and max(direct_idx) > min(non_direct):
        v.append(
            f"direct-list 规则不在最前：有非直连规则排在它们之前"
            f"（首个非直连@{min(non_direct)}，末个直连@{max(direct_idx)}）"
        )
    return v


def check_three_places(cfg: dict, direct: dict) -> list[str]:
    """每个 direct 目的地必须同时：① 对应类型的 DIRECT 规则 ② fake-ip 放行（开 fake-ip 时）③ TUN route-exclude（开 auto-route 时）。"""
    v: list[str] = []

    # ① DIRECT 规则，按类型精确校验（exact→DOMAIN / suffix→DOMAIN-SUFFIX / wildcard→DOMAIN-WILDCARD）
    for key, rtype in DOMAIN_RULE.items():
        have = _direct_payloads_by_rule_type(cfg, rtype)
        for d in direct.get(key, []):
            if d not in have:
                v.append(f"{key} {d} 缺 {rtype} DIRECT 规则")
    cidr_direct = {_norm_cidr(p[1]) for p in rules(cfg) if p[0] in ("IP-CIDR", "IP-CIDR6") and rule_outbound(p) == "DIRECT"}
    for c in direct.get("ip_cidr", []):
        if _norm_cidr(c) not in cidr_direct:
            v.append(f"ip_cidr {c} 缺 DIRECT 规则")

    # ② fake-ip 放行（开 fake-ip 时）：对 suffix/wildcard 不只测 apex，还测一个哨兵子域
    dns = cfg.get("dns", {})
    if dns.get("enhanced-mode") == "fake-ip":
        filt = dns.get("fake-ip-filter", [])
        samples: list[str] = list(direct.get("domain_exact", []))
        for d in direct.get("domain_suffix", []):
            samples += [d, "sentinel." + d]
        for w in direct.get("domain_wildcard", []):
            samples.append("sentinel." + (w[2:] if w.startswith("*.") else w))
        for s in samples:
            if not _domain_covered(s, filt):
                v.append(f"域名 {s} 不在 fake-ip-filter（会被解析成假 IP）")

    # ③ TUN 路由排除（开 TUN 且 auto-route 时）：direct CIDR 必须被某条 exclude 覆盖
    tun = cfg.get("tun", {})
    if tun.get("enable"):
        if not tun.get("auto-route"):
            v.append("TUN 开了但 auto-route 未开：route-exclude 语义不明，直连 CIDR 可能仍被劫持")
        else:
            excl = [_norm_cidr(c) for c in tun.get("route-exclude-address", [])]
            excl_nets = []
            for c in excl:
                try:
                    excl_nets.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    pass
            for c in direct.get("ip_cidr", []):
                net = ipaddress.ip_network(c, strict=False)
                if not any(net.version == e.version and net.subnet_of(e) for e in excl_nets):
                    v.append(f"CIDR {c} 未被 tun.route-exclude-address 覆盖")
    return v


def check_rule_targets(cfg: dict) -> list[str]:
    """关键隔离不变量：规则只引用 group 名或终端动作，绝不直接指向具体节点。"""
    allowed = group_names(cfg) | TERMINALS
    nodes = proxy_names(cfg)
    v: list[str] = []
    for parts in rules(cfg):
        if parts[0] == "SUB-RULE":
            continue  # TODO: 递归校验子规则
        t = rule_outbound(parts)
        if t is None:
            continue
        if t in nodes and t not in allowed:
            v.append(f"规则直接指向具体节点 {t}（应只引用 group）")
        elif t not in allowed:
            v.append(f"规则指向未定义的 group/terminal：{t}")
    return v


def check_group_members(cfg: dict) -> list[str]:
    names = proxy_names(cfg) | group_names(cfg)
    return [
        f"{g['name']} -> {m}"
        for g in cfg.get("proxy-groups", [])
        for m in g.get("proxies", [])
        if m not in names
    ]


def check_unique_names(cfg: dict) -> list[str]:
    names = [p["name"] for p in cfg.get("proxies", [])] + [g["name"] for g in cfg.get("proxy-groups", [])]
    v: list[str] = []
    dups = {n for n in names if names.count(n) > 1}
    if dups:
        v.append(f"重名 proxy/group：{dups}")
    clash = {n for n in names if n in TERMINALS}
    if clash:
        v.append(f"proxy/group 名与内置 outbound 冲突：{clash}")
    return v


def all_violations(cfg: dict) -> dict[str, list[str]]:
    return {
        "direct_first": check_direct_first_and_match(cfg, DIRECT),
        "three_places": check_three_places(cfg, DIRECT),
        "rule_targets": check_rule_targets(cfg),
        "group_members": check_group_members(cfg),
        "unique_names": check_unique_names(cfg),
    }


# ---- 测试 ----

def test_good_fixture_is_clean():
    flat = [f"[{k}] {m}" for k, ms in all_violations(load(GOOD)).items() for m in ms]
    assert not flat, "good 夹具有违规:\n" + "\n".join(flat)


def test_bad_fixture_is_caught():
    """坏配置必须被抓到，且这几个关键检查各自都要抓到它对应的坑（防某检查静默失效被别的掩盖）。"""
    v = all_violations(load(BAD))
    assert any(v.values()), "坏夹具竟然没被任何不变量抓到——检查形同虚设"
    for key in ("direct_first", "rule_targets", "group_members"):
        assert v[key], f"检查 {key} 没抓到坏夹具里它应抓的违规"


@pytest.mark.skipif(shutil.which("mihomo") is None, reason="mihomo 未安装")
def test_good_fixture_passes_mihomo_check():
    r = subprocess.run(["mihomo", "-t", "-f", str(GOOD)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
