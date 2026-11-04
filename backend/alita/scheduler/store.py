"""
Scheduler Store — persistent scheduled jobs and run history.

Shares ``state.db`` with ``SQLiteStore`` (conversation logs) and ``KanbanStore``
(board). Two tables:

    scheduled_jobs    — job definition (cron expr, prompt, agent, enabled, …)
    job_runs          — per-execution records with status, response, traces

A run's ``tool_calls`` column stores a JSON array of every tool invocation
made during the run (name, params, result, success) so users can replay and
audit scheduled executions in the Jobs UI.
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


@dataclass
class ScheduledJob:
    id: str
    name: str
    description: str
    prompt: str
    cron_expr: str
    enabled: bool = True
    last_run_at: str = ""
    next_run_at: str = ""
    run_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    timezone: str = ""
    max_runs_per_week: int = 0
    max_cost_per_run: float = 0.0
    paused_reason: str = ""
    paused_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
            "max_runs_per_week": self.max_runs_per_week,
            "max_cost_per_run": self.max_cost_per_run,
            "paused_reason": self.paused_reason,
            "paused_at": self.paused_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class JobRun:
    id: str
    job_id: str
    status: str  # running | completed | failed
    prompt: str
    response: str = ""
    error: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    cost: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "status": self.status,
            "prompt": self.prompt,
            "response": self.response,
            "error": self.error,
            "tool_calls": self.tool_calls,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "cost": self.cost,
        }


class SchedulerStore:
    """SQLite-backed store for scheduled jobs and their run history."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".alita" / "state.db")
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success, and always close it."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def set_next_run(self, job_id: str, next_run_at: str) -> None:
        with self._get_conn() as conn:
            conn.execute("UPDATE scheduled_jobs SET next_run_at = ? WHERE id = ?", (next_run_at, job_id))

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL,
                    cron_expr TEXT NOT NULL,
                    timezone TEXT DEFAULT '',
                    enabled BOOLEAN DEFAULT 1,
                    last_run_at TEXT DEFAULT '',
                    next_run_at TEXT DEFAULT '',
                    run_count INTEGER DEFAULT 0,
                    max_runs_per_week INTEGER DEFAULT 0,
                    max_cost_per_run REAL DEFAULT 0.0,
                    paused_reason TEXT DEFAULT '',
                    paused_at TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS job_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    prompt TEXT NOT NULL DEFAULT '',
                    response TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    tool_calls TEXT DEFAULT '[]',
                    started_at TEXT DEFAULT '',
                    completed_at TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0.0,
                    FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id);
                CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);
                CREATE INDEX IF NOT EXISTS idx_job_runs_job_started ON job_runs(job_id, started_at);
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_jobs)")}
            for column, ddl in [
                ("timezone", "TEXT DEFAULT ''"),
                ("max_runs_per_week", "INTEGER DEFAULT 0"),
                ("max_cost_per_run", "REAL DEFAULT 0.0"),
                ("paused_reason", "TEXT DEFAULT ''"),
                ("paused_at", "TEXT DEFAULT ''"),
            ]:
                if column not in columns:
                    conn.execute(f"ALTER TABLE scheduled_jobs ADD COLUMN {column} {ddl}")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_runs)")}
            if "cost" not in run_columns:
                conn.execute("ALTER TABLE job_runs ADD COLUMN cost REAL DEFAULT 0.0")

    # ─── Jobs ──────────────────────────────────────────────────────

    def create_job(self, name: str, prompt: str, cron_expr: str,
                   description: str = "", enabled: bool = True, timezone: str = "",
                   max_runs_per_week: int = 0, max_cost_per_run: float = 0.0) -> ScheduledJob:
        job_id = uuid.uuid4().hex[:12]
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (id, name, description, prompt, cron_expr, timezone, enabled,
                    max_runs_per_week, max_cost_per_run, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, name, description, prompt, cron_expr, timezone, enabled,
                 int(max_runs_per_week or 0), float(max_cost_per_run or 0.0), now, now),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self) -> List[ScheduledJob]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update_job(self, job_id: str, **fields) -> Optional[ScheduledJob]:
        allowed = {
            "name", "description", "prompt", "cron_expr", "enabled", "timezone",
            "max_runs_per_week", "max_cost_per_run", "paused_reason", "paused_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_job(job_id)
        current = self.get_job(job_id)
        # Raising a budget is an explicit signal to clear a budget pause.
        if current is not None and "paused_reason" not in updates:
            raised = (
                ("max_runs_per_week" in updates
                 and int(updates["max_runs_per_week"] or 0) > int(current.max_runs_per_week or 0))
                or ("max_cost_per_run" in updates
                    and float(updates["max_cost_per_run"] or 0.0) > float(current.max_cost_per_run or 0.0))
            )
            if raised and current.paused_reason:
                updates["paused_reason"] = ""
                updates["paused_at"] = ""
        updates["updated_at"] = datetime.utcnow().isoformat()
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        if "max_runs_per_week" in updates:
            updates["max_runs_per_week"] = int(updates["max_runs_per_week"] or 0)
        if "max_cost_per_run" in updates:
            updates["max_cost_per_run"] = float(updates["max_cost_per_run"] or 0.0)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [job_id]
        with self._get_conn() as conn:
            conn.execute(f"UPDATE scheduled_jobs SET {set_clause} WHERE id = ?", values)
        return self.get_job(job_id)

    def pause_job(self, job_id: str, reason: str) -> bool:
        """Pause a job once. Returns True only when this call changed the state."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """UPDATE scheduled_jobs
                   SET paused_reason = ?, paused_at = ?, updated_at = ?
                   WHERE id = ? AND COALESCE(paused_reason, '') = ''""",
                (str(reason or ""), now, now, job_id),
            )
            return cursor.rowcount > 0

    def resume_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Clear a pause, making the job eligible to run again."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE scheduled_jobs
                   SET paused_reason = '', paused_at = '', updated_at = ?
                   WHERE id = ?""",
                (now, job_id),
            )
        return self.get_job(job_id)

    def delete_job(self, job_id: str) -> bool:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM job_runs WHERE job_id = ?", (job_id,))
            cursor = conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))
            deleted = cursor.rowcount > 0
        return deleted

    def mark_job_run(self, job_id: str, next_run_at: str = "") -> None:
        """Update last_run_at, increment run_count, and optionally set next_run_at."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE scheduled_jobs
                   SET last_run_at = ?, run_count = run_count + 1,
                       next_run_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, next_run_at, now, job_id),
            )

    def _row_to_job(self, row: sqlite3.Row) -> ScheduledJob:
        return ScheduledJob(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            prompt=row["prompt"],
            cron_expr=row["cron_expr"],
            enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"] or "",
            next_run_at=row["next_run_at"] or "",
            run_count=row["run_count"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            timezone=row["timezone"] or "",
            max_runs_per_week=int(row["max_runs_per_week"] or 0),
            max_cost_per_run=float(row["max_cost_per_run"] or 0.0),
            paused_reason=row["paused_reason"] or "",
            paused_at=row["paused_at"] or "",
        )

    # ─── Runs ──────────────────────────────────────────────────────

    def create_run(self, job_id: str, prompt: str) -> JobRun:
        run_id = uuid.uuid4().hex[:12]
        now = datetime.utcnow().isoformat()
        run = JobRun(
            id=run_id, job_id=job_id, status="running", prompt=prompt,
            started_at=now,
        )
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO job_runs
                   (id, job_id, status, prompt, tool_calls, started_at)
                   VALUES (?, ?, 'running', ?, '[]', ?)""",
                (run_id, job_id, prompt, now),
            )
        return run

    def finish_run(self, run_id: str, status: str, response: str = "",
                   error: str = "", tool_calls: Optional[List[Dict]] = None,
                   duration_ms: int = 0, cost: float = 0.0) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE job_runs
                   SET status = ?, response = ?, error = ?,
                       tool_calls = ?, completed_at = ?, duration_ms = ?, cost = ?
                   WHERE id = ?""",
                (status, response, error,
                 json.dumps(tool_calls or []), now, duration_ms, float(cost or 0.0), run_id),
            )

    def count_runs_since(self, job_id: str, since_iso: str) -> int:
        """Count real executions since ``since_iso`` (away-mode skips do not count)."""
        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM job_runs
                   WHERE job_id = ? AND started_at >= ? AND status != 'skipped'""",
                (job_id, since_iso),
            ).fetchone()
        return int(row["n"] if row else 0)

    def usage_events_for_job_since(self, job_id: str, since_sql: str) -> List[Dict[str, Any]]:
        """Token usage recorded for a job run since a SQL-friendly timestamp."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT model, prompt_tokens, completion_tokens, cached_tokens
                       FROM usage_events
                       WHERE job_id = ? AND created_at >= ?""",
                    (job_id, since_sql),
                ).fetchall()
        except sqlite3.OperationalError:
            # A scheduler-only test database may not have the usage table.
            return []
        return [dict(row) for row in rows]

    def list_runs(self, job_id: Optional[str] = None, limit: int = 50) -> List[JobRun]:
        with self._get_conn() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT * FROM job_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
                    (job_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def get_run(self, run_id: str) -> Optional[JobRun]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def _row_to_run(self, row: sqlite3.Row) -> JobRun:
        return JobRun(
            id=row["id"],
            job_id=row["job_id"],
            status=row["status"],
            prompt=row["prompt"],
            response=row["response"] or "",
            error=row["error"] or "",
            tool_calls=json.loads(row["tool_calls"] or "[]"),
            started_at=row["started_at"] or "",
            completed_at=row["completed_at"] or "",
            duration_ms=row["duration_ms"] or 0,
            cost=float(row["cost"] or 0.0),
        )