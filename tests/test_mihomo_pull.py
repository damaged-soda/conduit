"""conduit-mihomo-pull 测试：merge / 安全不变量 / 原子安装 / 激活编排的失败路径。

不碰宿主机 mihomo（TESTING.md：宿主机神圣）——真实 fetch / validate / reload / 重启
全部用 monkeypatch 替身，只断言编排语义：fail-closed、回滚、权限不放宽、URL 不外泄。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import urllib.error

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
proxies:
  - {name: n1, type: socks5, server: a.example.com, port: 1080}
proxy-groups:
  - {name: PROXY, type: select, proxies: [n1]}
rules: [MATCH,PROXY]
"""
SUB_WITH_CONTROLLER = SUB + "external-controller: 127.0.0.1:9090\n"


# ---------- deep_merge ----------

def test_deep_merge_recurses_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    assert pull.deep_merge(base, {"a": {"y": 3, "z": 4}}) == {"a": {"x": 1, "y": 3, "z": 4}, "b": 1}
    assert base == {"a": {"x": 1, "y": 2}, "b": 1}  # 不改原对象


def test_deep_merge_replaces_lists_and_scalars():
    assert pull.deep_merge({"l": [1, 2]}, {"l": [3]}) == {"l": [3]}
    assert pull.deep_merge({"s": "a"}, {"s": "b"}) == {"s": "b"}
    assert pull.deep_merge({"k": {"d": 1}}, {"k": "flat"}) == {"k": "flat"}


# ---------- build_config / 安全不变量 ----------

def test_build_config_applies_overlay():
    out = pull.build_config(SUB, {"external-controller": "127.0.0.1:9090",
                                  "profile": {"store-selected": True}})
    assert out["external-controller"] == "127.0.0.1:9090"
    assert out["profile"]["store-selected"] is True
    assert out["proxies"][0]["name"] == "n1"  # 订阅本体不动


def test_build_config_fail_closed_on_garbage():
    for bad in ("", "not: a config", "proxies: []", "- just\n- a\n- list\n"):
        with pytest.raises(pull.PullError):
            pull.build_config(bad, None)


def test_controller_safety_matches_render_invariant():
    pull.check_controller_safety({"external-controller": "127.0.0.1:9090"})          # loopback 放行
    pull.check_controller_safety({"external-controller": "0.0.0.0:9090", "secret": "s"})
    with pytest.raises(pull.PullError):  # 非 loopback 且无 secret → 拒绝（同 render.py）
        pull.check_controller_safety({"external-controller": "0.0.0.0:9090"})


# ---------- fetch：URL 是 secret，不进错误信息 ----------

def test_fetch_error_never_contains_url(monkeypatch):
    secret = "https://rig.example.com/sub/clash?token=TOPSECRET&full=1"

    def boom_http(req, timeout):
        raise urllib.error.HTTPError(secret, 500, "err", {}, None)

    def boom_value(req, timeout):
        raise ValueError(f"unknown url type: {secret}")

    monkeypatch.setattr(pull.urllib.request, "urlopen", boom_http)
    with pytest.raises(pull.PullError) as e1:
        pull.fetch(secret)
    monkeypatch.setattr(pull.urllib.request, "urlopen", boom_value)
    with pytest.raises(pull.PullError) as e2:
        pull.fetch(secret)
    assert "TOPSECRET" not in str(e1.value) and "TOPSECRET" not in str(e2.value)


def test_no_url_flag():  # token 不该进进程参数：--url 不存在
    with pytest.raises(SystemExit):
        pull.main(["--url", "https://x?token=t"])


# ---------- install：原子替换 + 权限不放宽 ----------

def test_install_atomic_and_idempotent(tmp_path):
    cfg = tmp_path / "config.yaml"
    changed, backup = pull.install("v1", cfg)
    assert changed and backup is None
    assert (cfg.stat().st_mode & 0o777) == 0o600  # 新文件默认 0600（含节点凭据）

    changed, backup = pull.install("v1", cfg)
    assert not changed and backup is None  # 无变化：不写盘、不备份
    assert list(tmp_path.glob("*.bak.*-pull")) == []

    changed, backup = pull.install("v2", cfg)
    assert changed and backup.read_text() == "v1"
    assert cfg.read_text() == "v2"


def test_install_preserves_existing_mode(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("old")
    cfg.chmod(0o600)
    pull.install("new", cfg)
    assert (cfg.stat().st_mode & 0o777) == 0o600  # 不得放宽已有权限


# ---------- main 编排：fail-closed / 回滚 ----------

def _main_args(tmp_path):
    return ["--home", str(tmp_path), "--config", str(tmp_path / "config.yaml"),
            "--mihomo-bin", "/bin/true"]


def _stub_common(monkeypatch, sub=SUB):
    monkeypatch.setenv("CONDUIT_SUB_URL", "https://x/sub?token=t")
    monkeypatch.setattr(pull, "fetch", lambda url: sub)
    monkeypatch.setattr(pull, "validate", lambda *a: None)


def test_main_validate_failure_keeps_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("old")
    _stub_common(monkeypatch)

    def bad_validate(*a):
        raise pull.PullError("mihomo -t 校验失败")

    monkeypatch.setattr(pull, "validate", bad_validate)
    with pytest.raises(pull.PullError):
        pull.main(_main_args(tmp_path))
    assert cfg.read_text() == "old"
    assert list(tmp_path.glob("*.bak.*-pull")) == []


def test_main_refuses_install_without_activation(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("old")
    _stub_common(monkeypatch)  # SUB 无 controller
    monkeypatch.setattr(pull, "default_restart_cmd", lambda: None)
    with pytest.raises(pull.PullError):
        pull.main(_main_args(tmp_path))
    assert cfg.read_text() == "old"  # 未显式 --no-restart 且无激活方式 → 不安装


def test_main_restart_failure_rolls_back(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("old")
    _stub_common(monkeypatch)
    monkeypatch.setattr(pull, "default_restart_cmd", lambda: ["fake-restart"])

    class R:
        returncode, stdout, stderr = 1, "", "boom"

    monkeypatch.setattr(pull.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(pull.PullError):
        pull.main(_main_args(tmp_path))
    assert cfg.read_text() == "old"  # 已回滚
    assert len(list(tmp_path.glob("*.bak.*-pull"))) == 1  # 备份保留供排查


def test_main_api_reload_avoids_restart(tmp_path, monkeypatch):
    _stub_common(monkeypatch, SUB_WITH_CONTROLLER)
    monkeypatch.setattr(pull, "api_reload", lambda cfg, path: True)

    def no_run(*a, **k):
        raise AssertionError("reload 成功就不该走到重启")

    monkeypatch.setattr(pull.subprocess, "run", no_run)
    assert pull.main(_main_args(tmp_path)) == 0
    out = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert out["external-controller"] == "127.0.0.1:9090"


def test_main_restart_success(tmp_path, monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr(pull, "default_restart_cmd", lambda: ["fake-restart"])

    class R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(pull.subprocess, "run", lambda *a, **k: R())
    assert pull.main(_main_args(tmp_path)) == 0
    assert "n1" in (tmp_path / "config.yaml").read_text()


def test_default_restart_cmd_detection():
    assert pull.default_restart_cmd(exists=lambda p: "LaunchDaemons" in p) == [
        "sudo", "-n", "brew", "services", "restart", "mihomo"]
    assert pull.default_restart_cmd(exists=lambda p: "systemd" in p) == [
        "sudo", "-n", "systemctl", "restart", "mihomo"]
    assert pull.default_restart_cmd(exists=lambda p: False) is None
