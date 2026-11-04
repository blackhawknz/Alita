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
from datetime import datetime, timedelta, tzinfo
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore

from ..core import config, pricing
from .store import SchedulerStore, ScheduledJob

try:  # APScheduler already depends on tzlocal; keep a stdlib fallback.
    from tzlocal import get_localzone
except Exception:  # noqa: BLE001
    get_localzone = None

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
            timezone=self.resolve_timezone(),
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
    def resolve_timezone(name: Optional[str] = None) -> tzinfo:
        """Resolve an explicit/configured timezone, else the host's local zone."""
        raw = (name or config.timezone_name() or "").strip()
        if raw:
            try:
                return ZoneInfo(raw)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown timezone: {raw}") from exc
        if get_localzone is not None:
            try:
                return get_localzone()
            except Exception:  # noqa: BLE001
                pass
        return datetime.now().astimezone().tzinfo

    @staticmethod
    def validate_timezone(name: str) -> str:
        """Validate an IANA name; empty means host local time."""
        if not name:
            return ""
        SchedulerService.resolve_timezone(name)
        return name

    @staticmethod
    def validate_cron(cron_expr: str, timezone: Optional[str] = None) -> None:
        """Raise ValueError for an invalid 5-field cron expression or timezone."""
        CronTrigger.from_crontab(cron_expr, timezone=SchedulerService.resolve_timezone(timezone))

    def add_job(self, name: str, prompt: str, cron_expr: str,
                description: str = "", enabled: bool = True, timezone: str = "",
                max_runs_per_week: int = 0, max_cost_per_run: float = 0.0) -> ScheduledJob:
        """Create a new scheduled job and register it with APScheduler."""
        job = self.store.create_job(
            name, prompt, cron_expr, description, enabled, timezone,
            max_runs_per_week=max_runs_per_week, max_cost_per_run=max_cost_per_run,
        )
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

    def resume_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Clear a budget pause and register the job again."""
        job = self.store.resume_job(job_id)
        if job is None:
            return None
        self._unregister_aps_job(job_id)
        if job.enabled:
            self._register_aps_job(job)
        return job

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

    async def dry_run_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Run a job prompt with approvals denied, without recording a real run."""
        job = self.store.get_job(job_id)
        if job is None:
            return None
        start_time = time.time()
        sess = self.agent.sessions.create(
            kind="dry-run",
            title=f"Dry run: {job.name}",
            metadata={"job_id": job_id, "dry_run": True},
        )
        timeout = getattr(self.agent.settings, "background_timeout", 120) or 120
        try:
            try:
                result = await asyncio.wait_for(
                    self.agent.run_conversation(
                        job.prompt,
                        session=sess,
                        kind="dry-run",
                        title=f"Dry run: {job.name}",
                        metadata={"job_id": job_id, "dry_run": True},
                        system_message=(
                            "This is a dry run of an unattended scheduled job. Do not perform writes or deliver "
                            "anything; explain what you would do and report any actions that would need approval."
                        ),
                        headless_approval="deny",
                    ),
                    timeout=float(timeout),
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "timed_out": True,
                    "text": "",
                    "tool_calls": [],
                    "denied": [],
                    "error": f"Dry run timed out after {timeout}s.",
                    "duration_ms": int((time.time() - start_time) * 1000),
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("Dry run for job %s failed: %s", job_id, exc)
                return {
                    "success": False,
                    "timed_out": False,
                    "text": "",
                    "tool_calls": [],
                    "denied": [],
                    "error": str(exc),
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            tool_calls = []
            denied = []
            for entry in result.get("tool_trace", []):
                output = entry.get("output_result")
                was_denied = isinstance(output, dict) and bool(output.get("denied"))
                tool_calls.append({
                    "tool_name": entry.get("tool_name", ""),
                    "success": bool(entry.get("success", True)),
                    "denied": was_denied,
                })
                if was_denied:
                    denied.append(entry.get("tool_name", ""))
            error = result.get("error")
            return {
                "success": not result.get("interrupted") and not error,
                "timed_out": False,
                "text": result.get("final_response", "") or "",
                "tool_calls": tool_calls,
                "denied": denied,
                "error": error,
                "duration_ms": int((time.time() - start_time) * 1000),
            }
        finally:
            try:
                self.agent.sessions.delete(sess.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not clean up dry-run session %s: %s", sess.id, exc)

    # ─── APScheduler registration ──────────────────────────────────

    def _register_aps_job(self, job: ScheduledJob) -> None:
        if self._scheduler is None or not self._started:
            return
        if job.paused_reason:
            logger.info("Skipping registration of paused job %s: %s", job.id, job.paused_reason)
            return
        try:
            trigger = CronTrigger.from_crontab(job.cron_expr, timezone=self.resolve_timezone(job.timezone))
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

    def _pause_job(self, job: ScheduledJob, reason: str) -> bool:
        """Pause once, log once, and stop future APScheduler firings."""
        if not self.store.pause_job(job.id, reason):
            return False
        self._unregister_aps_job(job.id)
        logger.warning("Paused scheduled job %s: %s", job.id, reason)
        return True

    def _record_skipped_run(self, job_id: str, prompt: str, reason: str) -> None:
        run = self.store.create_run(job_id, prompt)
        self.store.finish_run(run.id, "skipped", error=reason)

    def _run_cost(self, job_id: str, started_at: str) -> float:
        """Cost of the usage events recorded since this run started."""
        if not started_at:
            return 0.0
        # job_runs.started_at uses an ISO "T"; usage_events.created_at uses a
        # SQL space. Comparing mixed formats would exclude same-day rows.
        since_sql = started_at.replace("T", " ")[:19]
        try:
            events = self.store.usage_events_for_job_since(job_id, since_sql)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read usage for job %s: %s", job_id, exc)
            return 0.0
        return pricing.events_cost(events)

    async def _execute_job(self, job_id: str, prompt: str) -> None:
        """Run one job invocation in its own ``job`` session (never touches chat sessions)."""
        job = self.store.get_job(job_id)
        if job is not None:
            prompt = job.prompt  # always run the latest prompt
            if job.paused_reason:
                logger.info("Scheduled job %s is paused (%s); skipping", job_id, job.paused_reason)
                return
            if job.max_runs_per_week > 0:
                cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
                count = self.store.count_runs_since(job_id, cutoff)
                if count >= job.max_runs_per_week:
                    reason = (
                        f"Weekly run budget reached "
                        f"({count} of {job.max_runs_per_week})"
                    )
                    self._record_skipped_run(job_id, prompt, reason)
                    self._pause_job(job, reason)
                    return
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
            cost = self._run_cost(job_id, run.started_at)
            self.store.finish_run(run.id, status, response, error, tool_calls, duration_ms, cost=cost)
            if job is not None and job.max_cost_per_run > 0 and cost > job.max_cost_per_run + 1e-9:
                reason = (
                    f"Run cost ${cost:.4f} exceeded budget "
                    f"${job.max_cost_per_run:.4f}"
                )
                self._pause_job(job, reason)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.time() - start_time) * 1000)
            cost = self._run_cost(job_id, run.started_at)
            self.store.finish_run(run.id, "failed", "", str(exc), [], duration_ms, cost=cost)
            logger.warning("Scheduled job %s failed: %s", job_id, exc)
        finally:
            self.store.mark_job_run(job_id, next_run_at=self._next_run_iso(job_id))