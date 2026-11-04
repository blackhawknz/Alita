"""Kanban cards that delegate to external executors (no real CLI is invoked)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from alita.api.server import app
from alita.executors.base import ExecutorEvent
from alita.kanban.executor import KanbanExecutor
from alita.kanban.store import KanbanStore
from alita.memory.sqlite_store import SCHEMA_VERSION, SQLiteStore
from alita.tools.external import ExternalToolManager


class Settings:
    executor_allowed_roots = ""
    executor_max_cost = 0.5
    executor_daily_cost = 5.0
    executor_allow_skip_permissions = False
    executor_opencode_model = "deepseek/deepseek-flash"
    executor_opencode_variant = ""


class FakeExternalExecutor:
    """Small Executor that records the mandate it was started with."""

    name = "opencode"

    def __init__(self, events=None):
        self.events = list(events or [])
        self.started = []
        self.mandates = []
        self.sent = []
        self.interrupted = []
        self.handle = "ses_fake"
        self.run_id = "run-fake"

    async def start(self, task, *, mandate=None, workdir=None):
        self.started.append((task, Path(workdir) if workdir is not None else None))
        self.mandates.append(mandate)
        return self.handle

    async def send(self, handle, message):
        self.sent.append((handle, message))
        for event in self.events:
            yield event

    async def interrupt(self, handle):
        self.interrupted.append(handle)

    async def close(self, handle):
        pass

    def has_handle(self, handle):
        return handle == self.handle

    def handle_cost(self, handle):
        return 0.0

    def handle_run_id(self, handle):
        return self.run_id if handle == self.handle else None


class BlockingExecutor(FakeExternalExecutor):
    """Blocks in ``send`` until interrupted, so cancel_card can be exercised."""

    def __init__(self):
        super().__init__()
        self.sending = asyncio.Event()
        self.gate = asyncio.Event()

    async def send(self, handle, message):
        self.sent.append((handle, message))
        self.sending.set()
        await self.gate.wait()
        yield ExecutorEvent(kind="text", text='```report\n{"kind": "ready", "summary": "done"}\n```')
        yield ExecutorEvent(kind="done")

    async def interrupt(self, handle):
        self.interrupted.append(handle)
        self.gate.set()


def happy_events(summary: str = '```report\n{"kind": "ready", "summary": "done"}\n```'):
    return [ExecutorEvent(kind="status", text="working"), ExecutorEvent(kind="text", text=summary),
            ExecutorEvent(kind="done", cost=0.01)]


def make_manager(tmp_path, fake):
    return ExternalToolManager(
        settings=Settings(),
        workspace=tmp_path,
        executors={"opencode": fake, "claude_code": fake},
    )


async def run_card(tmp_path, card_kwargs, fake, manager=None, away=None):
    store = KanbanStore(db_path=str(tmp_path / "kanban.db"))
    card = store.create_card(title="Task", description="do the thing",
                             column="executing", **card_kwargs)
    manager = manager or make_manager(tmp_path, fake)
    executor = KanbanExecutor(store, MagicMock(), external_manager=manager, away_state=away)
    await executor._execute_card(store.get_card(card.id))
    return store.get_card(card.id), fake


# ─── Mandates ─────────────────────────────────────────────────────────


async def test_external_card_defaults_to_read_only_mandate(tmp_path):
    fake = FakeExternalExecutor(happy_events())
    card, _ = await run_card(tmp_path, {"executor": "opencode"}, fake)

    assert fake.started
    mandate = fake.mandates[0]
    assert mandate.is_read_only() is True
    assert mandate.write == ()
    assert mandate.allow_bash is False
    assert card.status == "succeeded"
    assert card.external_run_id == "run-fake"
    assert card.execution_session_id == "ses_fake"


async def test_external_card_passes_explicit_write_mandate(tmp_path):
    fake = FakeExternalExecutor(happy_events())
    mandate = {"read_only": False, "write": ["src/*.py"]}
    card, _ = await run_card(tmp_path, {"executor": "opencode", "mandate": mandate}, fake)

    assert card.status == "succeeded"
    resolved = fake.mandates[0]
    assert resolved.is_read_only() is False
    assert resolved.write == ("src/*.py",)
    assert card.mandate == mandate


# ─── Guardrails ───────────────────────────────────────────────────────


async def test_daily_cost_cap_fails_before_starting(tmp_path):
    fake = FakeExternalExecutor(happy_events())
    manager = make_manager(tmp_path, fake)
    manager._spent = lambda _name: 5.0  # daily cap

    card, _ = await run_card(tmp_path, {"executor": "opencode"}, fake, manager)

    assert fake.started == []
    assert card.status == "failed"
    assert "daily cost cap" in card.error


async def test_away_mode_leaves_external_card_idle(tmp_path):
    class Away:
        def is_away(self):
            return True

    fake = FakeExternalExecutor(happy_events())
    card, _ = await run_card(tmp_path, {"executor": "opencode"}, fake, away=Away())

    assert fake.started == []
    assert card.status == "idle"
    assert card.column == "executing"


# ─── Report mapping ───────────────────────────────────────────────────


async def test_waiting_report_blocks_without_auto_advance(tmp_path):
    fake = FakeExternalExecutor([
        ExecutorEvent(
            kind="text",
            text='```report\n{"kind": "waiting", "summary": "need info", '
                 '"question": "Which database?"}\n```',
        ),
        ExecutorEvent(kind="done"),
    ])
    card, _ = await run_card(tmp_path, {"executor": "opencode"}, fake)

    assert card.status == "blocked"
    assert card.column == "executing"
    assert card.error == "Which database?"
    assert card.result["kind"] == "waiting"


async def test_failed_external_run_marks_card_failed(tmp_path):
    fake = FakeExternalExecutor([ExecutorEvent(kind="error", text="auth failed")])
    card, _ = await run_card(tmp_path, {"executor": "opencode"}, fake)

    assert card.status == "failed"
    assert card.column == "executing"
    assert "auth failed" in card.error


# ─── Cancellation ─────────────────────────────────────────────────────


async def test_cancel_external_card_interrupts_the_process(tmp_path):
    fake = BlockingExecutor()
    store = KanbanStore(db_path=str(tmp_path / "cancel.db"))
    card = store.create_card(title="Long", description="run", column="executing",
                             executor="opencode")
    manager = make_manager(tmp_path, fake)
    executor = KanbanExecutor(store, MagicMock(), external_manager=manager)
    executor.start()
    try:
        for _ in range(100):
            if fake.sending.is_set():
                break
            await asyncio.sleep(0.02)
        assert fake.sending.is_set()
        assert executor.cancel_card(card.id) is True
        await asyncio.sleep(0.1)
    finally:
        await executor.stop()

    assert fake.handle in fake.interrupted
    updated = store.get_card(card.id)
    assert updated.status == "failed"
    assert "Cancelled" in updated.error


# ─── Migration ────────────────────────────────────────────────────────

_OLD_SCHEMA = """
CREATE TABLE kanban_cards (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    column TEXT NOT NULL DEFAULT 'idea',
    priority INTEGER NOT NULL DEFAULT 0,
    tags TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'idle',
    result TEXT DEFAULT '{}',
    execution_session_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    auto_advance BOOLEAN DEFAULT 1,
    max_iterations INTEGER DEFAULT 20
);
INSERT INTO kanban_cards (id, title, description, column, status)
VALUES ('old1', 'Old card', 'legacy', 'executing', 'idle');
PRAGMA user_version = 4;
"""


def test_kanban_migration_is_idempotent_across_three_opens(tmp_path):
    db = tmp_path / "state_copy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.commit()
    conn.close()

    for _ in range(3):
        store = SQLiteStore(str(db))
        store.close()
        kanban = KanbanStore(str(db))
        card = kanban.get_card("old1")
        assert card is not None
        assert card.executor == "native"
        assert card.workdir == ""
        assert card.mandate == {}
        assert card.max_minutes == 30.0
        assert card.external_run_id == ""

    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_cards)").fetchall()}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    assert {"executor", "workdir", "mandate", "max_minutes", "external_run_id"} <= cols
    assert version == SCHEMA_VERSION


# ─── API ──────────────────────────────────────────────────────────────


class _StubAgent:
    def __init__(self, store):
        self.kanban_store = store
        self.kanban_executor = MagicMock()
        self.kanban_executor.retry_card = MagicMock(return_value=True)
        self.kanban_executor.cancel_card = MagicMock(return_value=True)


@pytest.fixture
def stub_agent(tmp_path, monkeypatch):
    store = KanbanStore(db_path=str(tmp_path / "api.db"))
    agent = _StubAgent(store)
    monkeypatch.setattr("alita.api.server.get_agent", lambda: agent)
    return agent


def test_api_rejects_unknown_executor(stub_agent):
    client = TestClient(app)
    r = client.post("/kanban/cards", json={"title": "Bad", "executor": "gpt-cli"})
    assert r.status_code == 400
    assert "unknown executor" in r.json()["detail"]


def test_api_create_and_patch_external_card_fields(stub_agent):
    client = TestClient(app)
    r = client.post("/kanban/cards", json={
        "title": "External", "column": "executing", "executor": "opencode",
        "workdir": "backend", "mandate": {"read_only": False, "write": ["*.py"]},
        "max_minutes": 12,
    })
    assert r.status_code == 200
    card = r.json()["card"]
    assert card["executor"] == "opencode"
    assert card["workdir"] == "backend"
    assert card["mandate"] == {"read_only": False, "write": ["*.py"]}
    assert card["max_minutes"] == 12
    assert card["external_run_id"] == ""

    r = client.patch(f"/kanban/cards/{card['id']}", json={"executor": "claude_code"})
    assert r.status_code == 200
    assert r.json()["card"]["executor"] == "claude_code"

    r = client.patch(f"/kanban/cards/{card['id']}", json={"executor": "bogus"})
    assert r.status_code == 400
