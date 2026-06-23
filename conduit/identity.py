"""节点两层身份的计算（见 CONSTRAINTS「标签隔离」）。

- EndpointId = (type, 规范化 server, port)：粗物理聚合。
- AccessId   = sha256(规范化连接参数去掉显示名)：稳定身份；改名不变、任何连接参数变了就是不同节点。
  人工标签挂 AccessId，跨订阅改名仍跟随。

身份与 endpoint 共用同一套规范化（type/server 小写、port 取 int），否则 `port:8388` 与 `"8388"`
会同 endpoint 却不同 access_id。

注意：v1 里 server 的规范化也会进入渲染输出（render 从 endpoint 取 server）；只动大小写/空白、
DNS 不敏感、安全。做更激进归一（CNAME/IP）前需把「渲染用原始 server」与「身份用归一 server」解耦（TODO）。
TODO：身份字段 include/exclude 策略（udp/tfo 等本地偏好）等有标签了再调；若要从 sha256 改 HMAC，
必须先设计稳定 key 管理和旧标签迁移。
"""

from __future__ import annotations

import hashlib
import json

from .models import AccessId, EndpointId


def _core(proxy: dict) -> tuple[str, str, int]:
    """规范化核心三元组（endpoint 与 access_id 共用，保证一致）。"""
    return (
        str(proxy.get("type", "")).strip().lower(),
        str(proxy.get("server", "")).strip().lower(),
        int(proxy["port"]),
    )


def endpoint_id(proxy: dict) -> EndpointId:
    t, s, p = _core(proxy)
    return EndpointId(type=t, server=s, port=p)


def access_id(proxy: dict) -> AccessId:
    t, s, p = _core(proxy)
    rest = {k: v for k, v in proxy.items() if k not in ("name", "type", "server", "port")}
    canonical = json.dumps({"type": t, "server": s, "port": p, **rest}, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return AccessId(value=h, endpoint=EndpointId(type=t, server=s, port=p))
