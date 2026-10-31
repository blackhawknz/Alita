"""
Scheduler Service — cron-based job execution backed by APScheduler.

Each enabled job is registered with APScheduler's ``AsyncIOScheduler``.
On each tick, the job invokes the parent ``AlitaAgent``'s
``run_conversation()`` method with the configured prompt, capturing the
full execution trace (tool calls, response, errors, duration) into the
``SchedulerStore`` for later replay in the Jobs UI.

Lifecycle:
    scheduler = SchedulerService(agent, store)
    scheduler.start()           # registers all enabled jobs + starts poller
    scheduler.add_job(...)      # add a new job at runtime
    scheduler.update_job(...)
    scheduler.remove_job(...)
    scheduler.run_job_now(...)  # manual trigger
    await scheduler.stop()      # graceful shutdown
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore

from .store import SchedulerStore, ScheduledJob

logger = logging.getLogger(__name__)


class SchedulerService:
    """Manages cron-scheduled agent invocations with trace persistence."""

    def __init__(self, agent: Any, store: SchedulerStore):
        self.agent = agent
        self.store = store
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._started = False
        self._tasks: set = set()

    # ─── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler and register all enabled jobs. Idempotent."""
        if self._started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("SchedulerService.start() called with no running event loop — deferring")
            return

        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            timezone="UTC",
        )
        self._scheduler.start()
        self._started = True

        # Register all enabled jobs from the store.
        for job in self.store.list_jobs():
            if job.enabled:
                self._register_aps_job(job)

        logger.info("SchedulerService started with %d job(s)", len(self.store.list_jobs()))

    async def stop(self) -> None:
        """Gracefully stop the scheduler."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        for task in list(self._tasks):
            task.cancel()
        self._started = False
        logger.info("SchedulerService stopped")

    @property
    def is_running(self) -> bool:
        return self._started

    # ─── Job management ─────────────────────────────────────────────

    @staticmethod
    def validate_cron(cron_expr: str) -> None:
        """Raise ValueError for an invalid 5-field cron expression."""
        CronTrigger.from_crontab(cron_expr, timezone="UTC")

    def add_job(self, name: str, prompt: str, cron_expr: str,
                description: str = "", enabled: bool = True) -> ScheduledJob:
        """Create a new scheduled job and register it with APScheduler."""
        job = self.store.create_job(name, prompt, cron_expr, description, enabled)
        if enabled:
            self._register_aps_job(job)
        return job

    def update_job(self, job_id: str, **fields) -> Optional[ScheduledJob]:
        """Update a job and re-register it with APScheduler if cron/enabled changed."""
        updated = self.store.update_job(job_id, **fields)
        if updated is None:
            return None
        # Remove and re-register the APScheduler job to pick up changes.
        self._unregister_aps_job(job_id)
        if updated.enabled:
            self._register_aps_job(updated)
        return updated

    def remove_job(self, job_id: str) -> bool:
        """Delete a job and unregister it from APScheduler."""
        self._unregister_aps_job(job_id)
        return self.store.delete_job(job_id)

    def run_job_now(self, job_id: str) -> Optional[str]:
        """
        Manually trigger a job immediately. Returns the run_id, or None if
        the job doesn't exist.
        """
        job = self.store.get_job(job_id)
        if job is None:
            return None
        task = asyncio.get_running_loop().create_task(self._execute_job(job_id, job.prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    # ─── APScheduler registration ──────────────────────────────────

    def _register_aps_job(self, job: ScheduledJob) -> None:
        if self._scheduler is None or not self._started:
            return
        try:
            trigger = CronTrigger.from_crontab(job.cron_expr, timezone="UTC")
        except ValueError as exc:
            logger.warning("Invalid cron expression %r for job %s: %s", job.cron_expr, job.id, exc)
            return
        self._scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            args=[job.id, job.prompt],
            id=f"alita_job_{job.id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        try:
            self.store.set_next_run(job.id, self._next_run_iso(job.id))
        except Exception:  # noqa: BLE001
            pass
        logger.info("Registered scheduled job %s [%s] cron=%s", job.id, job.name, job.cron_expr)

    def _unregister_aps_job(self, job_id: str) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(f"alita_job_{job_id}")
        except Exception:
            pass  # job not in scheduler — fine

    # ─── Job execution ──────────────────────────────────────────────

    def _next_run_iso(self, job_id: str) -> str:
        if self._scheduler is None:
            return ""
        try:
            aps = self._scheduler.get_job(f"alita_job_{job_id}")
            if aps is not None and aps.next_run_time is not None:
                return aps.next_run_time.isoformat()
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _execute_job(self, job_id: str, prompt: str) -> None:
        """Run one job invocation in its own ``job`` session (never touches chat sessions)."""
        job = self.store.get_job(job_id)
        if job is not None:
            prompt = job.prompt  # always run the latest prompt
        logger.info("Scheduled job %s executing: %s", job_id, prompt[:80])
        run = self.store.create_run(job_id, prompt)
        start_time = time.time()
        try:
            result = await self.agent.run_conversation(
                prompt,
                kind="job",
                title=f"Job: {job.name if job else job_id}",
                metadata={"job_id": job_id, "run_id": run.id},
                system_message=(
                    "This is an unattended scheduled job. Complete the task autonomously and finish with a concise "
                    "report of the results."
                ),
            )
            tool_calls = [
                {
                    "tool_name": t.get("tool_name", ""),
                    "input_params": t.get("input_params", {}),
                    "output_result": t.get("output_result", {}),
                    "success": t.get("success", True),
                }
                for t in result.get("tool_trace", [])
            ]
            duration_ms = int((time.time() - start_time) * 1000)
            response = result.get("final_response", "") or ""
            error = result.get("error") or ""
            if result.get("interrupted"):
                error = error or "Execution interrupted"
            status = "failed" if (error and not response) else "completed"
            self.store.finish_run(run.id, status, response, error, tool_calls, duration_ms)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.time() - start_time) * 1000)
            self.store.finish_run(run.id, "failed", "", str(exc), [], duration_ms)
            logger.warning("Scheduled job %s failed: %s", job_id, exc)
        finally:
            self.store.mark_job_run(job_id, next_run_at=self._next_run_iso(job_id))