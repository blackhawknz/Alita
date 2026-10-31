"""
Tests for the Kanban store, executor, tool, and API endpoints.

Network/LLM-free: the executor tests mock SubAgentManager.delegate() so no
real LLM is called. Store tests use a temp SQLite DB. API tests use FastAPI's
TestClient with a stubbed agent.
"""

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alita.kanban.store import KanbanCard, KanbanStore, COLUMNS, EXECUTING_COLUMN
from alita.kanban.executor import KanbanExecutor
from alita.tools.kanban_tool import make_kanban_tool


# ─── Store ─────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return KanbanStore(db_path=str(tmp_path / "kanban_test.db"))


def test_create_and_get_card(store):
    card = store.create_card(title="Build API", description="Build a REST API", tags=["coding"])
    assert card.id
    assert card.title == "Build API"
    assert card.column == "idea"
    assert card.status == "idle"
    assert card.tags == ["coding"]

    fetched = store.get_card(card.id)
    assert fetched is not None
    assert fetched.title == "Build API"


def test_get_card_not_found(store):
    assert store.get_card("nonexistent") is None


def test_list_cards(store):
    store.create_card(title="A", column="idea")
    store.create_card(title="B", column="planned")
    store.create_card(title="C", column="idea")

    all_cards = store.list_cards()
    assert len(all_cards) == 3

    idea_cards = store.list_cards(column="idea")
    assert len(idea_cards) == 2
    assert all(c.column == "idea" for c in idea_cards)


def test_move_card(store):
    card = store.create_card(title="Test", column="idea")
    updated = store.move_card(card.id, "planned")
    assert updated.column == "planned"

    # Invalid column → no change
    updated = store.move_card(card.id, "bogus")
    assert updated is None
    # Verify it wasn't actually moved
    assert store.get_card(card.id).column == "planned"


def test_update_card_fields(store):
    card = store.create_card(title="Old", description="old desc", tags=["a"])
    updated = store.update_card(card.id, title="New", description="new desc", tags=["a", "b"], priority=5)
    assert updated.title == "New"
    assert updated.description == "new desc"
    assert updated.tags == ["a", "b"]
    assert updated.priority == 5
    assert updated.updated_at != card.updated_at  # timestamp changed


def test_delete_card(store):
    card = store.create_card(title="Delete me")
    assert store.delete_card(card.id) is True
    assert store.get_card(card.id) is None
    assert store.delete_card(card.id) is False


def test_get_executable_cards_filters_by_column_and_status(store):
    idle_card = store.create_card(title="Idle", column="executing", description="do task")
    running_card = store.create_card(title="Running", column="executing")
    store.update_card(running_card.id, status="running")
    succeeded_card = store.create_card(title="Done", column="executing")
    store.update_card(succeeded_card.id, status="succeeded")
    failed_card = store.create_card(title="Retry", column="executing", description="retry me")
    store.update_card(failed_card.id, status="failed")

    plan_card = store.create_card(title="Planned", column="planned")  # not in executing

    executable = store.get_executable_cards()
    ids = {c.id for c in executable}
    # only idle is picked up; failed (no auto-retry loop), running, succeeded
    # and planned cards must not be
    assert idle_card.id in ids
    assert failed_card.id not in ids
    assert running_card.id not in ids
    assert succeeded_card.id not in ids
    assert plan_card.id not in ids


def test_column_invalid_falls_back_to_idea(store):
    card = store.create_card(title="X", column="bogus_column")
    assert card.column == "idea"


# ─── Kanban tool ──────────────────────────────────────────────────


async def test_tool_add_card_creates_in_idea_column(store):
    tool = make_kanban_tool(store)
    result = await tool.handler(action="add_card", title="Research topic X", description="Investigate X")
    assert result["success"] is True
    assert result["card"]["column"] == "idea"
    assert result["card"]["title"] == "Research topic X"
    assert result["card"]["description"] == "Investigate X"


async def test_tool_add_card_requires_title(store):
    tool = make_kanban_tool(store)
    result = await tool.handler(action="add_card", title="", description="desc")
    assert result["success"] is False
    assert "title is required" in result["error"]


async def test_tool_list_cards(store):
    tool = make_kanban_tool(store)
    await tool.handler(action="add_card", title="A")
    await tool.handler(action="add_card", title="B")
    result = await tool.handler(action="list_cards")
    assert result["success"] is True
    assert result["count"] == 2


async def test_tool_list_cards_by_column(store):
    tool = make_kanban_tool(store)
    await tool.handler(action="add_card", title="In idea")  # agent adds to idea
    # Manually move via store (human action)
    cards = store.list_cards()
    store.move_card(cards[0].id, "planned")

    result = await tool.handler(action="list_cards", column="idea")
    assert result["count"] == 0

    result = await tool.handler(action="list_cards", column="planned")
    assert result["count"] == 1


async def test_tool_get_card(store):
    tool = make_kanban_tool(store)
    created = await tool.handler(action="add_card", title="Test", description="desc")
    card_id = created["card"]["id"]
    result = await tool.handler(action="get_card", card_id=card_id)
    assert result["success"] is True
    assert result["card"]["title"] == "Test"


async def test_tool_get_card_not_found(store):
    tool = make_kanban_tool(store)
    result = await tool.handler(action="get_card", card_id="nonexistent")
    assert result["success"] is False
    assert "not found" in result["error"]


async def test_tool_update_card_description(store):
    tool = make_kanban_tool(store)
    created = await tool.handler(action="add_card", title="Original", description="old")
    card_id = created["card"]["id"]

    result = await tool.handler(action="update_card", card_id=card_id, description="updated desc", tags=["new"])
    assert result["success"] is True
    assert result["card"]["description"] == "updated desc"
    assert result["card"]["tags"] == ["new"]


async def test_tool_update_card_no_fields(store):
    tool = make_kanban_tool(store)
    created = await tool.handler(action="add_card", title="Test")
    result = await tool.handler(action="update_card", card_id=created["card"]["id"])
    assert result["success"] is False
    assert "No updatable fields" in result["error"]


async def test_tool_unknown_action(store):
    tool = make_kanban_tool(store)
    result = await tool.handler(action="bogus")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ─── Executor ─────────────────────────────────────────────────────


async def test_executor_executes_card_and_auto_advances(tmp_path):
    """A card in 'executing' column should be picked up, run, and moved to 'review'."""
    store = KanbanStore(db_path=str(tmp_path / "exec.db"))
    card = store.create_card(title="Test Task", description="do something", column="executing")

    subagent_mgr = MagicMock()
    sr = MagicMock()
    sr.error = None
    sr.task_id = "sub-task-1"
    sr.final_response = "Task done!"
    sr.tool_call_count = 3
    sr.api_call_count = 2
    sr.to_dict.return_value = {"response": "Task done!", "tool_call_count": 3}
    subagent_mgr.delegate = AsyncMock(return_value=sr)

    executor = KanbanExecutor(store, subagent_mgr)
    executor.start()
    await asyncio.sleep(3)  # allow poll loop to pick up + execute
    await executor.stop()

    updated = store.get_card(card.id)
    assert updated.status == "succeeded"
    assert updated.column == "review"  # auto-advanced
    assert updated.execution_session_id == "sub-task-1"
    subagent_mgr.delegate.assert_called_once_with(task="do something", max_iterations=20)


async def test_executor_failed_card_stays_in_executing(tmp_path):
    """A failed execution should stay in 'executing' with the error surfaced."""
    store = KanbanStore(db_path=str(tmp_path / "exec_fail.db"))
    card = store.create_card(title="Failing Task", description="do something that fails", column="executing")

    subagent_mgr = MagicMock()
    sr = MagicMock()
    sr.error = "LLM connection refused"
    sr.task_id = "sub-task-err"
    sr.to_dict.return_value = {"error": "LLM connection refused"}
    subagent_mgr.delegate = AsyncMock(return_value=sr)

    executor = KanbanExecutor(store, subagent_mgr)
    executor.start()
    await asyncio.sleep(3)
    await executor.stop()

    updated = store.get_card(card.id)
    assert updated.status == "failed"
    assert updated.column == "executing"  # stayed put
    assert "LLM connection refused" in updated.error


async def test_executor_does_not_double_pick_running_cards(tmp_path):
    store = KanbanStore(db_path=str(tmp_path / "exec_dbl.db"))
    card = store.create_card(title="Running", description="already running", column="executing")
    store.update_card(card.id, status="running")

    subagent_mgr = MagicMock()
    subagent_mgr.delegate = AsyncMock()

    executor = KanbanExecutor(store, subagent_mgr)
    executor.start()
    await asyncio.sleep(2)
    await executor.stop()

    subagent_mgr.delegate.assert_not_called()
    # A card left 'running' by a previous process is recovered as failed on start.
    recovered = store.get_card(card.id)
    assert recovered.status == "failed"
    assert recovered.error == "Interrupted by restart"


async def test_executor_auto_advance_disabled(tmp_path):
    """When auto_advance=False, a succeeded card should stay in 'executing'."""
    store = KanbanStore(db_path=str(tmp_path / "exec_noaa.db"))
    card = store.create_card(title="No Advance", description="do X", column="executing", auto_advance=False)

    subagent_mgr = MagicMock()
    sr = MagicMock()
    sr.error = None
    sr.task_id = "t"
    sr.to_dict.return_value = {"response": "done"}
    subagent_mgr.delegate = AsyncMock(return_value=sr)

    executor = KanbanExecutor(store, subagent_mgr)
    executor.start()
    await asyncio.sleep(3)
    await executor.stop()

    updated = store.get_card(card.id)
    assert updated.status == "succeeded"
    assert updated.column == "executing"  # did NOT auto-advance


async def test_executor_retry_card(tmp_path):
    """retry_card resets a failed card to idle so the executor can pick it up again."""
    store = KanbanStore(db_path=str(tmp_path / "exec_retry.db"))
    card = store.create_card(title="Retry", description="do X", column="executing")
    store.update_card(card.id, status="failed", error="old error")

    subagent_mgr = MagicMock()
    sr = MagicMock()
    sr.error = None
    sr.task_id = "t2"
    sr.to_dict.return_value = {"response": "ok now"}
    subagent_mgr.delegate = AsyncMock(return_value=sr)

    executor = KanbanExecutor(store, subagent_mgr)
    assert executor.retry_card(card.id) is True
    # The executor should pick it up on the next poll.
    executor.start()
    await asyncio.sleep(3)
    await executor.stop()

    updated = store.get_card(card.id)
    assert updated.status == "succeeded"
    assert updated.error == ""  # old error was cleared by retry


async def test_executor_retry_card_not_failed(tmp_path):
    store = KanbanStore(db_path=str(tmp_path / "exec_nf.db"))
    card = store.create_card(title="Idle", description="do X", column="executing")
    subagent_mgr = MagicMock()
    executor = KanbanExecutor(store, subagent_mgr)
    # Card is 'idle' not 'failed' — retry should return False
    assert executor.retry_card(card.id) is False


async def test_executor_cancel_card(tmp_path):
    store = KanbanStore(db_path=str(tmp_path / "exec_cancel.db"))
    card = store.create_card(title="Running", description="do X", column="executing")
    store.update_card(card.id, status="running")

    subagent_mgr = MagicMock()
    executor = KanbanExecutor(store, subagent_mgr)
    assert executor.cancel_card(card.id) is True
    updated = store.get_card(card.id)
    assert updated.status == "failed"
    assert "Cancelled" in updated.error

    # Cancel again on a now-failed card → False
    assert executor.cancel_card(card.id) is False


# ─── API endpoints ─────────────────────────────────────────────────


from fastapi.testclient import TestClient
from alita.api.server import app, get_agent


class _StubAgentKanban:
    def __init__(self, store):
        self.kanban_store = store
        self.kanban_executor = MagicMock()
        self.kanban_executor.retry_card = MagicMock(return_value=True)
        self.kanban_executor.cancel_card = MagicMock(return_value=True)


@pytest.fixture
def stub_agent_kanban(tmp_path, monkeypatch):
    store = KanbanStore(db_path=str(tmp_path / "api_test.db"))
    agent = _StubAgentKanban(store)
    monkeypatch.setattr("alita.api.server.get_agent", lambda: agent)
    return agent


def test_api_list_cards_empty(stub_agent_kanban):
    client = TestClient(app)
    r = client.get("/kanban/cards")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_api_create_and_get_card(stub_agent_kanban):
    client = TestClient(app)
    r = client.post("/kanban/cards", json={"title": "Build Feature X", "description": "Implement feature X"})
    assert r.status_code == 200
    card = r.json()["card"]
    assert card["title"] == "Build Feature X"
    assert card["column"] == "idea"

    r2 = client.get(f"/kanban/cards/{card['id']}")
    assert r2.status_code == 200
    assert r2.json()["card"]["title"] == "Build Feature X"


def test_api_get_card_not_found(stub_agent_kanban):
    client = TestClient(app)
    r = client.get("/kanban/cards/nonexistent")
    assert r.status_code == 404


def test_api_update_card(stub_agent_kanban):
    client = TestClient(app)
    create = client.post("/kanban/cards", json={"title": "Old"})
    card_id = create.json()["card"]["id"]

    r = client.patch(f"/kanban/cards/{card_id}", json={"title": "New Title", "column": "planned"})
    assert r.status_code == 200
    assert r.json()["card"]["title"] == "New Title"
    assert r.json()["card"]["column"] == "planned"


def test_api_delete_card(stub_agent_kanban):
    client = TestClient(app)
    create = client.post("/kanban/cards", json={"title": "Delete me"})
    card_id = create.json()["card"]["id"]

    r = client.delete(f"/kanban/cards/{card_id}")
    assert r.status_code == 200
    assert r.json()["success"] is True

    # Confirm gone
    assert client.get(f"/kanban/cards/{card_id}").status_code == 404


def test_api_list_by_column(stub_agent_kanban):
    client = TestClient(app)
    client.post("/kanban/cards", json={"title": "A", "column": "idea"})
    client.post("/kanban/cards", json={"title": "B", "column": "planned"})

    r = client.get("/kanban/cards?column=idea")
    assert r.json()["count"] == 1
    assert r.json()["cards"][0]["title"] == "A"


def test_api_get_columns(stub_agent_kanban):
    client = TestClient(app)
    r = client.get("/kanban/columns")
    assert r.status_code == 200
    assert r.json()["columns"] == COLUMNS
    assert r.json()["executing_column"] == "executing"


def test_api_retry_card(stub_agent_kanban):
    client = TestClient(app)
    create = client.post("/kanban/cards", json={"title": "Failed Card"})
    card_id = create.json()["card"]["id"]
    stub_agent_kanban.kanban_store.update_card(card_id, status="failed")

    r = client.post(f"/kanban/cards/{card_id}/retry")
    assert r.status_code == 200
    assert r.json()["success"] is True