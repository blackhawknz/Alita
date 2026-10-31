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
from typing import Any, Dict, List, Optional

from ..core.runtime import current_session
from .registry import Tool

logger = logging.getLogger(__name__)

SUBAGENT_INSTRUCTIONS = (
    "You are a sub-agent working on one delegated task for a parent agent. Work autonomously — you cannot ask "
    "questions. Use tools as needed, then reply with a complete, self-contained report of your findings or "
    "results (the parent sees only your final message). Be factual and include specifics (paths, URLs, numbers)."
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
        }


class SubAgentManager:
    """Runs delegated tasks as isolated child sessions."""

    def __init__(self, parent: Any, default_max_iterations: int = 25):
        self.parent = parent
        self.default_max_iterations = default_max_iterations
        self._active: Dict[str, Any] = {}

    async def delegate(
        self,
        task: str,
        *,
        max_iterations: Optional[int] = None,
        system_message: Optional[str] = None,
        kind: str = "subagent",
        title: str = "",
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
            return SubAgentResult(task_id, task, "", 0, 0, error=f"could not create sub-agent: {exc}")
        self._active[task_id] = child
        instructions = SUBAGENT_INSTRUCTIONS + (f"\n\n{system_message}" if system_message else "")
        try:
            result = await self.parent.run_conversation(
                task, system_message=instructions, session=child, max_iterations=cap,
            )
            summary = [
                {"tool": t.get("tool_name"), "success": t.get("success", True)}
                for t in result.get("tool_trace", [])
            ]
            return SubAgentResult(
                task_id=task_id,
                task=task,
                final_response=result.get("final_response", "") or "",
                tool_call_count=result.get("tool_call_count", 0),
                api_call_count=result.get("api_call_count", 0),
                tool_summary=summary[:50],
                interrupted=result.get("interrupted", False),
                error=result.get("error"),
                session_id=child.id,
            )
        except asyncio.CancelledError:
            child.interrupt_requested = True
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sub-agent %s failed: %s", task_id, exc)
            return SubAgentResult(task_id, task, "", 0, 0, error=str(exc), session_id=child.id)
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
        "run them in parallel. The sub-agent cannot ask questions, so include ALL needed context in `task`. "
        "Returns the sub-agent's final report."
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
