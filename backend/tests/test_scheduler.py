"""
Tests for the Scheduler store, service, and API endpoints.

Store tests use a temp SQLite DB. Service tests mock the agent's
``run_conversation`` so no LLM is called. API tests use FastAPI's TestClient
with a stubbed agent.
"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from alita.scheduler.store import SchedulerStore, ScheduledJob, JobRun
from alita.scheduler.service import SchedulerService


# ─── Store ────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return SchedulerStore(db_path=str(tmp_path / "scheduler_test.db"))


def test_create_and_get_job(store):
    job = store.create_job(name="Daily Summary", prompt="Summarize my inbox", cron_expr="0 9 * * *")
    assert job.id
    assert job.name == "Daily Summary"
    assert job.cron_expr == "0 9 * * *"
    assert job.enabled is True
    assert job.run_count == 0

    fetched = store.get_job(job.id)
    assert fetched is not None
    assert fetched.name == "Daily Summary"


def test_get_job_not_found(store):
    assert store.get_job("nonexistent") is None


def test_list_jobs(store):
    store.create_job(name="A", prompt="a", cron_expr="* * * * *")
    store.create_job(name="B", prompt="b", cron_expr="0 * * * *")
    jobs = store.list_jobs()
    assert len(jobs) == 2


def test_update_job(store):
    job = store.create_job(name="Old", prompt="old", cron_expr="* * * * *")
    updated = store.update_job(job.id, name="New", prompt="new prompt", enabled=False)
    assert updated.name == "New"
    assert updated.prompt == "new prompt"
    assert updated.enabled is False


def test_delete_job(store):
    job = store.create_job(name="Del", prompt="x", cron_expr="* * * * *")
    assert store.delete_job(job.id) is True
    assert store.get_job(job.id) is None
    assert store.delete_job(job.id) is False


def test_job_timezone_round_trip(store):
    job = store.create_job(
        name="Auckland", prompt="x", cron_expr="0 9 * * *", timezone="Pacific/Auckland"
    )
    assert job.timezone == "Pacific/Auckland"
    assert store.get_job(job.id).timezone == "Pacific/Auckland"
    updated = store.update_job(job.id, timezone="Europe/London")
    assert updated.timezone == "Europe/London"


def test_store_migrates_old_jobs_table_without_timezone(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE scheduled_jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            enabled BOOLEAN DEFAULT 1,
            last_run_at TEXT DEFAULT '',
            next_run_at TEXT DEFAULT '',
            run_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO scheduled_jobs (id, name, prompt, cron_expr)
        VALUES ('old1', 'Old', 'x', '* * * * *');
        """
    )
    conn.commit()
    conn.close()

    store = SchedulerStore(db_path=str(path))
    job = store.get_job("old1")
    assert job is not None and job.timezone == ""


def test_service_resolves_timezones():
    assert SchedulerService.resolve_timezone("Pacific/Auckland") == ZoneInfo("Pacific/Auckland")
    assert SchedulerService.resolve_timezone(None) is not None
    with pytest.raises(ValueError):
        SchedulerService.resolve_timezone("Mars/Olympus")


async def test_service_registers_job_in_its_timezone(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "svc_tz.db"))
    agent = MagicMock()
    agent.run_conversation = AsyncMock()
    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(
        name="NZ", prompt="x", cron_expr="0 9 * * *", timezone="Pacific/Auckland"
    )
    aps = service._scheduler.get_job(f"alita_job_{job.id}")
    assert aps is not None
    assert str(aps.trigger.timezone) == "Pacific/Auckland"
    await service.stop()


def test_mark_job_run_increments_count(store):
    job = store.create_job(name="Counter", prompt="x", cron_expr="* * * * *")
    store.mark_job_run(job.id, next_run_at="2026-01-01T00:00:00")
    updated = store.get_job(job.id)
    assert updated.run_count == 1
    assert updated.last_run_at != ""
    assert updated.next_run_at == "2026-01-01T00:00:00"


# ─── Runs ─────────────────────────────────────────────────────────


def test_create_and_finish_run(store):
    job = store.create_job(name="Run Test", prompt="do X", cron_expr="* * * * *")
    run = store.create_run(job.id, "do X")
    assert run.status == "running"
    assert run.prompt == "do X"

    store.finish_run(
        run.id, status="completed", response="done!",
        tool_calls=[{"tool_name": "browser", "success": True}],
        duration_ms=1500,
    )
    fetched = store.get_run(run.id)
    assert fetched.status == "completed"
    assert fetched.response == "done!"
    assert len(fetched.tool_calls) == 1
    assert fetched.tool_calls[0]["tool_name"] == "browser"
    assert fetched.duration_ms == 1500


def test_list_runs_by_job(store):
    job = store.create_job(name="Multi", prompt="x", cron_expr="* * * * *")
    run1 = store.create_run(job.id, "x")
    store.finish_run(run1.id, "completed", "ok")
    run2 = store.create_run(job.id, "x")
    store.finish_run(run2.id, "failed", "", "LLM down")

    runs = store.list_runs(job_id=job.id)
    assert len(runs) == 2
    # Newest first
    assert runs[0].status == "failed"
    assert runs[1].status == "completed"


def test_list_all_runs(store):
    j1 = store.create_job(name="A", prompt="a", cron_expr="* * * * *")
    j2 = store.create_job(name="B", prompt="b", cron_expr="* * * * *")
    r1 = store.create_run(j1.id, "a")
    store.finish_run(r1.id, "completed", "ok")
    r2 = store.create_run(j2.id, "b")
    store.finish_run(r2.id, "completed", "ok2")

    all_runs = store.list_runs()
    assert len(all_runs) == 2


# ─── Service ──────────────────────────────────────────────────────


async def test_service_add_job_and_run(tmp_path):
    """Create a job, manually trigger it, verify the run is persisted."""
    store = SchedulerStore(db_path=str(tmp_path / "svc_test.db"))
    agent = MagicMock()
    agent.execution_traces = []
    agent.run_conversation = AsyncMock(return_value={
        "final_response": "Here's your summary!",
        "tool_call_count": 2,
        "interrupted": False,
    })

    service = SchedulerService(agent, store)
    service.start()

    job = service.add_job(name="Test Job", prompt="Summarize", cron_expr="0 9 * * *")
    assert job.enabled is True

    # Manually trigger execution.
    await service._execute_job(job.id, "Summarize")

    runs = store.list_runs(job_id=job.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].response == "Here's your summary!"
    assert runs[0].error == ""

    updated_job = store.get_job(job.id)
    assert updated_job.run_count == 1

    await service.stop()


async def test_service_run_captures_tool_traces(tmp_path):
    """Tool calls made during a run should appear in the run's tool_calls trace."""
    store = SchedulerStore(db_path=str(tmp_path / "svc_trace.db"))
    agent = MagicMock()
    seen = {}

    async def mock_run(prompt, **kwargs):
        # Jobs run in their own "job" session; traces come back in the result.
        seen.update(kwargs)
        return {
            "final_response": "done", "tool_call_count": 1, "interrupted": False, "error": None,
            "tool_trace": [{
                "tool_name": "browser",
                "input_params": {"url": "x"},
                "output_result": {"success": True},
                "success": True,
            }],
        }

    agent.run_conversation = mock_run

    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(name="Trace", prompt="go", cron_expr="* * * * *")

    await service._execute_job(job.id, "go")

    runs = store.list_runs(job_id=job.id)
    assert len(runs[0].tool_calls) == 1
    assert runs[0].tool_calls[0]["tool_name"] == "browser"
    assert runs[0].status == "completed"
    assert seen["kind"] == "job"
    assert seen["metadata"]["job_id"] == job.id

    await service.stop()


async def test_service_failed_run_persists_error(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "svc_fail.db"))
    agent = MagicMock()
    agent.execution_traces = []
    agent.run_conversation = AsyncMock(side_effect=RuntimeError("LLM exploded"))

    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(name="Fail", prompt="boom", cron_expr="* * * * *")

    await service._execute_job(job.id, "boom")

    runs = store.list_runs(job_id=job.id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "LLM exploded" in runs[0].error

    await service.stop()


async def test_service_update_job_re_registers(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "svc_upd.db"))
    agent = MagicMock()
    agent.execution_traces = []
    agent.run_conversation = AsyncMock(return_value={"final_response": "", "interrupted": False})

    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(name="Upd", prompt="x", cron_expr="0 9 * * *")

    updated = service.update_job(job.id, cron_expr="0 10 * * *", name="Updated")
    assert updated.cron_expr == "0 10 * * *"
    assert updated.name == "Updated"

    await service.stop()


async def test_service_remove_job(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "svc_rm.db"))
    agent = MagicMock()
    agent.execution_traces = []
    agent.run_conversation = AsyncMock()

    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(name="RM", prompt="x", cron_expr="* * * * *")
    assert service.remove_job(job.id) is True
    assert store.get_job(job.id) is None

    await service.stop()


def test_service_invalid_cron_does_not_crash(tmp_path):
    """An invalid cron expression should be logged and skipped, not crash."""
    store = SchedulerStore(db_path=str(tmp_path / "svc_bad.db"))
    agent = MagicMock()
    agent.execution_traces = []
    agent.run_conversation = AsyncMock()

    service = SchedulerService(agent, store)
    service.start()
    # add_job stores the job, and _register_aps_job will catch the invalid cron
    job = service.add_job(name="Bad", prompt="x", cron_expr="not-a-cron-expr")
    # The job should exist in the store even if APScheduler refused it.
    assert store.get_job(job.id) is not None


# ─── API endpoints ─────────────────────────────────────────────────


from fastapi.testclient import TestClient
from alita.api.server import app


class _StubAgentJobs:
    def __init__(self, store):
        self.scheduler_store = store
        self.scheduler_service = MagicMock()
        self.scheduler_service.add_job = MagicMock(side_effect=store.create_job)
        self.scheduler_service.update_job = MagicMock(side_effect=store.update_job)
        self.scheduler_service.remove_job = MagicMock(side_effect=store.delete_job)
        self.scheduler_service.run_job_now = MagicMock(return_value="job-id")


@pytest.fixture
def stub_agent_jobs(tmp_path, monkeypatch):
    store = SchedulerStore(db_path=str(tmp_path / "api_jobs.db"))
    agent = _StubAgentJobs(store)
    monkeypatch.setattr("alita.api.server.get_agent", lambda: agent)
    return agent


def test_api_list_jobs_empty(stub_agent_jobs):
    client = TestClient(app)
    r = client.get("/jobs")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_api_create_and_get_job(stub_agent_jobs):
    client = TestClient(app)
    r = client.post("/jobs", json={
        "name": "Daily Report", "prompt": "Give me a report",
        "cron_expr": "0 9 * * *", "timezone": "Pacific/Auckland",
    })
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["name"] == "Daily Report"
    assert job["cron_expr"] == "0 9 * * *"
    assert job["timezone"] == "Pacific/Auckland"

    r2 = client.get(f"/jobs/{job['id']}")
    assert r2.status_code == 200
    assert r2.json()["job"]["name"] == "Daily Report"
    assert r2.json()["job"]["timezone"] == "Pacific/Auckland"


def test_api_rejects_unknown_timezone(stub_agent_jobs):
    client = TestClient(app)
    r = client.post("/jobs", json={
        "name": "Bad zone", "prompt": "x", "cron_expr": "0 9 * * *",
        "timezone": "Mars/Olympus",
    })
    assert r.status_code == 400
    assert "timezone" in r.json()["detail"].lower()


def test_api_get_job_not_found(stub_agent_jobs):
    client = TestClient(app)
    assert client.get("/jobs/nonexistent").status_code == 404


def test_api_update_job(stub_agent_jobs):
    client = TestClient(app)
    create = client.post("/jobs", json={"name": "Old", "prompt": "x", "cron_expr": "* * * * *"})
    job_id = create.json()["job"]["id"]

    r = client.patch(f"/jobs/{job_id}", json={"name": "New Name", "enabled": False, "timezone": "Europe/London"})
    assert r.status_code == 200
    assert r.json()["job"]["name"] == "New Name"
    assert r.json()["job"]["enabled"] is False
    assert r.json()["job"]["timezone"] == "Europe/London"


def test_api_delete_job(stub_agent_jobs):
    client = TestClient(app)
    create = client.post("/jobs", json={"name": "Delete me", "prompt": "x", "cron_expr": "* * * * *"})
    job_id = create.json()["job"]["id"]

    r = client.delete(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_api_run_job_now(stub_agent_jobs):
    client = TestClient(app)
    create = client.post("/jobs", json={"name": "Run Me", "prompt": "x", "cron_expr": "* * * * *"})
    job_id = create.json()["job"]["id"]

    r = client.post(f"/jobs/{job_id}/run")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_api_run_job_not_found(stub_agent_jobs):
    client = TestClient(app)
    # Stub returns "job-id" for run_job_now, but we need it to return None for unknown
    stub_agent_jobs.scheduler_service.run_job_now = MagicMock(return_value=None)
    assert client.post("/jobs/nonexistent/run").status_code == 404


def test_api_list_job_runs(stub_agent_jobs):
    client = TestClient(app)
    create = client.post("/jobs", json={"name": "Runs", "prompt": "x", "cron_expr": "* * * * *"})
    job_id = create.json()["job"]["id"]

    # Manually insert a run via the store.
    store = stub_agent_jobs.scheduler_store
    run = store.create_run(job_id, "x")
    store.finish_run(run.id, "completed", "ok", duration_ms=500)

    r = client.get(f"/jobs/{job_id}/runs")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["runs"][0]["status"] == "completed"
    assert r.json()["runs"][0]["response"] == "ok"