"""标签层纯函数：从节点名解析 region（解码旗帜 emoji，或关键词兜底）。

region 是「自动派生」的；人工覆盖 / 隔离存在 service 层（按 access_id），不在这里。
render 用 region_of 把节点分到地区组；规则只引用组名（隔离原则）。
"""

from __future__ import annotations

UNKNOWN = "未分类"

# 旗帜 emoji = 两个区域指示符（U+1F1E6..U+1F1FF），分别映射 A..Z。🇭🇰 → "HK"。
_RI_LO, _RI_HI = 0x1F1E6, 0x1F1FF


def _flag_code(name: str) -> str:
    ris = [ord(c) - _RI_LO for c in name if _RI_LO <= ord(c) <= _RI_HI]
    if len(ris) >= 2:
        return chr(ris[0] + ord("A")) + chr(ris[1] + ord("A"))
    return ""


# 无旗帜时的关键词兜底；都按小写匹配 name.lower()。只放描述性词，避免 2 字母码误命中子串。
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "HK": ("香港", "hong kong", "hongkong"),
    "TW": ("台湾", "臺灣", "taiwan"),
    "JP": ("日本", "japan", "tokyo", "osaka"),
    "SG": ("新加坡", "狮城", "singapore"),
    "US": ("美国", "united states", "los angeles", "san jose", "seattle", "silicon valley"),
    "KR": ("韩国", "韓國", "korea", "seoul"),
    "GB": ("英国", "united kingdom", "london", "britain"),
    "DE": ("德国", "germany", "frankfurt"),
    "FR": ("法国", "france", "paris"),
    "NL": ("荷兰", "netherlands", "amsterdam"),
    "RU": ("俄罗斯", "russia", "moscow"),
    "IN": ("印度", "india", "mumbai"),
    "CN": ("中国", "china", "回国"),
    "MY": ("马来", "malaysia", "kuala lumpur"),
    "TH": ("泰国", "thailand", "bangkok"),
    "VN": ("越南", "vietnam"),
    "PH": ("菲律宾", "philippines"),
    "AU": ("澳大利亚", "澳洲", "australia", "sydney"),
    "CA": ("加拿大", "canada", "toronto"),
    "TR": ("土耳其", "turkey", "istanbul"),
    "AR": ("阿根廷", "argentina"),
}


# 不能当 region（会撞 mihomo 组名/策略）
_RESERVED_GROUPS = {"AUTO", "PROXY", "DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE"}


def normalize_region(value: str | None) -> str | None:
    """规范化人工 region 覆盖：去空→None；ascii 短码大写(hk→HK)；非 ascii 标签保留。

    拒绝保留名 / 逗号换行 / 超长（避免产出坏组名）。非法抛 ValueError。
    """
    s = (value or "").strip()
    if not s:
        return None
    if len(s) > 24 or any(ch in s for ch in ",\n\r\t"):
        raise ValueError("region 非法（过长或含逗号/换行）")
    norm = s.upper() if s.isascii() else s
    if norm.upper() in _RESERVED_GROUPS:
        raise ValueError(f"region 不能用保留名：{norm}")
    return norm


def region_of(name: str) -> str:
    """节点名 → region code（HK/JP/…）。优先解码旗帜 emoji，否则关键词，再否则「未分类」。"""
    name = name or ""
    code = _flag_code(name)
    if code:
        return code
    low = name.lower()
    for region, kws in _KEYWORDS.items():
        if any(kw in low for kw in kws):
            return region
    return UNKNOWN
