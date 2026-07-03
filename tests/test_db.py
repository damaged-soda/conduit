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
