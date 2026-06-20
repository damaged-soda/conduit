"""ingest / 身份 测试：clash → Node（丢规则）+ 两层身份性质 + 脏数据诊断 + import→render→golden。"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))           # for test_config_invariants
sys.path.insert(0, str(HERE.parent))    # repo root, for the conduit package

from conduit.identity import InvalidProxy, access_id, endpoint_id  # noqa: E402
from conduit.ingest import ingest, normalize  # noqa: E402
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


def test_ingest_reports_skipped():
    res = ingest(FIXTURE, "clash", "vendor-a")
    assert len(res.nodes) == 2
    assert len(res.skipped) == 1 and res.skipped[0]["name"] == "broken"
    assert "server" in res.skipped[0]["reason"]


def test_bad_port_skipped_not_aborted():
    raw = "proxies:\n  - {name: ok, type: ss, server: s.com, port: 8388, password: p}\n  - {name: bad, type: ss, server: s.com, port: nope, password: p}\n"
    res = ingest(raw)
    assert len(res.nodes) == 1 and len(res.skipped) == 1  # 坏 port 不炸整批
    assert res.skipped[0]["name"] == "bad"


@pytest.mark.parametrize("bad_port", [True, 0, 70000, "nope"])
def test_invalid_port_rejected(bad_port):
    with pytest.raises(InvalidProxy):
        access_id({"name": "x", "type": "ss", "server": "s.com", "port": bad_port, "password": "p"})


def test_blank_or_missing_core_rejected():
    for bad in ({"type": "ss", "server": " ", "port": 1}, {"server": "s.com", "port": 1}, {"type": "ss", "port": 1}):
        with pytest.raises(InvalidProxy):
            access_id(bad)


def test_port_string_vs_int_same_identity():
    a = {"name": "A", "type": "ss", "server": "s.com", "port": 8388, "password": "p"}
    b = {**a, "port": "8388"}  # 字符串 port
    assert access_id(a).value == access_id(b).value
    assert endpoint_id(a) == endpoint_id(b)


def test_access_id_stable_across_rename_but_sensitive_to_params():
    base = {"name": "A", "type": "ss", "server": "s.com", "port": 8388, "cipher": "aes-256-gcm", "password": "p"}
    assert access_id(base).value == access_id({**base, "name": "B 完全不同"}).value   # 改名不变
    assert access_id(base).value != access_id({**base, "password": "p2"}).value      # 参数变则不同


def test_identity_ignores_client_prefs():
    base = {"name": "A", "type": "ss", "server": "s.com", "port": 8388, "password": "p"}
    assert access_id({**base, "udp": True}).value == access_id({**base, "udp": False}).value  # 本地偏好不进身份
    assert access_id({**base, "tfo": True}).value == access_id(base).value


def test_server_normalized_in_identity():
    p = {"name": "x", "type": "ss", "server": " S.Example.COM ", "port": 1, "password": "p"}
    assert endpoint_id(p).server == "s.example.com"
    assert access_id(p).value == access_id({**p, "server": "s.example.com"}).value


def test_dedup_same_access_id_across_imports():
    p = "proxies:\n  - {name: X, type: ss, server: s.com, port: 8388, password: p}\n"
    n1 = normalize(p, "clash", "vendor-a")[0]
    n2 = normalize(p, "clash", "vendor-b")[0]  # 同节点不同来源
    assert n1.access_id.value == n2.access_id.value


def test_missing_or_invalid_doc_reported():
    assert ingest("port: 7890\n").skipped[0]["reason"] == "未找到 proxies 列表"
    assert ingest("- just\n- a\n- list\n").skipped[0]["reason"] == "不是合法的 clash YAML 文档"


def test_malformed_yaml_reported_not_raised():
    res = ingest("proxies: 'unterminated\n  - {oops")  # YAML 语法坏
    assert res.nodes == [] and res.skipped[0]["reason"] == "不是合法的 clash YAML 文档"


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


def test_rendered_server_is_normalized_v1():
    """记录 v1 行为：身份归一（lower server）也进渲染输出；v1 仅大小写、DNS 不敏感、安全。"""
    nodes = normalize("proxies:\n  - {name: x, type: ss, server: Mixed.Case.COM, port: 8388, password: p}\n")
    cfg = yaml.safe_load(render(nodes, "t", DIRECT, {"listen": "127.0.0.1:7890", "controller": {"bind": "127.0.0.1:9090"}}))
    assert cfg["proxies"][0]["server"] == "mixed.case.com"
