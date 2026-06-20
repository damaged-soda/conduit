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


def test_refresh_fetches_url_and_imports():
    app = create_app(":memory:", fetcher=lambda url: FIXTURE)  # 注入假 fetcher，不碰真网络
    c = TestClient(app)
    c.post("/api/subscriptions", json={"id": "v", "url": "https://example/sub"})
    r = c.post("/api/subscriptions/v/refresh")
    assert r.status_code == 200 and r.json()["imported"] == 2
    sub = c.get("/api/subscriptions").json()[0]
    assert sub["has_url"] == 1 and "url" not in sub  # url 是 secret，列表不泄露


def test_url_scheme_validated():
    c = _client()
    assert c.post("/api/subscriptions", json={"id": "v", "url": "file:///etc/passwd"}).status_code == 400


def test_refresh_without_url_400():
    c = _client()
    c.post("/api/subscriptions", json={"id": "v"})  # 没 url
    assert c.post("/api/subscriptions/v/refresh").status_code == 400


def test_refresh_fetch_failure_502():
    def boom(url):
        raise RuntimeError("network down")

    c = TestClient(create_app(":memory:", fetcher=boom))
    c.post("/api/subscriptions", json={"id": "v", "url": "https://x/sub"})
    assert c.post("/api/subscriptions/v/refresh").status_code == 502  # 抓取失败 → 502，不回显 url


def test_bad_proxy_import_sanitized_400():
    c = _client()
    c.post("/api/subscriptions", json={"id": "v"})
    bad = "proxies:\n  - {name: x, type: ss, server: s.com, port: NOTAPORT, password: p}\n"
    r = c.post("/api/subscriptions/v/import", json={"raw": bad})
    assert r.status_code == 400 and "NOTAPORT" not in r.json()["detail"]  # 不回显订阅内容


def test_migration_adds_url_column_to_old_db(tmp_path):
    import sqlite3

    from service.db import Store

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)  # 旧 schema：subscriptions 无 url 列
    conn.execute("CREATE TABLE subscriptions (id TEXT PRIMARY KEY, type TEXT, note TEXT, created_at TEXT)")
    conn.commit()
    conn.close()
    s = Store(str(p))  # 应迁移补上 url 列
    s.add_subscription("v", url="https://x/sub")
    assert s.get_subscription("v")["url"] == "https://x/sub"


def test_index_page():
    assert _client().get("/").status_code == 200
