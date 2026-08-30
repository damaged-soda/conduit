"""把 conduit 的 Clash/Mihomo 订阅骨架转换成其他客户端格式。

输入必须来自 :func:`conduit.render.build_subscription`，因此隔离、UDP 资格、
订阅优先级、节点显示名和地区分组都已统一处理。转换器只负责客户端格式和协议能力差异：
无法可靠映射的节点会跳过，并由调用方通过响应头报告数量；不能生成一个偷偷退化为直连的
空订阅。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .udp import truthy


_MRS_BASE = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo"
_CORE_KEYS = {"name", "type", "server", "port"}
_CLIENT_BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP"}
_CORE_GROUPS = {"PROXY", "AUTO", "AUTO-FAST"}
_SHADOWROCKET_TEST_URL = "http://www.gstatic.com/generate_204"


class NoCompatibleProxies(ValueError):
    """当前快照没有能安全转换到目标客户端的节点。"""


class _UnsupportedProxy(ValueError):
    pass


@dataclass(frozen=True)
class ClientSubscription:
    content: str
    included: int
    omitted: int


def _b64(data: str, *, urlsafe: bool = False, padding: bool = True) -> str:
    raw = data.encode("utf-8")
    encoded = (base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)).decode()
    return encoded if padding else encoded.rstrip("=")


def _authority(server: object, port: object) -> str:
    host = str(server).strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{int(port)}"


def _required(proxy: dict, key: str) -> object:
    value = proxy.get(key)
    if value is None or value == "":
        raise _UnsupportedProxy(f"缺少 {key}")
    return value


def _only_keys(proxy: dict, allowed: set[str]) -> None:
    extra = set(proxy) - _CORE_KEYS - allowed
    if extra:
        raise _UnsupportedProxy(f"含无法映射的字段：{sorted(extra)}")


def _bool_query(items: list[tuple[str, str]], key: str, value: object) -> None:
    if value is not None:
        items.append((key, "1" if truthy(value) else "0"))


def _transport_query(proxy: dict, query: list[tuple[str, str]]) -> None:
    network = str(proxy.get("network") or "tcp").lower()
    if network == "tcp":
        return
    query.append(("type", network))
    if network == "ws":
        opts = proxy.get("ws-opts") or {}
        if opts.get("path"):
            query.append(("path", str(opts["path"])))
        host = (opts.get("headers") or {}).get("Host")
        if host:
            query.append(("host", str(host)))
    elif network == "grpc":
        service = (proxy.get("grpc-opts") or {}).get("grpc-service-name")
        if service:
            query.append(("serviceName", str(service)))
    elif network == "h2":
        opts = proxy.get("h2-opts") or {}
        if opts.get("path"):
            query.append(("path", str(opts["path"])))
        hosts = opts.get("host") or []
        if hosts:
            query.append(("host", str(hosts[0])))
    else:
        raise _UnsupportedProxy(f"不支持的传输层：{network}")


def _tls_query(proxy: dict, query: list[tuple[str, str]], *, always: bool = False) -> None:
    reality = proxy.get("reality-opts") or {}
    if reality:
        query.append(("security", "reality"))
        if reality.get("public-key"):
            query.append(("pbk", str(reality["public-key"])))
        if reality.get("short-id"):
            query.append(("sid", str(reality["short-id"])))
    elif always or truthy(proxy.get("tls")):
        query.append(("security", "tls"))
    sni = proxy.get("sni") or proxy.get("servername")
    if sni:
        query.append(("sni", str(sni)))
    if proxy.get("skip-cert-verify") is not None:
        _bool_query(query, "allowInsecure", proxy.get("skip-cert-verify"))
    client_fp = proxy.get("client-fingerprint")
    if client_fp:
        query.append(("fp", str(client_fp)))
    fingerprint = proxy.get("fingerprint")
    if fingerprint:
        query.append(("fingerprint", str(fingerprint)))
    alpn = proxy.get("alpn")
    if alpn:
        query.append(("alpn", ",".join(str(x) for x in alpn)))


def _shadowrocket_ss(proxy: dict) -> str:
    _only_keys(proxy, {"cipher", "password", "udp", "plugin", "plugin-opts"})
    cipher = str(_required(proxy, "cipher"))
    password = str(_required(proxy, "password"))
    # SIP002：普通 AEAD/stream 推荐 Base64URL；AEAD-2022 必须用 percent-encoded 明文。
    if cipher.startswith("2022-"):
        userinfo = f"{quote(cipher, safe='')}:{quote(password, safe='')}"
    else:
        userinfo = _b64(f"{cipher}:{password}", urlsafe=True, padding=False)
    query: list[tuple[str, str]] = []
    plugin = proxy.get("plugin")
    if plugin:
        opts = proxy.get("plugin-opts") or {}
        if plugin == "obfs":
            parts = ["obfs-local"]
            if opts.get("mode"):
                parts.append(f"obfs={opts['mode']}")
            if opts.get("host"):
                parts.append(f"obfs-host={opts['host']}")
        elif plugin == "v2ray-plugin":
            parts = ["v2ray-plugin"]
            for key in ("mode", "host", "path"):
                if opts.get(key):
                    parts.append(f"{key}={opts[key]}")
            if opts.get("tls"):
                parts.append("tls")
        else:
            raise _UnsupportedProxy(f"不支持的 SS plugin：{plugin}")
        query.append(("plugin", ";".join(parts)))
    _bool_query(query, "udp", proxy.get("udp"))
    suffix = f"?{urlencode(query, quote_via=quote)}" if query else ""
    return (
        f"ss://{userinfo}@{_authority(proxy['server'], proxy['port'])}/{suffix}"
        f"#{quote(str(proxy['name']), safe='')}"
    )


def _shadowrocket_vmess(proxy: dict) -> str:
    _only_keys(proxy, {
        "uuid", "alterId", "cipher", "network", "tls", "servername", "sni",
        "skip-cert-verify", "client-fingerprint", "alpn", "ws-opts", "grpc-opts",
        "h2-opts", "udp",
    })
    network = str(proxy.get("network") or "tcp").lower()
    data: dict[str, object] = {
        "v": "2",
        "ps": str(proxy["name"]),
        "add": str(proxy["server"]),
        "port": str(int(proxy["port"])),
        "id": str(_required(proxy, "uuid")),
        "aid": str(int(proxy.get("alterId") or 0)),
        "scy": str(proxy.get("cipher") or "auto"),
        "net": network,
        "type": "none",
        "host": "",
        "path": "",
        "tls": "tls" if truthy(proxy.get("tls")) else "",
    }
    if proxy.get("servername") or proxy.get("sni"):
        data["sni"] = str(proxy.get("servername") or proxy.get("sni"))
    if proxy.get("skip-cert-verify") is not None:
        data["allowInsecure"] = 1 if truthy(proxy.get("skip-cert-verify")) else 0
    if proxy.get("client-fingerprint"):
        data["fp"] = str(proxy["client-fingerprint"])
    if proxy.get("alpn"):
        data["alpn"] = ",".join(str(x) for x in proxy["alpn"])
    if proxy.get("udp") is not None:
        data["udp"] = 1 if truthy(proxy.get("udp")) else 0
    if network == "ws":
        opts = proxy.get("ws-opts") or {}
        data["path"] = str(opts.get("path") or "")
        data["host"] = str((opts.get("headers") or {}).get("Host") or "")
    elif network == "grpc":
        data["path"] = str((proxy.get("grpc-opts") or {}).get("grpc-service-name") or "")
    elif network == "h2":
        opts = proxy.get("h2-opts") or {}
        data["path"] = str(opts.get("path") or "")
        hosts = opts.get("host") or []
        data["host"] = str(hosts[0]) if hosts else ""
    elif network != "tcp":
        raise _UnsupportedProxy(f"不支持的 VMess 传输层：{network}")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return "vmess://" + _b64(payload, padding=False)


def _shadowrocket_url(proxy: dict, scheme: str) -> str:
    common = {
        "password" if scheme != "vless" else "uuid", "network", "tls", "sni", "servername",
        "skip-cert-verify", "client-fingerprint", "fingerprint", "alpn", "ws-opts",
        "grpc-opts", "h2-opts", "udp", "flow", "packet-encoding", "encryption",
        "reality-opts",
    }
    _only_keys(proxy, common)
    credential = _required(proxy, "uuid" if scheme == "vless" else "password")
    query: list[tuple[str, str]] = []
    _tls_query(proxy, query, always=scheme == "trojan")
    _transport_query(proxy, query)
    for source, target in (
        ("flow", "flow"), ("packet-encoding", "packetEncoding"), ("encryption", "encryption")
    ):
        if proxy.get(source):
            query.append((target, str(proxy[source])))
    _bool_query(query, "udp", proxy.get("udp"))
    suffix = f"?{urlencode(query, quote_via=quote)}" if query else ""
    return (
        f"{scheme}://{quote(str(credential), safe='')}@{_authority(proxy['server'], proxy['port'])}"
        f"{suffix}#{quote(str(proxy['name']), safe='')}"
    )


def _shadowrocket_hysteria(proxy: dict) -> str:
    _only_keys(proxy, {
        "auth-str", "protocol", "sni", "skip-cert-verify", "up", "down", "obfs", "udp"
    })
    query: list[tuple[str, str]] = []
    for source, target in (
        ("auth-str", "auth"), ("protocol", "protocol"), ("sni", "peer"),
        ("up", "up"), ("down", "down"), ("obfs", "obfs"),
    ):
        if proxy.get(source):
            query.append((target, str(proxy[source])))
    _bool_query(query, "insecure", proxy.get("skip-cert-verify"))
    _bool_query(query, "udp", proxy.get("udp"))
    suffix = f"?{urlencode(query, quote_via=quote)}" if query else ""
    return (
        f"hysteria://{_authority(proxy['server'], proxy['port'])}{suffix}"
        f"#{quote(str(proxy['name']), safe='')}"
    )


def _shadowrocket_hysteria2(proxy: dict) -> str:
    _only_keys(proxy, {
        "password", "ports", "hop-interval", "up", "down", "obfs", "obfs-password",
        "sni", "fingerprint", "skip-cert-verify", "alpn", "udp",
    })
    query: list[tuple[str, str]] = []
    for source, target in (
        ("ports", "ports"), ("hop-interval", "hop-interval"), ("up", "up"),
        ("down", "down"), ("obfs", "obfs"), ("obfs-password", "obfs-password"),
        ("sni", "sni"), ("fingerprint", "fingerprint"),
    ):
        if proxy.get(source):
            query.append((target, str(proxy[source])))
    _bool_query(query, "insecure", proxy.get("skip-cert-verify"))
    if proxy.get("alpn"):
        query.append(("alpn", ",".join(str(x) for x in proxy["alpn"])))
    _bool_query(query, "udp", proxy.get("udp"))
    suffix = f"?{urlencode(query, quote_via=quote)}" if query else ""
    return (
        f"hysteria2://{quote(str(_required(proxy, 'password')), safe='')}@"
        f"{_authority(proxy['server'], proxy['port'])}{suffix}#{quote(str(proxy['name']), safe='')}"
    )


def _shadowrocket_uri(proxy: dict) -> str:
    kind = str(proxy.get("type") or "").lower()
    if kind == "ss":
        return _shadowrocket_ss(proxy)
    if kind == "vmess":
        return _shadowrocket_vmess(proxy)
    if kind in {"trojan", "vless"}:
        return _shadowrocket_url(proxy, kind)
    if kind == "hysteria":
        return _shadowrocket_hysteria(proxy)
    if kind in {"hysteria2", "hy2"}:
        return _shadowrocket_hysteria2(proxy)
    raise _UnsupportedProxy(f"Shadowrocket 不支持 {kind}")


def _safe_config_labels(names: list[str], reserved: set[str]) -> dict[str, str]:
    used = set(reserved)
    out: dict[str, str] = {}
    for original in names:
        base = re.sub(r'[=,\r\n\t"\\#;]+', " ", original).strip() or "node"
        name = base
        if name in used:
            name = f"{base}-{hashlib.sha1(original.encode()).hexdigest()[:6]}"
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        out[original] = name
    return out


def _shadowrocket_region_markers(regions: list[str]) -> dict[str, str]:
    """生成只含字母数字/下划线的稳定 marker，供 policy-regex-filter 精确匹配。"""
    used: set[str] = set()
    out: dict[str, str] = {}
    for region in regions:
        base = "".join(ch if ch.isalnum() else "_" for ch in region).strip("_") or "region"
        marker = base
        if marker in used:
            marker = f"{base}_{hashlib.sha1(region.encode()).hexdigest()[:6]}"
        suffix = 2
        while marker in used:
            marker = f"{base}_{suffix}"
            suffix += 1
        used.add(marker)
        out[region] = marker
    return out


def _shadowrocket_nodes(clash_config: dict) -> tuple[list[tuple[dict, str, str]], list[str]]:
    """返回兼容节点 (原 proxy, URI, region) 与按配置顺序出现的地区。"""
    all_proxies = clash_config.get("proxies") or []
    region_by_proxy: dict[str, str] = {}
    ordered_regions: list[str] = []
    for group in clash_config.get("proxy-groups") or []:
        region = str(group.get("name") or "")
        if not region or region in _CORE_GROUPS:
            continue
        if region not in ordered_regions:
            ordered_regions.append(region)
        for member in group.get("proxies") or []:
            region_by_proxy.setdefault(str(member), region)

    uncategorized = "未分类"
    if (
        any(str(proxy.get("name")) not in region_by_proxy for proxy in all_proxies)
        and uncategorized not in ordered_regions
    ):
        ordered_regions.append(uncategorized)
    markers = _shadowrocket_region_markers(ordered_regions)

    compatible: list[tuple[dict, str, str]] = []
    for proxy in all_proxies:
        region = region_by_proxy.get(str(proxy.get("name")), uncategorized)
        renamed = {**proxy, "name": f"@{markers[region]}:{proxy.get('name', '')}"}
        try:
            compatible.append((proxy, _shadowrocket_uri(renamed), region))
        except (_UnsupportedProxy, TypeError, ValueError, KeyError):
            continue
    return compatible, ordered_regions


def render_shadowrocket_subscription(clash_config: dict) -> ClientSubscription:
    """输出 Shadowrocket 常用的整份 base64 URI 行节点订阅。"""
    all_proxies = clash_config.get("proxies") or []
    compatible, _ = _shadowrocket_nodes(clash_config)
    if not compatible:
        raise NoCompatibleProxies("没有可导出的 Shadowrocket 兼容节点")
    uris = [uri for _, uri, _ in compatible]
    raw = "\n".join(uris) + "\n"
    return ClientSubscription(_b64(raw), len(uris), len(all_proxies) - len(uris))


def _config_value(value: object) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise _UnsupportedProxy("Surge 值含换行")
    if not text or text != text.strip() or any(ch in text for ch in '=,"\\#;'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _surge_params(parts: list[str], params: list[tuple[str, object]]) -> str:
    return ", ".join([*parts, *(f"{key}={_config_value(value)}" for key, value in params)])


def _surge_tls(proxy: dict) -> list[tuple[str, object]]:
    params: list[tuple[str, object]] = []
    sni = proxy.get("sni") or proxy.get("servername")
    if sni:
        params.append(("sni", sni))
    if proxy.get("skip-cert-verify") is not None:
        params.append(("skip-cert-verify", str(truthy(proxy.get("skip-cert-verify"))).lower()))
    if proxy.get("alpn"):
        params.append(("alpn", ",".join(str(x) for x in proxy["alpn"])))
    return params


def _surge_ws(proxy: dict) -> list[tuple[str, object]]:
    network = str(proxy.get("network") or "tcp").lower()
    if network == "tcp":
        return []
    if network != "ws":
        raise _UnsupportedProxy(f"Surge 不支持该传输层：{network}")
    opts = proxy.get("ws-opts") or {}
    params: list[tuple[str, object]] = [("ws", "true")]
    if opts.get("path"):
        params.append(("ws-path", opts["path"]))
    headers = opts.get("headers") or {}
    if headers:
        rendered = "|".join(f"{key}:{value}" for key, value in headers.items())
        params.append(("ws-headers", rendered))
    return params


def _surge_ss(proxy: dict) -> str:
    _only_keys(proxy, {"cipher", "password", "udp", "plugin", "plugin-opts"})
    params: list[tuple[str, object]] = [
        ("encrypt-method", _required(proxy, "cipher")),
        ("password", _required(proxy, "password")),
        ("udp-relay", "true"),
    ]
    plugin = proxy.get("plugin")
    if plugin:
        if plugin != "obfs":
            raise _UnsupportedProxy(f"Surge 不支持 SS plugin：{plugin}")
        opts = proxy.get("plugin-opts") or {}
        mode = opts.get("mode")
        if mode not in {"http", "tls"}:
            raise _UnsupportedProxy("Surge SS obfs mode 非法")
        params.append(("obfs", mode))
        if opts.get("host"):
            params.append(("obfs-host", opts["host"]))
    return _surge_params(
        ["ss", _config_value(proxy["server"]), str(int(proxy["port"]))], params
    )


def _surge_vmess(proxy: dict) -> str:
    _only_keys(proxy, {
        "uuid", "alterId", "cipher", "network", "tls", "servername", "sni",
        "skip-cert-verify", "alpn", "ws-opts", "udp",
    })
    if int(proxy.get("alterId") or 0) != 0:
        raise _UnsupportedProxy("Surge 无法表达 VMess alterId")
    cipher = str(proxy.get("cipher") or "auto").lower()
    if cipher not in {"auto", "aes-128-gcm", "chacha20-ietf-poly1305"}:
        raise _UnsupportedProxy(f"Surge 不支持 VMess cipher：{cipher}")
    params: list[tuple[str, object]] = [
        ("username", _required(proxy, "uuid")), ("vmess-aead", "true")
    ]
    if cipher != "auto":
        params.append(("encrypt-method", cipher))
    if truthy(proxy.get("tls")):
        params.append(("tls", "true"))
    params += _surge_ws(proxy) + _surge_tls(proxy)
    return _surge_params(
        ["vmess", _config_value(proxy["server"]), str(int(proxy["port"]))], params
    )


def _surge_trojan(proxy: dict) -> str:
    _only_keys(proxy, {
        "password", "network", "sni", "servername", "skip-cert-verify", "alpn",
        "ws-opts", "udp",
    })
    params: list[tuple[str, object]] = [("password", _required(proxy, "password"))]
    params += _surge_ws(proxy) + _surge_tls(proxy)
    return _surge_params(
        ["trojan", _config_value(proxy["server"]), str(int(proxy["port"]))], params
    )


def _number(value: object) -> str:
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    if not match:
        raise _UnsupportedProxy(f"无法解析数字：{value}")
    return match.group(0)


def _surge_hysteria2(proxy: dict) -> str:
    _only_keys(proxy, {
        "password", "ports", "hop-interval", "up", "down", "obfs", "obfs-password",
        "sni", "fingerprint", "skip-cert-verify", "alpn", "udp",
    })
    params: list[tuple[str, object]] = [("password", _required(proxy, "password"))]
    if proxy.get("down"):
        params.append(("download-bandwidth", _number(proxy["down"])))
    if proxy.get("ports"):
        params.append(("port-hopping", str(proxy["ports"]).replace(",", ";")))
    if proxy.get("hop-interval"):
        params.append(("port-hopping-interval", _number(proxy["hop-interval"])))
    # Surge 的 salamander 参数目前不是 iOS/macOS 共同能力；跨平台订阅不做有损映射。
    if proxy.get("obfs") or proxy.get("obfs-password"):
        raise _UnsupportedProxy("Surge 跨平台配置无法映射 Hysteria 2 obfs")
    fingerprint = str(proxy.get("fingerprint") or "").replace(":", "")
    if fingerprint:
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", fingerprint):
            raise _UnsupportedProxy("Surge 无法映射 Hysteria 2 fingerprint")
        params.append(("server-cert-fingerprint-sha256", fingerprint.lower()))
    params += _surge_tls(proxy)
    return _surge_params(
        ["hysteria2", _config_value(proxy["server"]), str(int(proxy["port"]))], params
    )


def _surge_proxy(proxy: dict) -> str:
    kind = str(proxy.get("type") or "").lower()
    if kind == "ss":
        return _surge_ss(proxy)
    if kind == "vmess":
        return _surge_vmess(proxy)
    if kind == "trojan":
        return _surge_trojan(proxy)
    if kind in {"hysteria2", "hy2"}:
        return _surge_hysteria2(proxy)
    raise _UnsupportedProxy(f"Surge 不支持 {kind}")


def _client_rules(
    clash_config: dict,
    target_map: dict[str, str],
    *,
    destination_port: str,
    ipv6_cidr: str = "IP-CIDR6",
    unsupported_kinds: frozenset[str] = frozenset(),
    ruleset_options: bool = True,
) -> list[str]:
    providers = clash_config.get("rule-providers") or {}
    out: list[str] = []
    for rule in clash_config.get("rules") or []:
        parts = str(rule).split(",")
        kind = parts[0]
        if kind in unsupported_kinds:
            continue
        if kind == "MATCH" and len(parts) >= 2:
            target = target_map.get(parts[1], "PROXY")
            out.append(f"FINAL,{target}")
            continue
        if len(parts) < 3:
            continue
        value, target = parts[1], parts[2]
        target = target_map.get(target, "PROXY")
        options = parts[3:]
        if kind == "RULE-SET":
            spec = providers.get(value)
            if not spec:
                continue
            url = str(spec.get("url") or "")
            parsed = urlsplit(url)
            path = parsed.path[:-4] + ".list" if parsed.path.endswith(".mrs") else parsed.path
            value = urlunsplit(parsed._replace(path=path))
            if not value:
                continue
        elif kind == "GEOSITE":
            kind, value = "RULE-SET", f"{_MRS_BASE}/geosite/{value.lower()}.list"
        elif kind == "GEOIP":
            kind, value = "RULE-SET", f"{_MRS_BASE}/geoip/{value.lower()}.list"
        elif kind == "DST-PORT":
            kind = destination_port
        elif kind == "IP-CIDR6":
            # Shadowrocket 当前用 IP-CIDR 同时承载 IPv4/IPv6；Surge 仍保留 IP-CIDR6。
            kind = ipv6_cidr
        if kind == "RULE-SET" and not ruleset_options:
            options = []
        out.append(",".join([kind, _config_value(value), target, *options]))
    return out


def _shadowrocket_subscription_name(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 64
        or any(ch in value for ch in '=,#;\r\n\t"\\')
    ):
        raise ValueError("Shadowrocket 节点订阅名称非法")
    return value


def render_shadowrocket_config(
    clash_config: dict,
    *,
    subscription_name: str = "conduit",
    update_url: str | None = None,
) -> ClientSubscription:
    """输出引用同名节点订阅、包含地区组与规则的 Shadowrocket 完整配置。"""
    subscription_name = _shadowrocket_subscription_name(subscription_name)
    all_proxies = clash_config.get("proxies") or []
    compatible, ordered_regions = _shadowrocket_nodes(clash_config)
    if not compatible:
        raise NoCompatibleProxies("没有可导出的 Shadowrocket 兼容节点")

    active_regions = {region for _, _, region in compatible}
    regions = [region for region in ordered_regions if region in active_regions]
    region_name_map = _safe_config_labels(
        regions, _CORE_GROUPS | _CLIENT_BUILTINS
    )
    markers = _shadowrocket_region_markers(ordered_regions)

    groups: list[str] = []
    for region in regions:
        groups.append(
            f"{region_name_map[region]} = fallback,{subscription_name},use=true,"
            f"policy-regex-filter=^@{markers[region]}:,interval=60,timeout=2,"
            f"url={_SHADOWROCKET_TEST_URL}"
        )
    groups.append(
        f"AUTO = url-test,{subscription_name},use=true,interval=60,timeout=2,"
        f"tolerance=200,url={_SHADOWROCKET_TEST_URL}"
    )
    groups.append(
        "PROXY = select,AUTO"
        + "".join(f",{region_name_map[region]}" for region in regions)
    )

    target_map = {name: name for name in _CLIENT_BUILTINS | _CORE_GROUPS}
    target_map.update(region_name_map)
    rules = _client_rules(
        clash_config,
        target_map,
        destination_port="DST-PORT",
        ipv6_cidr="IP-CIDR",
        unsupported_kinds=frozenset({"PROCESS-NAME"}),
        ruleset_options=False,
    )
    if not rules or not rules[-1].startswith("FINAL,"):
        rules.append("FINAL,PROXY")

    lines = ["[General]", "loglevel = notify", "dns-server = system, 1.1.1.1, 8.8.8.8"]
    if update_url:
        if "\n" in update_url or "\r" in update_url:
            raise ValueError("update_url 含换行")
        lines.append(f"update-url = {update_url}")
    lines += ["", "[Proxy Group]", *groups, "", "[Rule]", *rules, ""]
    return ClientSubscription(
        "\n".join(lines), len(compatible), len(all_proxies) - len(compatible)
    )


def render_surge_subscription(
    clash_config: dict, *, managed_url: str | None = None
) -> ClientSubscription:
    """输出可直接导入和远程更新的完整 Surge profile。"""
    all_proxies = clash_config.get("proxies") or []
    compatible: list[tuple[dict, str]] = []
    for proxy in all_proxies:
        try:
            compatible.append((proxy, _surge_proxy(proxy)))
        except (_UnsupportedProxy, TypeError, ValueError, KeyError):
            continue
    if not compatible:
        raise NoCompatibleProxies("没有可导出的 Surge 兼容节点")

    group_specs = {g["name"]: g for g in clash_config.get("proxy-groups") or []}
    proxy_names = {proxy["name"] for proxy, _ in compatible}

    region_groups: list[str] = []
    for group in clash_config.get("proxy-groups") or []:
        name = group.get("name")
        if name in {"PROXY", "AUTO", "AUTO-FAST"}:
            continue
        if any(member in proxy_names for member in group.get("proxies") or []):
            region_groups.append(name)

    core_groups = _CORE_GROUPS
    region_name_map = _safe_config_labels(region_groups, core_groups | _CLIENT_BUILTINS)
    name_map = _safe_config_labels(
        [proxy["name"] for proxy, _ in compatible],
        core_groups | _CLIENT_BUILTINS | set(region_name_map.values()),
    )

    # 叶子到根：避免旧版 Surge 对同 section 内的 group 前向引用解析不一致。
    groups: list[str] = []
    ordered_names = [name_map[p["name"]] for p, _ in compatible]
    for region in region_groups:
        members = [
            name_map[name] for name in group_specs[region].get("proxies") or [] if name in name_map
        ]
        groups.append(
            f"{region_name_map[region]} = fallback, "
            + ", ".join(members)
            + ", interval=60, timeout=2"
        )
    groups.append(
        "AUTO-FAST = url-test, " + ", ".join(ordered_names)
        + ", interval=60, tolerance=200, hidden=true"
    )
    groups.append(
        "AUTO = fallback, AUTO-FAST, "
        + ", ".join(ordered_names)
        + ", interval=60, timeout=2"
    )
    groups.append(
        "PROXY = select, AUTO"
        + "".join(f", {region_name_map[name]}" for name in region_groups)
    )

    target_map = {name: name for name in _CLIENT_BUILTINS | core_groups}
    target_map.update(region_name_map)
    rules = _client_rules(clash_config, target_map, destination_port="DEST-PORT")
    if not rules or not rules[-1].startswith("FINAL,"):
        rules.append("FINAL,PROXY")

    lines: list[str] = []
    if managed_url:
        if "\n" in managed_url or "\r" in managed_url:
            raise ValueError("managed_url 含换行")
        lines += [f"#!MANAGED-CONFIG {managed_url} interval=86400 strict=false", ""]
    lines += [
        "[General]",
        "loglevel = notify",
        "dns-server = system, 1.1.1.1, 8.8.8.8",
        "proxy-test-url = http://www.gstatic.com/generate_204",
        "test-timeout = 5",
        "",
        "[Proxy]",
    ]
    for proxy, declaration in compatible:
        lines.append(f"{name_map[proxy['name']]} = {declaration}")
    lines += ["", "[Proxy Group]", *groups, "", "[Rule]", *rules, ""]
    return ClientSubscription(
        "\n".join(lines), len(compatible), len(all_proxies) - len(compatible)
    )
