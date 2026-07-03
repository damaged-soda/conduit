from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from conduit.identity import access_id  # noqa: E402
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
