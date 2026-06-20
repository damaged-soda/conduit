"""ingest：把导入的订阅原始内容解析成统一的 Node 列表。

v1 只做 **clash YAML**（你那种「自带 proxies + 整套规则」的订阅）：只取 `proxies:`，
**丢掉订阅自带的 rules / proxy-groups / 其它**。摄入方式是「文件导入」（内容由调用方给），
网络 fetch 推后。

不可靠订阅常见脏数据：缺字段的 proxy 直接跳过（不让一个坏节点炸掉整批）。
TODO：base64 / v2ray 等其它格式、被跳过项的回报、server 的 CNAME/IP 归一。
"""

from __future__ import annotations

import yaml

from .identity import access_id
from .models import Node

_CORE = {"name", "type", "server", "port"}


def parse_clash(raw: str | bytes) -> list[dict]:
    """从 clash YAML 取出 proxies 列表（丢弃 rules/groups 等）。"""
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return []
    proxies = data.get("proxies")
    return proxies if isinstance(proxies, list) else []


def _valid(proxy: object) -> bool:
    return (
        isinstance(proxy, dict)
        and proxy.get("type")
        and proxy.get("server")
        and proxy.get("port") is not None
    )


def _to_node(proxy: dict, source_id: str) -> Node:
    params = {k: v for k, v in proxy.items() if k not in _CORE}  # 连接参数（render 据此重建 proxy）
    return Node(access_id=access_id(proxy), raw_name=str(proxy.get("name", "")), params=params, source=source_id)


def normalize(raw: str | bytes, source_type: str = "clash", source_id: str = "") -> list[Node]:
    """把一份导入内容解析为 Node 列表。脏 proxy 跳过。"""
    if source_type != "clash":
        raise ValueError(f"暂不支持的订阅类型：{source_type}（v1 只做 clash YAML）")
    return [_to_node(p, source_id) for p in parse_clash(raw) if _valid(p)]
