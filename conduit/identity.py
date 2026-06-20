"""节点两层身份的计算（见 CONSTRAINTS「标签隔离」）。

- EndpointId = (type, 规范化 server, port)：粗物理聚合。
- AccessId   = sha256(连接参数去掉显示名)：稳定身份；改名不变、任何连接参数变了就是不同节点。
  人工标签挂 AccessId，跨订阅改名仍跟随。

v1 取「去掉 name 之外的全部字段」做哈希——宁可过度区分（同节点偶尔被拆成两个），不要错并（把不同
节点当成一个）。TODO：把 udp/tfo/skip-cert-verify 这类客户端偏好从哈希里剔掉，避免同节点被拆。
"""

from __future__ import annotations

import hashlib
import json

from .models import AccessId, EndpointId

_COSMETIC = {"name"}  # 不参与身份的字段（v1 仅显示名）


def _norm_server(server: object) -> str:
    return str(server).strip().lower()


def endpoint_id(proxy: dict) -> EndpointId:
    return EndpointId(
        type=str(proxy.get("type", "")),
        server=_norm_server(proxy.get("server", "")),
        port=int(proxy.get("port", 0)),
    )


def _canonical(proxy: dict) -> str:
    d = {k: v for k, v in proxy.items() if k not in _COSMETIC}
    if "server" in d:
        d["server"] = _norm_server(d["server"])
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def access_id(proxy: dict) -> AccessId:
    h = hashlib.sha256(_canonical(proxy).encode("utf-8")).hexdigest()
    return AccessId(value=h, endpoint=endpoint_id(proxy))
