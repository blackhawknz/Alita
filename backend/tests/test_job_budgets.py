"""Per-job budgets: schema migration, enforcement, pausing and resuming."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from alita.core import pricing
from alita.scheduler.service import SchedulerService
from alita.scheduler.store import SchedulerStore


USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    job_id TEXT,
    source TEXT NOT NULL,
    model TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    estimated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _ensure_usage_table(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(USAGE_SCHEMA)
    conn.commit()
    conn.close()


class BudgetAgent:
    """Fake agent: writes a canned usage row instead of calling a real model."""

    def __init__(self, db_path: str, events=None):
        self.db_path = db_path
        self.events = list(events or [])
        self.calls = []
        self.settings = SimpleNamespace()

    async def run_conversation(self, prompt, **kwargs):
        self.calls.append(prompt)
        job_id = (kwargs.get("metadata") or {}).get("job_id")
        _ensure_usage_table(self.db_path)
        conn = sqlite3.connect(self.db_path)
        for event in self.events:
            conn.execute(
                "INSERT INTO usage_events (job_id, source, model, prompt_tokens, "
                "completion_tokens, cached_tokens) VALUES (?, 'job', ?, ?, ?, ?)",
                (
                    job_id,
                    event.get("model", "deepseek-chat"),
                    event.get("prompt_tokens", 0),
                    event.get("completion_tokens", 0),
                    event.get("cached_tokens", 0),
                ),
            )
        conn.commit()
        conn.close()
        return {
            "final_response": "done",
            "interrupted": False,
            "error": None,
            "tool_trace": [],
        }


@pytest.fixture
def store(tmp_path):
    return SchedulerStore(db_path=str(tmp_path / "budgets.db"))


def _seed_runs(store, job_id: str, count: int) -> None:
    for _ in range(count):
        run = store.create_run(job_id, "x")
        store.finish_run(run.id, "completed", "ok")


def _age_runs(store, job_id: str, days: int) -> None:
    with store._get_conn() as conn:
        conn.execute(
            "UPDATE job_runs SET started_at = datetime('now', ?) WHERE job_id = ?",
            (f"-{days} days", job_id),
        )


# ─── Migration ────────────────────────────────────────────────────


def test_store_migrates_budget_columns_without_data_loss(tmp_path):
    path = tmp_path / "old_budgets.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE scheduled_jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            cron_expr TEXT NOT NULL,
            timezone TEXT DEFAULT '',
            enabled BOOLEAN DEFAULT 1,
            last_run_at TEXT DEFAULT '',
            next_run_at TEXT DEFAULT '',
            run_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE job_runs (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            prompt TEXT NOT NULL DEFAULT '',
            response TEXT DEFAULT '',
            error TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '[]',
            started_at TEXT DEFAULT '',
            completed_at TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0
        );
        INSERT INTO scheduled_jobs (id, name, prompt, cron_expr, run_count)
        VALUES ('old1', 'Old job', 'x', '* * * * *', 9);
        INSERT INTO job_runs (id, job_id, status, prompt, response)
        VALUES ('run1', 'old1', 'completed', 'x', 'kept');
        """
    )
    conn.commit()
    conn.close()

    store = SchedulerStore(db_path=str(path))
    job = store.get_job("old1")
    assert job is not None
    assert job.name == "Old job" and job.run_count == 9
    assert job.max_runs_per_week == 0
    assert job.max_cost_per_run == 0.0
    assert job.paused_reason == "" and job.paused_at == ""
    runs = store.list_runs(job_id="old1")
    assert len(runs) == 1 and runs[0].response == "kept" and runs[0].cost == 0.0


def test_budget_fields_default_and_round_trip(store):
    job = store.create_job(
        name="Budgeted", prompt="x", cron_expr="0 9 * * *",
        max_runs_per_week=7, max_cost_per_run=0.25,
    )
    assert job.max_runs_per_week == 7
    assert job.max_cost_per_run == pytest.approx(0.25)
    data = job.to_dict()
    assert data["max_runs_per_week"] == 7
    assert data["max_cost_per_run"] == pytest.approx(0.25)
    assert data["paused_reason"] == ""

    updated = store.update_job(job.id, max_runs_per_week=3, max_cost_per_run=0.5)
    assert updated.max_runs_per_week == 3
    assert updated.max_cost_per_run == pytest.approx(0.5)


# ─── Pause / resume ───────────────────────────────────────────────


def test_pause_is_idempotent(store):
    job = store.create_job(name="P", prompt="x", cron_expr="* * * * *")
    assert store.pause_job(job.id, "budget") is True
    first = store.get_job(job.id)
    assert first.paused_reason == "budget" and first.paused_at

    assert store.pause_job(job.id, "a different reason") is False
    second = store.get_job(job.id)
    assert second.paused_reason == "budget"
    assert second.paused_at == first.paused_at


def test_resume_clears_pause(store):
    job = store.create_job(name="R", prompt="x", cron_expr="* * * * *")
    store.pause_job(job.id, "budget")
    resumed = store.resume_job(job.id)
    assert resumed.paused_reason == ""
    assert resumed.paused_at == ""


def test_raising_a_budget_clears_the_pause(store):
    job = store.create_job(
        name="Raise", prompt="x", cron_expr="* * * * *",
        max_runs_per_week=1, max_cost_per_run=0.10,
    )
    store.pause_job(job.id, "budget")

    lowered = store.update_job(job.id, max_runs_per_week=1)
    assert lowered.paused_reason == "budget"  # not a raise

    raised = store.update_job(job.id, max_runs_per_week=5)
    assert raised.paused_reason == ""
    assert raised.paused_at == ""


# ─── Enforcement ──────────────────────────────────────────────────


async def test_weekly_budget_pauses_once_and_skips(store):
    agent = BudgetAgent(store.db_path)
    service = SchedulerService(agent, store)
    job = service.add_job(
        name="Weekly", prompt="run", cron_expr="* * * * *", max_runs_per_week=2,
    )
    _seed_runs(store, job.id, 2)

    await service._execute_job(job.id, "run")

    assert agent.calls == []
    paused = store.get_job(job.id)
    assert paused.paused_reason == "Weekly run budget reached (2 of 2)"
    assert paused.paused_at
    runs = store.list_runs(job_id=job.id)
    assert runs[0].status == "skipped"
    assert runs[0].error == paused.paused_reason
    before = len(runs)

    # A second invocation is a no-op: no new run row, no second pause log.
    await service._execute_job(job.id, "run")
    assert len(store.list_runs(job_id=job.id)) == before
    assert store.get_job(job.id).paused_reason == paused.paused_reason


async def test_runs_outside_the_trailing_week_do_not_count(store):
    agent = BudgetAgent(store.db_path)
    service = SchedulerService(agent, store)
    job = service.add_job(
        name="Old runs", prompt="run", cron_expr="* * * * *", max_runs_per_week=1,
    )
    _seed_runs(store, job.id, 2)
    _age_runs(store, job.id, 30)

    await service._execute_job(job.id, "run")

    assert agent.calls == ["run"]
    assert store.get_job(job.id).paused_reason == ""


async def test_cost_budget_pauses_after_an_over_budget_run(store):
    agent = BudgetAgent(
        store.db_path,
        events=[{"model": "deepseek-chat", "prompt_tokens": 1_000_000}],
    )
    service = SchedulerService(agent, store)
    job = service.add_job(
        name="Costly", prompt="run", cron_expr="* * * * *", max_cost_per_run=0.01,
    )

    await service._execute_job(job.id, "run")

    runs = store.list_runs(job_id=job.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].cost == pytest.approx(pricing.compute_cost("deepseek-chat", 1_000_000))
    paused = store.get_job(job.id)
    assert "exceeded budget" in paused.paused_reason
    assert paused.run_count == 1


async def test_cost_budget_not_hit_does_not_pause(store):
    agent = BudgetAgent(
        store.db_path,
        events=[{"model": "deepseek-chat", "prompt_tokens": 1_000}],
    )
    service = SchedulerService(agent, store)
    job = service.add_job(
        name="Cheap", prompt="run", cron_expr="* * * * *", max_cost_per_run=1.0,
    )

    await service._execute_job(job.id, "run")

    assert store.get_job(job.id).paused_reason == ""
    assert store.list_runs(job_id=job.id)[0].cost > 0


async def test_paused_jobs_are_not_registered(store):
    agent = BudgetAgent(store.db_path)
    service = SchedulerService(agent, store)
    service.start()
    job = service.add_job(name="Reg", prompt="x", cron_expr="0 9 * * *")
    service.store.pause_job(job.id, "budget")
    service._unregister_aps_job(job.id)

    paused = store.get_job(job.id)
    service._register_aps_job(paused)
    assert service._scheduler.get_job(f"alita_job_{job.id}") is None

    resumed = service.resume_job(job.id)
    assert resumed.paused_reason == ""
    assert service._scheduler.get_job(f"alita_job_{job.id}") is not None

    await service.stop()


async def test_manual_run_skips_a_paused_job(store):
    agent = BudgetAgent(store.db_path)
    service = SchedulerService(agent, store)
    job = service.add_job(name="Paused", prompt="x", cron_expr="* * * * *")
    store.pause_job(job.id, "budget")

    await service._execute_job(job.id, "x")

    assert agent.calls == []
    assert store.list_runs(job_id=job.id) == []


# ─── API ──────────────────────────────────────────────────────────


class _StubAgent:
    def __init__(self, store):
        self.scheduler_store = store
        self.scheduler_service = MagicMock()
        self.scheduler_service.add_job = MagicMock(side_effect=store.create_job)
        self.scheduler_service.update_job = MagicMock(side_effect=store.update_job)
        self.scheduler_service.resume_job = MagicMock(side_effect=store.resume_job)
        self.scheduler_service.remove_job = MagicMock(side_effect=store.delete_job)
        self.scheduler_service.run_job_now = MagicMock(return_value="job-id")


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from alita.api import server

    store = SchedulerStore(db_path=str(tmp_path / "api_budgets.db"))
    agent = _StubAgent(store)
    monkeypatch.setattr(server, "get_agent", lambda: agent)
    return TestClient(server.app), store


def test_api_create_job_with_budgets(api_client):
    client, _ = api_client
    response = client.post("/jobs", json={
        "name": "Budgeted", "prompt": "x", "cron_expr": "0 9 * * *",
        "max_runs_per_week": 7, "max_cost_per_run": 0.25,
    })
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["max_runs_per_week"] == 7
    assert job["max_cost_per_run"] == pytest.approx(0.25)


def test_api_patch_job_budgets(api_client):
    client, _ = api_client
    created = client.post("/jobs", json={
        "name": "Patch", "prompt": "x", "cron_expr": "0 9 * * *",
    }).json()["job"]
    response = client.patch(
        f"/jobs/{created['id']}",
        json={"max_runs_per_week": 2, "max_cost_per_run": 0.5},
    )
    assert response.status_code == 200
    assert response.json()["job"]["max_runs_per_week"] == 2
    assert response.json()["job"]["max_cost_per_run"] == pytest.approx(0.5)


def test_api_rejects_negative_budgets(api_client):
    client, _ = api_client
    response = client.post("/jobs", json={
        "name": "Negative", "prompt": "x", "cron_expr": "0 9 * * *",
        "max_runs_per_week": -1,
    })
    assert response.status_code == 400
    assert "max_runs_per_week" in response.json()["detail"]


def test_api_rejects_non_numeric_budgets(api_client):
    client, _ = api_client
    response = client.post("/jobs", json={
        "name": "Text", "prompt": "x", "cron_expr": "0 9 * * *",
        "max_cost_per_run": "lots",
    })
    assert response.status_code == 422


def test_api_resume_job(api_client):
    client, store = api_client
    created = client.post("/jobs", json={
        "name": "Resume", "prompt": "x", "cron_expr": "0 9 * * *",
    }).json()["job"]
    store.pause_job(created["id"], "Weekly run budget reached (1 of 1)")

    response = client.post(f"/jobs/{created['id']}/resume")
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["paused_reason"] == ""
    assert job["paused_at"] == ""
    assert client.post("/jobs/nope/resume").status_code == 404
