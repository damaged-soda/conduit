"""ingest：把导入的订阅原始内容解析成统一的 Node 列表。

v1 只做 **clash YAML**（你那种「自带 proxies + 整套规则」的订阅）：只取 `proxies:`，
**丢掉订阅自带的 rules / proxy-groups / 其它**。摄入方式是「文件导入」（内容由调用方给），网络 fetch 推后。

刻意保持简单：**不在这里做格式容错/逐条诊断**。订阅是机器生成的、格式基本规整；真坏的内容就让它
响亮失败（罕见，重导即可）。节点「连不上」那种脏 = 后续 health-check + prune 的事，不在 ingest 管。
只留一行过滤跳过明显残缺（缺 type/server/port）的条目，避免空条目把整批弄崩。
"""

from __future__ import annotations

import yaml

from .identity import access_id
from .models import Node

_CORE = {"name", "type", "server", "port"}


def parse_clash(raw: str | bytes) -> list:
    """从 clash YAML 取出 proxies 列表（丢弃 rules/groups 等）。"""
    data = yaml.safe_load(raw)
    proxies = data.get("proxies") if isinstance(data, dict) else None
    return proxies if isinstance(proxies, list) else []


def _usable(p: object) -> bool:
    return isinstance(p, dict) and bool(p.get("type")) and bool(p.get("server")) and p.get("port") is not None


def _to_node(proxy: dict, source_id: str) -> Node:
    params = {k: v for k, v in proxy.items() if k not in _CORE}  # 连接参数（render 据此重建 proxy）
    return Node(access_id=access_id(proxy), raw_name=str(proxy.get("name", "")), params=params, source=source_id)


def normalize(raw: str | bytes, source_type: str = "clash", source_id: str = "") -> list[Node]:
    """把一份导入内容解析为 Node 列表。"""
    if source_type != "clash":
        raise ValueError(f"暂不支持的订阅类型：{source_type}（v1 只做 clash YAML）")
    return [_to_node(p, source_id) for p in parse_clash(raw) if _usable(p)]
