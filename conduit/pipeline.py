"""生成流水线骨架：定义阶段接口，不含真逻辑。

阶段：fetch → normalize → tag → prune → render → validate
送达（怎么把配置送到主机、怎么 reload）由调用方负责，不在核心流水线里。
约束见 CONSTRAINTS.md，阶段说明见 ARCHITECTURE.md。
"""

from __future__ import annotations

from .models import Fingerprint, Node, NodeTags


def fetch(sources: list[str]) -> list[bytes]:
    """抓订阅原始内容（多格式）。sources 是 secret，不进 git。"""
    raise NotImplementedError


def normalize(raw: list[bytes]) -> list[Node]:
    """解析为统一 Node 列表，算指纹。丢弃订阅自带的规则系统。"""
    raise NotImplementedError


def tag(nodes: list[Node]) -> dict[Fingerprint, NodeTags]:
    """auto（正则）+ manual（映射）打标；未见过的指纹进隔离区。"""
    raise NotImplementedError


def prune(tagged: dict, health: dict) -> dict:
    """按健康历史剔除长期不健康节点（阈值/时间窗待定）。"""
    raise NotImplementedError


def render(tagged: dict, target: str, direct_list: list[str], overlay: dict) -> str:
    """渲染某个 target 的 mihomo 配置：inline proxies + 标签 group + 规则
    + 注入调用方提供的 direct_list + per-target overlay。

    必须保证 direct_list 中的目的地 DIRECT 且最高优先级。
    conduit 不关心 direct_list / overlay 里具体是什么——都是调用方的现状。
    """
    raise NotImplementedError


def validate(config: str) -> None:
    """mihomo 自检 + schema 校验；失败即抛错，阻断下发。"""
    raise NotImplementedError
