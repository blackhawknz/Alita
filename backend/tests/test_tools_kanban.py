"""Tests for KanbanExecutor fixes: no failed-retry loop, real cancellation, recovery, limits."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from alita.kanban import executor as executor_mod
from alita.kanban.executor import KanbanExecutor
from alita.kanban.store import KanbanStore
from alita.tools.kanban_tool import make_kanban_tool


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(executor_mod, "POLL_INTERVAL", 0.05)


@pytest.fixture
def store(tmp_path):
    return KanbanStore(db_path=str(tmp_path / "k.db"))


def _ok_result():
    sr = MagicMock()
    sr.error = None
    sr.task_id = "t"
    sr.to_dict.return_value = {"response": "ok"}
    return sr


async def _wait_for(pred, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_failed_cards_not_executable(store):
    card = store.create_card(title="F", column="executing")
    store.update_card(card.id, status="failed", error="boom")
    assert store.get_executable_cards() == []


async def test_failed_card_is_not_retried_automatically(store):
    card = store.create_card(title="Fail", description="x", column="executing")
    sr = MagicMock()
    sr.error = "boom"
    sr.task_id = "t"
    sr.to_dict.return_value = {}
    mgr = MagicMock()
    mgr.delegate = AsyncMock(return_value=sr)

    ex = KanbanExecutor(store, mgr)
    ex.start()
    await _wait_for(lambda: store.get_card(card.id).status == "failed")
    await asyncio.sleep(0.3)  # several more poll ticks
    await ex.stop()
    assert mgr.delegate.await_count == 1


async def test_cancel_card_cancels_running_task(store):
    card = store.create_card(title="Slow", description="x", column="executing")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_delegate(task, max_iterations):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _ok_result()

    mgr = MagicMock()
    mgr.delegate = AsyncMock(side_effect=slow_delegate)
    ex = KanbanExecutor(store, mgr)
    ex.start()
    await asyncio.wait_for(started.wait(), 3)
    assert ex.in_flight_count == 1

    assert ex.cancel_card(card.id) is True
    await asyncio.wait_for(cancelled.wait(), 3)
    await _wait_for(lambda: ex.in_flight_count == 0)
    await asyncio.sleep(0.2)  # must not be re-triggered
    await ex.stop()

    updated = store.get_card(card.id)
    assert updated.status == "failed"
    assert updated.error == "Cancelled by user"
    assert mgr.delegate.await_count == 1


async def test_stop_cancels_and_awaits_in_flight(store):
    store.create_card(title="Slow", description="x", column="executing")
    started = asyncio.Event()

    async def slow_delegate(task, max_iterations):
        started.set()
        await asyncio.sleep(30)

    mgr = MagicMock()
    mgr.delegate = AsyncMock(side_effect=slow_delegate)
    ex = KanbanExecutor(store, mgr)
    ex.start()
    await asyncio.wait_for(started.wait(), 3)
    await asyncio.wait_for(ex.stop(), 3)
    assert ex.in_flight_count == 0
    assert ex.is_running is False
    assert store.list_by_status("running") == []


async def test_concurrency_limit(store):
    for i in range(4):
        store.create_card(title=f"C{i}", description="x", column="executing")
    gate = asyncio.Event()
    active = []
    peak = []

    async def delegate(task, max_iterations):
        active.append(1)
        peak.append(len(active))
        await gate.wait()
        active.pop()
        return _ok_result()

    mgr = MagicMock()
    mgr.delegate = AsyncMock(side_effect=delegate)
    ex = KanbanExecutor(store, mgr, max_concurrent=2)
    ex.start()
    await _wait_for(lambda: len(active) == 2)
    await asyncio.sleep(0.2)
    assert max(peak) == 2
    gate.set()
    await _wait_for(lambda: mgr.delegate.await_count == 4)
    await ex.stop()


def test_max_concurrent_from_env(monkeypatch, store):
    monkeypatch.setenv("ALITA_KANBAN_MAX_CONCURRENT", "5")
    assert KanbanExecutor(store, MagicMock()).max_concurrent == 5


def test_start_without_loop_is_noop(store):
    ex = KanbanExecutor(store, MagicMock())
    ex.start()  # no running loop
    assert ex.is_running is False


async def test_start_recovers_interrupted_cards_and_is_idempotent(store):
    card = store.create_card(title="Stuck", column="executing")
    store.update_card(card.id, status="running")
    ex = KanbanExecutor(store, MagicMock())
    ex.start()
    first_task = ex._task
    ex.start()
    assert ex._task is first_task
    await ex.stop()
    updated = store.get_card(card.id)
    assert updated.status == "failed"
    assert updated.error == "Interrupted by restart"


# ─── kanban tool validation ───────────────────────────────────────


@pytest.mark.parametrize("kwargs,msg", [
    ({"title": None}, "title"),
    ({"title": 123}, "title"),
    ({"title": "   "}, "title"),
    ({"title": "ok", "priority": "high"}, "priority"),
    ({"title": "ok", "priority": True}, "priority"),
    ({"title": "ok", "tags": "a,b"}, "tags"),
    ({"title": "ok", "tags": ["a", 1]}, "tags"),
])
async def test_kanban_tool_validation(store, kwargs, msg):
    tool = make_kanban_tool(store)
    res = await tool.handler(action="add_card", **kwargs)
    assert res["success"] is False
    assert msg in res["error"]


async def test_kanban_tool_accepts_valid_input(store):
    tool = make_kanban_tool(store)
    res = await tool.handler(action="add_card", title="  T  ", priority="3", tags=["a", "b"])
    assert res["success"] is True
    assert res["card"]["title"] == "T"
    assert res["card"]["priority"] == 3
    assert res["card"]["tags"] == ["a", "b"]
