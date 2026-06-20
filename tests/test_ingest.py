"""ingest / 身份 测试：clash 订阅 → Node（丢规则/groups）+ 两层身份性质 + import→render→golden 端到端。"""

from __future__ import annotations

import pathlib
import sys

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
    assert len(nodes) == 2  # broken（缺 server/port）被跳过
    assert {n.access_id.endpoint.type for n in nodes} == {"ss", "trojan"}
    us = next(n for n in nodes if n.access_id.endpoint.type == "ss")
    assert us.access_id.endpoint.server == "us1.example.com"  # 规范化（原 US1.Example.com）
    assert us.access_id.endpoint.port == 8388
    assert us.raw_name == "🇺🇸 US-01 | 1x"
    assert us.source == "vendor-a"
    assert set(us.params) == {"cipher", "password", "udp"}  # 核心字段不进 params
    assert "type" not in us.params and "server" not in us.params


def test_access_id_stable_across_rename_but_sensitive_to_params():
    base = {"name": "A", "type": "ss", "server": "s.example.com", "port": 8388, "cipher": "aes-256-gcm", "password": "p"}
    renamed = {**base, "name": "B 完全不同的名字"}
    changed = {**base, "password": "p2"}
    assert access_id(base).value == access_id(renamed).value   # 改名不变身份
    assert access_id(base).value != access_id(changed).value   # 任何连接参数变 = 不同节点
    assert endpoint_id(base) == endpoint_id(renamed)           # 同 endpoint


def test_server_normalized_in_identity():
    p = {"name": "x", "type": "ss", "server": " S.Example.COM ", "port": 1, "password": "p"}
    assert endpoint_id(p).server == "s.example.com"
    assert access_id(p).value == access_id({**p, "server": "s.example.com"}).value  # 大小写/空白归一


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


def test_unsupported_type_raises():
    import pytest

    with pytest.raises(ValueError):
        normalize("x", "base64", "v")
