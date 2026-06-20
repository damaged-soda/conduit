"""规范化后的节点数据模型。先定 schema，不含逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fingerprint:
    """跨订阅稳定的节点身份。人工标签按此跟随节点。

    指纹是否够稳是待定设计点（CDN 域名 / SNI 落地、同节点多端口等边界）。
    """

    type: str  # ss / vmess / trojan / hysteria2 / ...
    server: str
    port: int


@dataclass
class Node:
    """从订阅解析出的一个机房节点：丢掉订阅自带规则，只留地址与连接参数。"""

    fingerprint: Fingerprint
    raw_name: str  # 订阅里的原始名
    params: dict = field(default_factory=dict)  # 加密 / uuid / sni 等，含 secret，不进 git
    source: str = ""  # 来源订阅标识（人工标注的粗分类锚点之一）


@dataclass
class NodeTags:
    """一个节点的标签集合。标签维度正交：auto 的可重算，manual 的跟随指纹。"""

    region: str | None = None  # auto（正则）
    rate: float | None = None  # auto（倍率）
    trust: str = "quarantine"  # manual: quarantine / normal / trusted
    purpose: set[str] = field(default_factory=set)  # manual: streaming / general / ...
