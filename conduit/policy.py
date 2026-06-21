"""规则面（策略层）：conduit 自有、**版本管理在仓库**的路由策略，跟订阅 / 节点地址解耦。

模型 = 你说的「规则 = 一组匹配 → 一个目标(标签/组)」，也是机场/subconverter 的行业主流
（类别 → rule-provider → 策略组）。每条 `route` = `{to: 目标, 匹配...}`，顺序即优先级。

- 目标 `to`：节点组名（HK/JP/US/PROXY… 来自标签层）或内置 `DIRECT`/`REJECT`。
  render 会把指向「当前不存在的组」的 route 落到 `final`，保证配置合法。
- 匹配来源：内置 `geosite`/`geoip`（mihomo 自带 geo 库，用于 cn/广告这种大类）+ `rule_set`
  （外部维护的 .mrs 规则集，用于流媒体/AI 这种细类，**引用而非拷贝**，自动更新）。

改路由就改这个文件，不碰节点 / 订阅（目标 #2 解耦、#5 版本管理）。
"""

from __future__ import annotations

_MRS = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo"

# v1 默认策略。改这里就改全局分流。
DEFAULT_POLICY: dict = {
    # 外部规则集（mihomo rule-provider，.mrs 二进制，引用而非拷贝）
    "rule_providers": {
        "ai": {"behavior": "domain", "url": f"{_MRS}/geosite/category-ai-!cn.mrs"},
        "netflix": {"behavior": "domain", "url": f"{_MRS}/geosite/netflix.mrs"},
        "disney": {"behavior": "domain", "url": f"{_MRS}/geosite/disney.mrs"},
        "youtube": {"behavior": "domain", "url": f"{_MRS}/geosite/youtube.mrs"},
    },
    # 路由：一组匹配 → 一个目标，顺序即优先级
    "routes": [
        {"name": "广告", "to": "REJECT", "geosite": ["category-ads-all"]},
        {"name": "中国大陆", "to": "DIRECT", "geosite": ["cn"], "geoip": ["CN"]},
        {"name": "AI（ChatGPT/Claude…）", "to": "US", "rule_set": ["ai"]},
        {"name": "流媒体", "to": "HK", "rule_set": ["netflix", "disney", "youtube"]},
    ],
    "final": "PROXY",  # 其余兜底走哪个组
}


def policy_rules(policy: dict, resolve=None) -> list[str]:
    """策略 → mihomo 规则串（不含 baseline 私网兜底 + 调用方 direct-list + 末尾 MATCH）。

    resolve(to)→to：render 传入，把指向「不存在的组」的 route 落到 final（默认原样）。
    ipcidr 类（geoip / ipcidr 的 rule_set）带 no-resolve。
    """
    resolve = resolve or (lambda to: to)
    providers = policy.get("rule_providers", {})
    rules: list[str] = []
    for route in policy.get("routes", []):
        to = resolve(route["to"])
        for name in route.get("rule_set", []):
            if name not in providers:  # 编辑后可能引用未声明 provider → 跳过，别产坏配置
                continue
            ipcidr = providers[name].get("behavior") == "ipcidr"
            rules.append(f"RULE-SET,{name},{to}" + (",no-resolve" if ipcidr else ""))
        for site in route.get("geosite", []):
            rules.append(f"GEOSITE,{site},{to}")
        for ip in route.get("geoip", []):
            rules.append(f"GEOIP,{ip},{to},no-resolve")
    return rules


def rule_providers_block(policy: dict) -> dict:
    """policy.rule_providers → mihomo `rule-providers:` 块（只含被 routes 引用到的，按出现顺序去重→输出稳定）。"""
    referenced: list[str] = []
    for route in policy.get("routes", []):
        for name in route.get("rule_set", []):
            if name not in referenced:
                referenced.append(name)
    specs = policy.get("rule_providers", {})
    out: dict = {}
    for name in referenced:
        spec = specs.get(name)
        if not spec:
            continue
        out[name] = {
            "type": "http",
            "behavior": spec.get("behavior", "domain"),
            "format": spec.get("format", "mrs"),
            "url": spec["url"],
            "interval": spec.get("interval", 86400),
            "path": f"./ruleset/{name}.mrs",
        }
    return out
