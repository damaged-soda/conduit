"""render v1 测试：合成节点 + direct-list + overlay → render() → 让 golden 不变量断言真实产出。

这就是把第 1 层 golden 从「手写夹具」升级为「断言生成器输出」——render 一产出，不变量就盯着。
"""

from __future__ import annotations

import pathlib
import sys

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


def _node(name: str, server: str, port: int = 1080, type_: str = "socks5", **params) -> Node:
    ep = EndpointId(type=type_, server=server, port=port)
    return Node(access_id=AccessId(value=f"hash-{name}", endpoint=ep), raw_name=name, params=params, source="syn")


def _render_cfg() -> dict:
    nodes = [_node("up-a", "placeholder-a"), _node("up-b", "placeholder-b")]
    return yaml.safe_load(render(nodes, "host-test", DIRECT, OVERLAY))


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
    """生产安全：overlay 不要求时不开 allow-lan（Codex 提的前向注意）。"""
    nodes = [_node("up-a", "placeholder-a")]
    overlay = {"listen": "127.0.0.1:7890", "controller": {"bind": "127.0.0.1:9090"}}
    cfg = yaml.safe_load(render(nodes, "host-prod", DIRECT, overlay))
    assert "allow-lan" not in cfg
