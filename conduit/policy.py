"""规则面（策略层）：conduit 自有、**版本管理在仓库**的路由策略，跟订阅 / 节点地址解耦。

三平面里的顶层：规则只引用**组名**（PROXY/HK/…，来自标签层）+ **geo 类别**（cn / category-ads-all…）。
geo 数据靠 mihomo 内置的 geosite/geoip 库 —— **引用而非拷贝**，所以这份策略可读、可 diff、可长期维护
（目标 #2 解耦、#5 版本管理）。要调路由就改这个文件，不碰节点 / 订阅。

规则顺序（render 拼）：私网/tailnet 兜底直连(rule#0) → 调用方 direct-list → 本策略(reject→direct) → MATCH,final。
"""

from __future__ import annotations

# v1 默认策略：广告拒绝 + 中国直连 + 其余走代理。改这里就改全局分流。
DEFAULT_POLICY: dict = {
    "reject": {"geosite": ["category-ads-all"]},  # 广告 → REJECT
    "direct": {"geosite": ["cn"], "geoip": ["CN"]},  # 中国域名/IP → 直连
    "final": "PROXY",  # 其余兜底走哪个组（顶层 select）
}


def policy_rules(policy: dict) -> list[str]:
    """策略 → mihomo 规则串（不含 baseline 私网兜底 + 调用方 direct-list + 末尾 MATCH，那些 render 拼）。

    顺序：先 reject（广告），再 direct（中国）。geoip 用 no-resolve（只匹配已是 IP 的连接，不额外解析）。
    """
    rules: list[str] = []
    rej = policy.get("reject", {})
    for site in rej.get("geosite", []):
        rules.append(f"GEOSITE,{site},REJECT")
    for ip in rej.get("geoip", []):
        rules.append(f"GEOIP,{ip},REJECT,no-resolve")
    dr = policy.get("direct", {})
    for site in dr.get("geosite", []):
        rules.append(f"GEOSITE,{site},DIRECT")
    for ip in dr.get("geoip", []):
        rules.append(f"GEOIP,{ip},DIRECT,no-resolve")
    return rules
