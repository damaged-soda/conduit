"""render v1 测试：合成节点 + direct-list + overlay → render() → 让 golden 不变量断言真实产出，
外加负例 / 安全用例（按 Codex review：去重、保留名、空节点、params 覆盖、route_exclude 合并、controller 安全）。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest
import yaml

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))           # for test_config_invariants
sys.path.insert(0, str(HERE.parent))    # repo root, for the conduit package

from conduit.models import AccessId, EndpointId, Node  # noqa: E402
from conduit.render import render  # noqa: E402
from test_config_invariants import DIRECT, all_violations  # noqa: E402

OVERLAY = {
    "listen": "0.0.0.0:7890",
    "allow_lan": True,
    "controller": {"bind": "0.0.0.0:9090", "secret": "test"},
    "dns": {"fake_ip": True},
    "tun": {"enable": True},
}


def _node(name: str, server: str, port: int = 1080, type_: str = "socks5", aid: str | None = None, **params) -> Node:
    ep = EndpointId(type=type_, server=server, port=port)
    return Node(access_id=AccessId(value=aid or f"hash-{name}-{server}", endpoint=ep), raw_name=name, params=params, source="syn")


def _render_cfg(nodes=None, overlay=None) -> dict:
    nodes = nodes or [_node("up-a", "placeholder-a"), _node("up-b", "placeholder-b")]
    return yaml.safe_load(render(nodes, "host-test", DIRECT, overlay or OVERLAY))


def test_render_output_satisfies_invariants():
    """render 的真实产出必须过全部 golden 不变量（DIRECT 最前、三处覆盖、规则只引用 group…）。"""
    flat = [f"[{k}] {m}" for k, ms in all_violations(_render_cfg()).items() for m in ms]
    assert not flat, "render 产出违反不变量:\n" + "\n".join(flat)


def test_render_structure():
    cfg = _render_cfg()
    assert {p["name"] for p in cfg["proxies"]} == {"up-a", "up-b"}
    assert cfg["proxy-groups"][0]["name"] == "PROXY"
    assert cfg["rules"][0].startswith("DOMAIN,")   # direct 在最前
    assert cfg["rules"][-1] == "MATCH,PROXY"
    assert cfg["dns"]["enhanced-mode"] == "fake-ip"
    assert cfg["tun"]["auto-route"] is True
    assert cfg["mixed-port"] == 7890


def test_no_allow_lan_by_default():
    """生产安全：overlay 不要求时不开 allow-lan（Codex 前向注意）。"""
    cfg = _render_cfg(
        nodes=[_node("up-a", "placeholder-a")],
        overlay={"listen": "127.0.0.1:7890", "controller": {"bind": "127.0.0.1:9090"}},
    )
    assert "allow-lan" not in cfg


def test_empty_nodes_fail_closed():
    with pytest.raises(ValueError):
        render([], "host-test", DIRECT, OVERLAY)


def test_duplicate_raw_names_get_unique_proxy_names():
    nodes = [_node("dup", "s1", aid="a1"), _node("dup", "s2", aid="a2")]
    cfg = _render_cfg(nodes=nodes)
    names = [p["name"] for p in cfg["proxies"]]
    assert len(set(names)) == 2, f"重名节点的 proxy 名没去重：{names}"
    assert not any(ms for ms in all_violations(cfg).values())


def test_reserved_name_avoided():
    cfg = _render_cfg(nodes=[_node("PROXY", "s1"), _node("up-b", "s2")])
    names = {p["name"] for p in cfg["proxies"]}
    assert "PROXY" not in names, "proxy 名撞上了 group/保留名 PROXY"
    assert not any(ms for ms in all_violations(cfg).values())


def test_params_cannot_override_core_identity():
    ep = EndpointId(type="socks5", server="real-server", port=1080)
    n = Node(
        access_id=AccessId(value="a1", endpoint=ep),
        raw_name="up-a",
        params={"type": "evil", "server": "evil-server", "port": 9999, "username": "u"},  # 恶意覆盖 + 正常字段
        source="syn",
    )
    cfg = _render_cfg(nodes=[n])
    p = cfg["proxies"][0]
    assert p["type"] == "socks5" and p["server"] == "real-server" and p["port"] == 1080
    assert p["username"] == "u"  # 非核心 param 正常透传


def test_overlay_tun_route_exclude_merged():
    overlay = {**OVERLAY, "tun": {"enable": True, "route_exclude": ["192.168.0.0/16"]}}
    cfg = _render_cfg(overlay=overlay)
    excl = cfg["tun"]["route-exclude-address"]
    assert "192.168.0.0/16" in excl and "10.0.0.0/8" in excl


def test_controller_nonloopback_without_secret_fail_closed():
    overlay = {"listen": "127.0.0.1:7890", "controller": {"bind": "0.0.0.0:9090"}}  # 无 secret
    with pytest.raises(ValueError):
        render([_node("up-a", "s1")], "host-test", DIRECT, overlay)


@pytest.mark.skipif(shutil.which("mihomo") is None, reason="mihomo 未安装")
def test_render_output_passes_mihomo_check():
    cfg = render([_node("up-a", "placeholder-a"), _node("up-b", "placeholder-b")], "host-test", DIRECT, OVERLAY)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(cfg)
        path = f.name
    r = subprocess.run(["mihomo", "-t", "-f", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
