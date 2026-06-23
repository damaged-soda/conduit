"""conduit-service 的存储层（SQLite）。core 仍是纯函数，状态都在这。

三张表：
- subscriptions：**id 内部不透明 key（自动生成，稳定，节点按它归属）**；name 是可随意改的显示名；
  type / note / source_type(file|url) / url（含 token = secret，API 不返回；file 来源无 url）。
- imports：每次导入的原始内容（含凭据）+ 来源类型 + 节点数 + 时间。
- nodes：节点池，按 access_id 去重，sub_id 指向 subscriptions.id（稳定，改名不影响）。

⚠️ nodes/imports/subscriptions.url 含明文凭据 → 这个 DB 是 secret 载体：访问控制、别对公网暴露、别进 git。
TODO：health / traffic；连接并发；凭据加密；定时刷新。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading

from conduit.models import AccessId, EndpointId, Node

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id         TEXT PRIMARY KEY,                       -- 内部不透明 key（节点按它归属，稳定）
  name       TEXT NOT NULL DEFAULT '',               -- 显示名（用户可随意改）
  type       TEXT NOT NULL DEFAULT 'auto',
  note       TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'file' CHECK (source_type IN ('file', 'url')),
  url        TEXT,                                   -- 基于链接拉取的 URL（含 token = secret，API 不返回）
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (
    (source_type = 'file' AND (url IS NULL OR url = '')) OR
    (source_type = 'url' AND url IS NOT NULL AND url != '')
  )
);
CREATE TABLE IF NOT EXISTS imports (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  sub_id     TEXT NOT NULL REFERENCES subscriptions(id),
  raw        TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'file' CHECK (source_type IN ('file', 'url')),
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
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_tags (
  access_id   TEXT PRIMARY KEY,
  region      TEXT,
  quarantined INTEGER NOT NULL DEFAULT 0
);
"""

_UNSET = object()  # set_node_tag 的「未提供」哨兵，支持部分更新
_SOURCE_TYPES = {"file", "url"}


def _source_type_for_url(url: str | None) -> str:
    return "url" if url else "file"


def _check_source_type(source_type: str) -> str:
    if source_type not in _SOURCE_TYPES:
        raise ValueError(f"unknown source_type: {source_type}")
    return source_type


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
        if "source_type" not in cols:
            self._conn.execute("ALTER TABLE subscriptions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'")
        self._conn.execute(
            "UPDATE subscriptions SET source_type = "
            "CASE WHEN url IS NOT NULL AND url != '' THEN 'url' ELSE 'file' END "
            "WHERE source_type NOT IN ('file', 'url') "
            "OR source_type != CASE WHEN url IS NOT NULL AND url != '' THEN 'url' ELSE 'file' END"
        )
        import_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(imports)").fetchall()}
        if "source_type" not in import_cols:
            self._conn.execute("ALTER TABLE imports ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'")
        self._conn.commit()

    # ---- subscriptions ----

    def add_subscription(self, name: str, type: str = "auto", note: str = "", url: str | None = None) -> str:
        """新建订阅，返回自动生成的内部 id。"""
        sub_id = secrets.token_hex(8)
        clean_url = (url or "").strip() or None
        source_type = _source_type_for_url(clean_url)
        with self._lock:
            self._conn.execute(
                "INSERT INTO subscriptions(id, name, type, note, source_type, url) VALUES (?, ?, ?, ?, ?, ?)",
                (sub_id, name, type, note, source_type, clean_url),
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
                "s.source_type, "
                "(s.url IS NOT NULL AND s.url != '') AS has_url, "
                "(SELECT COUNT(*) FROM nodes n WHERE n.sub_id = s.id) AS node_count "
                "FROM subscriptions s ORDER BY s.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_subscription(self, sub_id: str, name=_UNSET, url=_UNSET) -> None:
        """改名 / 改 URL（只更新提供的字段；URL 可清空为 NULL；改名不动节点）。

        URL 存在即链接来源，URL 清空即文件来源；同一订阅当前只允许一种来源。
        """
        with self._lock:
            if name is not _UNSET:
                self._conn.execute("UPDATE subscriptions SET name = ? WHERE id = ?", (name, sub_id))
            if url is not _UNSET:
                clean_url = (url or "").strip() or None
                self._conn.execute(
                    "UPDATE subscriptions SET source_type = ?, url = ? WHERE id = ?",
                    (_source_type_for_url(clean_url), clean_url, sub_id),
                )
            self._conn.commit()

    def delete_subscription(self, sub_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE sub_id = ?", (sub_id,))
            self._conn.execute("DELETE FROM imports WHERE sub_id = ?", (sub_id,))
            self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            self._conn.commit()

    # ---- nodes ----

    def import_nodes(self, sub_id: str, raw: str, nodes: list[Node], source_type: str = "file") -> int:
        """记录一次导入，并按 access_id upsert 节点。返回本次节点数。

        节点是**全局池**（access_id 唯一）；同一 access_id 跨订阅出现时，`sub_id` 归**最后导入的那条**
        （「全局池，后导入者赢」）。真·多订阅归属 = later（需要成员表）。
        """
        source_type = _check_source_type(source_type)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO imports(sub_id, raw, source_type, node_count) VALUES (?, ?, ?, ?)",
                    (sub_id, raw, source_type, len(nodes)),
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

    # ---- subscription serving ----

    def get_sub_token(self) -> str:
        """订阅 URL 的 token（首次自动生成并持久化）。"""
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'sub_token'").fetchone()
            if row:
                return row["value"]
            token = secrets.token_urlsafe(16)
            self._conn.execute("INSERT INTO meta(key, value) VALUES ('sub_token', ?)", (token,))
            self._conn.commit()
            return token

    def get_policy(self) -> dict | None:
        """页面编辑的规则策略（DB 为准）；无则 None → 服务回落到仓库 DEFAULT_POLICY。"""
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'policy'").fetchone()
        return json.loads(row["value"]) if row else None

    def set_policy(self, policy: dict | None) -> None:
        """存策略；policy=None 删除（恢复仓库默认）。"""
        with self._lock:
            if policy is None:
                self._conn.execute("DELETE FROM meta WHERE key = 'policy'")
            else:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('policy', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(policy),),
                )
            self._conn.commit()

    def nodes_for_render(self, sub_id: str | None = None) -> list[Node]:
        """取节点并重建成 Node（**含 params 凭据**，仅服务内部渲染订阅用，绝不经 API 暴露）。"""
        q = "SELECT access_id, sub_id, type, server, port, raw_name, params FROM nodes"
        args: tuple = ()
        if sub_id is not None:
            q += " WHERE sub_id = ?"
            args = (sub_id,)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out: list[Node] = []
        for r in rows:
            ep = EndpointId(type=r["type"], server=r["server"], port=r["port"])
            out.append(
                Node(
                    access_id=AccessId(value=r["access_id"], endpoint=ep),
                    raw_name=r["raw_name"],
                    params=json.loads(r["params"]),
                    source=r["sub_id"] or "",
                )
            )
        return out

    # ---- 标签（按 access_id，跟着节点走、不随订阅删除）----

    def get_node_tags(self) -> dict[str, dict]:
        """{access_id: {"region": override|None, "quarantined": bool}}，传给 render 分组。"""
        with self._lock:
            rows = self._conn.execute("SELECT access_id, region, quarantined FROM node_tags").fetchall()
        return {r["access_id"]: {"region": r["region"], "quarantined": bool(r["quarantined"])} for r in rows}

    def set_node_tag(self, access_id: str, region=_UNSET, quarantined=_UNSET) -> None:
        """部分更新某节点的标签（region 覆盖 / 隔离）。未传的字段保持不变。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT region, quarantined FROM node_tags WHERE access_id = ?", (access_id,)
            ).fetchone()
            cur_region = row["region"] if row else None
            cur_q = bool(row["quarantined"]) if row else False
            new_region = cur_region if region is _UNSET else ((region or "").strip() or None)
            new_q = cur_q if quarantined is _UNSET else bool(quarantined)
            self._conn.execute(
                "INSERT INTO node_tags(access_id, region, quarantined) VALUES (?, ?, ?) "
                "ON CONFLICT(access_id) DO UPDATE SET region = excluded.region, quarantined = excluded.quarantined",
                (access_id, new_region, 1 if new_q else 0),
            )
            self._conn.commit()
