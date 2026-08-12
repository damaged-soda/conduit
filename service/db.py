"""conduit-service 的存储层（SQLite）。core 仍是纯函数，状态都在这。

三张表：
- subscriptions：**id 内部不透明 key（自动生成，稳定，节点按它归属）**；name 是可随意改的显示名；
  position 是导出优先级（越小越优先）；type / note / source_type(file|url) / url（含 token = secret，
  API 不返回；file 来源无 url）。
- imports：每次导入的原始内容（含凭据）+ 来源类型 + 节点数 + 时间。
- nodes：每条订阅的当前节点快照；position 保留上游原序。相同 access_id 可在不同订阅分别存在。

⚠️ nodes/imports/subscriptions.url 含明文凭据 → 这个 DB 是 secret 载体：访问控制、别对公网暴露、别进 git。
TODO：health / traffic；连接并发；凭据加密；定时刷新。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading

import yaml

from conduit.identity import access_id as compute_access_id
from conduit.ingest import extract_proxy_server_nameservers, normalize
from conduit.models import AccessId, EndpointId, Node

_NODES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  access_id  TEXT NOT NULL,
  sub_id     TEXT,
  position   INTEGER NOT NULL DEFAULT 0,
  type       TEXT NOT NULL,
  server     TEXT NOT NULL,
  port       INTEGER NOT NULL,
  raw_name   TEXT NOT NULL DEFAULT '',
  params     TEXT NOT NULL DEFAULT '{}',
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id         TEXT PRIMARY KEY,                       -- 内部不透明 key（节点按它归属，稳定）
  name       TEXT NOT NULL DEFAULT '',               -- 显示名（用户可随意改）
  position   INTEGER NOT NULL DEFAULT 0,              -- 越小导出优先级越高
  type       TEXT NOT NULL DEFAULT 'auto',
  note       TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'file' CHECK (source_type IN ('file', 'url')),
  url        TEXT,                                   -- 基于链接拉取的 URL（含 token = secret，API 不返回）
  proxy_server_nameservers TEXT NOT NULL DEFAULT '[]', -- 当前成功快照的来源级节点 DNS（secret，不经 API）
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
""" + _NODES_TABLE_SQL + """
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
        if "position" not in cols:
            self._conn.execute("ALTER TABLE subscriptions ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
            rows = self._conn.execute(
                "SELECT id FROM subscriptions ORDER BY created_at, rowid"
            ).fetchall()
            for position, row in enumerate(rows):
                self._conn.execute(
                    "UPDATE subscriptions SET position = ? WHERE id = ?", (position, row["id"])
                )
        if "proxy_server_nameservers" not in cols:
            self._conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN proxy_server_nameservers "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        # 元数据属于当前节点快照；从最近一次成功 import 回填，升级后无需重新导入订阅。
        # 空值每次启动都可重试：即使 ALTER 已提交后进程中断，下次也不会永久跳过回填。
        rows = self._conn.execute(
            "SELECT id, type FROM subscriptions WHERE proxy_server_nameservers = '[]'"
        ).fetchall()
        for sub in rows:
            latest = self._conn.execute(
                "SELECT raw FROM imports WHERE sub_id = ? ORDER BY id DESC LIMIT 1",
                (sub["id"],),
            ).fetchone()
            if not latest:
                continue
            try:
                nameservers = extract_proxy_server_nameservers(latest["raw"], sub["type"])
            except (TypeError, ValueError, yaml.YAMLError):
                continue  # 历史坏元数据不能阻止服务启动；节点快照仍保持可用。
            if nameservers:
                self._conn.execute(
                    "UPDATE subscriptions SET proxy_server_nameservers = ? WHERE id = ?",
                    (json.dumps(nameservers, ensure_ascii=False), sub["id"]),
                )
        self._conn.execute(
            "UPDATE subscriptions SET source_type = "
            "CASE WHEN url IS NOT NULL AND url != '' THEN 'url' ELSE 'file' END "
            "WHERE source_type NOT IN ('file', 'url') "
            "OR source_type != CASE WHEN url IS NOT NULL AND url != '' THEN 'url' ELSE 'file' END"
        )
        import_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(imports)").fetchall()}
        if "source_type" not in import_cols:
            self._conn.execute("ALTER TABLE imports ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'")
        node_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "id" not in node_cols or "position" not in node_cols:
            self._upgrade_nodes_table_locked()
        self._migrate_access_ids_locked()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_sub_position ON nodes(sub_id, position, id)"
        )
        self._conn.commit()

    @staticmethod
    def _stable_id_for_row(row: sqlite3.Row | dict) -> str:
        try:
            params = json.loads(row["params"])
            return compute_access_id(
                {"type": row["type"], "server": row["server"], "port": row["port"], **params}
            ).value
        except (TypeError, ValueError, json.JSONDecodeError):
            return row["access_id"]

    def _upgrade_nodes_table_locked(self) -> None:
        """把旧的全局去重节点池升级为 per-subscription 有序快照。

        最近一次 imports.raw 是最可靠的原序来源；解析不了或没有历史时，才按旧表顺序兜底。
        """
        legacy = [
            dict(r)
            for r in self._conn.execute(
                "SELECT rowid AS legacy_rowid, access_id, sub_id, type, server, port, raw_name, "
                "params, first_seen, last_seen FROM nodes ORDER BY rowid"
            ).fetchall()
        ]
        for row in legacy:
            self._merge_node_tag_locked(row["access_id"], self._stable_id_for_row(row))

        self._conn.execute("ALTER TABLE nodes RENAME TO nodes_legacy")
        self._conn.execute(_NODES_TABLE_SQL)

        timestamps: dict[tuple[str | None, str], tuple[str, str]] = {}
        for row in legacy:
            key = (row["sub_id"], self._stable_id_for_row(row))
            seen = timestamps.get(key)
            first = row["first_seen"] if not seen else min(seen[0], row["first_seen"])
            last = row["last_seen"] if not seen else max(seen[1], row["last_seen"])
            timestamps[key] = (first, last)

        reconstructed: set[str] = set()
        subs = self._conn.execute("SELECT id, type FROM subscriptions").fetchall()
        for sub in subs:
            latest = self._conn.execute(
                "SELECT raw FROM imports WHERE sub_id = ? ORDER BY id DESC LIMIT 1", (sub["id"],)
            ).fetchone()
            if not latest:
                continue
            try:
                snapshot = normalize(latest["raw"], sub["type"], sub["id"])
            except Exception:  # 迁移必须 best-effort；坏历史不能阻止服务启动
                continue
            reconstructed.add(sub["id"])
            for position, node in enumerate(snapshot):
                first, last = timestamps.get(
                    (sub["id"], node.access_id.value),
                    ("", ""),
                )
                self._insert_migrated_node_locked(sub["id"], position, node, first, last)

        # 没有可用 imports 快照的订阅，按旧表 rowid 兜底。仅合并身份算法升级造成的碰撞；
        # 不同订阅中的相同 access_id 仍各自保留。
        fallback: dict[tuple[str | None, str], dict] = {}
        for row in legacy:
            if row["sub_id"] in reconstructed:
                continue
            stable_id = self._stable_id_for_row(row)
            key = (row["sub_id"], stable_id)
            current = fallback.get(key)
            if current is None or row["last_seen"] >= current["last_seen"]:
                chosen = dict(row)
                chosen["access_id"] = stable_id
                if current:
                    chosen["first_seen"] = min(current["first_seen"], row["first_seen"])
                fallback[key] = chosen
            elif row["first_seen"] < current["first_seen"]:
                current["first_seen"] = row["first_seen"]

        per_sub_position: dict[str | None, int] = {}
        for row in sorted(fallback.values(), key=lambda item: item["legacy_rowid"]):
            sub_id = row["sub_id"]
            position = per_sub_position.get(sub_id, 0)
            per_sub_position[sub_id] = position + 1
            ep = EndpointId(type=row["type"], server=row["server"], port=row["port"])
            try:
                params = json.loads(row["params"])
            except (TypeError, json.JSONDecodeError):
                params = {}
            node = Node(
                access_id=AccessId(value=row["access_id"], endpoint=ep),
                raw_name=row["raw_name"],
                params=params,
                source=sub_id or "",
            )
            self._insert_migrated_node_locked(
                sub_id, position, node, row["first_seen"], row["last_seen"]
            )

        self._conn.execute("DROP TABLE nodes_legacy")

    def _insert_migrated_node_locked(
        self, sub_id: str | None, position: int, node: Node, first_seen: str, last_seen: str
    ) -> None:
        ep = node.access_id.endpoint
        self._conn.execute(
            "INSERT INTO nodes(access_id, sub_id, position, type, server, port, raw_name, params, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "COALESCE(NULLIF(?, ''), datetime('now')), COALESCE(NULLIF(?, ''), datetime('now')))",
            (
                node.access_id.value,
                sub_id,
                position,
                ep.type,
                ep.server,
                ep.port,
                node.raw_name,
                json.dumps(node.params, ensure_ascii=False),
                first_seen,
                last_seen,
            ),
        )

    def _merge_node_tag_locked(self, old_id: str, new_id: str) -> None:
        if old_id == new_id:
            return
        old = self._conn.execute(
            "SELECT region, quarantined FROM node_tags WHERE access_id = ?", (old_id,)
        ).fetchone()
        if not old:
            return
        new = self._conn.execute(
            "SELECT region, quarantined FROM node_tags WHERE access_id = ?", (new_id,)
        ).fetchone()
        if new:
            region = new["region"] or old["region"]
            quarantined = int(bool(new["quarantined"]) or bool(old["quarantined"]))
            self._conn.execute(
                "UPDATE node_tags SET region = ?, quarantined = ? WHERE access_id = ?",
                (region, quarantined, new_id),
            )
            self._conn.execute("DELETE FROM node_tags WHERE access_id = ?", (old_id,))
        else:
            self._conn.execute("UPDATE node_tags SET access_id = ? WHERE access_id = ?", (new_id, old_id))

    def _migrate_access_ids_locked(self) -> None:
        """Recompute IDs after identity-only metadata keys change, preserving node tags and memberships."""
        rows = self._conn.execute(
            "SELECT id FROM nodes ORDER BY id"
        ).fetchall()
        for r in rows:
            current = self._conn.execute(
                "SELECT id, access_id, sub_id, position, type, server, port, raw_name, params, "
                "first_seen, last_seen FROM nodes WHERE id = ?", (r["id"],)
            ).fetchone()
            if not current:  # 前一个碰撞已把它合并掉
                continue
            old_id = current["access_id"]
            params = json.loads(current["params"])
            new_id = compute_access_id(
                {
                    "type": current["type"],
                    "server": current["server"],
                    "port": current["port"],
                    **params,
                }
            ).value
            if new_id == old_id:
                continue
            target = self._conn.execute(
                "SELECT id, first_seen, last_seen FROM nodes "
                "WHERE sub_id IS ? AND access_id = ? AND id != ? ORDER BY id LIMIT 1",
                (current["sub_id"], new_id, current["id"]),
            ).fetchone()
            if target:
                self._conn.execute(
                    "UPDATE nodes SET type = ?, server = ?, port = ?, raw_name = ?, params = ?, "
                    "position = min(position, ?), first_seen = min(first_seen, ?), "
                    "last_seen = max(last_seen, ?) WHERE id = ?",
                    (
                        current["type"], current["server"], current["port"], current["raw_name"],
                        current["params"], current["position"], current["first_seen"],
                        current["last_seen"], target["id"],
                    ),
                )
                self._merge_node_tag_locked(old_id, new_id)
                self._conn.execute("DELETE FROM nodes WHERE id = ?", (current["id"],))
            else:
                self._conn.execute(
                    "UPDATE nodes SET access_id = ? WHERE id = ?", (new_id, current["id"])
                )
                self._merge_node_tag_locked(old_id, new_id)

        sub_ids = self._conn.execute("SELECT DISTINCT sub_id FROM nodes").fetchall()
        for sub in sub_ids:
            members = self._conn.execute(
                "SELECT id FROM nodes WHERE sub_id IS ? ORDER BY position, id", (sub["sub_id"],)
            ).fetchall()
            for position, member in enumerate(members):
                self._conn.execute(
                    "UPDATE nodes SET position = ? WHERE id = ?", (position, member["id"])
                )

    # ---- subscriptions ----

    def add_subscription(self, name: str, type: str = "auto", note: str = "", url: str | None = None) -> str:
        """新建最低优先级订阅，返回自动生成的内部 id。"""
        sub_id = secrets.token_hex(8)
        clean_url = (url or "").strip() or None
        source_type = _source_type_for_url(clean_url)
        with self._lock:
            row = self._conn.execute("SELECT max(position) AS p FROM subscriptions").fetchone()
            position = (row["p"] + 1) if row and row["p"] is not None else 0
            self._conn.execute(
                "INSERT INTO subscriptions(id, name, position, type, note, source_type, url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sub_id, name, position, type, note, source_type, clean_url),
            )
            self._conn.commit()
        return sub_id

    def get_subscription(self, sub_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        return dict(row) if row else None

    def latest_import_raw(self, sub_id: str) -> str | None:
        """返回最近一次成功导入的原文，供订阅详情编辑；不要用于列表接口。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT raw FROM imports WHERE sub_id = ? ORDER BY id DESC LIMIT 1",
                (sub_id,),
            ).fetchone()
        return row["raw"] if row else None

    def list_subscriptions(self) -> list[dict]:
        """列订阅（**不返回 url**，含 token = secret；只给 has_url 标志）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.name, s.type, s.created_at, "
                "s.position, s.source_type, "
                "(s.url IS NOT NULL AND s.url != '') AS has_url, "
                "(SELECT COUNT(*) FROM nodes n WHERE n.sub_id = s.id) AS node_count "
                "FROM subscriptions s ORDER BY s.position, s.created_at, s.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def reorder_subscriptions(self, sub_ids: list[str]) -> None:
        """用完整 id 列表原子替换优先级；拒绝缺失、重复或未知 id。"""
        with self._lock:
            current = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM subscriptions ORDER BY position, created_at, id"
                ).fetchall()
            ]
            if len(sub_ids) != len(set(sub_ids)) or set(sub_ids) != set(current):
                raise ValueError("订阅顺序必须包含全部且不重复的 subscription id")
            try:
                for position, sub_id in enumerate(sub_ids):
                    self._conn.execute(
                        "UPDATE subscriptions SET position = ? WHERE id = ?", (position, sub_id)
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

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

    def import_nodes(
        self,
        sub_id: str,
        raw: str,
        nodes: list[Node],
        source_type: str = "file",
        proxy_server_nameservers: list[str] | None = None,
    ) -> int:
        """记录一次导入，并用上游原序原子替换该订阅的完整节点快照。"""
        source_type = _check_source_type(source_type)
        proxy_server_nameservers = list(proxy_server_nameservers or [])
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO imports(sub_id, raw, source_type, node_count) VALUES (?, ?, ?, ?)",
                    (sub_id, raw, source_type, len(nodes)),
                )
                first_seen = {
                    r["access_id"]: r["first_seen"]
                    for r in self._conn.execute(
                        "SELECT access_id, min(first_seen) AS first_seen FROM nodes "
                        "WHERE sub_id = ? GROUP BY access_id",
                        (sub_id,),
                    ).fetchall()
                }
                self._conn.execute("DELETE FROM nodes WHERE sub_id = ?", (sub_id,))
                for position, n in enumerate(nodes):
                    ep = n.access_id.endpoint
                    self._conn.execute(
                        "INSERT INTO nodes(access_id, sub_id, position, type, server, port, raw_name, "
                        "params, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                        "COALESCE(?, datetime('now')))",
                        (
                            n.access_id.value,
                            sub_id,
                            position,
                            ep.type,
                            ep.server,
                            ep.port,
                            n.raw_name,
                            json.dumps(n.params, ensure_ascii=False),
                            first_seen.get(n.access_id.value),
                        ),
                    )
                self._conn.execute(
                    "UPDATE subscriptions SET proxy_server_nameservers = ? WHERE id = ?",
                    (json.dumps(proxy_server_nameservers, ensure_ascii=False), sub_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()  # 整批导入原子化：中途失败不留半成品
                raise
            return len(nodes)

    def list_nodes(self, sub_id: str | None = None) -> list[dict]:
        """列节点（**不含 params**，避免泄露凭据）；给 sub_id 则只列该订阅的。"""
        q = (
            "SELECT n.access_id, n.sub_id, n.position, n.type, n.server, n.port, n.raw_name, "
            "n.first_seen, n.last_seen FROM nodes n "
        )
        args: tuple = ()
        if sub_id is not None:
            q += "WHERE n.sub_id = ? "
            args = (sub_id,)
            q += "ORDER BY n.position, n.id"
        else:
            q += (
                "LEFT JOIN subscriptions s ON s.id = n.sub_id "
                "ORDER BY COALESCE(s.position, 2147483647), n.position, n.id"
            )
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
        q = (
            "SELECT n.access_id, n.sub_id, n.position, n.type, n.server, n.port, n.raw_name, "
            "n.params FROM nodes n "
        )
        args: tuple = ()
        if sub_id is not None:
            q += "WHERE n.sub_id = ? ORDER BY n.position, n.id"
            args = (sub_id,)
        else:
            q += (
                "LEFT JOIN subscriptions s ON s.id = n.sub_id "
                "ORDER BY COALESCE(s.position, 2147483647), n.position, n.id"
            )
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return self._nodes_from_rows(rows)

    @staticmethod
    def _nodes_from_rows(rows) -> list[Node]:
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

    def render_snapshot(self) -> tuple[list[Node], dict[str, list[str]]]:
        """原子读取节点与其来源 DNS，避免刷新并发时把新旧快照拼在一起。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT n.access_id, n.sub_id, n.position, n.type, n.server, n.port, "
                "n.raw_name, n.params, s.proxy_server_nameservers "
                "FROM nodes n LEFT JOIN subscriptions s ON s.id = n.sub_id "
                "ORDER BY COALESCE(s.position, 2147483647), n.position, n.id"
            ).fetchall()
        source_dns: dict[str, list[str]] = {}
        for row in rows:
            sub_id = row["sub_id"] or ""
            if not sub_id or sub_id in source_dns:
                continue
            values = self._parse_proxy_nameservers(row["proxy_server_nameservers"])
            if values:
                source_dns[sub_id] = values
        return self._nodes_from_rows(rows), source_dns

    @staticmethod
    def _parse_proxy_nameservers(raw: str | None) -> list[str]:
        try:
            values = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            return values
        return []

    def source_proxy_nameservers(self) -> dict[str, list[str]]:
        """返回来源订阅 id → 节点域名 DNS；仅供内部 render，绝不经 API 暴露。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, proxy_server_nameservers FROM subscriptions"
            ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            values = self._parse_proxy_nameservers(row["proxy_server_nameservers"])
            if values:
                out[row["id"]] = values
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
