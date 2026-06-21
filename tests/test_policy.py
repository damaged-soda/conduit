"""规则面：策略 → 规则串，以及 render 把策略拼进订阅（顺序）。"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.models import AccessId, EndpointId, Node  # noqa: E402
from conduit.policy import DEFAULT_POLICY, policy_rules, rule_providers_block  # noqa: E402
from conduit.render import build_subscription  # noqa: E402


def _node(name: str) -> Node:
    ep = EndpointId(type="trojan", server="s.com", port=443)
    return Node(access_id=AccessId(value=name, endpoint=ep), raw_name=name, params={}, source="t")


def test_policy_rules_content_and_order():
    rules = policy_rules(DEFAULT_POLICY)
    assert "GEOSITE,category-ads-all,REJECT" in rules
    assert "GEOSITE,cn,DIRECT" in rules
    assert "GEOIP,CN,DIRECT,no-resolve" in rules
    # reject 在 direct 之前
    assert rules.index("GEOSITE,category-ads-all,REJECT") < rules.index("GEOSITE,cn,DIRECT")


def test_empty_policy_yields_no_rules():
    assert policy_rules({"final": "PROXY"}) == []


def test_subscription_rule_order():
    rules = build_subscription([_node("🇭🇰 HK 01")], {})["rules"]
    assert rules[0] == "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve"  # rule#0 私网兜底最前
    assert "GEOSITE,cn,DIRECT" in rules  # 中国直连
    assert rules[-1] == "MATCH,PROXY"  # 末尾兜底
    # geo 在私网兜底之后、MATCH 之前
    assert 0 < rules.index("GEOSITE,cn,DIRECT") < len(rules) - 1


def test_explicit_matchers():
    pol = {"routes": [{"to": "DIRECT", "domain_suffix": ["tailscale.com"], "domain": ["derp.x"],
                       "ip_cidr": ["123.57.92.37/32"], "process_name": ["ssh"], "dst_port": ["22"]}], "final": "PROXY"}
    rules = policy_rules(pol)
    assert "DOMAIN-SUFFIX,tailscale.com,DIRECT" in rules
    assert "DOMAIN,derp.x,DIRECT" in rules
    assert "IP-CIDR,123.57.92.37/32,DIRECT,no-resolve" in rules
    assert "PROCESS-NAME,ssh,DIRECT" in rules and "DST-PORT,22,DIRECT" in rules


def test_full_mode_dns_tun_from_direct_routes():
    pol = {"routes": [{"to": "DIRECT", "domain_suffix": ["ts.net"], "ip_cidr": ["123.57.92.37/32"]}],
           "final": "PROXY", "dns": {"nameserver_policy": {"+.ts.net": "100.100.100.100"}}}
    cfg = build_subscription([_node("🇭🇰 HK 01")], {}, full=True, policy=pol)
    assert "+.ts.net" in cfg["dns"]["fake-ip-filter"]
    assert cfg["dns"]["nameserver-policy"] == {"+.ts.net": "100.100.100.100"}
    assert "123.57.92.37/32" in cfg["tun"]["route-exclude-address"]


def test_full_dns_has_default_nameserver():
    cfg = build_subscription([_node("🇭🇰 HK 01")], {}, full=True)
    assert "system" in cfg["dns"]["default-nameserver"]  # 引导 DNS（关键修复：没它出网会断）


def test_full_dns_configurable():
    pol = {"final": "PROXY", "dns": {
        "default_nameserver": ["223.6.6.6"], "nameserver": ["https://dns.alidns.com/dns-query"], "fallback": ["8.8.8.8"]}}
    cfg = build_subscription([_node("🇭🇰 HK 01")], {}, full=True, policy=pol)["dns"]
    assert cfg["default-nameserver"] == ["223.6.6.6"]
    assert cfg["nameserver"] == ["https://dns.alidns.com/dns-query"]
    assert cfg["fallback"] == ["8.8.8.8"]


def test_custom_final_group():
    rules = build_subscription([_node("🇯🇵 JP 01")], {}, policy={"final": "JP"})["rules"]
    assert rules[-1] == "MATCH,JP"


def test_rule_set_routes_and_providers_block():
    rules = policy_rules(DEFAULT_POLICY)
    assert "RULE-SET,ai,US" in rules and "RULE-SET,netflix,HK" in rules  # 类别 → 地区组
    block = rule_providers_block(DEFAULT_POLICY)
    assert set(block) == {"ai", "netflix", "disney", "youtube"}  # 只含被引用的
    assert block["ai"]["format"] == "mrs" and block["ai"]["behavior"] == "domain"
    assert block["netflix"]["url"].endswith("netflix.mrs")


def test_resolve_falls_back_to_final_for_missing_group():
    # 没有 US 节点 → AI 那条 route 落到 final；HK 存在 → 保留
    rules = policy_rules(DEFAULT_POLICY, resolve=lambda to: to if to in {"DIRECT", "REJECT", "HK"} else "PROXY")
    assert "RULE-SET,ai,PROXY" in rules and "RULE-SET,ai,US" not in rules
    assert "RULE-SET,netflix,HK" in rules


def test_rule_providers_only_when_used():
    cfg = build_subscription([_node("🇭🇰 HK 01")], {})
    assert "rule-providers" in cfg and "netflix" in cfg["rule-providers"]
    cfg2 = build_subscription([_node("🇭🇰 HK 01")], {}, policy={"final": "PROXY"})
    assert "rule-providers" not in cfg2  # 无 rule_set 的策略 → 无 providers 块
