"""Shadowrocket / Surge 客户端订阅格式。"""

from __future__ import annotations

import base64
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.client_subscriptions import (  # noqa: E402
    NoCompatibleProxies,
    render_shadowrocket_subscription,
    render_surge_subscription,
)
from conduit.ingest import normalize  # noqa: E402


def _config(proxies: list[dict]) -> dict:
    names = [proxy["name"] for proxy in proxies]
    return {
        "proxies": proxies,
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["AUTO", "HK", "US"]},
            {"name": "AUTO", "type": "fallback", "proxies": ["AUTO-FAST", *names]},
            {"name": "AUTO-FAST", "type": "url-test", "proxies": names},
            {"name": "HK", "type": "fallback", "proxies": names[:2]},
            {"name": "US", "type": "fallback", "proxies": names[2:]},
        ],
        "rule-providers": {
            "ai": {
                "url": (
                    "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/meta/geo/"
                    "geosite/category-ai-!cn.mrs"
                )
            }
        },
        "rules": [
            "RULE-SET,ai,US",
            "GEOSITE,cn,DIRECT",
            "GEOIP,CN,DIRECT,no-resolve",
            "DST-PORT,22,DIRECT",
            "MATCH,PROXY",
        ],
    }


def test_shadowrocket_is_base64_uri_feed_and_round_trips_supported_protocols():
    proxies = [
        {
            "name": "SS 香港",
            "type": "ss",
            "server": "ss.example",
            "port": 8388,
            "cipher": "aes-256-gcm",
            "password": "p:a@ss",
            "udp": True,
        },
        {
            "name": "VM WS",
            "type": "vmess",
            "server": "vm.example",
            "port": 443,
            "uuid": "0233d11c-15a4-47d3-ade3-48ffca0ce119",
            "alterId": 0,
            "cipher": "auto",
            "network": "ws",
            "tls": True,
            "servername": "vm.example",
            "ws-opts": {"path": "/v2", "headers": {"Host": "cdn.example"}},
            "udp": True,
        },
        {
            "name": "Trojan",
            "type": "trojan",
            "server": "tj.example",
            "port": 443,
            "password": "tj/pass",
            "sni": "tj.example",
            "udp": True,
        },
        {
            "name": "VLESS Reality",
            "type": "vless",
            "server": "vl.example",
            "port": 443,
            "uuid": "uuid-vless",
            "tls": True,
            "servername": "www.example",
            "client-fingerprint": "chrome",
            "reality-opts": {"public-key": "pub", "short-id": "abcd"},
            "udp": True,
        },
        {
            "name": "HY2",
            "type": "hysteria2",
            "server": "hy.example",
            "port": 443,
            "password": "hy2-pass",
            "sni": "hy.example",
            "obfs": "salamander",
            "obfs-password": "obfs-pass",
            "udp": True,
        },
    ]

    rendered = render_shadowrocket_subscription(_config(proxies))
    raw = base64.b64decode(rendered.content).decode()
    assert [line.split(":", 1)[0] for line in raw.splitlines()] == [
        "ss", "vmess", "trojan", "vless", "hysteria2"
    ]
    round_tripped = normalize(raw, "uri", "shadowrocket")
    assert {node.raw_name for node in round_tripped} == {proxy["name"] for proxy in proxies}
    by_name = {node.raw_name: node for node in round_tripped}
    assert by_name["SS 香港"].params["password"] == "p:a@ss"
    assert by_name["VM WS"].params["ws-opts"] == {
        "path": "/v2", "headers": {"Host": "cdn.example"}
    }
    assert by_name["VLESS Reality"].params["reality-opts"] == {
        "public-key": "pub", "short-id": "abcd"
    }
    assert rendered.included == 5 and rendered.omitted == 0


def test_shadowrocket_skips_unmappable_nodes_and_rejects_empty_output():
    unsupported = {"name": "SOCKS", "type": "socks5", "server": "s", "port": 1080}
    supported = {
        "name": "SS", "type": "ss", "server": "s", "port": 8388,
        "cipher": "aes-128-gcm", "password": "p", "udp": True,
    }
    rendered = render_shadowrocket_subscription(_config([unsupported, supported]))
    assert rendered.included == 1 and rendered.omitted == 1
    with pytest.raises(NoCompatibleProxies):
        render_shadowrocket_subscription(_config([unsupported]))


def test_surge_profile_maps_nodes_groups_rules_and_reports_omissions():
    proxies = [
        {
            "name": "SS,HK",
            "type": "ss",
            "server": "ss.example",
            "port": 8388,
            "cipher": "aes-256-gcm",
            "password": 'p,a"b',
            "udp": True,
        },
        {
            "name": "Trojan WS",
            "type": "trojan",
            "server": "tj.example",
            "port": 443,
            "password": "tjpass",
            "sni": "tj.example",
            "network": "ws",
            "ws-opts": {"path": "/ws", "headers": {"Host": "cdn.example"}},
            "udp": True,
        },
        {
            "name": "VMess",
            "type": "vmess",
            "server": "vm.example",
            "port": 443,
            "uuid": "0233d11c-15a4-47d3-ade3-48ffca0ce119",
            "alterId": 0,
            "cipher": "auto",
            "tls": True,
            "udp": True,
        },
        {
            "name": "Hysteria 2",
            "type": "hysteria2",
            "server": "hy.example",
            "port": 443,
            "password": "hy-pass",
            "sni": "hy.example",
            "down": "100 Mbps",
            "udp": True,
        },
        {
            "name": "VLESS only",
            "type": "vless",
            "server": "vl.example",
            "port": 443,
            "uuid": "uuid-vless",
            "udp": True,
        },
    ]

    rendered = render_surge_subscription(
        _config(proxies), managed_url="https://conduit.example/sub/surge?token=secret"
    )
    profile = rendered.content
    assert profile.startswith(
        "#!MANAGED-CONFIG https://conduit.example/sub/surge?token=secret "
        "interval=86400 strict=false"
    )
    assert "[General]" in profile and "[Proxy]" in profile
    assert (
        'SS HK = ss, ss.example, 8388, encrypt-method=aes-256-gcm, '
        'password="p,a\\\"b", udp-relay=true'
    ) in profile
    assert "Trojan WS = trojan, tj.example, 443, password=tjpass, ws=true" in profile
    assert "VMess = vmess, vm.example, 443" in profile and "vmess-aead=true" in profile
    assert "Hysteria 2 = hysteria2, hy.example, 443, password=hy-pass" in profile
    assert "download-bandwidth=100" in profile
    assert "VLESS only =" not in profile
    assert "PROXY = select, AUTO, HK, US" in profile
    assert "/geosite/category-ai-!cn.list,US" in profile
    assert "/geosite/cn.list,DIRECT" in profile
    assert "/geoip/cn.list,DIRECT,no-resolve" in profile
    assert "DEST-PORT,22,DIRECT" in profile
    assert profile.rstrip().endswith("FINAL,PROXY")
    assert rendered.included == 4 and rendered.omitted == 1


def test_surge_rejects_snapshot_with_only_unsupported_protocols():
    proxy = {
        "name": "VLESS", "type": "vless", "server": "vl.example", "port": 443,
        "uuid": "uuid", "udp": True,
    }
    with pytest.raises(NoCompatibleProxies):
        render_surge_subscription(_config([proxy]))
