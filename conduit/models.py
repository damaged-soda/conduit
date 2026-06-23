"""规范化后的节点数据模型。先定 schema，不含逻辑。

身份分两层（精确规则见后续身份模型设计轮）：
- EndpointId：粗物理聚合，(type, 规范化 server, port)。
- AccessId：稳定身份，连接参数的安全哈希；人工标签挂这层。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EndpointId:
    """粗物理身份：同一落地的多个节点会聚到这里。"""

    type: str  # ss / vmess / trojan / hysteria2 / ...
    server: str  # 规范化后的 server（CNAME / 大小写归一等待定）
    port: int


@dataclass(frozen=True)
class AccessId:
    """稳定身份：连接参数的哈希。人工标签挂这层，跨订阅改名仍跟随。

    进哪些参数（sni / network / ws-path / grpc-service / cipher / uuid|password…）
    是下一轮身份模型设计的核心；这里先放占位。
    """

    value: str  # sha256(规范化连接参数去掉显示名)
    endpoint: EndpointId


@dataclass
class Node:
    """从订阅解析出的一个机房节点：丢掉订阅自带规则，只留地址与连接参数。"""

    access_id: AccessId
    raw_name: str  # 订阅里的原始名
    params: dict = field(default_factory=dict)  # 加密 / uuid / sni 等，含 secret，不进 git
    source: str = ""  # 来源订阅 id


@dataclass
class NodeTags:
    """一个节点的标签集合（挂在 AccessId 上）。auto 可重算，manual 跟随身份。"""

    region: str | None = None  # auto（正则）
    rate: float | None = None  # auto（倍率）
    trust: str = "quarantine"  # manual: quarantine / normal / trusted
    purpose: set[str] = field(default_factory=set)  # manual: streaming / general / ...
