"""External run telemetry, the stale sweep, and the /activity endpoint."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from alita.api import server
from alita.executors.opencode import OpenCodeExecutor
from alita.memory.sqlite_store import STALE_RUN_ERROR, SQLiteStore

FAKE_SCRIPT = r'''
import json, os, sys

session = "ses_activity_%d" % os.getpid()
mode = os.environ.get("FAKE_ACTIVITY_MODE", "happy")


def emit(payload):
    print(json.dumps(payload), flush=True)


emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})
if mode == "fail":
    sys.stderr.write("boom\n")
    sys.exit(2)
if mode == "hang":
    import time
    time.sleep(30)
    sys.exit(0)
emit({"type": "tool_use", "sessionID": session,
      "part": {"type": "tool", "tool": "bash",
               "state": {"status": "completed", "input": {"command": "pytest tests -q"}}}})
emit({"type": "text", "sessionID": session,
      "part": {"type": "text", "text": "all tests passed"}})
emit({"type": "step_finish", "sessionID": session,
      "part": {"type": "step-finish", "reason": "stop",
               "tokens": {"total": 7, "input": 5, "output": 2},
               "cost": 0.001}})
'''


def _fake_binary(tmp_path: Path) -> str:
    script = tmp_path / "fake_activity_opencode.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_activity_opencode.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return str(shim)
    shim = tmp_path / "fake_activity_opencode"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o755)
    return str(shim)


# ─── Store ─────────────────────────────────────────────────────────


def test_store_records_and_finishes_a_run(tmp_path):
    store = SQLiteStore(str(tmp_path / "state.db"))
    try:
        store.start_external_run("run-1", "opencode", str(tmp_path), "do the work")
        row = store.get_external_run("run-1")
        assert row["status"] == "running"
        assert row["cost"] == 0
        assert row["tool_calls"] == 0

        store.record_external_run_activity(
            "run-1", tool_calls=1, cost=0.25, last_activity="bash: pytest"
        )
        store.record_external_run_activity("run-1", tool_calls=2, cost=0.10)
        row = store.get_external_run("run-1")
        assert row["tool_calls"] == 3
        assert row["cost"] == pytest.approx(0.35)
        assert row["last_activity"] == "bash: pytest"

        store.finish_external_run(
            "run-1", status="succeeded", result_summary="done", duration_ms=1234
        )
        row = store.get_external_run("run-1")
        assert row["status"] == "succeeded"
        assert row["finished_at"]
        assert row["duration_ms"] == 1234
        assert row["result_summary"] == "done"
    finally:
        store.close()


def test_stale_sweep_only_fails_old_running_rows(tmp_path):
    store = SQLiteStore(str(tmp_path / "state.db"))
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=3)).isoformat()
    try:
        store.start_external_run("old", "opencode", str(tmp_path), "stuck", started_at=old)
        store.start_external_run("new", "opencode", str(tmp_path), "live")
        assert store.sweep_stale_external_runs(2) == 1

        stuck = store.get_external_run("old")
        assert stuck["status"] == "failed"
        assert stuck["error"] == STALE_RUN_ERROR
        assert stuck["finished_at"]
        assert store.get_external_run("new")["status"] == "running"
    finally:
        store.close()


# ─── Executor wiring ───────────────────────────────────────────────


async def test_executor_persists_a_real_run(tmp_path):
    store = SQLiteStore(str(tmp_path / "state.db"))
    executor = OpenCodeExecutor(
        binary=_fake_binary(tmp_path),
        default_model="test/model",
        timeout=10,
        store=store,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    try:
        handle = await executor.start("run the tests", workdir=workdir)
        events = [event async for event in executor.send(handle, "")]
        assert any(event.kind == "tool" for event in events)
        rows = store.list_external_runs()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "succeeded"
        assert row["cost"] == pytest.approx(0.001)
        assert row["tool_calls"] == 1
        assert row["last_activity"] == "bash: pytest tests -q"
        assert row["result_summary"] == "all tests passed"
        assert row["handle"] == handle
        assert row["duration_ms"] is not None
    finally:
        await executor.close(handle)
        store.close()


class _ExplodingStore:
    """Every telemetry write fails; the executor must not notice."""

    def save_external_handle(self, *args, **kwargs):
        raise RuntimeError("telemetry down")

    def start_external_run(self, *args, **kwargs):
        raise RuntimeError("telemetry down")

    def update_external_run(self, *args, **kwargs):
        raise RuntimeError("telemetry down")

    def record_external_run_activity(self, *args, **kwargs):
        raise RuntimeError("telemetry down")

    def finish_external_run(self, *args, **kwargs):
        raise RuntimeError("telemetry down")


async def test_running_row_exists_before_the_run_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACTIVITY_MODE", "hang")
    store = SQLiteStore(str(tmp_path / "state.db"))
    executor = OpenCodeExecutor(
        binary=_fake_binary(tmp_path),
        default_model="test/model",
        timeout=0.5,
        store=store,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    handle = await executor.start("still working", workdir=workdir)
    try:
        rows = store.list_external_runs()
        assert len(rows) == 1
        assert rows[0]["status"] == "running"
        assert rows[0]["task_summary"] == "still working"
        assert rows[0]["finished_at"] is None
    finally:
        await executor.close(handle)
        store.close()


async def test_telemetry_failure_never_breaks_the_run(tmp_path):
    executor = OpenCodeExecutor(
        binary=_fake_binary(tmp_path),
        default_model="test/model",
        timeout=10,
        store=_ExplodingStore(),
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    handle = await executor.start("still finish", workdir=workdir)
    try:
        events = [event async for event in executor.send(handle, "")]
    finally:
        await executor.close(handle)

    assert any(event.kind == "done" for event in events)
    assert any(event.text == "all tests passed" for event in events if event.kind == "text")


# ─── API ───────────────────────────────────────────────────────────


@pytest.fixture
def client(agent, monkeypatch):
    monkeypatch.setattr(server, "_agent", agent)
    return TestClient(server.app)


def test_activity_empty_shape(client):
    response = client.get("/activity")
    assert response.status_code == 200
    data = response.json()
    assert data["running"] == []
    assert data["recent"] == []
    assert data["needs_attention"] == []
    assert data["counts"] == {
        "running": 0,
        "failed_24h": 0,
        "pending_approvals": 0,
    }
    assert set(data["spend"]) >= {
        "today",
        "month_to_date",
        "projected_month",
        "executor_today",
    }


def test_activity_running_recent_and_attention(client, agent, monkeypatch):
    db = agent.sqlite
    db.start_external_run("live", "opencode", "/work", "long task")
    db.record_external_run_activity(
        "live", tool_calls=4, cost=0.02, last_activity="bash: npm test"
    )
    db.start_external_run("bad", "opencode", "/work", "broken task")
    db.finish_external_run("bad", status="failed", error="boom", duration_ms=500)

    monkeypatch.setattr(
        agent.approval_coordinator,
        "list_pending",
        lambda: [
            {
                "approval_id": "ap-1",
                "tool_name": "execute_code",
                "params": {},
                "reason": "host exec",
                "severity": "high",
                "created_at": 0,
                "age_seconds": 1,
            }
        ],
    )
    monkeypatch.setattr(
        type(agent.approval_coordinator),
        "pending_count",
        property(lambda self: 1),
    )

    response = client.get("/activity")
    assert response.status_code == 200
    data = response.json()

    assert data["counts"]["running"] == 1
    assert data["counts"]["failed_24h"] == 1
    assert data["counts"]["pending_approvals"] == 1
    assert [r["id"] for r in data["running"]] == ["live"]
    live = data["running"][0]
    assert live["tool_calls"] == 4
    assert live["cost"] == pytest.approx(0.02)
    assert live["last_activity"] == "bash: npm test"
    assert live["elapsed_ms"] >= 0
    assert [r["id"] for r in data["recent"]] == ["bad"]
    assert {r["type"] for r in data["needs_attention"]} == {"run", "approval"}

    detail = client.get("/activity/runs/live")
    assert detail.status_code == 200
    assert detail.json()["run"]["id"] == "live"
    assert client.get("/activity/runs/missing").status_code == 404
