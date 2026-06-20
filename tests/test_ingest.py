"""ingest / 身份 测试：clash → Node（丢规则）+ 两层身份性质 + import→render→golden。

刻意不测格式容错（订阅机器生成、格式规整；节点连不上交给健康回路，不在 ingest 管）。
"""

from __future__ import annotations

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


def test_unsupported_type_raises():
    with pytest.raises(ValueError):
        normalize("x", "base64", "v")


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
