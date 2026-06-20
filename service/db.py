"""conduit-service 的存储层（SQLite）。core 仍是纯函数，状态都在这。

三张表：
- subscriptions：**id 内部不透明 key（自动生成，稳定，节点按它归属）**；name 是可随意改的显示名；
  type / note / url（含 token = secret，API 不返回）。
- imports：每次导入的原始内容（含凭据）+ 节点数 + 时间。
- nodes：节点池，按 access_id 去重，sub_id 指向 subscriptions.id（稳定，改名不影响）。

⚠️ nodes/imports/subscriptions.url 含明文凭据 → 这个 DB 是 secret 载体：访问控制、别对公网暴露、别进 git。
TODO：tags / health / traffic；连接并发；凭据加密；定时刷新。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading

from conduit.models import Node

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id         TEXT PRIMARY KEY,                       -- 内部不透明 key（节点按它归属，稳定）
  name       TEXT NOT NULL DEFAULT '',               -- 显示名（用户可随意改）
  type       TEXT NOT NULL DEFAULT 'clash',
  note       TEXT NOT NULL DEFAULT '',
  url        TEXT,                                   -- 基于链接拉取的 URL（含 token = secret，API 不返回）
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS imports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sub_id     TEXT NOT NULL REFERENCES subscriptions(id),
  raw        TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS nodes (
  access_id  TEXT PRIMARY KEY,
  sub_id     TEXT,
  type       TEXT NOT NULL,
  server     TEXT NOT NULL,
  port       INTEGER NOT NULL,
  raw_name   TEXT NOT NULL DEFAULT '',
  params     TEXT NOT NULL DEFAULT '{}',
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_locked()

    def _migrate_locked(self) -> None:
        """轻量迁移：给旧 DB（骨架早期版本）补上后加的列。`CREATE TABLE IF NOT EXISTS` 不改已有表。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
        if "url" not in cols:
            self._conn.execute("ALTER TABLE subscriptions ADD COLUMN url TEXT")
        if "name" not in cols:
            self._conn.execute("ALTER TABLE subscriptions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            self._conn.execute("UPDATE subscriptions SET name = id WHERE name = ''")  # 旧行回填 name=id
        self._conn.commit()

    # ---- subscriptions ----

    def add_subscription(self, name: str, type: str = "clash", note: str = "", url: str | None = None) -> str:
        """新建订阅，返回自动生成的内部 id。"""
        sub_id = secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscriptions(id, name, type, note, url) VALUES (?, ?, ?, ?, ?)",
                (sub_id, name, type, note, url),
            )
            self._conn.commit()
        return sub_id

    def get_subscription(self, sub_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        return dict(row) if row else None

    def list_subscriptions(self) -> list[dict]:
        """列订阅（**不返回 url**，含 token = secret；只给 has_url 标志）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.name, s.type, s.created_at, "
                "(s.url IS NOT NULL AND s.url != '') AS has_url, "
                "(SELECT COUNT(*) FROM nodes n WHERE n.sub_id = s.id) AS node_count "
                "FROM subscriptions s ORDER BY s.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_subscription(self, sub_id: str, name: str | None = None, url: str | None = None) -> None:
        """改名 / 改 URL（只更新非 None 字段；改名不动节点）。"""
        with self._lock:
            if name is not None:
                self._conn.execute("UPDATE subscriptions SET name = ? WHERE id = ?", (name, sub_id))
            if url is not None:
                self._conn.execute("UPDATE subscriptions SET url = ? WHERE id = ?", (url, sub_id))
            self._conn.commit()

    def delete_subscription(self, sub_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE sub_id = ?", (sub_id,))
            self._conn.execute("DELETE FROM imports WHERE sub_id = ?", (sub_id,))
            self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            self._conn.commit()

    # ---- nodes ----

    def import_nodes(self, sub_id: str, raw: str, nodes: list[Node]) -> int:
        """记录一次导入，并按 access_id upsert 节点。返回本次节点数。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO imports(sub_id, raw, node_count) VALUES (?, ?, ?)", (sub_id, raw, len(nodes))
                )
                for n in nodes:
                    ep = n.access_id.endpoint
                    self._conn.execute(
                        "INSERT INTO nodes(access_id, sub_id, type, server, port, raw_name, params) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(access_id) DO UPDATE SET sub_id=excluded.sub_id, raw_name=excluded.raw_name, "
                        "params=excluded.params, last_seen=datetime('now')",
                        (n.access_id.value, sub_id, ep.type, ep.server, ep.port, n.raw_name,
                         json.dumps(n.params, ensure_ascii=False)),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()  # 整批导入原子化：中途失败不留半成品
                raise
            return len(nodes)

    def list_nodes(self, sub_id: str | None = None) -> list[dict]:
        """列节点（**不含 params**，避免泄露凭据）；给 sub_id 则只列该订阅的。"""
        q = ("SELECT access_id, sub_id, type, server, port, raw_name, first_seen, last_seen FROM nodes")
        args: tuple = ()
        if sub_id is not None:
            q += " WHERE sub_id = ?"
            args = (sub_id,)
        q += " ORDER BY type, server, port"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]
