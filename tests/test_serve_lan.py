"""Verify optional LAN mapping and removal through the production entry point."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "conduit_serve", Path(__file__).resolve().parents[1] / "spine" / "serve.py")
serve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serve)


def test_lan_enable_and_remove(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(serve.os, "execvp", lambda _, args: calls.append(args))
    # Keep environment changes isolated from other tests.
    for key in ("CONDUIT_BIND", "CONDUIT_PORT", "CONDUIT_IMAGE_TAG"):
        monkeypatch.setenv(key, "")
    argv = ["serve.py", "--root", str(tmp_path), "--nats", "nats://unused",
            "--image-tag", "v0.1.18"]
    monkeypatch.setattr(sys, "argv", argv + ["--lan-bind", "192.168.1.2"])
    serve.main()
    override = tmp_path / "compose-lan.json"
    assert json.loads(override.read_text())["services"]["conduit"]["ports"] == [
        "192.168.1.2:8000:8000"]
    assert calls[-1].count("--file") == 2
    assert serve.os.environ["CONDUIT_BIND"] == "127.0.0.1"
    assert json.loads((tmp_path / "endpoints.json").read_text())["lan"] == (
        "http://192.168.1.2:8000")
    monkeypatch.setattr(sys, "argv", argv)
    serve.main()
    assert calls[-1].count("--file") == 1
    assert str(override) not in calls[-1]
    assert "lan" not in json.loads((tmp_path / "endpoints.json").read_text())


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "8.8.8.8",
                                    "100.64.0.1", "::", "localhost"])
def test_lan_rejects_non_rfc1918(address):
    with pytest.raises(serve.argparse.ArgumentTypeError):
        serve.lan_address(address)
