"""SQLite storage – the only module that speaks SQL (the swap point for Postgres later).

Design
* one file (default ``data/landscape.db``), WAL mode
* every object is stored as a JSON document in ``data`` plus a few indexed columns → new fields need
  no migration; queries can still use ``json_extract(data, '$.field')``
* schema changes go into ``MIGRATIONS`` (append-only, tracked via ``PRAGMA user_version``)
* FTS5 full-text index over mission deliverables and agent artifacts (optional if FTS5 is unavailable)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# append-only: never edit a released migration, add a new one
MIGRATIONS: list[str] = [
    """
    CREATE TABLE regions (id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL);
    CREATE TABLE topics (
        id TEXT PRIMARY KEY, region_id TEXT, kind TEXT, status TEXT, title TEXT,
        data TEXT NOT NULL, created_at REAL, updated_at REAL);
    CREATE INDEX topics_region ON topics(region_id);
    CREATE INDEX topics_status ON topics(status);
    CREATE TABLE agents (id TEXT PRIMARY KEY, topic_id TEXT, role TEXT, name TEXT, data TEXT NOT NULL, updated_at REAL);
    CREATE INDEX agents_topic ON agents(topic_id);
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY, context_id TEXT, agent_id TEXT, state TEXT,
        data TEXT NOT NULL, created_at REAL, updated_at REAL);
    CREATE INDEX tasks_context ON tasks(context_id);
    CREATE INDEX tasks_agent ON tasks(agent_id);
    CREATE TABLE mission_docs (topic_id TEXT, name TEXT, source TEXT, origin TEXT, text TEXT,
        PRIMARY KEY (topic_id, name));
    CREATE TABLE mission_intel (topic_id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE kb_links (topic_id TEXT, store_id TEXT, data TEXT NOT NULL, PRIMARY KEY (topic_id, store_id));
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]

FTS_SQL = ("CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5("
           "kind UNINDEXED, ref UNINDEXED, topic_id UNINDEXED, title, body, "
           "tokenize = 'unicode61 remove_diacritics 2')")

UPSERT = {
    "regions": "INSERT INTO regions (id, data, created_at) VALUES (?, ?, ?) "
               "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
    "topics": "INSERT INTO topics (id, region_id, kind, status, title, data, created_at, updated_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET region_id = excluded.region_id, "
              "kind = excluded.kind, status = excluded.status, title = excluded.title, data = excluded.data, "
              "updated_at = excluded.updated_at",
    "agents": "INSERT INTO agents (id, topic_id, role, name, data, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
              "ON CONFLICT(id) DO UPDATE SET topic_id = excluded.topic_id, role = excluded.role, "
              "name = excluded.name, data = excluded.data, updated_at = excluded.updated_at",
    "tasks": "INSERT INTO tasks (id, context_id, agent_id, state, data, created_at, updated_at) "
             "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
             "data = excluded.data, updated_at = excluded.updated_at",
    "intel": "INSERT INTO mission_intel (topic_id, data) VALUES (?, ?) "
             "ON CONFLICT(topic_id) DO UPDATE SET data = excluded.data",
    "kv": "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
}


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()
        self.fts = self._init_fts()

    # ------------------------------------------------------------- schema
    @property
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        current = self.version
        for number, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            with self.lock:
                self.conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {number}; COMMIT;")
            log.info("Database migrated to schema v%s", number)

    def _init_fts(self) -> bool:
        try:
            with self.lock:
                self.conn.execute(FTS_SQL)
            return True
        except sqlite3.OperationalError as exc:  # SQLite built without FTS5
            log.warning("Full-text search disabled: %s", exc)
            return False

    # -------------------------------------------------------------- write
    def write(self, b: dict[str, Any]) -> int:
        """Apply one batch atomically. Returns the number of rows written."""
        n = 0
        with self.lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                for topic_id in b.get("deletes", []):
                    for sql in ("DELETE FROM topics WHERE id = ?", "DELETE FROM agents WHERE topic_id = ?",
                                "DELETE FROM tasks WHERE context_id = ?", "DELETE FROM mission_docs WHERE topic_id = ?",
                                "DELETE FROM mission_intel WHERE topic_id = ?", "DELETE FROM kb_links WHERE topic_id = ?"):
                        c.execute(sql, (topic_id,))
                    if self.fts:
                        c.execute("DELETE FROM search_index WHERE topic_id = ?", (topic_id,))
                for key in ("regions", "topics", "agents", "tasks", "intel", "kv"):
                    rows = b.get(key) or []
                    if rows:
                        c.executemany(UPSERT[key], rows)
                        n += len(rows)
                for topic_id, docs in (b.get("docs") or {}).items():
                    c.execute("DELETE FROM mission_docs WHERE topic_id = ?", (topic_id,))
                    c.executemany("INSERT INTO mission_docs (topic_id, name, source, origin, text) VALUES (?, ?, ?, ?, ?)",
                                  [(topic_id, *d) for d in docs])
                    n += len(docs)
                for topic_id, links in (b.get("links") or {}).items():
                    c.execute("DELETE FROM kb_links WHERE topic_id = ?", (topic_id,))
                    c.executemany("INSERT INTO kb_links (topic_id, store_id, data) VALUES (?, ?, ?)",
                                  [(topic_id, *row) for row in links])
                    n += len(links)
                if self.fts:
                    for kind, ref, topic_id, title, body in b.get("fts") or []:
                        c.execute("DELETE FROM search_index WHERE ref = ?", (ref,))
                        c.execute("INSERT INTO search_index (kind, ref, topic_id, title, body) VALUES (?, ?, ?, ?, ?)",
                                  (kind, ref, topic_id, title, body))
                        n += 1
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return n

    # --------------------------------------------------------------- read
    def load(self, task_limit: int = 5000) -> dict[str, Any]:
        with self.lock:
            q = self.conn.execute
            data: dict[str, Any] = {
                "regions": [json.loads(r["data"]) for r in q("SELECT data FROM regions ORDER BY created_at")],
                "topics": [json.loads(r["data"]) for r in q("SELECT data FROM topics ORDER BY created_at")],
                "agents": [json.loads(r["data"]) for r in q("SELECT data FROM agents")],
                "tasks": [json.loads(r["data"]) for r in reversed(q(
                    "SELECT data FROM tasks ORDER BY created_at DESC LIMIT ?", (task_limit,)).fetchall())],
                "kv": {r["key"]: json.loads(r["value"]) for r in q("SELECT key, value FROM kv")},
                "intel": {r["topic_id"]: json.loads(r["data"]) for r in q("SELECT topic_id, data FROM mission_intel")},
                "docs": {}, "links": {},
            }
            for r in q("SELECT topic_id, name, source, origin, text FROM mission_docs"):
                data["docs"].setdefault(r["topic_id"], []).append(dict(r))
            for r in q("SELECT topic_id, store_id, data FROM kb_links"):
                data["links"].setdefault(r["topic_id"], []).append((r["store_id"], json.loads(r["data"])))
        return data

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search: all words must match, the last one as a prefix ('graph ap' finds 'Graph API')."""
        if not self.fts:
            return []
        words = re.findall(r"\w+", query or "", re.UNICODE)[:12]
        if not words:
            return []
        match = " ".join(f'"{w}"' for w in words[:-1]) + (" " if len(words) > 1 else "") + f'"{words[-1]}"*'
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind, ref, topic_id, title, snippet(search_index, 4, '[[', ']]', '…', 16) AS snippet, "
                "bm25(search_index, 0, 0, 0, 3.0, 1.0) AS score FROM search_index WHERE search_index MATCH ? "
                "ORDER BY score LIMIT ?", (match, limit)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self.lock:
            out = {t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608 – fixed names
                   for t in ("regions", "topics", "agents", "tasks", "mission_docs", "kb_links")}
        out["bytes"] = self.path.stat().st_size if self.path.exists() else 0
        return out

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                self.conn.close()
