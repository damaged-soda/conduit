"""规则面：策略 → 规则串，以及 render 把策略拼进订阅（顺序）。"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.models import AccessId, EndpointId, Node  # noqa: E402
from conduit.policy import DEFAULT_POLICY, policy_rules  # noqa: E402
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


def test_custom_final_group():
    rules = build_subscription([_node("🇯🇵 JP 01")], {}, policy={"final": "JP"})["rules"]
    assert rules[-1] == "MATCH,JP"
