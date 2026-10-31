"""
Sessions — per-conversation runtime state.

Every conversation (chat, scheduled job run, sub-agent task, kanban card)
is a :class:`Session` with its own message history, todo list, interrupt
flag and lock, so any number of sessions can run concurrently on one agent
without interfering.

:class:`SessionManager` loads sessions lazily from SQLite (reconstructing the
exact OpenAI-format history after the last compaction point), caches them,
and persists every appended message immediately.

:class:`TurnRun` buffers the events of an in-flight turn so clients can
disconnect and re-attach (e.g. switch sessions while one is still working).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncGenerator, Dict, List, Optional

from ..memory.sqlite_store import SQLiteStore
from ..tools.todo_tool import TodoStore

logger = logging.getLogger(__name__)

_CACHE_SIZE = 64


class TurnRun:
    """Event buffer for one in-flight agent turn (supports re-attaching)."""

    def __init__(self, session_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.events: List[Dict[str, Any]] = []
        self.done = False
        self.task: Optional[asyncio.Task] = None
        self.started_at = time.time()
        self._cond = asyncio.Condition()

    async def emit(self, event: Dict[str, Any]) -> None:
        async with self._cond:
            event = {**event, "seq": len(self.events)}
            self.events.append(event)
            self._cond.notify_all()

    async def finish(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def subscribe(self, after: int = 0) -> AsyncGenerator[Dict[str, Any], None]:
        idx = max(0, after)
        while True:
            async with self._cond:
                await self._cond.wait_for(lambda: len(self.events) > idx or self.done)
                batch = self.events[idx:]
                idx = len(self.events)
                finished = self.done
            for ev in batch:
                yield ev
            if finished and idx >= len(self.events):
                return


class Session:
    """Runtime state of one conversation."""

    def __init__(
        self,
        session_id: str,
        kind: str = "chat",
        title: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        context_summary: str = "",
        compacted_upto: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        parent_id: Optional[str] = None,
    ):
        self.id = session_id
        self.kind = kind
        self.title = title or ""
        self.messages: List[Dict[str, Any]] = messages or []
        self.context_summary = context_summary or ""
        self.compacted_upto = compacted_upto or 0
        self.metadata: Dict[str, Any] = metadata or {}
        self.parent_id = parent_id
        self.todos = TodoStore()
        if isinstance(self.metadata.get("todos"), list):
            try:
                self.todos.from_dict_list(self.metadata["todos"])
            except Exception:  # noqa: BLE001
                pass
        self.lock = asyncio.Lock()
        self.turn: Optional[TurnRun] = None
        self.interrupt_requested = False
        self.pending_approvals: List[str] = []
        self.last_usage: Dict[str, Any] = {}
        self.headless = kind != "chat"

    @property
    def depth(self) -> int:
        return int(self.metadata.get("depth", 0) or 0)

    @property
    def is_running(self) -> bool:
        return self.turn is not None and not self.turn.done

    def user_turn_count(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "parent_id": self.parent_id,
            "is_running": self.is_running,
            "turn_id": self.turn.id if self.is_running else None,
            "compacted": bool(self.context_summary),
            "todos": self.todos.to_dict_list(),
            "usage": self.last_usage,
        }


def repair_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Make a message list valid for chat-completion APIs.

    * every assistant ``tool_calls`` entry gets exactly one tool result
      (a synthetic "interrupted" result is added if missing)
    * tool messages that don't answer a preceding call are dropped
    * empty assistant messages without tool calls are dropped
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            i += 1  # orphan: not preceded by its assistant message
            continue
        if role == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id") for tc in m["tool_calls"]]
            results: Dict[str, Dict[str, Any]] = {}
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                tid = messages[j].get("tool_call_id")
                if tid in ids and tid not in results:
                    results[tid] = messages[j]
                j += 1
            out.append(m)
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid in results:
                    out.append(results[tid])
                else:
                    out.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "name": (tc.get("function") or {}).get("name", ""),
                        "content": json.dumps({"success": False, "error": "No result recorded (the turn was interrupted)."}),
                        "_synthetic": True,
                    })
            i = j
            continue
        if role == "assistant" and not m.get("content") and not m.get("tool_calls"):
            i += 1
            continue
        out.append(m)
        i += 1
    return out


class SessionManager:
    """Creates, loads, caches and persists sessions."""

    def __init__(self, db: SQLiteStore):
        self.db = db
        self._cache: "OrderedDict[str, Session]" = OrderedDict()

    # ─── Lookup ───────────────────────────────────────────────────

    def get(self, session_id: str) -> Optional[Session]:
        if not session_id:
            return None
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
            return self._cache[session_id]
        row = self.db.get_session(session_id)
        if not row:
            return None
        compacted_upto = int(row.get("compacted_upto") or 0)
        messages = repair_history(self.db.load_messages(session_id, after_id=compacted_upto))
        sess = Session(
            session_id,
            kind=row.get("kind") or "chat",
            title=row.get("title") or "",
            messages=messages,
            context_summary=row.get("context_summary") or "",
            compacted_upto=compacted_upto,
            metadata=row.get("metadata") or {},
            parent_id=row.get("parent_id"),
        )
        self._remember(sess)
        return sess

    def _remember(self, sess: Session) -> None:
        self._cache[sess.id] = sess
        self._cache.move_to_end(sess.id)
        while len(self._cache) > _CACHE_SIZE:
            oldest_id, oldest = next(iter(self._cache.items()))
            if oldest.is_running:
                self._cache.move_to_end(oldest_id)
                if all(s.is_running for s in self._cache.values()):
                    break
                continue
            self._cache.pop(oldest_id)

    def create(
        self,
        kind: str = "chat",
        title: str = "",
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        sid = self.db.create_session(session_id, kind=kind, title=title, parent_id=parent_id, metadata=metadata)
        sess = Session(sid, kind=kind, title=title, metadata=dict(metadata or {}), parent_id=parent_id)
        self._remember(sess)
        return sess

    def get_or_create(self, session_id: Optional[str], kind: str = "chat") -> Session:
        sess = self.get(session_id) if session_id else None
        return sess or self.create(kind=kind, session_id=session_id or None)

    def running(self) -> List[Session]:
        return [s for s in self._cache.values() if s.is_running]

    # ─── Mutation ─────────────────────────────────────────────────

    def append(self, sess: Session, message: Dict[str, Any], persist: bool = True) -> Dict[str, Any]:
        if persist:
            message["_id"] = self.db.append_message(sess.id, message)
        sess.messages.append(message)
        return message

    def update_message(self, message: Dict[str, Any]) -> None:
        if message.get("_id"):
            self.db.update_message(message["_id"], message)

    def save_todos(self, sess: Session) -> None:
        sess.metadata["todos"] = sess.todos.to_dict_list()
        self.db.update_session(sess.id, metadata=sess.metadata)

    def set_title(self, sess: Session, title: str) -> None:
        sess.title = title
        self.db.update_session(sess.id, title=title)

    def set_compaction(self, sess: Session, summary: str, upto_id: int, kept: List[Dict[str, Any]]) -> None:
        sess.context_summary = summary
        sess.compacted_upto = upto_id
        sess.messages = kept
        self.db.update_session(sess.id, context_summary=summary, compacted_upto=upto_id)

    def truncate_from(self, sess: Session, message_id: int) -> None:
        """Delete a message and everything after it (edit / regenerate)."""
        self.db.delete_messages_from(sess.id, message_id)
        sess.messages = [m for m in sess.messages if not m.get("_id") or m["_id"] < message_id]
        if sess.compacted_upto and message_id <= sess.compacted_upto:
            # Editing before the compaction point: drop the summary and reload.
            self.db.update_session(sess.id, context_summary="", compacted_upto=0)
            sess.context_summary = ""
            sess.compacted_upto = 0
            sess.messages = repair_history(self.db.load_messages(sess.id))

    def delete(self, session_id: str) -> bool:
        sess = self._cache.get(session_id)
        if sess and sess.is_running:
            raise RuntimeError("Session is running; stop it first.")
        self._cache.pop(session_id, None)
        return self.db.delete_session(session_id)

    def forget(self, session_id: str) -> None:
        sess = self._cache.get(session_id)
        if sess and not sess.is_running:
            self._cache.pop(session_id, None)
