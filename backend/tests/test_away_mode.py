"""Away mode: settings, persistence, auto-away, and all four surfaces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from alita.core.away import AwayState
from alita.core.config import AgentSettings
from alita.kanban.executor import KanbanExecutor
from alita.kanban.store import EXECUTING_COLUMN, KanbanStore
from alita.memory.sqlite_store import SQLiteStore
from alita.scheduler.service import SchedulerService
from alita.scheduler.store import SchedulerStore
from alita.tools.external import ExternalToolManager


# ─── Settings and persistence ─────────────────────────────────────


def test_away_settings_default_off_and_disabled(monkeypatch):
    monkeypatch.delenv("ALITA_AWAY_MODE", raising=False)
    monkeypatch.delenv("ALITA_AUTO_AWAY_AFTER_HOURS", raising=False)
    settings = AgentSettings()
    assert settings.away_mode is False
    assert settings.away_auto_after_hours == 0


def test_env_away_mode_is_the_default(monkeypatch):
    monkeypatch.setenv("ALITA_AWAY_MODE", "true")
    state = AwayState(None, AgentSettings())
    assert state.manual_enabled() is True
    assert state.is_away() is True


def test_manual_toggle_persists_across_reopen(agent):
    state = AwayState(agent.sqlite, agent.settings)
    assert state.is_away() is False

    state.set_manual(True)
    assert state.is_away() is True
    assert state.since()

    reopened = SQLiteStore(str(agent.sqlite.db_path))
    try:
        state2 = AwayState(reopened, agent.settings)
        assert state2.manual_enabled() is True
        assert state2.is_away() is True
        assert state2.since() == state.since()
    finally:
        reopened.close()

    state.set_manual(False)
    assert state.is_away() is False
    assert state.since() == ""


def test_auto_away_uses_the_most_recent_chat(agent):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    sid = agent.sqlite.create_session(kind="chat")
    agent.sqlite.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ((now - timedelta(hours=10)).isoformat(), sid),
    )
    agent.settings.away_auto_after_hours = 6
    state = AwayState(agent.sqlite, agent.settings)

    assert state.auto_active(now) is True
    assert state.is_away(now) is True
    assert state.since(now)


def test_auto_away_stays_off_for_recent_chat(agent):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    sid = agent.sqlite.create_session(kind="chat")
    agent.sqlite.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ((now - timedelta(hours=1)).isoformat(), sid),
    )
    agent.settings.away_auto_after_hours = 6
    state = AwayState(agent.sqlite, agent.settings)

    assert state.auto_active(now) is False
    assert state.is_away(now) is False


def test_a_new_chat_message_turns_auto_away_off(agent):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    sid = agent.sqlite.create_session(kind="chat")
    agent.sqlite.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ((now - timedelta(hours=10)).isoformat(), sid),
    )
    agent.settings.away_auto_after_hours = 6
    state = AwayState(agent.sqlite, agent.settings)
    assert state.auto_active(now) is True

    agent.sqlite.append_message(sid, {"role": "user", "content": "I'm back"})

    assert state.auto_active(datetime.now(timezone.utc)) is False


def test_auto_away_is_disabled_by_zero_hours(agent):
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    sid = agent.sqlite.create_session(kind="chat")
    agent.sqlite.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ((now - timedelta(days=30)).isoformat(), sid),
    )
    agent.settings.away_auto_after_hours = 0
    assert AwayState(agent.sqlite, agent.settings).auto_active(now) is False


# ─── Scheduled jobs ────────────────────────────────────────────────


async def test_away_skips_jobs_without_advancing_run_count(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "away_jobs.db"))
    agent = SimpleNamespace(
        away_state=SimpleNamespace(is_away=lambda: True),
        run_conversation=AsyncMock(return_value={"final_response": "ran"}),
    )
    service = SchedulerService(agent, store)
    job = store.create_job(name="Away job", prompt="do it", cron_expr="0 9 * * *")

    await service._execute_job(job.id, "do it")

    assert agent.run_conversation.await_count == 0
    runs = store.list_runs(job_id=job.id)
    assert len(runs) == 1
    assert runs[0].status == "skipped"
    assert runs[0].error == "away mode"
    current = store.get_job(job.id)
    assert current.run_count == 0
    assert current.paused_reason == ""


async def test_away_off_still_runs_jobs(tmp_path):
    store = SchedulerStore(db_path=str(tmp_path / "on_jobs.db"))
    agent = SimpleNamespace(
        away_state=SimpleNamespace(is_away=lambda: False),
        run_conversation=AsyncMock(return_value={"final_response": "ran", "tool_trace": []}),
    )
    service = SchedulerService(agent, store)
    job = store.create_job(name="On job", prompt="do it", cron_expr="0 9 * * *")

    await service._execute_job(job.id, "do it")

    assert agent.run_conversation.await_count == 1
    assert store.list_runs(job_id=job.id)[0].status == "completed"
    assert store.get_job(job.id).run_count == 1


# ─── Kanban ────────────────────────────────────────────────────────


async def test_away_keeps_kanban_cards_idle(tmp_path):
    store = KanbanStore(db_path=str(tmp_path / "away_kanban.db"))
    card = store.create_card(title="Task", description="do it", column=EXECUTING_COLUMN)
    manager = SimpleNamespace(delegate=AsyncMock(return_value=SimpleNamespace(
        error=None, task_id="task", to_dict=lambda: {},
    )))
    executor = KanbanExecutor(
        store, manager, away_state=SimpleNamespace(is_away=lambda: True),
    )

    await executor._tick()

    assert manager.delegate.await_count == 0
    assert store.get_card(card.id).status == "idle"


async def test_kanban_runs_when_away_ends(tmp_path):
    store = KanbanStore(db_path=str(tmp_path / "on_kanban.db"))
    card = store.create_card(title="Task", description="do it", column=EXECUTING_COLUMN)
    manager = SimpleNamespace(delegate=AsyncMock(return_value=SimpleNamespace(
        error=None, task_id="task", to_dict=lambda: {},
    )))
    executor = KanbanExecutor(
        store, manager, away_state=SimpleNamespace(is_away=lambda: False),
    )

    await executor._tick()
    for task in list(executor._card_tasks.values()):
        await task

    assert manager.delegate.await_count == 1
    assert store.get_card(card.id).status == "succeeded"


# ─── External delegation ───────────────────────────────────────────


class _FakeExternal:
    name = "opencode"

    def __init__(self):
        self.events = []
        self.started = []
        self.sent = []

    async def start(self, task, *, mandate=None, workdir=None):
        self.started.append(task)
        return "ses_fake"

    async def send(self, handle, message):
        self.sent.append((handle, message))
        for event in self.events:
            yield event

    async def interrupt(self, handle):
        return None

    def has_handle(self, handle):
        return handle == "ses_fake"

    def handle_cost(self, handle):
        return 0.0


def _away_settings(enabled: bool = True):
    return SimpleNamespace(
        away_mode=enabled,
        executor_allowed_roots="",
        executor_max_cost=0.5,
        executor_daily_cost=5.0,
        executor_allow_skip_permissions=False,
    )


def test_away_blocks_external_delegation(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    fake = _FakeExternal()
    manager = ExternalToolManager(
        settings=_away_settings(True), workspace=ws, executors={"opencode": fake},
    )

    result = asyncio.run(manager.delegate("work"))

    assert result["success"] is False
    assert result["error"] == "away mode is on; external delegation is paused"
    assert fake.started == []


def test_away_blocks_external_resume(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    fake = _FakeExternal()
    manager = ExternalToolManager(
        settings=_away_settings(True), workspace=ws, executors={"opencode": fake},
    )

    result = asyncio.run(manager.answer("ses_fake", "continue"))

    assert result["success"] is False
    assert "away mode" in result["error"]
    assert fake.sent == []


def test_external_delegation_works_when_not_away(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    fake = _FakeExternal()
    manager = ExternalToolManager(
        settings=_away_settings(False), workspace=ws, executors={"opencode": fake},
    )

    result = asyncio.run(manager.delegate("work"))

    assert result["success"] is True
    assert fake.started == ["work"]


# ─── Chat always works, API ────────────────────────────────────────


async def test_chat_still_works_during_away(agent):
    agent.settings.away_mode = True
    agent.away_state.set_manual(True)
    agent.llm.script = [{"content": "I'm still here."}]

    result = await agent.run_conversation("hello", kind="chat")

    assert result["final_response"] == "I'm still here."
    assert result["error"] is None


def test_api_away_toggles_and_reports(agent, monkeypatch):
    from alita.api import server

    monkeypatch.setattr(server, "_agent", agent)
    client = TestClient(server.app)

    initial = client.get("/away")
    assert initial.status_code == 200
    body = initial.json()
    assert body["away_mode"] is False
    assert body["auto_after_hours"] == 0
    assert body["auto_active"] is False
    assert body["since"] == ""

    enabled = client.post("/away", json={"enabled": True}).json()
    assert enabled["away_mode"] is True
    assert enabled["since"]

    assert client.get("/away").json()["away_mode"] is True
    assert agent.sqlite.get_setting("away_mode") == "1"

    disabled = client.post("/away", json={"enabled": False}).json()
    assert disabled["away_mode"] is False
    assert agent.sqlite.get_setting("away_mode") == "0"
