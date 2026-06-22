"""ingest / 身份 测试：clash → Node（丢规则）+ 两层身份性质 + import→render→golden。

刻意不测格式容错（订阅机器生成、格式规整；节点连不上交给健康回路，不在 ingest 管）。
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))           # for test_config_invariants
sys.path.insert(0, str(HERE.parent))    # repo root, for the conduit package

from conduit.identity import access_id, endpoint_id  # noqa: E402
from conduit.ingest import normalize  # noqa: E402
from conduit.render import render  # noqa: E402
from test_config_invariants import DIRECT, all_violations  # noqa: E402

FIXTURE = (HERE / "fixtures" / "sub.clash.yaml").read_text()


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_normalize_extracts_proxies_drops_rules():
    nodes = normalize(FIXTURE, "clash", "vendor-a")
    assert len(nodes) == 2  # 残缺条目（缺 server/port）被一行过滤跳过
    assert {n.access_id.endpoint.type for n in nodes} == {"ss", "trojan"}
    us = next(n for n in nodes if n.access_id.endpoint.type == "ss")
    assert us.access_id.endpoint.server == "us1.example.com"  # 规范化（原 US1.Example.com）
    assert us.access_id.endpoint.port == 8388
    assert us.raw_name == "🇺🇸 US-01 | 1x"
    assert us.source == "vendor-a"
    assert set(us.params) == {"cipher", "password", "udp"}  # 核心字段不进 params
    assert "type" not in us.params and "server" not in us.params


def test_access_id_stable_across_rename_but_sensitive_to_params():
    base = {"name": "A", "type": "ss", "server": "s.com", "port": 8388, "cipher": "aes-256-gcm", "password": "p"}
    assert access_id(base).value == access_id({**base, "name": "B 完全不同"}).value   # 改名不变
    assert access_id(base).value != access_id({**base, "password": "p2"}).value      # 参数变则不同


def test_port_string_vs_int_same_identity():
    a = {"name": "A", "type": "ss", "server": "s.com", "port": 8388, "password": "p"}
    assert access_id(a).value == access_id({**a, "port": "8388"}).value
    assert endpoint_id(a) == endpoint_id({**a, "port": "8388"})


def test_server_normalized_in_identity():
    p = {"name": "x", "type": "ss", "server": " S.Example.COM ", "port": 1, "password": "p"}
    assert endpoint_id(p).server == "s.example.com"
    assert access_id(p).value == access_id({**p, "server": "s.example.com"}).value


def test_dedup_same_access_id_across_imports():
    p = "proxies:\n  - {name: X, type: ss, server: s.com, port: 8388, password: p}\n"
    assert normalize(p, "clash", "vendor-a")[0].access_id.value == normalize(p, "clash", "vendor-b")[0].access_id.value


def test_vmess_ws_params_preserved():
    raw = (
        "proxies:\n"
        "  - {name: v, type: vmess, server: v.com, port: 443, uuid: u-1, alterId: 0, cipher: auto,"
        " network: ws, ws-opts: {path: /ray, headers: {Host: v.com}}}\n"
    )
    n = normalize(raw)[0]
    assert n.params["network"] == "ws"
    assert n.params["ws-opts"]["path"] == "/ray"  # 嵌套字段无损保留


def test_normalize_uri_subscription_lines():
    vmess = _b64(json.dumps({
        "ps": "VM WS",
        "add": "vm.example.com",
        "port": "443",
        "id": "uuid-1",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "host": "cdn.example.com",
        "path": "/ray",
        "tls": "tls",
        "sni": "vm.example.com",
        "fp": "chrome",
    }))
    raw = "\n".join([
        "ss://"
        + _b64("aes-256-gcm:ss-pass")
        + "@SS.Example.com:8388?plugin=obfs-local%3Bobfs%3Dtls%3Bobfs-host%3Dcdn.example.com#SS",
        f"vmess://{vmess}",
        "trojan://tj-pass@tj.example.com:443?sni=tj.example.com&type=ws&host=cdn.example.com&path=%2Fws#TJ",
        "vless://uuid-2@vl.example.com:443?security=reality&sni=www.example.com&fp=chrome"
        "&pbk=pubkey&sid=abcd&type=grpc&serviceName=svc&flow=xtls-rprx-vision#VL",
        "hy2://hy-pass@hy.example.com:443?sni=hy.example.com&obfs=salamander"
        "&obfs-password=obfs-pass#HY2",
    ])

    nodes = normalize(raw, "clash", "vendor-uri")
    by_name = {n.raw_name: n for n in nodes}
    assert set(by_name) == {"SS", "VM WS", "TJ", "VL", "HY2"}
    assert by_name["SS"].access_id.endpoint.server == "ss.example.com"
    assert by_name["SS"].params["plugin"] == "obfs"
    assert by_name["VM WS"].params["ws-opts"]["headers"]["Host"] == "cdn.example.com"
    assert by_name["TJ"].params["sni"] == "tj.example.com"
    assert by_name["VL"].params["reality-opts"] == {"public-key": "pubkey", "short-id": "abcd"}
    assert by_name["HY2"].params["obfs-password"] == "obfs-pass"


def test_normalize_base64_wrapped_uri_subscription():
    raw = "ss://" + _b64("aes-128-gcm:p") + "@s.example.com:8388#S"
    nodes = normalize(base64.b64encode(raw.encode()).decode(), "base64", "vendor-b64")
    assert len(nodes) == 1
    assert nodes[0].raw_name == "S"
    assert nodes[0].params["cipher"] == "aes-128-gcm"


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        normalize("x", "singbox", "v")


def test_malformed_uri_raises_when_no_node_survives():
    with pytest.raises(ValueError):
        normalize("ss://not-a-valid-node", "uri", "v")


def test_normalize_then_render_passes_invariants():
    """端到端：导入 clash → Node → render → 过全部 golden 不变量。"""
    nodes = normalize(FIXTURE, "clash", "vendor-a")
    overlay = {
        "listen": "0.0.0.0:7890",
        "allow_lan": True,
        "controller": {"bind": "0.0.0.0:9090", "secret": "t"},
        "dns": {"fake_ip": True},
        "tun": {"enable": True},
    }
    cfg = yaml.safe_load(render(nodes, "host-test", DIRECT, overlay))
    flat = [f"[{k}] {m}" for k, ms in all_violations(cfg).items() for m in ms]
    assert not flat, "import→render 产出违反不变量:\n" + "\n".join(flat)
    assert {p["name"] for p in cfg["proxies"]} == {"🇺🇸 US-01 | 1x", "🇯🇵 JP-01"}
