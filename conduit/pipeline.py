"""生成流水线骨架：定义阶段接口，不含真逻辑。

阶段：fetch → normalize → tag → prune → render → validate
送达（怎么把配置送到主机、怎么 reload）由调用方负责，不在核心流水线里。
约束见 CONSTRAINTS.md，阶段说明见 ARCHITECTURE.md。
"""

from __future__ import annotations

from .ingest import normalize as _normalize_one
from .models import AccessId, Node, NodeTags
from .render import render as _render


def fetch(sources: list[dict]) -> list[bytes]:
    """抓订阅原始内容（多格式）。sources 是结构化订阅清单，敏感值用 *_ref 指向外部 secret。"""
    raise NotImplementedError


def normalize(imports: list[dict]) -> list[Node]:
    """每个 import = {raw, type, id}：解析为统一 Node 列表，算两层身份，丢弃订阅自带规则。实现见 ingest.py。"""
    out: list[Node] = []
    for imp in imports:
        out += _normalize_one(imp["raw"], imp.get("type", "auto"), imp.get("id", ""))
    return out


def tag(nodes: list[Node]) -> dict[AccessId, NodeTags]:
    """auto（正则）+ manual（按 access_id 映射）打标；没见过的身份进隔离区。"""
    raise NotImplementedError


def prune(tagged: dict, health: dict) -> dict:
    """按健康历史（见 ARCHITECTURE 的 health-history schema）剔除长期不健康节点。"""
    raise NotImplementedError


def render(nodes: list[Node], target: str, direct_list: dict, overlay: dict) -> str:
    """渲染某个 target 的 mihomo 配置（实现见 conduit/render.py）。"""
    return _render(nodes, target, direct_list, overlay)


def validate(config: str) -> None:
    """mihomo 自检 + schema 校验（含 direct_list 三处覆盖一致）；失败即抛错，阻断下发。"""
    raise NotImplementedError
