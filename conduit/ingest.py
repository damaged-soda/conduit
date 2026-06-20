"""ingest：把导入的订阅原始内容解析成统一的 Node 列表 + 跳过诊断。

v1 只做 **clash YAML**（你那种「自带 proxies + 整套规则」的订阅）：只取 `proxies:`，
**丢掉订阅自带的 rules / proxy-groups / 其它**。摄入方式是「文件导入」（内容由调用方给），
网络 fetch 推后。

不可靠订阅常见脏数据：缺/坏字段的 proxy 跳过并**计入 `skipped`**（不静默、不炸整批）。
TODO：base64 / v2ray 等格式；跨订阅去重（同 access_id）；无损重建（render 用原始 server）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .identity import InvalidProxy, access_id
from .models import Node

_CORE = {"name", "type", "server", "port"}


@dataclass
class IngestResult:
    nodes: list[Node] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # 每条 {name, reason}


def parse_clash(raw: str | bytes) -> list | None:
    """取 proxies 列表。None = 不是合法 clash 文档；[] = 是文档但没有 proxies。"""
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return None
    proxies = data.get("proxies")
    return proxies if isinstance(proxies, list) else []


def _to_node(proxy: dict, source_id: str) -> Node:
    aid = access_id(proxy)  # 触发核心字段校验，坏的抛 InvalidProxy
    params = {k: v for k, v in proxy.items() if k not in _CORE}  # 连接参数（render 据此重建 proxy）
    return Node(access_id=aid, raw_name=str(proxy.get("name", "")), params=params, source=source_id)


def ingest(raw: str | bytes, source_type: str = "clash", source_id: str = "") -> IngestResult:
    """解析一份导入内容 → Node 列表 + 跳过项。"""
    if source_type != "clash":
        raise ValueError(f"暂不支持的订阅类型：{source_type}（v1 只做 clash YAML）")
    proxies = parse_clash(raw)
    res = IngestResult()
    if proxies is None:
        res.skipped.append({"name": None, "reason": "不是合法的 clash YAML 文档"})
        return res
    if not proxies:
        res.skipped.append({"name": None, "reason": "未找到 proxies 列表"})
        return res
    for p in proxies:
        if not isinstance(p, dict):
            res.skipped.append({"name": None, "reason": "proxy 条目不是 dict"})
            continue
        try:
            res.nodes.append(_to_node(p, source_id))
        except InvalidProxy as e:
            res.skipped.append({"name": p.get("name"), "reason": str(e)})
    return res


def normalize(raw: str | bytes, source_type: str = "clash", source_id: str = "") -> list[Node]:
    """只要 Node 列表（丢诊断）；要诊断用 ingest()。"""
    return ingest(raw, source_type, source_id).nodes
