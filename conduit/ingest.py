"""ingest：把导入的订阅原始内容解析成统一的 Node 列表。

支持 Clash/Mihomo YAML（只取 `proxies:`，丢弃订阅自带规则）、URI 行订阅，以及整份
base64 包裹的 URI/YAML 订阅。节点「连不上」那种脏 = 后续 health-check + prune 的事，
不在 ingest 管；这里只过滤明显残缺（缺 type/server/port）的条目。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from .identity import access_id
from .models import Node

_CORE = {"name", "type", "server", "port"}
_URI_SCHEMES = {"ss", "vmess", "trojan", "vless", "hysteria", "hysteria2", "hy2"}
_URI_START = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/")


def _text(raw: str | bytes) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def parse_clash(raw: str | bytes) -> list:
    """从 clash YAML 取出 proxies 列表（丢弃 rules/groups 等）。"""
    data = yaml.safe_load(raw)
    proxies = data.get("proxies") if isinstance(data, dict) else None
    return proxies if isinstance(proxies, list) else []


def _usable(p: object) -> bool:
    return isinstance(p, dict) and bool(p.get("type")) and bool(p.get("server")) and p.get("port") is not None


def _to_node(proxy: dict, source_id: str) -> Node:
    params = {k: v for k, v in proxy.items() if k not in _CORE}  # 连接参数（render 据此重建 proxy）
    return Node(access_id=access_id(proxy), raw_name=str(proxy.get("name", "")), params=params, source=source_id)


def _b64decode(s: str) -> str | None:
    clean = re.sub(r"\s+", "", s)
    if len(clean) < 8 or not re.fullmatch(r"[A-Za-z0-9+/_=-]+", clean):
        return None
    padded = clean + "=" * (-len(clean) % 4)
    for altchars in (None, b"-_"):
        try:
            raw = base64.b64decode(padded, altchars=altchars, validate=True)
        except (binascii.Error, ValueError):
            continue
        decoded = raw.decode("utf-8", errors="replace")
        if decoded and decoded.count("\ufffd") <= max(1, len(decoded) // 20):
            return decoded
    return None


def _query(uri) -> dict[str, list[str]]:
    return parse_qs(uri.query, keep_blank_values=True)


def _q(q: dict[str, list[str]], *names: str) -> str:
    lowered = {k.lower(): v for k, v in q.items()}
    for name in names:
        vals = lowered.get(name.lower())
        if vals:
            return vals[-1]
    return ""


def _flag(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: object, default: int | None = None) -> int | None:
    if v in (None, ""):
        return default
    return int(str(v))


def _name(uri, fallback: str) -> str:
    name = unquote(uri.fragment or "").strip()
    return name or fallback


def _host_port(uri) -> tuple[str, int]:
    try:
        host, port = uri.hostname, uri.port
    except ValueError as e:
        raise ValueError("URI port 非法") from e
    if not host or port is None:
        raise ValueError("URI 缺 server/port")
    return host, port


def _split_alpn(v: str) -> list[str]:
    return [x.strip() for x in re.split(r"[, ]+", v) if x.strip()]


def _apply_bool(proxy: dict, key: str, value: str) -> None:
    if value:
        proxy[key] = _flag(value)


def _apply_common_tls(proxy: dict, q: dict[str, list[str]], sni_key: str = "servername") -> None:
    security = _q(q, "security", "tls")
    if security in {"tls", "reality"} or _flag(security):
        proxy["tls"] = True
    sni = _q(q, "sni", "peer", "servername", "serverName")
    if sni:
        proxy[sni_key] = sni
    insecure = _q(q, "allowInsecure", "allow-insecure", "skip-cert-verify", "insecure")
    _apply_bool(proxy, "skip-cert-verify", insecure)
    fingerprint = _q(q, "fingerprint")
    if fingerprint:
        proxy["fingerprint"] = fingerprint
    client_fp = _q(q, "fp", "client-fingerprint", "clientFingerprint")
    if client_fp:
        proxy["client-fingerprint"] = client_fp
    alpn = _q(q, "alpn")
    if alpn:
        proxy["alpn"] = _split_alpn(alpn)


def _apply_transport(proxy: dict, q: dict[str, list[str]]) -> None:
    network = _q(q, "type", "network", "net")
    if not network or network == "tcp":
        return
    proxy["network"] = "h2" if network == "http" else network
    if network == "ws":
        opts: dict = {}
        path = _q(q, "path")
        host = _q(q, "host")
        if path:
            opts["path"] = path
        if host:
            opts["headers"] = {"Host": host}
        if opts:
            proxy["ws-opts"] = opts
    elif network == "grpc":
        service = _q(q, "serviceName", "service-name", "grpc-service-name", "path")
        if service:
            proxy["grpc-opts"] = {"grpc-service-name": service.lstrip("/")}
    elif network in {"h2", "http"}:
        opts = {}
        path = _q(q, "path")
        host = _q(q, "host")
        if path:
            opts["path"] = path
        if host:
            opts["host"] = [host]
        if opts:
            proxy["h2-opts"] = opts


def _parse_ss_plugin(proxy: dict, plugin: str) -> None:
    parts = [unquote(x) for x in plugin.split(";") if x]
    if not parts:
        return
    name = parts[0]
    opts = {}
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            opts[k] = v
    if "obfs" in name:
        proxy["plugin"] = "obfs"
        popts = {}
        if opts.get("obfs"):
            popts["mode"] = opts["obfs"]
        if opts.get("obfs-host"):
            popts["host"] = opts["obfs-host"]
        if popts:
            proxy["plugin-opts"] = popts
    elif "v2ray" in name:
        proxy["plugin"] = "v2ray-plugin"
        popts = {}
        if opts.get("mode"):
            popts["mode"] = opts["mode"]
        if opts.get("host"):
            popts["host"] = opts["host"]
        if opts.get("path"):
            popts["path"] = opts["path"]
        if opts.get("tls"):
            popts["tls"] = _flag(opts["tls"])
        if popts:
            proxy["plugin-opts"] = popts
    else:
        proxy["plugin"] = name


def _split_ss_userinfo(s: str) -> tuple[str, str]:
    decoded = _b64decode(s) or unquote(s)
    if ":" not in decoded:
        raise ValueError("ss URI 缺 cipher/password")
    method, password = decoded.split(":", 1)
    return method, password


def _parse_ss(uri: str) -> dict:
    u = urlsplit(uri)
    q = _query(u)
    if u.hostname and u.username:
        server, port = _host_port(u)
        if u.password is not None:
            method, password = unquote(u.username), unquote(u.password)
        else:
            method, password = _split_ss_userinfo(unquote(u.username))
    else:
        decoded = _b64decode(u.netloc)
        if not decoded or "@" not in decoded:
            raise ValueError("ss URI 非法")
        userinfo, hostport = decoded.rsplit("@", 1)
        method, password = _split_ss_userinfo(userinfo)
        hp = urlsplit(f"ss://{hostport}")
        server, port = _host_port(hp)
    proxy = {
        "name": _name(u, server),
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
    }
    plugin = _q(q, "plugin")
    if plugin:
        _parse_ss_plugin(proxy, plugin)
    return proxy


def _parse_vmess(uri: str) -> dict:
    body = uri[len("vmess://"):]
    decoded = _b64decode(unquote(body))
    if not decoded:
        raise ValueError("vmess URI 非法")
    data = json.loads(decoded)
    server = data.get("add")
    port = _int(data.get("port"))
    if not server or port is None or not data.get("id"):
        raise ValueError("vmess URI 缺必要字段")
    proxy = {
        "name": str(data.get("ps") or server),
        "type": "vmess",
        "server": str(server),
        "port": port,
        "uuid": str(data["id"]),
        "alterId": _int(data.get("aid"), 0),
        "cipher": str(data.get("scy") or "auto"),
    }
    net = str(data.get("net") or "")
    if net and net != "tcp":
        proxy["network"] = net
    if str(data.get("tls") or "").lower() not in {"", "none", "false"}:
        proxy["tls"] = True
    if data.get("sni"):
        proxy["servername"] = data["sni"]
    if data.get("alpn"):
        proxy["alpn"] = _split_alpn(str(data["alpn"]))
    if data.get("fp"):
        proxy["client-fingerprint"] = data["fp"]
    if net == "ws":
        opts: dict = {}
        if data.get("path"):
            opts["path"] = data["path"]
        if data.get("host"):
            opts["headers"] = {"Host": data["host"]}
        if opts:
            proxy["ws-opts"] = opts
    elif net == "grpc" and data.get("path"):
        proxy["grpc-opts"] = {"grpc-service-name": str(data["path"]).lstrip("/")}
    return proxy


def _parse_trojan(uri: str) -> dict:
    u = urlsplit(uri)
    server, port = _host_port(u)
    if not u.username:
        raise ValueError("trojan URI 缺 password")
    q = _query(u)
    proxy = {
        "name": _name(u, server),
        "type": "trojan",
        "server": server,
        "port": port,
        "password": unquote(u.username),
    }
    _apply_common_tls(proxy, q, "sni")
    proxy.pop("tls", None)  # trojan 本身就是 TLS 协议，mihomo 配置不需要 `tls: true`
    _apply_transport(proxy, q)
    udp = _q(q, "udp")
    if udp:
        proxy["udp"] = _flag(udp)
    if _q(q, "flow"):
        proxy["flow"] = _q(q, "flow")
    return proxy


def _parse_vless(uri: str) -> dict:
    u = urlsplit(uri)
    server, port = _host_port(u)
    if not u.username:
        raise ValueError("vless URI 缺 uuid")
    q = _query(u)
    proxy = {
        "name": _name(u, server),
        "type": "vless",
        "server": server,
        "port": port,
        "uuid": unquote(u.username),
    }
    _apply_common_tls(proxy, q, "servername")
    _apply_transport(proxy, q)
    encryption = _q(q, "encryption")
    if encryption and encryption != "none":
        proxy["encryption"] = encryption
    for src, dst in [("flow", "flow"), ("packetEncoding", "packet-encoding")]:
        value = _q(q, src)
        if value:
            proxy[dst] = value
    udp = _q(q, "udp")
    if udp:
        proxy["udp"] = _flag(udp)
    if _q(q, "security") == "reality":
        proxy["tls"] = True
        reality = {}
        if _q(q, "pbk", "public-key"):
            reality["public-key"] = _q(q, "pbk", "public-key")
        if _q(q, "sid", "short-id"):
            reality["short-id"] = _q(q, "sid", "short-id")
        if reality:
            proxy["reality-opts"] = reality
    return proxy


def _parse_hysteria(uri: str) -> dict:
    u = urlsplit(uri)
    server, port = _host_port(u)
    q = _query(u)
    proxy = {"name": _name(u, server), "type": "hysteria", "server": server, "port": port}
    auth = _q(q, "auth", "auth-str", "auth_str")
    if auth:
        proxy["auth-str"] = auth
    if _q(q, "protocol"):
        proxy["protocol"] = _q(q, "protocol")
    if _q(q, "peer", "sni"):
        proxy["sni"] = _q(q, "peer", "sni")
    _apply_bool(proxy, "skip-cert-verify", _q(q, "insecure", "allowInsecure"))
    for src, dst in [("up", "up"), ("down", "down"), ("upmbps", "up"), ("downmbps", "down")]:
        value = _q(q, src)
        if value and dst not in proxy:
            proxy[dst] = f"{value} Mbps" if src.endswith("mbps") else value
    if _q(q, "obfs"):
        proxy["obfs"] = _q(q, "obfs")
    return proxy


def _parse_hysteria2(uri: str) -> dict:
    u = urlsplit(uri)
    server, port = _host_port(u)
    q = _query(u)
    password = unquote(u.username or "") or _q(q, "password", "auth")
    if not password:
        raise ValueError("hysteria2 URI 缺 password")
    proxy = {
        "name": _name(u, server),
        "type": "hysteria2",
        "server": server,
        "port": port,
        "password": password,
    }
    for src, dst in [
        ("ports", "ports"),
        ("hop-interval", "hop-interval"),
        ("up", "up"),
        ("down", "down"),
        ("obfs", "obfs"),
        ("obfs-password", "obfs-password"),
        ("obfs_password", "obfs-password"),
        ("sni", "sni"),
        ("fingerprint", "fingerprint"),
    ]:
        value = _q(q, src)
        if value:
            proxy[dst] = value
    _apply_bool(proxy, "skip-cert-verify", _q(q, "insecure", "allowInsecure"))
    alpn = _q(q, "alpn")
    if alpn:
        proxy["alpn"] = _split_alpn(alpn)
    return proxy


def _parse_uri_line(line: str) -> dict:
    scheme = urlsplit(line).scheme.lower()
    if scheme == "ss":
        return _parse_ss(line)
    if scheme == "vmess":
        return _parse_vmess(line)
    if scheme == "trojan":
        return _parse_trojan(line)
    if scheme == "vless":
        return _parse_vless(line)
    if scheme == "hysteria":
        return _parse_hysteria(line)
    if scheme in {"hysteria2", "hy2"}:
        return _parse_hysteria2(line)
    raise ValueError(f"不支持的 URI scheme: {scheme}")


def parse_uri(raw: str | bytes) -> list:
    """解析常见 URI 行订阅（ss/vmess/trojan/vless/hysteria/hysteria2）。"""
    out: list[dict] = []
    saw_supported = False
    errors = []
    for line in _text(raw).splitlines():
        line = line.strip()
        m = _URI_START.match(line)
        if not m:
            continue
        if m.group(1).lower() not in _URI_SCHEMES:
            continue
        saw_supported = True
        try:
            out.append(_parse_uri_line(line))
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            errors.append(e)
    if saw_supported and not out and errors:
        raise ValueError("URI 订阅未解析到有效节点") from errors[0]
    return out


def _parse_auto(raw: str | bytes) -> list:
    proxies = parse_clash(raw)
    if proxies:
        return proxies
    proxies = parse_uri(raw)
    if proxies:
        return proxies
    decoded = _b64decode(_text(raw))
    if decoded:
        proxies = parse_clash(decoded)
        return proxies if proxies else parse_uri(decoded)
    return []


def normalize(raw: str | bytes, source_type: str = "clash", source_id: str = "") -> list[Node]:
    """把一份导入内容解析为 Node 列表。"""
    kind = (source_type or "auto").strip().lower()
    if kind in {"auto", "clash"}:
        proxies = _parse_auto(raw)
    elif kind == "uri":
        proxies = parse_uri(raw)
    elif kind == "base64":
        decoded = _b64decode(_text(raw))
        if decoded is None:
            raise ValueError("base64 订阅解码失败")
        proxies = _parse_auto(decoded)
    else:
        raise ValueError(f"暂不支持的订阅类型：{source_type}")
    return [_to_node(p, source_id) for p in proxies if _usable(p)]
