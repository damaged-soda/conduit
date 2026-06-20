"""节点两层身份的计算（见 CONSTRAINTS「标签隔离」）。

- EndpointId = (type, 规范化 server, port)：粗物理聚合。
- AccessId   = sha256(规范化连接参数去掉显示名 + 本地偏好)：稳定身份；改名/换本地开关不变，
  任何真·连接参数变了就是不同节点。人工标签挂 AccessId，跨订阅改名仍跟随。

身份与 endpoint **用同一套规范化**（type/server/port 统一 coerce），否则 `port:8388` 与 `"8388"`
会同 endpoint 却不同 access_id。

注意：v1 里 server 的规范化（lower/strip）也会进入**渲染输出**（render 从 endpoint 取 server）。
v1 只动大小写/空白，DNS 不敏感、安全；**做更激进的归一（CNAME/IP 解析）之前**，必须先把
「渲染用的原始 server」和「身份用的归一 server」解耦（TODO）。
TODO：access_id 改 HMAC（CONSTRAINTS 要求；它可能进 API/日志，且哈希输入含凭据）——需要 key 管理。
"""

from __future__ import annotations

import hashlib
import json

from .models import AccessId, EndpointId

# 不参与身份的字段：显示名 + 客户端本地偏好（避免同节点因本地开关被拆成多个）。
_NON_IDENTITY = {
    "name", "udp", "tfo", "fast-open", "skip-cert-verify",
    "ip-version", "mptcp", "interface-name", "routing-mark", "dialer-proxy",
}


class InvalidProxy(ValueError):
    """proxy 缺/坏核心字段（type/server/port），应被跳过而不是炸掉整批。"""


def _core(proxy: dict) -> tuple[str, str, int]:
    """coerce + 校验核心三元组；坏的抛 InvalidProxy。endpoint 与 access_id 共用，保证一致。"""
    type_ = str(proxy.get("type", "")).strip().lower()
    server = str(proxy.get("server", "")).strip().lower()
    port = proxy.get("port")
    if not type_:
        raise InvalidProxy("缺 type")
    if not server:
        raise InvalidProxy("缺 server")
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise InvalidProxy(f"port 非法: {port!r}")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise InvalidProxy(f"port 非数字: {proxy.get('port')!r}")
    if not (1 <= port <= 65535):
        raise InvalidProxy(f"port 越界: {port}")
    return type_, server, port


def endpoint_id(proxy: dict) -> EndpointId:
    t, s, p = _core(proxy)
    return EndpointId(type=t, server=s, port=p)


def access_id(proxy: dict) -> AccessId:
    t, s, p = _core(proxy)
    rest = {k: v for k, v in proxy.items() if k not in _NON_IDENTITY and k not in ("type", "server", "port")}
    canonical = json.dumps({"type": t, "server": s, "port": p, **rest}, sort_keys=True, ensure_ascii=False, default=str)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return AccessId(value=h, endpoint=EndpointId(type=t, server=s, port=p))
