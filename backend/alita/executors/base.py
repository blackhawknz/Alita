"""
Executor protocol.

An executor is anything that can take a task, run it, stream structured
events back and be interrupted or closed. :class:`NativeExecutor` adapts the
existing sub-agent runtime; later executors adapt external CLIs.

The protocol deliberately says nothing about how the work is performed — only
that a handle identifies each run, and that follow-up messages are streamed as
:class:`ExecutorEvent` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Protocol

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from .mandate import Mandate


@dataclass
class ExecutorEvent:
    """One event emitted by an executor run."""

    kind: str  # "text" | "tool" | "status" | "report" | "error" | "done"
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    tokens: Dict[str, int] = field(default_factory=dict)


class Executor(Protocol):
    """Structural interface implemented by every executor backend."""

    name: str

    async def start(
        self,
        task: str,
        *,
        mandate: "Mandate | None" = None,
        workdir: Path | None = None,
    ) -> str:
        """Start a task and return an opaque handle."""
        ...

    def send(self, handle: str, message: str) -> AsyncIterator[ExecutorEvent]:
        """Send a follow-up message; implementations are async generators."""
        ...

    async def interrupt(self, handle: str) -> None:
        """Ask the run to stop as soon as possible."""
        ...

    async def close(self, handle: str) -> None:
        """Release the handle and any resources it owns."""
        ...
