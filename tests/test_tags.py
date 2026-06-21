"""标签层：region 解析（纯函数）+ render 的地区分组。"""

from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.models import AccessId, EndpointId, Node  # noqa: E402
from conduit.render import build_subscription  # noqa: E402
from conduit.tags import UNKNOWN, normalize_region, region_of  # noqa: E402


def test_normalize_region():
    assert normalize_region("hk") == "HK"
    assert normalize_region("  jp ") == "JP"
    assert normalize_region("") is None and normalize_region(None) is None
    assert normalize_region("流媒体") == "流媒体"  # 非 ascii 标签保留
    for bad in ("AUTO", "PROXY", "DIRECT", "a,b", "x\ny", "z" * 25):
        with pytest.raises(ValueError):
            normalize_region(bad)


def _node(name: str, server: str = "s.com", port: int = 443, **params) -> Node:
    ep = EndpointId(type="trojan", server=server, port=port)
    return Node(access_id=AccessId(value=f"{name}|{server}|{port}", endpoint=ep), raw_name=name, params=params, source="t")


# ---- region 解析 ----

def test_flag_emoji_decode():
    assert region_of("🇭🇰 Hong Kong | 01") == "HK"
    assert region_of("🇯🇵 Tokyo 03") == "JP"
    assert region_of("🇺🇸 LA premium") == "US"
    assert region_of("🇸🇬 x") == "SG"
    assert region_of("🇹🇼 台北") == "TW"


def test_text_beats_flag_on_mismatch():
    # 机场常把台湾标 🇨🇳；文本「Taiwan」应胜出 → TW，不是 CN
    assert region_of("🇨🇳 Taiwan | 06") == "TW"
    assert region_of("🇭🇰 Hong Kong | 01") == "HK"
    assert region_of("🇨🇳 中国 回国") == "CN"  # 文本也是中国 → CN
    assert region_of("🇮🇹 Italy | 01") == "IT"  # 无关键词 → 旗帜兜底


def test_keyword_fallback_no_flag():
    assert region_of("Hong Kong 01") == "HK"
    assert region_of("日本 IEPL 专线") == "JP"
    assert region_of("Singapore-Premium") == "SG"
    assert region_of("洛杉矶 los angeles") == "US"


def test_unknown_region():
    assert region_of("Premium Node 01") == UNKNOWN
    assert region_of("") == UNKNOWN
    assert region_of("V2-高速-01") == UNKNOWN


# ---- 分组 ----

def test_grouping_by_region():
    nodes = [_node("🇭🇰 HK 01", port=1), _node("🇭🇰 HK 02", port=2), _node("🇯🇵 JP 01", port=3), _node("Premium X", port=4)]
    cfg = build_subscription(nodes, {})
    groups = {g["name"]: g for g in cfg["proxy-groups"]}
    assert groups["PROXY"]["type"] == "select"
    assert groups["PROXY"]["proxies"][0] == "AUTO"  # 默认走 AUTO
    assert {"HK", "JP", UNKNOWN, "AUTO", "PROXY"} <= set(groups)
    assert len(groups["HK"]["proxies"]) == 2
    assert len(groups["AUTO"]["proxies"]) == 4  # 全部非隔离
    assert groups["HK"]["type"] == "fallback"
    assert "HK" in groups["PROXY"]["proxies"] and UNKNOWN in groups["PROXY"]["proxies"]


def test_quarantine_excludes_node_and_empty_region():
    n1, n2 = _node("🇭🇰 HK 01", port=1), _node("🇯🇵 JP 01", port=2)
    cfg = build_subscription([n1, n2], {}, tags={n2.access_id.value: {"quarantined": True}})
    assert len(cfg["proxies"]) == 1  # JP 被隔离
    names = {g["name"] for g in cfg["proxy-groups"]}
    assert "HK" in names and "JP" not in names


def test_region_override_wins():
    n = _node("Premium X")  # 自动会是「未分类」
    cfg = build_subscription([n], {}, tags={n.access_id.value: {"region": "US"}})
    names = {g["name"] for g in cfg["proxy-groups"]}
    assert "US" in names and UNKNOWN not in names


def test_all_quarantined_fail_closed():
    n = _node("🇭🇰 HK 01")
    cfg = build_subscription([n], {}, tags={n.access_id.value: {"quarantined": True}})
    assert cfg["proxies"] == [] and cfg["rules"] == ["MATCH,DIRECT"]
