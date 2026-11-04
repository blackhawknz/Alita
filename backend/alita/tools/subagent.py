"""
Sub-agent delegation.

A sub-agent is simply a child :class:`~alita.core.session.Session`
(``kind="subagent"``) run on the same agent runtime. It shares tools, memory
and skills, but has its own isolated history, todo list and iteration
budget. Sub-agents run *headless*: calls that need user approval are denied
(unless ``ALITA_HEADLESS_APPROVAL=allow``), and nesting is limited by
``ALITA_SUBAGENT_MAX_DEPTH``.

The parent's interrupt propagates to running children.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..core.runtime import current_session
from ..executors.report import REPORT_KINDS, Report
from .registry import Tool

logger = logging.getLogger(__name__)

SUBAGENT_INSTRUCTIONS = (
    "You are a sub-agent working on one delegated task for a parent agent. Work autonomously and use tools as "
    "needed. The parent sees only your final message, so include specifics (paths, URLs, numbers). End every "
    "final message with a fenced ```report block containing JSON: "
    '{"kind": "...", "summary": "...", "evidence": ["..."], "question": "..."}. '
    f"kind must be one of: {', '.join(REPORT_KINDS)}. Use progress for an interim update, ready when the work "
    "is complete, blocked/failed when you are stuck, and waiting with a question when you need a decision — the "
    "parent can answer via answer_subagent and you will resume in this same session."
)


@dataclass
class SubAgentResult:
    """Outcome of a delegated sub-task."""

    task_id: str
    task: str
    final_response: str
    tool_call_count: int
    api_call_count: int
    tool_summary: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    error: Optional[str] = None
    session_id: Optional[str] = None
    report: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.error is None and not self.interrupted,
            "task_id": self.task_id,
            "task": self.task,
            "response": self.final_response,
            "tool_call_count": self.tool_call_count,
            "api_call_count": self.api_call_count,
            "tool_summary": self.tool_summary,
            "interrupted": self.interrupted,
            "error": self.error,
            "session_id": self.session_id,
            "report": self.report,
        }


class SubAgentManager:
    """Runs delegated tasks as isolated child sessions."""

    def __init__(self, parent: Any, default_max_iterations: int = 25):
        self.parent = parent
        self.default_max_iterations = default_max_iterations
        self._active: Dict[str, Any] = {}

    @staticmethod
    def _report_dict(final_response: str, error: Optional[str]) -> Dict[str, Any]:
        if error and not final_response:
            return Report(kind="failed", summary=error).to_dict()
        return Report.parse(final_response).to_dict()

    def _result_from_run(
        self,
        task_id: str,
        task: str,
        child: Any,
        result: Dict[str, Any],
    ) -> SubAgentResult:
        final_response = result.get("final_response", "") or ""
        error = result.get("error")
        summary = [
            {"tool": t.get("tool_name"), "success": t.get("success", True)}
            for t in result.get("tool_trace", [])
        ]
        return SubAgentResult(
            task_id=task_id,
            task=task,
            final_response=final_response,
            tool_call_count=result.get("tool_call_count", 0),
            api_call_count=result.get("api_call_count", 0),
            tool_summary=summary[:50],
            interrupted=result.get("interrupted", False),
            error=error,
            session_id=child.id,
            report=self._report_dict(final_response, error),
        )

    async def delegate(
        self,
        task: str,
        *,
        max_iterations: Optional[int] = None,
        system_message: Optional[str] = None,
        kind: str = "subagent",
        title: str = "",
        on_child: Optional[Callable[[Any], None]] = None,
    ) -> SubAgentResult:
        task_id = uuid.uuid4().hex[:8]
        cap = max_iterations if max_iterations and max_iterations > 0 else self.default_max_iterations
        cap = min(cap, max(self.default_max_iterations * 2, 10))
        parent_sess = current_session.get()
        depth = (parent_sess.depth + 1) if parent_sess is not None else 1
        try:
            child = self.parent.sessions.create(
                kind=kind,
                title=title or f"Sub-task: {task[:60]}",
                parent_id=parent_sess.id if parent_sess is not None else None,
                metadata={"depth": depth},
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"could not create sub-agent: {exc}"
            return SubAgentResult(
                task_id, task, "", 0, 0, error=msg,
                report=Report(kind="failed", summary=msg).to_dict(),
            )
        self._active[task_id] = child
        if on_child is not None:
            try:
                on_child(child)
            except Exception:  # noqa: BLE001
                logger.debug("Sub-agent on_child callback failed", exc_info=True)
        instructions = SUBAGENT_INSTRUCTIONS + (f"\n\n{system_message}" if system_message else "")
        try:
            result = await self.parent.run_conversation(
                task, system_message=instructions, session=child, max_iterations=cap,
            )
            return self._result_from_run(task_id, task, child, result)
        except asyncio.CancelledError:
            child.interrupt_requested = True
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sub-agent %s failed: %s", task_id, exc)
            return SubAgentResult(
                task_id, task, "", 0, 0, error=str(exc), session_id=child.id,
                report=Report(kind="failed", summary=str(exc)).to_dict(),
            )
        finally:
            self._active.pop(task_id, None)
            try:
                self.parent.sessions.forget(child.id)
            except Exception:  # noqa: BLE001
                pass

    async def resume(
        self,
        session_id: str,
        message: str,
        *,
        max_iterations: Optional[int] = None,
        system_message: Optional[str] = None,
    ) -> SubAgentResult:
        """Continue an existing sub-agent session with a new message.

        The child may have been evicted from the in-memory cache (``delegate``
        calls ``forget`` in ``finally``); ``SessionManager.get`` reloads it
        lazily from SQLite, so no new session is created.
        """
        task_id = uuid.uuid4().hex[:8]
        child = self.parent.sessions.get(session_id) if self.parent is not None else None
        if child is None:
            msg = f"sub-agent session not found: {session_id}"
            return SubAgentResult(
                task_id, message, "", 0, 0, error=msg, session_id=session_id,
                report=Report(kind="failed", summary=msg).to_dict(),
            )

        cap = max_iterations if max_iterations and max_iterations > 0 else self.default_max_iterations
        cap = min(cap, max(self.default_max_iterations * 2, 10))
        self._active[task_id] = child
        instructions = SUBAGENT_INSTRUCTIONS + (f"\n\n{system_message}" if system_message else "")
        try:
            result = await self.parent.run_conversation(
                message, system_message=instructions, session=child, max_iterations=cap,
            )
            return self._result_from_run(task_id, message, child, result)
        except asyncio.CancelledError:
            child.interrupt_requested = True
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sub-agent resume %s failed: %s", session_id, exc)
            return SubAgentResult(
                task_id, message, "", 0, 0, error=str(exc), session_id=child.id,
                report=Report(kind="failed", summary=str(exc)).to_dict(),
            )
        finally:
            self._active.pop(task_id, None)
            try:
                self.parent.sessions.forget(child.id)
            except Exception:  # noqa: BLE001
                pass

    def active_count(self) -> int:
        return len(self._active)

    def interrupt_all(self) -> None:
        for child in list(self._active.values()):
            child.interrupt_requested = True


DELEGATE_TOOL_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Delegate a self-contained sub-task to a sub-agent with its own fresh context (same tools, memory and "
        "skills). Good for research, analysis or independent chunks of work; issue several calls in one step to "
        "run them in parallel. Include ALL needed context in `task`. The sub-agent returns a structured `report`; "
        "if its kind is `waiting`, answer its question with `answer_subagent` using the returned `session_id`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Complete, self-contained task description."},
            "max_iterations": {"type": "integer", "description": "Optional step cap (default 25)."},
        },
        "required": ["task"],
    },
}


def make_subagent_tool(manager: SubAgentManager) -> Tool:
    async def _handler(**kwargs: Any) -> Dict[str, Any]:
        task = kwargs.get("task") or ""
        if not task:
            return {"success": False, "error": "task is required"}
        cap = kwargs.get("max_iterations")
        try:
            cap_i = int(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap_i = None
        sr = await manager.delegate(task, max_iterations=cap_i)
        return sr.to_dict()

    return Tool(
        name="delegate_task",
        description="Delegate a self-contained sub-task to a sub-agent.",
        definition=DELEGATE_TOOL_SCHEMA,
        handler=_handler,
        tags=["planning", "subagent", "delegation"],
    )


ANSWER_SUBAGENT_TOOL_SCHEMA = {
    "name": "answer_subagent",
    "description": (
        "Answer a sub-agent that returned a `waiting` report, resuming the SAME session with your message. "
        "Use the `session_id` from `delegate_task`'s result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The sub-agent session to resume."},
            "message": {"type": "string", "description": "The answer or instruction to send."},
        },
        "required": ["session_id", "message"],
    },
}


def make_answer_subagent_tool(manager: SubAgentManager) -> Tool:
    async def _handler(**kwargs: Any) -> Dict[str, Any]:
        session_id = kwargs.get("session_id") or ""
        message = kwargs.get("message") or ""
        if not session_id:
            return {"success": False, "error": "session_id is required"}
        if not message:
            return {"success": False, "error": "message is required"}
        sr = await manager.resume(str(session_id), str(message))
        return sr.to_dict()

    return Tool(
        name="answer_subagent",
        description="Answer a waiting sub-agent and resume its session.",
        definition=ANSWER_SUBAGENT_TOOL_SCHEMA,
        handler=_handler,
        tags=["planning", "subagent", "delegation"],
    )
