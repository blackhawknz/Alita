"""
Kanban Store — persistent board state for the Alita Kanban feature.

Stores cards in a dedicated table inside the existing ``state.db`` SQLite
database (the same one ``SQLiteStore`` uses for conversation logs). Columns
are hardcoded for v1 — ``idea → planned → executing → review → done`` — but
stored as a free-text field so future migrations to configurable columns
require no schema change.

Card lifecycle:
    idea → planned → executing (auto-execute via KanbanExecutor) →
    review (human inspection) → done
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# ─── Column definitions (v1 hardcoded) ────────────────────────────

COLUMNS = ["idea", "planned", "executing", "review", "done"]

#: The column whose entry triggers automatic agent execution.
EXECUTING_COLUMN = "executing"

#: Columns the agent is allowed to create cards in (via the kanban tool).
AGENT_ALLOWED_CREATE_COLUMNS = ["idea"]

#: Executors a card may delegate to.
CARD_EXECUTORS = ("native", "opencode", "claude_code")


# ─── Data model ───────────────────────────────────────────────────


@dataclass
class KanbanCard:
    """A single Kanban card."""

    id: str
    title: str
    description: str
    column: str
    priority: int = 0
    tags: List[str] = field(default_factory=list)

    # Execution state
    status: str = "idle"  # idle | running | succeeded | failed | blocked
    result: Dict[str, Any] = field(default_factory=dict)
    execution_session_id: str = ""
    error: str = ""

    # Audit
    created_at: str = ""
    updated_at: str = ""
    executed_at: str = ""
    completed_at: str = ""

    # Tuning
    auto_advance: bool = True
    max_iterations: int = 20

    # Delegation
    executor: str = "native"
    workdir: str = ""
    mandate: Dict[str, Any] = field(default_factory=dict)
    max_minutes: float = 30.0
    external_run_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "column": self.column,
            "priority": self.priority,
            "tags": self.tags,
            "status": self.status,
            "result": self.result,
            "execution_session_id": self.execution_session_id,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "executed_at": self.executed_at,
            "completed_at": self.completed_at,
            "auto_advance": self.auto_advance,
            "max_iterations": self.max_iterations,
            "executor": self.executor,
            "workdir": self.workdir,
            "mandate": self.mandate,
            "max_minutes": self.max_minutes,
            "external_run_id": self.external_run_id,
        }


# ─── Store ────────────────────────────────────────────────────────


class KanbanStore:
    """
    Persistent Kanban card store backed by SQLite.

    Shares ``state.db`` with ``SQLiteStore`` (the conversation log) — both
    use the same database file but separate tables, so no migration is
    needed if either feature is added or removed independently.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".alita" / "state.db")
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Open a new connection. Callers own it and must close it."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that commits on success, rolls back on error, and always closes."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kanban_cards (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    column TEXT NOT NULL DEFAULT 'idea',
                    priority INTEGER NOT NULL DEFAULT 0,
                    tags TEXT DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'idle',
                    result TEXT DEFAULT '{}',
                    execution_session_id TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    executed_at TEXT DEFAULT '',
                    completed_at TEXT DEFAULT '',
                    auto_advance BOOLEAN DEFAULT 1,
                    max_iterations INTEGER DEFAULT 20,
                    executor TEXT NOT NULL DEFAULT 'native',
                    workdir TEXT NOT NULL DEFAULT '',
                    mandate TEXT NOT NULL DEFAULT '{}',
                    max_minutes REAL NOT NULL DEFAULT 30,
                    external_run_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_kanban_column ON kanban_cards(column);
                CREATE INDEX IF NOT EXISTS idx_kanban_status ON kanban_cards(status);
                CREATE INDEX IF NOT EXISTS idx_kanban_priority ON kanban_cards(column, priority);
            """)
            # Columns added for external execution (idempotent for old databases).
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(kanban_cards)").fetchall()}
            for col, ddl in [
                ("executor", "TEXT NOT NULL DEFAULT 'native'"),
                ("workdir", "TEXT NOT NULL DEFAULT ''"),
                ("mandate", "TEXT NOT NULL DEFAULT '{}'"),
                ("max_minutes", "REAL NOT NULL DEFAULT 30"),
                ("external_run_id", "TEXT NOT NULL DEFAULT ''"),
            ]:
                if col not in cols:
                    conn.execute(f"ALTER TABLE kanban_cards ADD COLUMN {col} {ddl}")

    # ─── CRUD ──────────────────────────────────────────────────────

    def create_card(
        self,
        title: str,
        description: str = "",
        column: str = "idea",
        priority: int = 0,
        tags: Optional[List[str]] = None,
        auto_advance: bool = True,
        max_iterations: int = 20,
        executor: str = "native",
        workdir: str = "",
        mandate: Optional[Dict[str, Any]] = None,
        max_minutes: float = 30.0,
        external_run_id: str = "",
    ) -> KanbanCard:
        if column not in COLUMNS:
            column = "idea"
        if executor not in CARD_EXECUTORS:
            executor = "native"
        card_id = uuid.uuid4().hex[:12]
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO kanban_cards
                   (id, title, description, column, priority, tags, status,
                    result, execution_session_id, error, created_at, updated_at,
                    auto_advance, max_iterations, executor, workdir, mandate,
                    max_minutes, external_run_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'idle', '{}', '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id, title, description, column, priority,
                    json.dumps(tags or []), now, now,
                    1 if auto_advance else 0, max_iterations,
                    executor, workdir, json.dumps(mandate or {}), max_minutes,
                    external_run_id,
                ),
            )
        return self.get_card(card_id)  # type: ignore[return-value]

    def get_card(self, card_id: str) -> Optional[KanbanCard]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM kanban_cards WHERE id = ?", (card_id,)
            ).fetchone()
        return self._row_to_card(row) if row else None

    def list_cards(self, column: Optional[str] = None) -> List[KanbanCard]:
        with self._conn() as conn:
            if column:
                rows = conn.execute(
                    "SELECT * FROM kanban_cards WHERE column = ? ORDER BY priority ASC, created_at ASC",
                    (column,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM kanban_cards ORDER BY column ASC, priority ASC, created_at ASC"
                ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def list_by_status(self, status: str) -> List[KanbanCard]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM kanban_cards WHERE status = ? ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def update_card(self, card_id: str, **fields) -> Optional[KanbanCard]:
        """Update any subset of card fields. Returns the updated card or None."""
        if not fields:
            return self.get_card(card_id)

        # Whitelist + JSON-serialise complex fields.
        allowed = {
            "title", "description", "column", "priority", "tags",
            "status", "result", "execution_session_id", "error",
            "executed_at", "completed_at", "auto_advance", "max_iterations",
            "executor", "workdir", "mandate", "max_minutes", "external_run_id",
        }
        updates: Dict[str, Any] = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("tags", "result", "mandate") and not isinstance(v, str):
                v = json.dumps(v, default=str)
            updates[k] = v

        if not updates:
            return self.get_card(card_id)

        updates["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [card_id]

        with self._conn() as conn:
            conn.execute(
                f"UPDATE kanban_cards SET {set_clause} WHERE id = ?",
                values,
            )
        return self.get_card(card_id)

    def move_card(self, card_id: str, column: str) -> Optional[KanbanCard]:
        if column not in COLUMNS:
            return None
        return self.update_card(card_id, column=column)

    def delete_card(self, card_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM kanban_cards WHERE id = ?", (card_id,)
            )
            deleted = cursor.rowcount
        return deleted > 0

    def get_executable_cards(self) -> List[KanbanCard]:
        """Return 'idle' cards in the 'executing' column.

        Failed cards are deliberately excluded (no automatic retry loop); use
        ``KanbanExecutor.retry_card`` to reset a failed card to 'idle'.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM kanban_cards
                   WHERE column = ? AND status = 'idle'
                   ORDER BY priority ASC, created_at ASC""",
                (EXECUTING_COLUMN,),
            ).fetchall()
        return [self._row_to_card(r) for r in rows]

    def recover_interrupted(self, error: str = "Interrupted by restart") -> int:
        """Mark cards stuck in 'running' (e.g. after a crash) as 'failed'. Returns the count."""
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE kanban_cards
                   SET status = 'failed', error = ?, updated_at = ?, completed_at = ?
                   WHERE status = 'running'""",
                (error, now, now),
            )
            count = cursor.rowcount
        return count

    # ─── Row mapping ───────────────────────────────────────────────

    def _row_to_card(self, row: sqlite3.Row) -> KanbanCard:
        return KanbanCard(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            column=row["column"],
            priority=row["priority"],
            tags=json.loads(row["tags"] or "[]"),
            status=row["status"],
            result=json.loads(row["result"] or "{}"),
            execution_session_id=row["execution_session_id"] or "",
            error=row["error"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            executed_at=row["executed_at"] or "",
            completed_at=row["completed_at"] or "",
            auto_advance=bool(row["auto_advance"]),
            max_iterations=row["max_iterations"],
            executor=row["executor"] or "native",
            workdir=row["workdir"] or "",
            mandate=json.loads(row["mandate"] or "{}"),
            max_minutes=float(row["max_minutes"] if row["max_minutes"] is not None else 30.0),
            external_run_id=row["external_run_id"] or "",
        )
