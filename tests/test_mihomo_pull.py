"""conduit-mihomo-pull 的纯函数测试：merge / 解析 fail-closed / 原子安装 / 重启命令探测。

不碰宿主机 mihomo（TESTING.md：宿主机神圣）——validate / fetch / 真实重启都不在这里跑。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
import yaml

SCRIPT = pathlib.Path(__file__).parent.parent / "scripts" / "conduit-mihomo-pull.py"
spec = importlib.util.spec_from_file_location("conduit_mihomo_pull", SCRIPT)
pull = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pull
spec.loader.exec_module(pull)

SUB = """\
port: 7890
mode: rule
dns:
  enable: true
  nameserver: [223.5.5.5]
proxies:
  - {name: n1, type: socks5, server: a.example.com, port: 1080}
proxy-groups:
  - {name: PROXY, type: select, proxies: [n1]}
rules: [MATCH,PROXY]
"""


def test_deep_merge_recurses_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    assert pull.deep_merge(base, {"a": {"y": 3, "z": 4}}) == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # 不改原对象


def test_deep_merge_replaces_lists_and_scalars():
    assert pull.deep_merge({"l": [1, 2]}, {"l": [3]}) == {"l": [3]}
    assert pull.deep_merge({"s": "a"}, {"s": "b"}) == {"s": "b"}
    assert pull.deep_merge({"k": {"d": 1}}, {"k": "flat"}) == {"k": "flat"}


def test_build_config_applies_overlay():
    out = yaml.safe_load(pull.build_config(SUB, {"external-controller": "127.0.0.1:9090",
                                                 "profile": {"store-selected": True}}))
    assert out["external-controller"] == "127.0.0.1:9090"
    assert out["profile"]["store-selected"] is True
    assert out["proxies"][0]["name"] == "n1"  # 订阅本体不动


def test_build_config_fail_closed_on_garbage():
    for bad in ("", "not: a config", "proxies: []", "- just\n- a\n- list\n"):
        with pytest.raises(pull.PullError):
            pull.build_config(bad, None)


def test_install_atomic_and_idempotent(tmp_path):
    cfg = tmp_path / "config.yaml"
    assert pull.install("v1", cfg) is True
    assert cfg.read_text() == "v1"
    assert (cfg.stat().st_mode & 0o777) == 0o644

    assert pull.install("v1", cfg) is False  # 无变化：不写盘、不备份
    assert list(tmp_path.glob("*.bak.*-pull")) == []

    assert pull.install("v2", cfg) is True   # 有变化：旧版本进备份
    backups = list(tmp_path.glob("config.yaml.bak.*-pull"))
    assert len(backups) == 1 and backups[0].read_text() == "v1"
    assert cfg.read_text() == "v2"


def test_default_restart_cmd_detection():
    assert pull.default_restart_cmd(exists=lambda p: "LaunchDaemons" in p) == [
        "sudo", "-n", "brew", "services", "restart", "mihomo"]
    assert pull.default_restart_cmd(exists=lambda p: "systemd" in p) == [
        "sudo", "-n", "systemctl", "restart", "mihomo"]
    assert pull.default_restart_cmd(exists=lambda p: False) is None
