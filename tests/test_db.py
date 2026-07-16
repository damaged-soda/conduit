from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import sys

import yaml

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.identity import access_id  # noqa: E402
from conduit.ingest import normalize  # noqa: E402
from service.db import Store  # noqa: E402


def _legacy_access_id(proxy: dict) -> str:
    t = str(proxy.get("type", "")).strip().lower()
    s = str(proxy.get("server", "")).strip().lower()
    p = int(proxy["port"])
    rest = {k: v for k, v in proxy.items() if k not in ("name", "type", "server", "port")}
    canonical = json.dumps(
        {"type": t, "server": s, "port": p, **rest},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_store_migrates_access_ids_and_tags_when_udp_becomes_identity_ignored(tmp_path):
    path = tmp_path / "service.db"
    bootstrap = Store(str(path))
    bootstrap._conn.close()

    proxy = {
        "name": "HK",
        "type": "ss",
        "server": "a.example.com",
        "port": 8388,
        "cipher": "2022-blake3-aes-256-gcm",
        "password": "p",
        "udp": True,
    }
    old_id = _legacy_access_id(proxy)
    new_id = access_id(proxy).value
    assert old_id != new_id

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO nodes(access_id, sub_id, type, server, port, raw_name, params) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            old_id, "sub-a", "ss", "a.example.com", 8388, "HK",
            json.dumps({"cipher": proxy["cipher"], "password": "p", "udp": True}),
        ),
    )
    conn.execute(
        "INSERT INTO node_tags(access_id, region, quarantined) VALUES (?, ?, ?)",
        (old_id, "HK", 1),
    )
    conn.commit()
    conn.close()

    migrated = Store(str(path))

    rows = migrated.list_nodes()
    assert [r["access_id"] for r in rows] == [new_id]
    assert migrated.nodes_for_render()[0].params["udp"] is True
    assert migrated.get_node_tags() == {new_id: {"region": "HK", "quarantined": True}}


def test_store_merges_legacy_access_id_collisions(tmp_path):
    path = tmp_path / "service.db"
    bootstrap = Store(str(path))
    bootstrap._conn.close()

    base_proxy = {
        "name": "old",
        "type": "ss",
        "server": "a.example.com",
        "port": 8388,
        "cipher": "2022-blake3-aes-256-gcm",
        "password": "p",
    }
    udp_proxy = {**base_proxy, "name": "new", "udp": True}
    stable_id = access_id(base_proxy).value
    legacy_udp_id = _legacy_access_id(udp_proxy)
    assert stable_id != legacy_udp_id

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO nodes(access_id, sub_id, type, server, port, raw_name, params, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stable_id, "sub-a", "ss", "a.example.com", 8388, "old",
            json.dumps({"cipher": base_proxy["cipher"], "password": "p"}),
            "2020-01-01 00:00:00", "2020-01-02 00:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO nodes(access_id, sub_id, type, server, port, raw_name, params, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            legacy_udp_id, "sub-a", "ss", "a.example.com", 8388, "new",
            json.dumps({"cipher": base_proxy["cipher"], "password": "p", "udp": True}),
            "2019-01-01 00:00:00", "2022-01-01 00:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO node_tags(access_id, region, quarantined) VALUES (?, ?, ?)",
        (stable_id, "SG", 0),
    )
    conn.execute(
        "INSERT INTO node_tags(access_id, region, quarantined) VALUES (?, ?, ?)",
        (legacy_udp_id, "HK", 1),
    )
    conn.commit()
    conn.close()

    migrated = Store(str(path))

    rows = migrated.list_nodes()
    assert len(rows) == 1
    assert rows[0]["access_id"] == stable_id
    assert rows[0]["raw_name"] == "new"
    assert rows[0]["first_seen"] == "2019-01-01 00:00:00"
    assert rows[0]["last_seen"] == "2022-01-01 00:00:00"
    assert migrated.nodes_for_render()[0].params["udp"] is True
    assert migrated.get_node_tags() == {stable_id: {"region": "SG", "quarantined": True}}


def test_store_migrates_subscription_and_node_order_from_latest_import(tmp_path):
    path = tmp_path / "legacy-order.db"
    first = {
        "name": "SG first", "type": "ss", "server": "first.example", "port": 1,
        "password": "p", "udp": True,
    }
    second = {
        "name": "SG second", "type": "ss", "server": "second.example", "port": 2,
        "password": "p", "udp": True,
    }
    raw = yaml.safe_dump({"proxies": [first, second]}, sort_keys=False)

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE subscriptions (
          id TEXT PRIMARY KEY, name TEXT, type TEXT, note TEXT, source_type TEXT, url TEXT,
          created_at TEXT
        );
        CREATE TABLE imports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT, raw TEXT, source_type TEXT,
          node_count INTEGER, at TEXT
        );
        CREATE TABLE nodes (
          access_id TEXT PRIMARY KEY, sub_id TEXT, type TEXT, server TEXT, port INTEGER,
          raw_name TEXT, params TEXT, first_seen TEXT, last_seen TEXT
        );
        """
    )
    # 故意让插入顺序与创建时间、上游节点顺序相反，验证迁移使用正确事实源。
    conn.execute(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("late", "Late", "auto", "", "file", None, "2025-02-01 00:00:00"),
    )
    conn.execute(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("early", "Early", "auto", "", "file", None, "2025-01-01 00:00:00"),
    )
    conn.execute(
        "INSERT INTO imports(sub_id, raw, source_type, node_count, at) VALUES (?, ?, ?, ?, ?)",
        ("early", raw, "file", 2, "2025-03-01 00:00:00"),
    )
    for proxy in (second, first):
        aid = access_id(proxy).value
        params = {k: v for k, v in proxy.items() if k not in {"name", "type", "server", "port"}}
        conn.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                aid, "early", proxy["type"], proxy["server"], proxy["port"], proxy["name"],
                json.dumps(params), "2025-01-01 00:00:00", "2025-03-01 00:00:00",
            ),
        )
    conn.commit()
    conn.close()

    migrated = Store(str(path))
    assert [sub["id"] for sub in migrated.list_subscriptions()] == ["early", "late"]
    assert [node["raw_name"] for node in migrated.list_nodes("early")] == ["SG first", "SG second"]
    assert [node["position"] for node in migrated.list_nodes("early")] == [0, 1]


def test_store_persists_current_source_proxy_nameservers_without_api_fields():
    store = Store(":memory:")
    sub_id = store.add_subscription("Source DNS")
    proxy = {
        "name": "A", "type": "ss", "server": "a.example", "port": 1,
        "password": "p", "udp": True,
    }
    nodes = normalize(yaml.safe_dump({"proxies": [proxy]}), "auto", sub_id)
    store.import_nodes(
        sub_id,
        yaml.safe_dump({"proxies": [proxy]}),
        nodes,
        proxy_server_nameservers=["https://source.example/dns-query"],
    )
    assert store.source_proxy_nameservers() == {
        sub_id: ["https://source.example/dns-query"]
    }
    snapshot_nodes, snapshot_dns = store.render_snapshot()
    assert [node.raw_name for node in snapshot_nodes] == ["A"]
    assert snapshot_dns == {sub_id: ["https://source.example/dns-query"]}
    assert "proxy_server_nameservers" not in store.list_subscriptions()[0]

    store.import_nodes(sub_id, yaml.safe_dump({"proxies": [proxy]}), nodes)
    assert store.source_proxy_nameservers() == {}


def test_store_migration_backfills_source_proxy_nameservers_from_latest_import(tmp_path):
    path = tmp_path / "source-dns.db"
    raw = yaml.safe_dump({
        "dns": {"proxy-server-nameserver": ["https://source.example/dns-query"]},
        "proxies": [
            {"name": "A", "type": "ss", "server": "a.example", "port": 1,
             "password": "p", "udp": True},
        ],
    })
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE subscriptions (
          id TEXT PRIMARY KEY, name TEXT, position INTEGER, type TEXT, note TEXT,
          source_type TEXT, url TEXT, created_at TEXT
        );
        CREATE TABLE imports (
          id INTEGER PRIMARY KEY AUTOINCREMENT, sub_id TEXT, raw TEXT,
          source_type TEXT, node_count INTEGER, at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("source", "Source", 0, "auto", "", "file", None, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO imports(sub_id, raw, source_type, node_count, at) VALUES (?, ?, ?, ?, ?)",
        ("source", raw, "file", 1, "2026-01-01"),
    )
    conn.commit()
    conn.close()

    assert Store(str(path)).source_proxy_nameservers() == {
        "source": ["https://source.example/dns-query"]
    }


def test_store_retries_source_dns_backfill_when_column_already_exists(tmp_path):
    path = tmp_path / "interrupted-source-dns.db"
    raw = yaml.safe_dump({
        "dns": {"proxy-server-nameserver": ["https://source.example/dns-query"]},
        "proxies": [
            {"name": "A", "type": "ss", "server": "a.example", "port": 1,
             "password": "p", "udp": True},
        ],
    })
    store = Store(str(path))
    sub_id = store.add_subscription("Source DNS")
    store._conn.execute(
        "INSERT INTO imports(sub_id, raw, source_type, node_count) VALUES (?, ?, ?, ?)",
        (sub_id, raw, "file", 1),
    )
    store._conn.commit()
    store._conn.close()

    assert Store(str(path)).source_proxy_nameservers() == {
        sub_id: ["https://source.example/dns-query"]
    }
