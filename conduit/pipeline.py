"""生成流水线骨架：定义阶段接口，不含真逻辑。

阶段：fetch → normalize → tag → prune → render → validate
送达（怎么把配置送到主机、怎么 reload）由调用方负责，不在核心流水线里。
约束见 CONSTRAINTS.md，阶段说明见 ARCHITECTURE.md。
"""

from __future__ import annotations

from .models import AccessId, Node, NodeTags


def fetch(sources: list[dict]) -> list[bytes]:
    """抓订阅原始内容（多格式）。sources 是结构化订阅清单，敏感值用 *_ref 指向外部 secret。"""
    raise NotImplementedError


def normalize(raw: list[bytes]) -> list[Node]:
    """解析为统一 Node 列表，算两层身份（endpoint_id / access_id）。丢弃订阅自带的规则系统。"""
    raise NotImplementedError


def tag(nodes: list[Node]) -> dict[AccessId, NodeTags]:
    """auto（正则）+ manual（按 access_id 映射）打标；没见过的身份进隔离区。"""
    raise NotImplementedError


def prune(tagged: dict, health: dict) -> dict:
    """按健康历史（见 ARCHITECTURE 的 health-history schema）剔除长期不健康节点。"""
    raise NotImplementedError


def render(tagged: dict, target: str, direct_list: dict, overlay: dict) -> str:
    """渲染某个 target 的 mihomo 配置：proxies/provider + 标签 group + 规则
    + 注入 direct_list（同时落到 DIRECT 规则 / fake-ip 放行 / TUN route-exclude）+ per-target overlay。

    conduit 不关心 direct_list / overlay 里具体是什么——都是调用方的现状。
    """
    raise NotImplementedError


def validate(config: str) -> None:
    """mihomo 自检 + schema 校验（含 direct_list 三处覆盖一致）；失败即抛错，阻断下发。"""
    raise NotImplementedError
