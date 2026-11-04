"""
SQLite state store.

Holds sessions, the full-fidelity message log (OpenAI-format messages
including tool calls / tool results / reasoning), transcript full-text
search, and the long-term memory table used by :mod:`alita.memory.longterm`.

One persistent connection guarded by a re-entrant lock is shared across the
process (WAL mode). All methods are synchronous and fast; callers in async
code may call them directly.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

SESSION_KINDS = ("chat", "job", "subagent", "kanban")


def _now_sql() -> str:
    return "strftime('%Y-%m-%d %H:%M:%f','now')"


def content_to_text(content: Any) -> str:
    """Extract plain text from str or multimodal content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(str(part.get("text", "")))
                elif part.get("type") in ("image_url", "image"):
                    out.append("[image]")
            else:
                out.append(str(part))
        return "\n".join(p for p in out if p)
    return str(content)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression (OR of quoted terms)."""
    terms = re.findall(r"[\w][\w'-]*", text.lower(), flags=re.UNICODE)
    stop = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on", "with", "what",
            "how", "do", "i", "you", "me", "my", "we", "be", "that", "this", "are", "was", "can"}
    terms = [t.replace('"', "") for t in terms if t not in stop and len(t) > 1][:16]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


class SQLiteStore:
    """Persistent SQLite database for sessions, messages and memories."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from ..core.config import db_path as _default_db
            db_path = str(_default_db())
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_db()

    # ─── Low-level helpers ────────────────────────────────────────

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Locked transaction; commits on success, rolls back on error."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def record_usage(
        self,
        *,
        session_id: Optional[str],
        job_id: Optional[str],
        source: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        estimated: bool = False,
    ) -> int:
        """Persist one provider call's token usage. Call from a worker thread."""
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO usage_events "
                "(session_id, job_id, source, model, prompt_tokens, completion_tokens, "
                " cached_tokens, estimated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    job_id,
                    source,
                    model,
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(cached_tokens or 0),
                    1 if estimated else 0,
                ),
            )
            return int(cur.lastrowid or 0)

    # ─── External executor spending ──────────────────────────────

    @staticmethod
    def _spend_date(date: Optional[str] = None) -> str:
        return date or datetime.now().date().isoformat()

    def record_executor_spend(
        self,
        executor: str,
        cost: float,
        date: Optional[str] = None,
    ) -> None:
        """Add to an executor's spend for a day (UPSERT, safe at the cent)."""
        if not cost:
            return
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO executor_spend (date, executor, cost) VALUES (?, ?, ?) "
                "ON CONFLICT(date, executor) DO UPDATE SET cost = cost + excluded.cost",
                (self._spend_date(date), str(executor), float(cost)),
            )

    def get_executor_spend(self, executor: str, date: Optional[str] = None) -> float:
        """Total recorded spend for an executor on a day (local date by default)."""
        row = self.query_one(
            "SELECT cost FROM executor_spend WHERE date = ? AND executor = ?",
            (self._spend_date(date), str(executor)),
        )
        return float(row["cost"]) if row else 0.0

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ─── Schema ───────────────────────────────────────────────────

    def _columns(self, table: str) -> set:
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _init_db(self) -> None:
        with self.tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    summary TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS execution_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER,
                    tool_name TEXT NOT NULL,
                    input_params TEXT,
                    output_result TEXT,
                    success BOOLEAN NOT NULL DEFAULT 1,
                    duration_ms INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sandbox_id TEXT
                );
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    file_path TEXT NOT NULL,
                    tags TEXT,
                    usage_count INTEGER DEFAULT 0,
                    success_rate REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
                    content, session_id, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'fact',
                    importance INTEGER NOT NULL DEFAULT 3,
                    source_session TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_accessed TIMESTAMP,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    job_id TEXT,
                    source TEXT NOT NULL,
                    model TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id);
                CREATE TABLE IF NOT EXISTS executor_spend (
                    date TEXT NOT NULL,
                    executor TEXT NOT NULL,
                    cost REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, executor)
                );
                """
            )
            # Columns added in schema v2 (idempotent for old databases).
            sess_cols = self._columns("sessions")
            for col, ddl in [
                ("title", "TEXT DEFAULT ''"),
                ("kind", "TEXT DEFAULT 'chat'"),
                ("updated_at", "TIMESTAMP"),
                ("pinned", "INTEGER DEFAULT 0"),
                ("context_summary", "TEXT DEFAULT ''"),
                ("compacted_upto", "INTEGER DEFAULT 0"),
                ("parent_id", "TEXT"),
                ("metadata", "TEXT"),
            ]:
                if col not in sess_cols:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
            conv_cols = self._columns("conversations")
            for col, ddl in [
                ("tool_call_id", "TEXT"),
                ("name", "TEXT"),
                ("reasoning", "TEXT"),
            ]:
                if col not in conv_cols:
                    conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {ddl}")
            # Scoped / fiction memories.
            # ``scope`` namespaces a world (e.g. "story:<worldId>"),
            # ``external_ref`` makes external writes idempotent, and
            # ``fiction`` marks memories that must never be treated as
            # real-world facts by recall, de-duplication or extraction.
            mem_cols = self._columns("memories")
            for col, ddl in [
                ("scope", "TEXT"),
                ("external_ref", "TEXT"),
                ("fiction", "INTEGER NOT NULL DEFAULT 0"),
            ]:
                if col not in mem_cols:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_external_ref
                    ON memories(external_ref) WHERE external_ref IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, active);
                """
            )
            # The v1 insert trigger indexed raw JSON/tool output; FTS is now
            # maintained explicitly with extracted text.
            conn.execute("DROP TRIGGER IF EXISTS conversations_ai")
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind, updated_at);
                CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(active, category);

                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
                WHEN new.active = 1
                BEGIN
                    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                END;

                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories
                WHEN new.active = 1
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS memories_active_au AFTER UPDATE OF active ON memories
                WHEN old.active <> new.active
                BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                    INSERT INTO memories_fts(rowid, content)
                        SELECT new.id, new.content WHERE new.active = 1;
                END;
                """
            )
            # Repair FTS drift left by pre-trigger code paths. The invariant is
            # exactly one index row per *active* memory.
            conn.execute(
                "DELETE FROM memories_fts "
                "WHERE rowid NOT IN (SELECT id FROM memories WHERE active = 1)"
            )
            conn.execute(
                "DELETE FROM memories_fts "
                "WHERE rowid IN (SELECT rowid FROM memories_fts GROUP BY rowid HAVING COUNT(*) > 1)"
            )
            conn.execute(
                "INSERT INTO memories_fts(rowid, content) "
                "SELECT m.id, m.content FROM memories m "
                "WHERE m.active = 1 "
                "AND NOT EXISTS (SELECT 1 FROM memories_fts f WHERE f.rowid = m.id)"
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < 2:
                self._migrate_v2(conn)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """One-shot data clean-up for databases written by v0.1."""
        logger.info("Migrating state.db to schema v2")
        conn.execute(
            """UPDATE sessions SET kind = CASE
                   WHEN summary LIKE 'Scheduled job:%' THEN 'job'
                   WHEN summary LIKE 'Sub-agent%' THEN 'subagent'
                   ELSE 'chat' END
               WHERE kind IS NULL OR kind = 'chat'"""
        )
        # v0.1 logged the same user/assistant message twice and stored tool
        # results without their tool_call_id; neither can be replayed.
        conn.execute(
            """DELETE FROM conversations WHERE id IN (
                   SELECT id FROM (
                       SELECT id, role, content,
                              LAG(role) OVER w AS prev_role,
                              LAG(content) OVER w AS prev_content
                       FROM conversations
                       WINDOW w AS (PARTITION BY session_id ORDER BY id)
                   ) WHERE role = prev_role AND content = prev_content
                     AND role IN ('user', 'assistant'))"""
        )
        conn.execute("DELETE FROM conversations WHERE role = 'tool' AND tool_call_id IS NULL")
        conn.execute(
            "DELETE FROM conversations WHERE role = 'assistant' AND TRIM(content) = '' AND tool_calls IS NULL"
        )
        conn.execute("DELETE FROM conversations WHERE role = 'system'")
        conn.execute(
            """DELETE FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM conversations)"""
        )
        conn.execute(
            """DELETE FROM conversations WHERE session_id NOT IN (SELECT id FROM sessions)"""
        )
        conn.execute(
            """UPDATE sessions SET updated_at = COALESCE(
                   (SELECT MAX(created_at) FROM conversations c WHERE c.session_id = sessions.id),
                   created_at)"""
        )
        rows = conn.execute(
            """SELECT s.id, (SELECT content FROM conversations c WHERE c.session_id = s.id
                             AND c.role = 'user' ORDER BY c.id LIMIT 1) AS first_user
               FROM sessions s WHERE COALESCE(s.title, '') = ''"""
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (fallback_title(r["first_user"] or ""), r["id"]))
        conn.execute("DELETE FROM conversations_fts")
        for r in conn.execute(
            "SELECT id, session_id, content FROM conversations WHERE role IN ('user', 'assistant')"
        ).fetchall():
            conn.execute(
                "INSERT INTO conversations_fts(rowid, content, session_id) VALUES (?, ?, ?)",
                (r["id"], r["content"], r["session_id"]),
            )

    # ─── Sessions ─────────────────────────────────────────────────

    def create_session(
        self,
        session_id: Optional[str] = None,
        summary: str = "",
        *,
        kind: str = "chat",
        title: str = "",
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        session_id = session_id or str(uuid.uuid4())
        with self.tx() as conn:
            conn.execute(
                f"""INSERT OR IGNORE INTO sessions (id, summary, kind, title, parent_id, metadata, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, {_now_sql()})""",
                (session_id, summary, kind, title, parent_id, json.dumps(metadata) if metadata else None),
            )
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self.query_one(
            """SELECT s.*, (SELECT COUNT(*) FROM conversations c WHERE c.session_id = s.id
                            AND (c.role = 'user' OR (c.role = 'assistant' AND TRIM(c.content) != ''))) AS message_count
               FROM sessions s WHERE s.id = ?""",
            (session_id,),
        )
        return _decode_session(row) if row else None

    def list_sessions(
        self,
        kind: Optional[str] = "chat",
        limit: int = 100,
        offset: int = 0,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        where, params = [], []
        if kind:
            where.append("s.kind = ?")
            params.append(kind)
        if query:
            where.append("s.title LIKE ?")
            params.append(f"%{query}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.query(
            f"""SELECT s.*, (SELECT COUNT(*) FROM conversations c WHERE c.session_id = s.id
                             AND (c.role = 'user' OR (c.role = 'assistant' AND TRIM(c.content) != ''))) AS message_count
                FROM sessions s {clause}
                ORDER BY s.pinned DESC, COALESCE(s.updated_at, s.created_at) DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        )
        return [_decode_session(r) for r in rows]

    def update_session(self, session_id: str, **fields: Any) -> bool:
        allowed = {"title", "pinned", "summary", "status", "context_summary", "compacted_upto", "metadata", "kind"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "metadata" and not isinstance(v, (str, type(None))):
                v = json.dumps(v)
            if k == "pinned":
                v = 1 if v else 0
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return False
        with self.tx() as conn:
            cur = conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", (*params, session_id))
            return cur.rowcount > 0

    def touch_session(self, session_id: str) -> None:
        self.execute(f"UPDATE sessions SET updated_at = {_now_sql()} WHERE id = ?", (session_id,))

    def delete_session(self, session_id: str) -> bool:
        with self.tx() as conn:
            conn.execute("DELETE FROM conversations_fts WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    def close_session(self, session_id: str, summary: str = "") -> None:
        """Mark a session closed (kept for history)."""
        self.execute(
            "UPDATE sessions SET status='closed', summary=COALESCE(NULLIF(?, ''), summary), "
            "closed_at=CURRENT_TIMESTAMP WHERE id=?",
            (summary, session_id),
        )

    def prune_empty_sessions(self, kinds: Sequence[str] = ("chat",), older_than_minutes: int = 60) -> int:
        marks = ",".join("?" for _ in kinds)
        with self.tx() as conn:
            cur = conn.execute(
                f"""DELETE FROM sessions WHERE kind IN ({marks})
                    AND id NOT IN (SELECT DISTINCT session_id FROM conversations)
                    AND created_at < datetime('now', ?)""",
                (*kinds, f"-{int(older_than_minutes)} minutes"),
            )
            return cur.rowcount

    # Back-compat helpers used by older callers.
    def get_all_sessions(self) -> List[Dict[str, Any]]:
        return self.list_sessions(kind=None, limit=1000)

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        return [s for s in self.list_sessions(kind=None, limit=1000) if s.get("status") == "active"]

    # ─── Messages ─────────────────────────────────────────────────

    def append_message(self, session_id: str, message: Dict[str, Any]) -> int:
        """Persist one OpenAI-format message; returns its row id."""
        role = message.get("role", "user")
        content = message.get("content")
        meta = dict(message.get("_meta") or {})
        if isinstance(content, list):
            stored = json.dumps(content)
            meta["content_format"] = "parts"
        else:
            stored = content or ""
        tool_calls = message.get("tool_calls")
        with self.tx() as conn:
            cur = conn.execute(
                """INSERT INTO conversations
                   (session_id, role, content, tool_calls, metadata, tool_call_id, name, reasoning, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%f','now'))""",
                (
                    session_id,
                    role,
                    stored,
                    json.dumps(tool_calls) if tool_calls else None,
                    json.dumps(meta) if meta else None,
                    message.get("tool_call_id"),
                    message.get("name"),
                    message.get("reasoning_content") or None,
                ),
            )
            row_id = cur.lastrowid
            text = content_to_text(content)
            if role in ("user", "assistant") and text.strip():
                conn.execute(
                    "INSERT INTO conversations_fts(rowid, content, session_id) VALUES (?, ?, ?)",
                    (row_id, text, session_id),
                )
            conn.execute(f"UPDATE sessions SET updated_at = {_now_sql()} WHERE id = ?", (session_id,))
        return row_id

    def update_message(self, message_id: int, message: Dict[str, Any]) -> None:
        content = message.get("content")
        meta = dict(message.get("_meta") or {})
        if isinstance(content, list):
            stored = json.dumps(content)
            meta["content_format"] = "parts"
        else:
            stored = content or ""
        with self.tx() as conn:
            conn.execute(
                "UPDATE conversations SET content = ?, tool_calls = ?, metadata = ?, reasoning = ? WHERE id = ?",
                (
                    stored,
                    json.dumps(message.get("tool_calls")) if message.get("tool_calls") else None,
                    json.dumps(meta) if meta else None,
                    message.get("reasoning_content") or None,
                    message_id,
                ),
            )
            conn.execute("DELETE FROM conversations_fts WHERE rowid = ?", (message_id,))
            text = content_to_text(content)
            if message.get("role") in ("user", "assistant") and text.strip():
                sid = conn.execute("SELECT session_id FROM conversations WHERE id = ?", (message_id,)).fetchone()
                if sid:
                    conn.execute(
                        "INSERT INTO conversations_fts(rowid, content, session_id) VALUES (?, ?, ?)",
                        (message_id, text, sid[0]),
                    )

    def delete_messages_from(self, session_id: str, from_id: int) -> int:
        """Delete messages with id >= from_id (used by edit / regenerate)."""
        with self.tx() as conn:
            conn.execute(
                "DELETE FROM conversations_fts WHERE rowid IN (SELECT id FROM conversations WHERE session_id = ? AND id >= ?)",
                (session_id, from_id),
            )
            cur = conn.execute("DELETE FROM conversations WHERE session_id = ? AND id >= ?", (session_id, from_id))
            return cur.rowcount

    def load_messages(self, session_id: str, after_id: int = 0, limit: int = 100000) -> List[Dict[str, Any]]:
        """Load messages as OpenAI-format dicts (with ``_id``/``_meta``/``_ts`` keys)."""
        rows = self.query(
            "SELECT * FROM conversations WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (session_id, after_id, limit),
        )
        return [row_to_message(r) for r in rows]

    # Back-compat names
    def log_message(self, session_id: str, role: str, content: str, tool_calls=None, metadata=None) -> int:
        msg: Dict[str, Any] = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if metadata:
            msg["_meta"] = metadata
        return self.append_message(session_id, msg)

    def get_conversation_history(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self.query(
            "SELECT * FROM conversations WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        )

    # ─── Transcript search ────────────────────────────────────────

    def search_messages(
        self,
        query: str,
        limit: int = 10,
        session_id: Optional[str] = None,
        exclude_session: Optional[str] = None,
        kinds: Sequence[str] = ("chat",),
    ) -> List[Dict[str, Any]]:
        match = fts_query(query)
        if not match:
            return []
        where = ["conversations_fts MATCH ?"]
        params: List[Any] = [match]
        if session_id:
            where.append("c.session_id = ?")
            params.append(session_id)
        if exclude_session:
            where.append("c.session_id != ?")
            params.append(exclude_session)
        if kinds:
            where.append(f"s.kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        try:
            return self.query(
                f"""SELECT c.id, c.session_id, c.role, c.created_at, s.title,
                           snippet(conversations_fts, 0, '**', '**', ' … ', 40) AS snippet,
                           bm25(conversations_fts) AS score
                    FROM conversations_fts
                    JOIN conversations c ON c.id = conversations_fts.rowid
                    JOIN sessions s ON s.id = c.session_id
                    WHERE {' AND '.join(where)}
                    ORDER BY score LIMIT ?""",
                (*params, limit),
            )
        except sqlite3.OperationalError as exc:
            logger.debug("FTS search failed (%s); falling back to LIKE", exc)
            like_where = ["c.content LIKE ?", "c.role IN ('user','assistant')"]
            like_params: List[Any] = [f"%{query}%"]
            if session_id:
                like_where.append("c.session_id = ?")
                like_params.append(session_id)
            return self.query(
                f"""SELECT c.id, c.session_id, c.role, c.created_at, s.title, substr(c.content, 1, 240) AS snippet
                    FROM conversations c JOIN sessions s ON s.id = c.session_id
                    WHERE {' AND '.join(like_where)} ORDER BY c.id DESC LIMIT ?""",
                (*like_params, limit),
            )

    # Back-compat
    def search_sessions(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.search_messages(query, limit=limit)

    def search_sessions_by_session(self, session_id: str, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.search_messages(query, limit=limit, session_id=session_id, kinds=())


# ─── Row decoding ─────────────────────────────────────────────────────

def _decode_session(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    meta = out.get("metadata")
    if isinstance(meta, str) and meta:
        try:
            out["metadata"] = json.loads(meta)
        except json.JSONDecodeError:
            out["metadata"] = {}
    elif not meta:
        out["metadata"] = {}
    out["pinned"] = bool(out.get("pinned"))
    out["title"] = out.get("title") or ""
    return out


def row_to_message(row: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if row.get("metadata"):
        try:
            meta = json.loads(row["metadata"]) or {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    content: Any = row.get("content") or ""
    if meta.get("content_format") == "parts":
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
    msg: Dict[str, Any] = {"role": row["role"], "content": content}
    if row.get("tool_calls"):
        try:
            tcs = json.loads(row["tool_calls"])
            # Only OpenAI-shaped tool calls are replayable.
            if isinstance(tcs, list) and all(isinstance(t, dict) and "function" in t for t in tcs):
                msg["tool_calls"] = tcs
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("tool_call_id"):
        msg["tool_call_id"] = row["tool_call_id"]
    if row.get("name"):
        msg["name"] = row["name"]
    if row.get("reasoning"):
        msg["reasoning_content"] = row["reasoning"]
    msg["_id"] = row["id"]
    msg["_meta"] = meta
    msg["_ts"] = row.get("created_at")
    return msg


def fallback_title(text: str, max_len: int = 60) -> str:
    text = content_to_text(text) if not isinstance(text, str) else text
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)
    if not line:
        return "New chat"
    return line if len(line) <= max_len else line[: max_len - 1].rstrip() + "…"
