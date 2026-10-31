"""
Database Tool -- SQLite query execution for the Alita agent.

Database paths are confined to the workspace. ``query`` opens the database
read-only, ``execute`` runs a single mutating statement, ``schema`` lists
tables. ATTACH is denied so statements cannot reach files outside the
workspace. All sqlite work runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .file_ops import resolve_within

_MAX_ROWS = 500
_MAX_CELL = 500  # chars per cell value to avoid huge context dumps
_TIMEOUT = 10.0


def _authorizer(action: int, *_args: Any) -> int:
    """Deny ATTACH/DETACH so SQL cannot open databases outside the workspace."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class DatabaseTool:
    """Execute SQL against SQLite databases in the workspace."""

    definition = {
        "name": "database",
        "description": (
            "Execute SQL against SQLite database files inside the workspace. "
            "Use action='query' for read-only SELECT/PRAGMA reads (the database is opened "
            "read-only), 'execute' for one INSERT/UPDATE/DELETE/CREATE/DROP statement, "
            "'schema' to list tables. Paths are relative to the workspace root."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["query", "execute", "schema"],
                    "description": "Operation type.",
                },
                "db_path": {
                    "type": "string",
                    "description": "Path to the SQLite .db file (relative to workspace).",
                },
                "sql": {
                    "type": "string",
                    "description": "SQL statement. Required for 'query' and 'execute'.",
                    "default": "",
                },
                "params": {
                    "type": "array",
                    "items": {},
                    "description": "Positional parameters for parameterized queries.",
                    "default": [],
                },
            },
            "required": ["action", "db_path"],
        },
    }

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    async def execute(
        self,
        action: str = "",
        db_path: str = "",
        sql: str = "",
        params: Optional[Union[List[Any], Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run a database action; unexpected kwargs are ignored."""
        if not db_path:
            return {"success": False, "error": "'db_path' is required."}
        try:
            full_path = resolve_within(Path(self.workspace), db_path)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if full_path.is_dir():
            return {"success": False, "error": f"{db_path} is a directory"}
        bound: Union[Sequence[Any], Dict[str, Any]] = (
            params if isinstance(params, dict) else list(params or [])
        )

        if action == "schema":
            if not full_path.exists():
                return {"success": False, "error": f"Database not found: {db_path}"}
            return await asyncio.to_thread(self._schema, str(full_path))
        if action == "query":
            if not sql:
                return {"success": False, "error": "'sql' is required for action='query'."}
            if not full_path.exists():
                return {"success": False, "error": f"Database not found: {db_path}"}
            return await asyncio.to_thread(self._query, str(full_path), sql, bound)
        if action == "execute":
            if not sql:
                return {"success": False, "error": "'sql' is required for action='execute'."}
            full_path.parent.mkdir(parents=True, exist_ok=True)
            return await asyncio.to_thread(self._execute_sql, str(full_path), sql, bound)
        return {"success": False, "error": f"Unknown action '{action}'. Use query/execute/schema."}

    # ── Synchronous helpers (run in a worker thread) ─────────────────────────

    @staticmethod
    def _connect(db_path: str, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT)
        else:
            conn = sqlite3.connect(db_path, timeout=_TIMEOUT)
        conn.set_authorizer(_authorizer)
        return conn

    def _schema(self, db_path: str) -> Dict[str, Any]:
        try:
            with closing(self._connect(db_path, read_only=True)) as conn:
                rows = conn.execute(
                    "SELECT name, type, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            tables = [{"name": r[0], "type": r[1], "schema": r[2]} for r in rows]
            return {"success": True, "tables": tables, "count": len(tables)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    def _query(self, db_path: str, sql: str, params: Any) -> Dict[str, Any]:
        try:
            with closing(self._connect(db_path, read_only=True)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                raw_rows = cur.fetchmany(_MAX_ROWS + 1)
                columns = [d[0] for d in (cur.description or [])]
            truncated = len(raw_rows) > _MAX_ROWS
            rows = [
                {k: self._truncate(v) for k, v in dict(r).items()}
                for r in raw_rows[:_MAX_ROWS]
            ]
            return {
                "success": True,
                "columns": columns,
                "rows": rows,
                "count": len(rows),
                "truncated": truncated,
            }
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            if "readonly" in msg.lower():
                msg += " (action='query' is read-only; use action='execute' for writes)"
            return {"success": False, "error": msg}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    def _execute_sql(self, db_path: str, sql: str, params: Any) -> Dict[str, Any]:
        try:
            with closing(self._connect(db_path)) as conn:
                cur = conn.execute(sql, params)
                conn.commit()
                return {
                    "success": True,
                    "rowcount": cur.rowcount,
                    "lastrowid": cur.lastrowid,
                }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    @staticmethod
    def _truncate(value: Any) -> Any:
        if isinstance(value, str) and len(value) > _MAX_CELL:
            return value[:_MAX_CELL] + "..."
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        return value
