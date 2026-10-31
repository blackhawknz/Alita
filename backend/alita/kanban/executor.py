"""
Kanban Executor -- background poller that auto-executes cards in the
'executing' column via the SubAgentManager.

Design:

* A single ``asyncio.Task`` runs ``_poll_loop()`` at 1-second intervals.
* Each tick fetches 'idle' cards in the 'executing' column (failed cards are
  never retried automatically) and dispatches up to the concurrency limit
  (``ALITA_KANBAN_MAX_CONCURRENT``, default 2). For each card it:
    1. Marks the card ``status=running`` so the next tick doesn't double-pick.
    2. Calls ``SubAgentManager.delegate(task=..., max_iterations=...)``.
    3. On success: writes the result + execution_session_id, sets
       ``status=succeeded`` and, if ``card.auto_advance``, moves it to 'review'.
    4. On failure: writes the error and sets ``status=failed``; the card stays
       in 'executing' so the user can retry/edit/abort.
* ``start()`` is idempotent and a no-op without a running event loop. On start,
  cards left 'running' by a previous process are marked failed.
* ``stop()`` cancels the poll loop and in-flight card tasks and awaits them.
* ``cancel_card()`` cancels the card's running task and marks it failed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Set

from .store import KanbanCard, KanbanStore

logger = logging.getLogger(__name__)

#: Poll interval in seconds.
POLL_INTERVAL = 1.0

#: Default number of cards executed concurrently.
DEFAULT_MAX_CONCURRENT = 2

CANCELLED_BY_USER = "Cancelled by user"
INTERRUPTED_BY_RESTART = "Interrupted by restart"


def _max_concurrent_from_env() -> int:
    try:
        return max(1, int(os.getenv("ALITA_KANBAN_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT


def _now() -> str:
    return datetime.utcnow().isoformat()


class KanbanExecutor:
    """
    Watches the 'executing' column and delegates cards to the SubAgentManager.

    Bound to the parent agent's ``SubAgentManager`` so it reuses the same
    shared resources (LLM, memory, skills, tools).
    """

    def __init__(self, store: KanbanStore, subagent_manager: Any,
                 max_concurrent: Optional[int] = None):
        self.store = store
        self.subagent_manager = subagent_manager
        self.max_concurrent = max(1, max_concurrent) if max_concurrent else _max_concurrent_from_env()
        self._task: Optional[asyncio.Task] = None
        self._in_flight: Set[str] = set()
        self._card_tasks: Dict[str, asyncio.Task] = {}
        self._cancelled: Set[str] = set()
        self._running = False

    # ─── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background poll loop. Idempotent; no-op without a running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # leave _running False so a later call (inside a loop) can start
        if (
            self._running
            and self._task is not None
            and not self._task.done()
            and self._task.get_loop() is loop
        ):
            return
        try:
            recovered = self.store.recover_interrupted(INTERRUPTED_BY_RESTART)
            if recovered:
                logger.warning("KanbanExecutor: marked %d interrupted card(s) as failed", recovered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KanbanExecutor: recovery of interrupted cards failed: %s", exc)
        self._running = True
        self._task = loop.create_task(self._poll_loop(), name="kanban-executor-poll")
        logger.info("KanbanExecutor started (max_concurrent=%d)", self.max_concurrent)

    async def stop(self) -> None:
        """Stop the poll loop, cancel in-flight card tasks and wait for them to settle."""
        self._running = False
        loop = asyncio.get_running_loop()
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            if task.get_loop() is loop:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        tasks = [t for t in self._card_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        same_loop = [t for t in tasks if t.get_loop() is loop]
        if same_loop:
            await asyncio.gather(*same_loop, return_exceptions=True)
        logger.info("KanbanExecutor stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    # ─── Poll loop ─────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("KanbanExecutor tick failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self) -> None:
        """Dispatch executable cards up to the concurrency limit."""
        available = self.max_concurrent - len(self._in_flight)
        if available <= 0:
            return
        loop = asyncio.get_running_loop()
        for card in self.store.get_executable_cards():
            if available <= 0:
                break
            if card.id in self._in_flight:
                continue
            # Mark as running immediately so the next tick doesn't double-pick.
            self.store.update_card(card.id, status="running", executed_at=_now(), error="")
            self._in_flight.add(card.id)
            task = loop.create_task(self._execute_card(card), name=f"kanban-card-{card.id}")
            self._card_tasks[card.id] = task
            task.add_done_callback(self._on_task_done)
            available -= 1

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        # Retrieve the outcome so asyncio never logs "exception was never retrieved".
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("KanbanExecutor: card task crashed: %s", exc)

    # ─── Single card execution ──────────────────────────────────────

    async def _execute_card(self, card: KanbanCard) -> None:
        """Delegate a single card to the sub-agent manager and write back the result."""
        logger.info("KanbanExecutor: executing card %s [%s]", card.id, card.title)
        try:
            result = await self.subagent_manager.delegate(
                task=card.description or card.title,
                max_iterations=card.max_iterations,
            )
            if card.id in self._cancelled:
                return  # cancelled while finishing; keep the 'Cancelled by user' state
            if result.error:
                self.store.update_card(
                    card.id,
                    status="failed",
                    error=result.error,
                    execution_session_id=result.task_id,
                    result=result.to_dict(),
                    completed_at=_now(),
                )
                logger.warning("KanbanExecutor: card %s failed: %s", card.id, result.error)
            else:
                self.store.update_card(
                    card.id,
                    status="succeeded",
                    result=result.to_dict(),
                    execution_session_id=result.task_id,
                    error="",
                    completed_at=_now(),
                )
                if card.auto_advance:
                    self.store.move_card(card.id, "review")
                logger.info("KanbanExecutor: card %s succeeded", card.id)
        except asyncio.CancelledError:
            error = CANCELLED_BY_USER if card.id in self._cancelled else "Execution cancelled (executor stopped)"
            try:
                self.store.update_card(card.id, status="failed", error=error, completed_at=_now())
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            self.store.update_card(
                card.id, status="failed", error=str(exc) or type(exc).__name__,
                completed_at=_now(),
            )
            logger.warning("KanbanExecutor: card %s raised: %s", card.id, exc)
        finally:
            self._in_flight.discard(card.id)
            self._card_tasks.pop(card.id, None)
            self._cancelled.discard(card.id)

    # ─── Manual control ─────────────────────────────────────────────

    def retry_card(self, card_id: str) -> bool:
        """Reset a failed card to 'idle' so the poll loop picks it up again."""
        card = self.store.get_card(card_id)
        if card is None or card.status != "failed":
            return False
        self.store.update_card(card_id, status="idle", error="")
        return True

    def cancel_card(self, card_id: str) -> bool:
        """Cancel a running card: stop its task and mark it failed ('Cancelled by user').

        The card is not re-triggered (failed cards are not executable).
        """
        card = self.store.get_card(card_id)
        if card is None or card.status != "running":
            return False
        self._cancelled.add(card_id)
        self.store.update_card(card_id, status="failed", error=CANCELLED_BY_USER, completed_at=_now())
        task = self._card_tasks.get(card_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            self._cancelled.discard(card_id)
        return True
