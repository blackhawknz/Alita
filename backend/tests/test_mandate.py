"""Mandate object, tool-layer enforcement and OpenCode config generation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from alita.executors.base import ExecutorEvent
from alita.executors.mandate import Mandate
from alita.executors.native import NativeExecutor
from alita.executors.opencode import OpenCodeExecutor
from alita.memory.sqlite_store import SQLiteStore
from alita.tools.external import (
    ANSWER_EXTERNAL_TOOL_SCHEMA,
    DELEGATE_EXTERNAL_TOOL_SCHEMA,
    ExternalToolManager,
)

FAKE_SCRIPT = r'''
import json, os, sys

session = "ses_cfg"
config = os.environ.get("OPENCODE_CONFIG", "")
disable = os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG", "")
print(json.dumps({"type": "step_start", "sessionID": session}), flush=True)
print(json.dumps({"type": "text", "sessionID": session,
                  "part": {"type": "text", "text": "CONFIG:" + config + "|DISABLE:" + disable}}),
      flush=True)
print(json.dumps({"type": "step_finish", "sessionID": session,
                  "part": {"reason": "stop", "tokens": {}, "cost": 0}}), flush=True)
'''


def _fake_binary(tmp_path: Path) -> str:
    script = tmp_path / "fake_opencode_mandate.py"
    script.write_text(FAKE_SCRIPT, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_opencode_mandate.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return str(shim)
    shim = tmp_path / "fake_opencode_mandate"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


class Settings:
    executor_allowed_roots = ""
    executor_max_cost = 0.0
    executor_daily_cost = 0.0
    executor_allow_skip_permissions = False
    executor_opencode_model = "test/model"
    executor_opencode_variant = ""
    away_mode = False


class RecordingExecutor:
    name = "opencode"

    def __init__(self):
        self.started = []
        self.sent = []
        self.mandates = {}
        self.set_calls = []
        self._handles = set()

    async def start(self, task, *, mandate=None, workdir=None):
        handle = "ses_recording"
        self.started.append((task, mandate, Path(workdir) if workdir else None))
        self.mandates[handle] = mandate
        self._handles.add(handle)
        return handle

    async def send(self, handle, message):
        self.sent.append((handle, message))
        yield ExecutorEvent(
            kind="text", text='```report\n{"kind": "ready", "summary": "ok"}\n```'
        )
        yield ExecutorEvent(kind="done", data={"reason": "stop"}, cost=0.0)

    async def interrupt(self, handle):
        pass

    async def close(self, handle):
        self._handles.discard(handle)

    def has_handle(self, handle):
        return handle in self._handles

    def handle_cost(self, handle):
        return 0.0

    def handle_mandate(self, handle):
        return self.mandates.get(handle)

    def set_mandate(self, handle, mandate):
        self.set_calls.append((handle, mandate))
        self.mandates[handle] = mandate


class RestartExecutor(RecordingExecutor):
    def __init__(self):
        super().__init__()
        self.rehydrated = []

    def has_handle(self, handle):
        return False

    def rehydrate(self, handle, *, workdir=None, session_id=None, cost=0.0, mandate=None):
        self.rehydrated.append(
            (handle, Path(workdir) if workdir else None, mandate)
        )
        self._handles.add(handle)
        self.mandates[handle] = mandate


def _manager(tmp_path, executor):
    return ExternalToolManager(
        settings=Settings(),
        workspace=tmp_path,
        executors={"opencode": executor},
    )


def _generated_configs(work: Path):
    return sorted((work / ".alita").glob("opencode-*.json"))


def _latest_config(work: Path) -> Path:
    configs = _generated_configs(work)
    assert configs
    return max(configs, key=lambda path: path.stat().st_mtime_ns)


# ─── Mandate object ─────────────────────────────────────────────────


def test_read_only_is_the_default_and_has_no_capabilities():
    mandate = Mandate.read_only()
    assert mandate.read == ("*",)
    assert mandate.write == ()
    assert mandate.allow_bash is False
    assert mandate.allow_webfetch is False
    assert mandate.allow_subagents is False
    assert mandate.is_read_only() is True


def test_mandate_is_frozen():
    mandate = Mandate.read_only()
    with pytest.raises(Exception):
        mandate.allow_bash = True  # type: ignore[misc]


def test_dict_round_trip():
    original = Mandate(
        read=("src/**",),
        write=("src/**",),
        allow_webfetch=True,
        source="job",
        note="narrow",
    )
    restored = Mandate.from_dict(original.to_dict())
    assert restored.read == original.read
    assert restored.write == original.write
    assert restored.allow_webfetch is True
    assert restored.source == "job"
    assert restored.note == "narrow"


def test_from_dict_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown mandate key"):
        Mandate.from_dict({"read": ["*"], "allow_shell": True})


@pytest.mark.parametrize("write", [(), ("src/*",), ("src/**",)])
def test_validate_rejects_bash_with_narrow_write(write):
    with pytest.raises(ValueError, match="full write scope"):
        Mandate(write=write, allow_bash=True).validate()


@pytest.mark.parametrize("write", [("*",), ("**",), ("**/*",)])
def test_validate_allows_bash_with_full_write(write):
    Mandate(write=write, allow_bash=True).validate()


def test_permissions_read_only_sets_every_key_explicitly(tmp_path):
    perms = Mandate.read_only().to_opencode_permissions(tmp_path)
    assert perms["edit"] == "deny"
    assert perms["bash"] == "deny"
    assert perms["webfetch"] == "deny"
    assert perms["websearch"] == "deny"
    assert perms["task"] == "deny"
    assert perms["external_directory"] == "deny"
    assert isinstance(perms["read"], dict)
    for key in ("edit", "bash", "webfetch", "websearch", "task", "external_directory"):
        assert perms[key] in {"allow", "deny"}
    assert set(perms["read"].values()) <= {"allow", "deny"}


def test_permissions_partial_write_denies_then_allows_glob(tmp_path):
    perms = Mandate(read=("*",), write=("src/**",)).to_opencode_permissions(tmp_path)
    assert perms["edit"] == {"*": "deny", "src/**": "allow"}
    assert perms["bash"] == "deny"


def test_permissions_full_write_and_bash_are_explicit_allow(tmp_path):
    perms = Mandate(
        read=("*",), write=("*",), allow_bash=True, allow_webfetch=True
    ).to_opencode_permissions(tmp_path)
    assert perms["edit"] == "allow"
    assert perms["bash"] == "allow"
    assert perms["webfetch"] == "allow"


def test_read_globs_are_rendered_relative_to_the_workdir(tmp_path):
    perms = Mandate(read=("docs/**",)).to_opencode_permissions(tmp_path)
    expected = str(tmp_path.resolve()).replace("\\", "/") + "/docs/**"
    assert perms["read"][expected] == "allow"


def test_external_directory_is_never_granted(tmp_path):
    perms = Mandate(read=("*",), write=("*",), allow_bash=True).to_opencode_permissions(
        tmp_path
    )
    assert perms["external_directory"] == "deny"


def test_intersect_narrows_write_and_capabilities():
    original = Mandate(read=("*",), write=("src/*",), allow_webfetch=True)
    requested = Mandate(
        read=("*",), write=("*",), allow_bash=True, allow_webfetch=True
    )
    merged = original.intersect(requested)
    assert merged.write == ("src/*",)
    assert merged.allow_bash is False
    assert merged.allow_webfetch is True


def test_intersect_never_adds_a_capability():
    original = Mandate.read_only()
    merged = original.intersect(
        Mandate(read=("*",), write=("*",), allow_bash=True, allow_webfetch=True)
    )
    assert merged.write == ()
    assert merged.allow_bash is False
    assert merged.allow_webfetch is False
    assert merged.is_read_only() is True


def test_intersect_drops_bash_when_write_is_narrowed():
    original = Mandate(read=("*",), write=("*",), allow_bash=True)
    requested = Mandate(read=("*",), write=("src/*",), allow_bash=True)
    merged = original.intersect(requested)
    assert merged.write == ("src/*",)
    assert merged.allow_bash is False


def test_validate_globs_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="'\\.\\.'"):
        Mandate(write=("../outside/*",)).validate_globs(tmp_path)
    with pytest.raises(ValueError, match="'\\.\\.'"):
        Mandate(write=("src/../../outside/*",)).validate_globs(tmp_path)


def test_validate_globs_rejects_absolute_outside(tmp_path):
    outside = tmp_path.parent / "outside"
    with pytest.raises(ValueError, match="outside the workdir"):
        Mandate(write=(str(outside / "*"),)).validate_globs(tmp_path)


def test_validate_globs_allows_relative_and_absolute_inside(tmp_path):
    inside = tmp_path / "src"
    inside.mkdir()
    Mandate(write=("src/**",)).validate_globs(tmp_path)
    Mandate(write=(str(inside / "*"),)).validate_globs(tmp_path)


def test_validate_globs_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "symlink-outside"
    outside.mkdir()
    root = tmp_path / "work"
    root.mkdir()
    link = root / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this machine")
    with pytest.raises(ValueError, match="outside the workdir"):
        Mandate(write=("escape/*",)).validate_globs(root)


# ─── Tool layer ─────────────────────────────────────────────────────


async def test_delegate_without_mandate_is_read_only(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate("work", workdir=str(tmp_path))
    assert result["success"] is True
    mandate = executor.started[0][1]
    assert isinstance(mandate, Mandate)
    assert mandate.is_read_only()
    assert mandate.read == ("*",)


async def test_delegate_builds_a_structured_mandate(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "write": ["src/**"], "allow_webfetch": True},
    )
    assert result["success"] is True
    mandate = executor.started[0][1]
    assert mandate.write == ("src/**",)
    assert mandate.allow_webfetch is True
    assert mandate.allow_bash is False


async def test_delegate_rejects_unknown_mandate_keys_before_start(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work", workdir=str(tmp_path), mandate={"read_only": False, "shell": True}
    )
    assert result["success"] is False
    assert "unknown mandate key" in result["error"]
    assert executor.started == []


async def test_delegate_rejects_a_free_text_mandate(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work", workdir=str(tmp_path), mandate="only touch src/"
    )
    assert result["success"] is False
    assert "not free text" in result["error"]
    assert executor.started == []


async def test_delegate_rejects_read_only_contradictions(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work", workdir=str(tmp_path), mandate={"read_only": True, "write": ["src/*"]}
    )
    assert result["success"] is False
    assert "read_only=True cannot" in result["error"]
    assert executor.started == []


async def test_delegate_rejects_bash_with_a_narrow_write(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "allow_bash": True, "write": ["src/*"]},
    )
    assert result["success"] is False
    assert "full write scope" in result["error"]
    assert executor.started == []


async def test_delegate_rejects_parent_glob_before_start(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    result = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "write": ["../escape/*"]},
    )
    assert result["success"] is False
    assert "'..'" in result["error"]
    assert executor.started == []


async def test_delegate_rejects_absolute_outside_glob(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    outside = tmp_path.parent / "outside-again"
    result = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "write": [str(outside / "*")]},
    )
    assert result["success"] is False
    assert "outside the workdir" in result["error"]
    assert executor.started == []


def test_delegate_schema_mandate_is_structured_not_free_text():
    props = DELEGATE_EXTERNAL_TOOL_SCHEMA["parameters"]["properties"]
    mandate = props["mandate"]
    assert mandate["type"] == "object"
    assert set(mandate["properties"]) == {
        "read_only",
        "allow_bash",
        "allow_webfetch",
        "write",
        "note",
    }
    assert mandate["properties"]["read_only"]["default"] is True
    assert mandate["properties"]["write"]["type"] == "array"
    assert mandate["additionalProperties"] is False
    assert props["mandate"] != {"type": "string"}
    assert ANSWER_EXTERNAL_TOOL_SCHEMA["parameters"]["properties"]["mandate"]["type"] == "object"


# ─── Resume / persistence ───────────────────────────────────────────


def test_store_round_trips_mandate_json(tmp_path):
    store = SQLiteStore(str(tmp_path / "state.db"))
    mandate = Mandate(read=("src/**",), write=("src/**",), source="user")
    store.save_external_handle(
        "ses_m", "opencode", str(tmp_path), cost=0.4, mandate=mandate.to_dict()
    )
    record = store.load_external_handle("ses_m")
    assert record["mandate_json"]
    restored = Mandate.from_dict(json.loads(record["mandate_json"]))
    assert restored.write == ("src/**",)
    assert record["cost"] == pytest.approx(0.4)

    store.save_external_handle("ses_m", "opencode", str(tmp_path), cost=0.5)
    assert Mandate.from_dict(
        json.loads(store.load_external_handle("ses_m")["mandate_json"])
    ).write == ("src/**",)
    store.close()


def test_old_external_handles_table_gains_mandate_json(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE external_handles (handle TEXT PRIMARY KEY, executor TEXT NOT NULL, "
        "workdir TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, cost REAL NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO external_handles (handle, executor, workdir, cost) "
        "VALUES ('old', 'opencode', '', 0.1)"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(str(db))
    record = store.load_external_handle("old")
    assert record["cost"] == pytest.approx(0.1)
    assert record["mandate_json"] == ""
    store.close()


async def test_answer_reuses_the_original_mandate(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    first = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "write": ["src/**"]},
    )
    assert first["success"] is True
    original = executor.started[0][1]

    second = await manager.answer("ses_recording", "continue")
    assert second["success"] is True
    assert executor.set_calls[-1][1].write == original.write
    assert executor.set_calls[-1][1].allow_bash is False


async def test_answer_cannot_escalate_a_wider_mandate(tmp_path):
    executor = RecordingExecutor()
    manager = _manager(tmp_path, executor)
    first = await manager.delegate(
        "work",
        workdir=str(tmp_path),
        mandate={"read_only": False, "write": ["src/**"]},
    )
    assert first["success"] is True

    second = await manager.answer(
        "ses_recording",
        "continue",
        mandate={"read_only": False, "write": ["*"], "allow_bash": True},
    )
    assert second["success"] is True
    effective = executor.set_calls[-1][1]
    assert effective.write == ("src/**",)
    assert effective.allow_bash is False


async def test_answer_rehydrates_the_persisted_mandate_after_restart(tmp_path):
    db = str(tmp_path / "state.db")
    store1 = SQLiteStore(db)
    store1.save_external_handle(
        "ses_restart",
        "opencode",
        str(tmp_path),
        mandate=Mandate(read=("src/**",), write=("src/**",)).to_dict(),
    )
    store1.close()

    store2 = SQLiteStore(db)
    executor = RestartExecutor()
    manager = ExternalToolManager(
        settings=Settings(),
        workspace=tmp_path,
        sqlite=store2,
        executors={"opencode": executor},
    )
    result = await manager.answer("ses_restart", "continue")
    assert result["success"] is True
    handle, workdir, mandate = executor.rehydrated[0]
    assert handle == "ses_restart"
    assert workdir == tmp_path.resolve()
    assert mandate is not None
    assert mandate.write == ("src/**",)
    store2.close()


# ─── NativeExecutor ─────────────────────────────────────────────────


async def test_native_rejects_a_non_read_only_mandate(agent):
    executor = NativeExecutor(agent.subagent_manager)
    with pytest.raises(NotImplementedError, match="non-read-only mandate"):
        await executor.start(
            "write a file",
            mandate=Mandate(read=("*",), write=("src/*",)),
        )


async def test_native_rejects_bash_even_without_write(agent):
    executor = NativeExecutor(agent.subagent_manager)
    with pytest.raises(NotImplementedError):
        await executor.start(
            "run a command",
            mandate=Mandate(read=("*",), write=("*",), allow_bash=True),
        )


async def test_native_read_only_mandate_still_runs(agent):
    agent.llm.script = [{"content": "read only"}]
    executor = NativeExecutor(agent.subagent_manager)
    handle = await executor.start("look around", mandate=Mandate.read_only())
    try:
        events = [event async for event in executor.send(handle, "")]
    finally:
        await executor.close(handle)
    assert events[-1].kind == "done"


# ─── OpenCode config generation ─────────────────────────────────────


def test_conflicting_opencode_json_fails_loudly(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "opencode.json").write_text('{"permission": {"bash": "allow"}}', encoding="utf-8")
    executor = OpenCodeExecutor(binary=_fake_binary(tmp_path), timeout=10)
    with pytest.raises(RuntimeError, match="already contains"):
        asyncio.run(executor.start("work", workdir=work))


def test_conflicting_opencode_jsonc_fails_loudly(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "opencode.jsonc").write_text("{/* comment */}", encoding="utf-8")
    executor = OpenCodeExecutor(binary=_fake_binary(tmp_path), timeout=10)
    with pytest.raises(RuntimeError, match="already contains"):
        asyncio.run(executor.start("work", workdir=work))


async def test_generated_config_sets_permissions_explicitly(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    executor = OpenCodeExecutor(binary=_fake_binary(tmp_path), timeout=10)
    handle = await executor.start("work", workdir=work)
    try:
        config_path = _latest_config(work)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        perms = payload["permission"]
        assert perms["edit"] == "deny"
        assert perms["bash"] == "deny"
        assert perms["webfetch"] == "deny"
        assert perms["task"] == "deny"
        assert perms["external_directory"] == "deny"
        for key in ("edit", "bash", "webfetch", "websearch", "task"):
            assert perms[key] in {"allow", "deny"}
    finally:
        await executor.close(handle)


async def test_generated_config_for_write_mandate_allows_only_the_scope(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    executor = OpenCodeExecutor(binary=_fake_binary(tmp_path), timeout=10)
    handle = await executor.start(
        "work", workdir=work, mandate=Mandate(read=("*",), write=("src/**",))
    )
    try:
        payload = json.loads(_latest_config(work).read_text(encoding="utf-8"))
        perms = payload["permission"]
        assert perms["edit"] == {"*": "deny", "src/**": "allow"}
        assert perms["bash"] == "deny"
    finally:
        await executor.close(handle)


async def test_generated_config_is_passed_to_the_cli_with_project_config_disabled(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    executor = OpenCodeExecutor(binary=_fake_binary(tmp_path), timeout=10)
    handle = await executor.start("work", workdir=work)
    try:
        events = [event async for event in executor.send(handle, "")]
        text = next(event.text for event in events if event.text.startswith("CONFIG:"))
        config_path, disable = text[len("CONFIG:"):].split("|DISABLE:")
        generated = Path(config_path)
        assert generated.parent == (work / ".alita").resolve()
        assert generated.name.startswith("opencode-")
        assert generated.name.endswith(".json")
        assert generated.exists()
        assert disable == "1"
    finally:
        await executor.close(handle)


async def test_resume_regenerates_the_config_from_the_effective_mandate(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    db = str(tmp_path / "state.db")
    store1 = SQLiteStore(db)
    executor1 = OpenCodeExecutor(
        binary=_fake_binary(tmp_path), default_model="test/model", timeout=10, store=store1
    )
    manager1 = ExternalToolManager(
        settings=Settings(),
        workspace=tmp_path,
        sqlite=store1,
        executors={"opencode": executor1},
    )
    first = await manager1.delegate(
        "work",
        workdir=str(work),
        mandate={"read_only": False, "write": ["src/**"]},
    )
    assert first["success"] is True
    store1.close()

    store2 = SQLiteStore(db)
    executor2 = OpenCodeExecutor(
        binary=_fake_binary(tmp_path), default_model="test/model", timeout=10, store=store2
    )
    manager2 = ExternalToolManager(
        settings=Settings(),
        workspace=tmp_path,
        sqlite=store2,
        executors={"opencode": executor2},
    )
    second = await manager2.answer(
        first["session_id"],
        "continue",
        mandate={"read_only": False, "write": ["*"], "allow_bash": True},
    )
    assert second["success"] is True
    perms = json.loads(_latest_config(work).read_text(encoding="utf-8"))["permission"]
    assert perms["edit"] == {"*": "deny", "src/**": "allow"}
    assert perms["bash"] == "deny"
    store2.close()


# ─── Gated real-CLI integration ─────────────────────────────────────


_INTEGRATION = pytest.mark.skipif(
    not (shutil.which("opencode") and os.environ.get("ALITA_OPENCODE_INTEGRATION") == "1"),
    reason="set ALITA_OPENCODE_INTEGRATION=1 (and install opencode) to run the real CLI",
)


async def _run_real(work: Path, mandate: Mandate, prompt: str) -> None:
    store = SQLiteStore(str(work.parent / "state.db"))
    executor = OpenCodeExecutor(
        default_model="deepseek/deepseek-flash", timeout=180, store=store
    )
    manager = ExternalToolManager(
        settings=Settings(),
        workspace=work.parent,
        sqlite=store,
        executors={"opencode": executor},
    )
    try:
        result = await manager.delegate(prompt, workdir=str(work), mandate=mandate)
        assert result["session_id"] or result["success"] is not None
    finally:
        store.close()


@_INTEGRATION
async def test_real_read_only_mandate_cannot_create_a_file(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    prompt = (
        "Create a file named pwned_readonly.txt in the current directory "
        "containing the word PWNED. Use whatever tool you need."
    )
    await _run_real(work, Mandate.read_only(), prompt)
    assert not (work / "pwned_readonly.txt").exists()


@_INTEGRATION
async def test_real_bash_denied_cannot_write_through_bash(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    prompt = (
        "Run this exact shell command: echo PWNED > pwned_bash.txt . "
        "Then report whether it worked."
    )
    await _run_real(
        work, Mandate(read=("*",), write=("allowed/**",)), prompt
    )
    assert not (work / "pwned_bash.txt").exists()


@_INTEGRATION
async def test_real_bash_allowed_can_write_through_bash(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    prompt = (
        "Run this exact shell command: echo PWNED > pwned_bash_allowed.txt . "
        "Then report whether it worked."
    )
    await _run_real(
        work,
        Mandate(read=("*",), write=("*",), allow_bash=True),
        prompt,
    )
    assert (work / "pwned_bash_allowed.txt").exists()
