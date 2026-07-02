"""UDP support policy shared by ingest and render."""

from __future__ import annotations

from .models import Node

UDP_DEFAULT_TRUE_TYPES = {"hysteria", "hysteria2", "hy2", "tuic", "wireguard"}


def truthy(v: object) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def proxy_supports_udp(proxy: dict) -> bool:
    """Conservative policy: explicit opt-in or UDP-based proxy types only."""
    if "udp" in proxy:
        return truthy(proxy["udp"])
    return str(proxy.get("type", "")).strip().lower() in UDP_DEFAULT_TRUE_TYPES


def node_supports_udp(node: Node) -> bool:
    return proxy_supports_udp({"type": node.access_id.endpoint.type, **(node.params or {})})
