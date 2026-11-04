"""
Native executor — adapts the existing :class:`SubAgentManager`.

The native path is fire-and-forget: ``delegate()`` is not streaming, so the
first :meth:`NativeExecutor.send` waits for the delegated run to finish and
emits a single ``done`` event carrying the final response. A second ``send``
returns an ``error`` event; real resumption is provided by
``SubAgentManager.resume`` and the ``answer_subagent`` tool.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, Optional

from .base import ExecutorEvent
from ..tools.subagent import SubAgentManager, SubAgentResult

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from .mandate import Mandate

logger = logging.getLogger(__name__)


@dataclass
class _NativeRun:
    """Bookkeeping for one delegated native run."""

    task: Optional[asyncio.Task]
    child: Any = None
    result: Optional[SubAgentResult] = None
    error: Optional[str] = None
    sends: int = 0
    interrupt_requested: bool = False


class NativeExecutor:
    """Run delegated work on Alita's own sub-agent runtime."""

    name = "native"

    def __init__(self, manager: SubAgentManager):
        self.manager = manager
        self._runs: Dict[str, _NativeRun] = {}

    async def start(
        self,
        task: str,
        *,
        mandate: "Mandate | None" = None,
        workdir: Path | None = None,
    ) -> str:
        """Schedule ``task`` on the sub-agent manager and return its handle."""
        handle = f"native-{uuid.uuid4().hex[:12]}"
        run = _NativeRun(task=None)
        self._runs[handle] = run

        def _on_child(child: Any) -> None:
            run.child = child
            if run.interrupt_requested:
                child.interrupt_requested = True

        run.task = asyncio.create_task(
            self._run(run, task, _on_child),
            name=f"native-executor-{handle}",
        )
        if workdir is not None:
            logger.debug("NativeExecutor ignores workdir=%s (runs in the workspace)", workdir)
        if mandate is not None:
            logger.debug("NativeExecutor ignores mandate for handle %s", handle)
        return handle

    async def _run(
        self,
        run: _NativeRun,
        task: str,
        on_child: Callable[[Any], None],
    ) -> None:
        try:
            run.result = await self.manager.delegate(task, on_child=on_child)
        except asyncio.CancelledError:
            run.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("NativeExecutor run failed: %s", exc)
            run.error = str(exc)

    async def send(self, handle: str, message: str) -> AsyncIterator[ExecutorEvent]:
        """Return the final result once; native runs are single-shot."""
        run = self._runs.get(handle)
        if run is None:
            yield ExecutorEvent(kind="error", text=f"Unknown native handle: {handle}")
            return
        if run.sends > 0:
            yield ExecutorEvent(
                kind="error",
                text=(
                    "Native executors are single-shot per handle; use "
                    "SubAgentManager.resume() or answer_subagent to continue."
                ),
            )
            return
        run.sends += 1

        if run.task is not None:
            try:
                await asyncio.shield(run.task)
            except asyncio.CancelledError:
                yield ExecutorEvent(kind="error", text="Native execution was cancelled")
                return
            except Exception as exc:  # noqa: BLE001
                yield ExecutorEvent(kind="error", text=str(exc))
                return

        result = run.result
        if result is None:
            yield ExecutorEvent(kind="error", text=run.error or "Native execution produced no result")
            return

        data: Dict[str, Any] = {"result": result.to_dict()}
        if result.error:
            yield ExecutorEvent(kind="error", text=result.error, data=data)
            return
        yield ExecutorEvent(kind="done", text=result.final_response, data=data)

    async def interrupt(self, handle: str) -> None:
        """Set the interrupt flag on this handle's child session."""
        run = self._runs.get(handle)
        if run is None:
            return
        run.interrupt_requested = True
        if run.child is not None:
            run.child.interrupt_requested = True

    async def close(self, handle: str) -> None:
        """Forget a handle, cancelling its run if it is still active."""
        run = self._runs.pop(handle, None)
        if run is None or run.task is None or run.task.done():
            return
        run.task.cancel()
        try:
            await run.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def has_handle(self, handle: str) -> bool:
        return handle in self._runs
