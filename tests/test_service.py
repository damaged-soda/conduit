"""conduit-service 骨架测试：建订阅 → 文件导入 → 列节点（经 FastAPI TestClient，内存 SQLite）。"""

from __future__ import annotations

import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # repo root：conduit + service 包

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from service.app import create_app  # noqa: E402

FIXTURE = (HERE / "fixtures" / "sub.clash.yaml").read_text()


def _client() -> TestClient:
    return TestClient(create_app(":memory:"))


def test_add_import_list_flow():
    c = _client()
    assert c.post("/api/subscriptions", json={"id": "vendor-a"}).status_code == 200
    r = c.post("/api/subscriptions/vendor-a/import", json={"raw": FIXTURE})
    assert r.status_code == 200 and r.json()["imported"] == 2
    nodes = c.get("/api/nodes").json()
    assert len(nodes) == 2
    assert "params" not in nodes[0]  # API 不泄露凭据
    assert c.get("/api/subscriptions").json()[0]["node_count"] == 2


def test_import_idempotent_by_access_id():
    c = _client()
    c.post("/api/subscriptions", json={"id": "v"})
    c.post("/api/subscriptions/v/import", json={"raw": FIXTURE})
    c.post("/api/subscriptions/v/import", json={"raw": FIXTURE})  # 再导一次
    assert len(c.get("/api/nodes").json()) == 2  # 按 access_id 去重，不翻倍


def test_import_unknown_sub_404():
    c = _client()
    assert c.post("/api/subscriptions/nope/import", json={"raw": "proxies: []"}).status_code == 404


def test_malformed_yaml_import_returns_400():
    c = _client()
    c.post("/api/subscriptions", json={"id": "v"})
    r = c.post("/api/subscriptions/v/import", json={"raw": "proxies: 'unterminated"})
    assert r.status_code == 400  # 坏 YAML 是 sanitized 400，不是 500


def test_duplicate_sub_409():
    c = _client()
    c.post("/api/subscriptions", json={"id": "v"})
    assert c.post("/api/subscriptions", json={"id": "v"}).status_code == 409


def test_index_page():
    assert _client().get("/").status_code == 200
