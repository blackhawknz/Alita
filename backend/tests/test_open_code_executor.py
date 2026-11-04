"""OpenCode executor, driven by a fake CLI binary (never the real install)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from alita.executors import OpenCodeExecutor
from alita.executors.opencode import _Run
from alita.memory.sqlite_store import SQLiteStore
from alita.tools.external import ExternalToolManager

FAKE_SCRIPT = r'''
import json, os, sys, time

mode = os.environ.get("FAKE_OPENCODE_MODE", "happy")
argv = sys.argv[1:]
if "-s" in argv:
    session = argv[argv.index("-s") + 1]
else:
    session = "ses_fake_%d" % os.getpid()


def emit(payload):
    print(json.dumps(payload), flush=True)


emit({"type": "step_start", "sessionID": session, "part": {"type": "step-start"}})

if mode == "fail":
    sys.stderr.write("boom: fake failure\n")
    sys.stderr.flush()
    sys.exit(2)

if mode == "hang":
    time.sleep(60)
    sys.exit(0)

if mode == "silent":
    sys.exit(0)

if mode == "authfail":
    emit({"type": "error", "sessionID": session,
          "error": {"name": "ProviderAuthError",
                    "data": {"providerID": "google",
                             "message": "Google Generative AI API key is missing"}}})
    sys.exit(0)

emit({"type": "tool_use", "sessionID": session,
      "part": {"type": "tool", "tool": "read", "state": {"status": "completed"}}})
emit({"type": "text", "sessionID": session,
      "part": {"type": "text", "text": "RESUMED" if "-s" in argv else "PONG"}})
emit({"type": "text", "sessionID": session,
      "part": {"type": "text", "text": "ARGS:" + json.dumps(argv)}})
emit({"type": "unknown_future_event", "sessionID": session, "part": {}})
emit({"type": "step_finish", "sessionID": session,
      "part": {"type": "step-finish", "reason": "stop",
               "tokens": {"total": 7, "input": 5, "output": 2, "reasoning": 0,
                          "cache": {"read": 0, "write": 0}},
               "cost": 0.001}})
'''


def _fake_binary(tmp_path: Path) -> str:
    script = tmp_path / "fake_opencode.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_opencode.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
        return str(shim)
    shim = tmp_path / "fake_opencode"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return str(shim)


@pytest.fixture
def fake_binary(tmp_path: Path) -> str:
    return _fake_binary(tmp_path)


async def _collect(iterator):
    return [event async for event in iterator]


async def test_happy_path_streams_events_and_captures_session(fake_binary, tmp_path):
    executor = OpenCodeExecutor(
        binary=fake_binary,
        default_model="test/model",
        timeout=10,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()

    handle = await executor.start("say PONG", workdir=workdir)
    try:
        assert handle.startswith("ses_fake_")
        events = await _collect(executor.send(handle, ""))
        kinds = [event.kind for event in events]

        assert "status" in kinds
        assert "tool" in kinds
        assert any(event.text == "PONG" for event in events if event.kind == "text")

        argv_text = next(
            event.text for event in events
            if event.kind == "text" and event.text.startswith("ARGS:")
        )
        argv = json.loads(argv_text[len("ARGS:"):])
        assert "--dir" in argv
        assert "-m" in argv and "test/model" in argv
        assert argv[-1] == "say PONG"

        done = [event for event in events if event.kind == "done"]
        assert done
        assert done[0].data["reason"] == "stop"
        assert done[0].data["tokens"]["total"] == 7
        assert done[0].cost == pytest.approx(0.001)
        assert executor.handle_cost(handle) == pytest.approx(0.001)
    finally:
        await executor.close(handle)

    assert not executor.has_handle(handle)


async def test_second_send_resumes_with_session_id(fake_binary, tmp_path):
    executor = OpenCodeExecutor(binary=fake_binary, timeout=10)
    handle = await executor.start("first", workdir=tmp_path)
    try:
        first = await _collect(executor.send(handle, ""))
        second = await _collect(executor.send(handle, "remember?"))
        cost = executor.handle_cost(handle)
    finally:
        await executor.close(handle)

    assert any(event.text == "PONG" for event in first)
    assert any(event.text == "RESUMED" for event in second)

    argv_text = next(
        event.text for event in second
        if event.kind == "text" and event.text.startswith("ARGS:")
    )
    argv = json.loads(argv_text[len("ARGS:"):])
    assert "-s" in argv
    assert argv[argv.index("-s") + 1] == handle
    assert argv[-1] == "remember?"
    assert cost == pytest.approx(0.002)


async def test_nonzero_exit_emits_error_with_stderr(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "fail")
    executor = OpenCodeExecutor(binary=fake_binary, timeout=10)
    handle = await executor.start("fail please", workdir=tmp_path)
    try:
        events = await _collect(executor.send(handle, ""))
    finally:
        await executor.close(handle)

    errors = [event for event in events if event.kind == "error"]
    assert errors
    assert errors[-1].data.get("returncode") == 2
    assert "boom: fake failure" in errors[-1].text


async def test_timeout_kills_process_and_emits_error_then_done(
    fake_binary, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "hang")
    executor = OpenCodeExecutor(binary=fake_binary, timeout=0.5)
    handle = await executor.start("hang", workdir=tmp_path)
    try:
        events = await asyncio.wait_for(
            _collect(executor.send(handle, "")), timeout=10
        )
        returncode = executor._runs[handle].returncode
    finally:
        await executor.close(handle)

    assert returncode is not None
    assert events[-2].kind == "error"
    assert "timed out after 0.5s" in events[-2].text
    assert events[-1].kind == "done"
    assert events[-1].data["reason"] == "timeout"


async def test_interrupt_terminates_running_process(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "hang")
    executor = OpenCodeExecutor(binary=fake_binary, timeout=30)
    handle = await executor.start("hang", workdir=tmp_path)

    consumer = asyncio.create_task(_collect(executor.send(handle, "")))
    try:
        await asyncio.sleep(0.2)
        await asyncio.wait_for(executor.interrupt(handle), timeout=10)
        events = await asyncio.wait_for(consumer, timeout=10)
        run = executor._runs[handle]
        assert run.returncode is not None
    finally:
        await executor.close(handle)

    assert all(event.kind != "error" for event in events)


async def test_unknown_handle_is_handled_gracefully(fake_binary):
    executor = OpenCodeExecutor(binary=fake_binary)
    events = await _collect(executor.send("nope", "hi"))
    assert events[0].kind == "error"
    await executor.interrupt("nope")
    await executor.close("nope")
    assert executor.handle_cost("nope") == 0.0


def test_missing_binary_raises():
    with pytest.raises(RuntimeError):
        OpenCodeExecutor(binary="definitely-not-opencode-xyz")


def test_error_event_is_mapped_with_real_message(fake_binary):
    executor = OpenCodeExecutor(binary=fake_binary, default_model="test/model")
    run = _Run(handle="temp")
    raw = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_auth",
            "error": {
                "name": "ProviderAuthError",
                "data": {
                    "providerID": "google",
                    "message": "Google Generative AI API key is missing",
                },
            },
        }
    )
    event = executor._map_line(raw, run)

    assert event is not None
    assert event.kind == "error"
    assert "ProviderAuthError" in event.text
    assert "Google Generative AI API key is missing" in event.text
    assert run.session_id == "ses_auth"


async def test_silent_run_is_an_error_not_a_success(fake_binary, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "silent")
    executor = OpenCodeExecutor(binary=fake_binary, default_model="test/model", timeout=10)
    handle = await executor.start("do nothing", workdir=tmp_path)
    try:
        events = await _collect(executor.send(handle, ""))
    finally:
        await executor.close(handle)

    errors = [event for event in events if event.kind == "error"]
    assert errors
    assert any("no output" in event.text for event in errors)


async def test_provider_error_surfaces_as_a_failed_report(
    fake_binary, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "authfail")
    executor = OpenCodeExecutor(binary=fake_binary, default_model="test/model", timeout=10)
    manager = ExternalToolManager(
        settings=_PersistSettings(),
        workspace=tmp_path,
        executors={"opencode": executor},
    )

    result = await manager.delegate("do the work", workdir=str(tmp_path))

    assert result["success"] is False
    assert "ProviderAuthError" in result["error"]
    assert "Google Generative AI API key is missing" in result["error"]
    assert result["report"]["kind"] == "failed"
    assert result["response"] == ""


def test_argv_includes_model_and_variant(fake_binary, tmp_path):
    executor = OpenCodeExecutor(
        binary=fake_binary,
        default_model="deepseek/deepseek-flash",
        variant="high",
    )
    argv = executor._argv(workdir=tmp_path, session_id=None, prompt="hi")
    assert argv[argv.index("-m") + 1] == "deepseek/deepseek-flash"
    assert argv[argv.index("--variant") + 1] == "high"


class _PersistSettings:
    executor_allowed_roots = ""
    executor_max_cost = 0.0
    executor_daily_cost = 0.0
    executor_allow_skip_permissions = False
    executor_opencode_model = "test/model"
    executor_opencode_variant = ""
    away_mode = False


async def test_handles_persist_across_manager_restart(fake_binary, tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    db = str(tmp_path / "state.db")

    store1 = SQLiteStore(db)
    executor1 = OpenCodeExecutor(
        binary=fake_binary, default_model="test/model", timeout=10, store=store1
    )
    manager1 = ExternalToolManager(
        settings=_PersistSettings(),
        workspace=ws,
        sqlite=store1,
        executors={"opencode": executor1},
    )
    first = await manager1.delegate("say PONG", workdir=str(ws))
    assert first["success"] is True, first
    handle = first["session_id"]
    assert handle.startswith("ses_fake_")

    record = store1.load_external_handle(handle)
    assert record is not None
    assert record["executor"] == "opencode"
    assert Path(record["workdir"]).resolve() == ws.resolve()
    assert record["cost"] == pytest.approx(0.001)
    store1.close()

    # Simulate a restart: new process => new store, executor and manager.
    store2 = SQLiteStore(db)
    executor2 = OpenCodeExecutor(
        binary=fake_binary, default_model="test/model", timeout=10, store=store2
    )
    manager2 = ExternalToolManager(
        settings=_PersistSettings(),
        workspace=ws,
        sqlite=store2,
        executors={"opencode": executor2},
    )
    second = await manager2.answer(handle, "what did you say?")
    assert second["success"] is True, second
    assert "RESUMED" in second["response"]
    # Rehydrated cost is preserved and the follow-up cost is added on top.
    assert executor2.handle_cost(handle) == pytest.approx(0.002)
    store2.close()


@pytest.mark.skipif(os.name != "nt", reason="PowerShell shim is a Windows concern")
def test_ps1_binary_is_invoked_through_powershell(monkeypatch, tmp_path):
    ps1 = tmp_path / "opencode.ps1"
    ps1.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        shutil, "which", lambda name: str(ps1) if name == "opencode" else None
    )

    executor = OpenCodeExecutor()
    prefix = executor.command_prefix
    assert prefix[-1] == str(ps1)
    assert prefix[0].lower().endswith("powershell.exe") or prefix[0].lower() == "powershell"
    assert "-File" in prefix


# Opt-in: only this test ever touches the real install/network. The env var
# makes it opt-in even on machines where `opencode` is on PATH.
@pytest.mark.skipif(
    not (shutil.which("opencode") and os.environ.get("ALITA_OPENCODE_INTEGRATION") == "1"),
    reason="set ALITA_OPENCODE_INTEGRATION=1 (and install opencode) to run the real CLI",
)
async def test_real_opencode_pong_and_resume(tmp_path):
    executor = OpenCodeExecutor(
        default_model="deepseek/deepseek-flash",
        timeout=180,
    )
    handle = await executor.start(
        "Reply with exactly the word PONG and nothing else.",
        workdir=tmp_path,
    )
    try:
        first = await _collect(executor.send(handle, ""))
        first_text = " ".join(e.text for e in first if e.kind == "text")
        assert "PONG" in first_text.upper()

        second = await _collect(executor.send(handle, "What word did you just say?"))
        second_text = " ".join(e.text for e in second if e.kind == "text")
        assert "PONG" in second_text.upper()
    finally:
        await executor.close(handle)


@pytest.mark.skipif(
    not (shutil.which("opencode") and os.environ.get("ALITA_OPENCODE_INTEGRATION") == "1"),
    reason="set ALITA_OPENCODE_INTEGRATION=1 (and install opencode) to run the real CLI",
)
async def test_real_cli_delegate_and_resume_through_manager(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    db = str(tmp_path / "state.db")

    store1 = SQLiteStore(db)
    executor1 = OpenCodeExecutor(
        default_model="deepseek/deepseek-flash", timeout=180, store=store1
    )
    manager1 = ExternalToolManager(
        settings=_PersistSettings(),
        workspace=ws,
        sqlite=store1,
        executors={"opencode": executor1},
    )
    first = await manager1.delegate(
        "Reply with exactly the word PONG and nothing else.", workdir=str(ws)
    )
    assert first["success"] is True, first
    assert first["response"].strip()
    assert first["cost"] > 0
    handle = first["session_id"]
    store1.close()

    store2 = SQLiteStore(db)
    executor2 = OpenCodeExecutor(
        default_model="deepseek/deepseek-flash", timeout=180, store=store2
    )
    manager2 = ExternalToolManager(
        settings=_PersistSettings(),
        workspace=ws,
        sqlite=store2,
        executors={"opencode": executor2},
    )
    second = await manager2.answer(handle, "What word did you just say?")
    assert second["success"] is True, second
    assert "PONG" in second["response"].upper()
    store2.close()
