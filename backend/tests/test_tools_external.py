"""External executor tools and their guardrails (no real CLI is invoked)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from alita.executors.base import ExecutorEvent
from alita.executors.report import Report
from alita.tools.external import (
    ANSWER_EXTERNAL_TOOL_SCHEMA,
    DELEGATE_EXTERNAL_TOOL_SCHEMA,
    ExternalToolManager,
    WorkdirError,
    resolve_workdir,
)
from alita.tools.subagent import DELEGATE_TOOL_SCHEMA

NATIVE_KEYS = {
    "success",
    "task_id",
    "task",
    "response",
    "tool_call_count",
    "api_call_count",
    "tool_summary",
    "interrupted",
    "error",
    "session_id",
    "report",
}

EXTERNAL_KEYS = {"success", "executor", "session_id", "cost", "report", "response"}


class FakeExternalExecutor:
    """Minimal Executor used instead of the real OpenCode CLI."""

    name = "opencode"

    def __init__(self, events=None):
        self.events = list(events or [])
        self.started = []
        self.sent = []
        self.interrupted = []
        self.closed = []
        self.cost = 0.0

    async def start(self, task, *, mandate=None, workdir=None):
        self.started.append((task, Path(workdir) if workdir is not None else None))
        return "ses_fake"

    async def send(self, handle, message):
        self.sent.append((handle, message))
        for event in self.events:
            if event.cost:
                self.cost += event.cost
            yield event

    async def interrupt(self, handle):
        self.interrupted.append(handle)

    async def close(self, handle):
        self.closed.append(handle)

    def has_handle(self, handle):
        return handle == "ses_fake"

    def handle_cost(self, handle):
        return self.cost


def _happy_events(summary: str = '```report\n{"kind": "ready", "summary": "done"}\n```'):
    return [
        ExecutorEvent(kind="status", text="working"),
        ExecutorEvent(kind="text", text=summary),
        ExecutorEvent(kind="done", data={"reason": "stop"}, cost=0.1),
    ]


def _manager(agent, executor=None):
    if executor is not None:
        agent.executors["opencode"] = executor
    return ExternalToolManager(agent)


# ─── workdir guardrails ──────────────────────────────────────────────


def test_workdir_defaults_to_workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert resolve_workdir(None, workspace=ws) == ws.resolve()
    assert resolve_workdir("", workspace=ws) == ws.resolve()


def test_workdir_rejects_absolute_outside_root(tmp_path):
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    with pytest.raises(WorkdirError) as exc:
        resolve_workdir(str(outside), workspace=ws)
    assert "allowed roots" in str(exc.value)
    assert str(ws.resolve()) in str(exc.value)


def test_workdir_rejects_parent_traversal(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (tmp_path / "outside").mkdir()
    with pytest.raises(WorkdirError):
        resolve_workdir("..\\outside", workspace=ws)
    with pytest.raises(WorkdirError):
        resolve_workdir("../outside", workspace=ws)


def test_workdir_accepts_relative_inside_workspace(tmp_path):
    ws = tmp_path / "workspace"
    child = ws / "project"
    child.mkdir(parents=True)
    assert resolve_workdir("project", workspace=ws) == child.resolve()


def test_workdir_rejects_symlink_escape(tmp_path):
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    link = ws / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this machine")
    with pytest.raises(WorkdirError):
        resolve_workdir("escape", workspace=ws)


def test_allowed_roots_env_extends_the_workspace(tmp_path):
    ws = tmp_path / "workspace"
    extra = tmp_path / "extra"
    ws.mkdir()
    extra.mkdir()
    assert resolve_workdir(extra, workspace=ws, allowed_roots_raw=str(extra)) == extra.resolve()


def test_delegate_rejects_bad_workdir_before_starting(tmp_path):
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    fake = FakeExternalExecutor(_happy_events())

    class Settings:
        executor_allowed_roots = ""
        executor_max_cost = 0.5
        executor_daily_cost = 5.0
        executor_allow_skip_permissions = False

    manager = ExternalToolManager(settings=Settings(), workspace=ws, executors={"opencode": fake})
    result = asyncio.run(manager.delegate("work", workdir=str(outside)))
    assert result["success"] is False
    assert "allowed roots" in result["error"]
    assert fake.started == []


# ─── schemas / registration / native default ────────────────────────


def test_external_tool_schemas():
    props = DELEGATE_EXTERNAL_TOOL_SCHEMA["parameters"]["properties"]
    assert DELEGATE_EXTERNAL_TOOL_SCHEMA["name"] == "delegate_external"
    assert props["executor"]["enum"] == ["opencode", "claude_code"]
    assert props["executor"]["default"] == "opencode"
    assert DELEGATE_EXTERNAL_TOOL_SCHEMA["parameters"]["required"] == ["task"]

    assert ANSWER_EXTERNAL_TOOL_SCHEMA["name"] == "answer_external"
    assert ANSWER_EXTERNAL_TOOL_SCHEMA["parameters"]["required"] == ["session_id", "message"]


def test_delegate_task_schema_is_additive():
    props = DELEGATE_TOOL_SCHEMA["parameters"]["properties"]
    assert props["executor"]["enum"] == ["native", "opencode", "claude_code"]
    assert props["executor"]["default"] == "native"
    assert "workdir" in props
    assert DELEGATE_TOOL_SCHEMA["parameters"]["required"] == ["task"]


def test_tools_are_registered_and_depth_gated(agent):
    assert agent.tool_registry.get("delegate_external") is not None
    assert agent.tool_registry.get("answer_external") is not None

    deep = agent.sessions.create(kind="subagent", metadata={"depth": agent.settings.subagent_max_depth})
    deep_names = {d["function"]["name"] for d in agent.tool_definitions(deep)}
    assert not {"delegate_task", "answer_subagent", "delegate_external", "answer_external"} & deep_names

    shallow = agent.sessions.create(kind="chat")
    shallow_names = {d["function"]["name"] for d in agent.tool_definitions(shallow)}
    assert {"delegate_external", "answer_external"} <= shallow_names


async def test_delegate_task_without_executor_stays_native(agent):
    agent.llm.script = [{"content": "native answer"}]
    tool = agent.tool_registry.get("delegate_task")
    result = await tool.handler(task="do it")
    assert set(result) == NATIVE_KEYS
    assert result["response"] == "native answer"
    assert result["success"] is True


async def test_delegate_task_can_route_to_external(agent):
    fake = FakeExternalExecutor(_happy_events())
    agent.executors["opencode"] = fake
    tool = agent.tool_registry.get("delegate_task")
    result = await tool.handler(task="external work", executor="opencode")
    assert result["success"] is True
    assert result["executor"] == "opencode"
    assert fake.started and fake.started[0][0] == "external work"
    assert fake.started[0][1] == Path(agent.workspace_dir).resolve()


# ─── delegate_external / answer_external ────────────────────────────


async def test_delegate_external_returns_the_compact_shape(agent):
    fake = FakeExternalExecutor(_happy_events())
    agent.executors["opencode"] = fake

    tool = agent.tool_registry.get("delegate_external")
    result = await tool.handler(task="build it", executor="opencode")

    assert set(result) == EXTERNAL_KEYS
    assert result["success"] is True
    assert result["executor"] == "opencode"
    assert result["session_id"] == "ses_fake"
    assert result["cost"] == pytest.approx(0.1)
    assert result["response"].startswith("```report")
    assert result["report"] == Report.parse(result["response"]).to_dict()
    assert agent.sqlite.get_executor_spend("opencode") == pytest.approx(0.1)
    assert fake.sent == [("ses_fake", "")]


async def test_answer_external_resumes_the_handle(agent):
    fake = FakeExternalExecutor(_happy_events('```report\n{"kind": "ready", "summary": "resumed"}\n```'))
    agent.executors["opencode"] = fake

    tool = agent.tool_registry.get("answer_external")
    result = await tool.handler(session_id="ses_fake", message="continue")

    assert set(result) == EXTERNAL_KEYS
    assert result["success"] is True
    assert result["report"]["summary"] == "resumed"
    assert fake.sent[-1] == ("ses_fake", "continue")


async def test_answer_external_unknown_handle_is_clear(agent):
    tool = agent.tool_registry.get("answer_external")
    result = await tool.handler(session_id="missing", message="hi")
    assert result["success"] is False
    assert "unknown external session" in result["error"]


# ─── cost guardrails ────────────────────────────────────────────────


async def test_per_call_cost_cap_kills_the_run(agent):
    agent.settings.executor_max_cost = 0.01
    fake = FakeExternalExecutor(
        [ExecutorEvent(kind="text", text="expensive"), ExecutorEvent(kind="done", cost=1.0)]
    )
    agent.executors["opencode"] = fake

    tool = agent.tool_registry.get("delegate_external")
    result = await tool.handler(task="spend", executor="opencode")

    assert result["success"] is False
    assert "ALITA_EXECUTOR_MAX_COST" in result["error"]
    assert fake.interrupted == ["ses_fake"]
    assert result["cost"] == pytest.approx(1.0)
    assert agent.sqlite.get_executor_spend("opencode") == pytest.approx(1.0)


async def test_daily_cost_cap_blocks_before_start(agent):
    agent.settings.executor_daily_cost = 0.05
    agent.sqlite.record_executor_spend("opencode", 0.06)
    fake = FakeExternalExecutor(_happy_events())
    agent.executors["opencode"] = fake

    tool = agent.tool_registry.get("delegate_external")
    result = await tool.handler(task="work", executor="opencode")

    assert result["success"] is False
    assert "ALITA_EXECUTOR_DAILY_COST" in result["error"]
    assert fake.started == []


async def test_daily_cost_cap_kills_a_long_run(agent):
    agent.settings.executor_daily_cost = 0.05
    agent.sqlite.record_executor_spend("opencode", 0.04)
    fake = FakeExternalExecutor([ExecutorEvent(kind="done", cost=0.02)])
    agent.executors["opencode"] = fake

    tool = agent.tool_registry.get("delegate_external")
    result = await tool.handler(task="work", executor="opencode")

    assert result["success"] is False
    assert "ALITA_EXECUTOR_DAILY_COST" in result["error"]
    assert fake.interrupted == ["ses_fake"]


async def test_unavailable_executor_is_reported(agent):
    tool = agent.tool_registry.get("delegate_external")
    result = await tool.handler(task="work", executor="claude_code")
    assert result["success"] is False
    assert "claude_code" in result["error"]


def test_skip_permissions_is_off_by_default():
    class Settings:
        executor_allow_skip_permissions = False

    manager = ExternalToolManager(settings=Settings(), executors={})
    assert manager._opencode_extra_args() == []

    class Allowed(Settings):
        executor_allow_skip_permissions = True

    assert ExternalToolManager(settings=Allowed(), executors={})._opencode_extra_args() == [
        "--dangerously-skip-permissions"
    ]
