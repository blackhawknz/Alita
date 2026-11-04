"""Sub-agent delegation: isolated child sessions on the shared runtime."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from alita.core.runtime import current_session
from alita.tools.subagent import (
    DELEGATE_TOOL_SCHEMA,
    SubAgentManager,
    SubAgentResult,
    make_answer_subagent_tool,
    make_subagent_tool,
)
from tests.conftest import tool_call


def test_result_to_dict():
    r = SubAgentResult("id1", "task", "resp", 2, 3, error=None)
    d = r.to_dict()
    assert d["success"] is True and d["response"] == "resp" and d["tool_call_count"] == 2
    assert SubAgentResult("id", "t", "", 0, 0, error="boom").to_dict()["success"] is False


def test_schema():
    assert DELEGATE_TOOL_SCHEMA["name"] == "delegate_task"
    assert DELEGATE_TOOL_SCHEMA["parameters"]["required"] == ["task"]


async def test_delegate_runs_child_session(agent):
    agent.llm.script = [{"content": "child report"}]
    parent = agent.sessions.create(kind="chat")
    token = current_session.set(parent)
    try:
        res = await agent.subagent_manager.delegate("research X")
    finally:
        current_session.reset(token)
    assert res.error is None and res.final_response == "child report"
    child_row = agent.sqlite.get_session(res.session_id)
    assert child_row["kind"] == "subagent" and child_row["parent_id"] == parent.id
    assert child_row["metadata"]["depth"] == 1
    # The parent session is untouched.
    assert parent.messages == []
    # Sub-agent instructions reached the system prompt.
    assert "sub-agent" in agent.llm.requests[0][0]["content"]


async def test_delegate_tool_end_to_end(agent):
    """Parent calls delegate_task; the child's report comes back as the tool result."""
    agent.llm.script = [
        {"content": "", "tool_calls": [tool_call("delegate_task", {"task": "find the answer"}, "d1")]},
        {"content": "42"},                      # child
        {"content": "The sub-agent says 42."},  # parent final
    ]
    sess = agent.sessions.create(kind="chat")
    events = []

    async def emit(ev):
        events.append(ev)

    await agent._run_turn(sess, "delegate please", emit)
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"]["response"] == "42"
    assert events[-1]["final_response"] == "The sub-agent says 42."


async def test_depth_limit_removes_delegate_tool(agent):
    deep = agent.sessions.create(kind="subagent", metadata={"depth": agent.settings.subagent_max_depth})
    names = {d["function"]["name"] for d in agent.tool_definitions(deep)}
    assert "delegate_task" not in names and "skill_manage" not in names
    shallow = agent.sessions.create(kind="chat")
    assert "delegate_task" in {d["function"]["name"] for d in agent.tool_definitions(shallow)}


async def test_delegate_error_is_captured():
    parent = type("P", (), {})()
    from alita.core.session import SessionManager
    from alita.memory.sqlite_store import SQLiteStore
    import tempfile, os
    tmp = tempfile.mkdtemp()
    db = SQLiteStore(os.path.join(tmp, "s.db"))
    parent.sessions = SessionManager(db)
    parent.run_conversation = AsyncMock(side_effect=RuntimeError("kaput"))
    mgr = SubAgentManager(parent)
    res = await mgr.delegate("x")
    assert res.error == "kaput" and mgr.active_count() == 0
    db.close()


async def test_cancel_marks_child_interrupted(agent):
    started = asyncio.Event()

    async def slow(*a, **kw):
        started.set()
        await asyncio.sleep(10)

    agent.run_conversation = slow
    task = asyncio.ensure_future(agent.subagent_manager.delegate("slow"))
    await started.wait()
    child = next(iter(agent.subagent_manager._active.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.interrupt_requested is True


async def test_tool_handler_validates():
    mgr = SubAgentManager(parent=None)
    mgr.delegate = AsyncMock(return_value=SubAgentResult("i", "t", "ok", 0, 1))
    tool = make_subagent_tool(mgr)
    assert (await tool.handler())["success"] is False
    out = await tool.handler(task="do x", max_iterations="bad")
    assert out["response"] == "ok"
    mgr.delegate.assert_awaited_with("do x", max_iterations=None)


async def test_delegate_attaches_a_structured_report(agent):
    agent.llm.script = [{"content": '```report\n{"kind": "ready", "summary": "done"}\n```'}]
    parent = agent.sessions.create(kind="chat")
    token = current_session.set(parent)
    try:
        res = await agent.subagent_manager.delegate("finish the task")
    finally:
        current_session.reset(token)

    assert res.report == {"kind": "ready", "summary": "done", "evidence": [], "question": None}
    assert res.to_dict()["report"]["kind"] == "ready"


async def test_waiting_report_can_be_resumed_after_forget(agent):
    agent.llm.script = [
        {"content": '```report\n{"kind": "waiting", "summary": "need a choice", '
                    '"question": "A or B?"}\n```'},
        {"content": '```report\n{"kind": "ready", "summary": "chose B"}\n```'},
    ]
    parent = agent.sessions.create(kind="chat")
    token = current_session.set(parent)
    try:
        first = await agent.subagent_manager.delegate("choose a letter")
        assert first.report["kind"] == "waiting"
        assert first.report["question"] == "A or B?"
        session_id = first.session_id
        # delegate() forgets the child; resume must reload it from SQLite.
        assert session_id not in agent.sessions._cache
        assert agent.sessions.get(session_id) is not None

        second = await agent.subagent_manager.resume(session_id, "B")
    finally:
        current_session.reset(token)

    assert second.session_id == session_id
    assert second.report["kind"] == "ready"
    assert "chose B" in second.final_response


async def test_answer_subagent_tool_resumes_the_session(agent):
    agent.llm.script = [
        {"content": '```report\n{"kind": "waiting", "summary": "need a choice", '
                    '"question": "A or B?"}\n```'},
        {"content": '```report\n{"kind": "ready", "summary": "chose B"}\n```'},
    ]
    parent = agent.sessions.create(kind="chat")
    token = current_session.set(parent)
    try:
        first = await agent.subagent_manager.delegate("choose a letter")
        tool = make_answer_subagent_tool(agent.subagent_manager)
        out = await tool.handler(session_id=first.session_id, message="B")
    finally:
        current_session.reset(token)

    assert out["success"] is True
    assert out["session_id"] == first.session_id
    assert out["report"]["kind"] == "ready"
    assert "chose B" in out["response"]


async def test_answer_subagent_tool_validates():
    mgr = SubAgentManager(parent=None)
    mgr.resume = AsyncMock(return_value=SubAgentResult("i", "t", "ok", 0, 1))
    tool = make_answer_subagent_tool(mgr)
    assert (await tool.handler(message="hi"))["success"] is False
    assert (await tool.handler(session_id="s"))["success"] is False
    out = await tool.handler(session_id="s", message="hi")
    assert out["response"] == "ok"
    mgr.resume.assert_awaited_with("s", "hi")


def test_answer_subagent_tool_is_registered_and_depth_limited(agent):
    assert agent.tool_registry.get("answer_subagent") is not None
    deep = agent.sessions.create(kind="subagent", metadata={"depth": agent.settings.subagent_max_depth})
    names = {d["function"]["name"] for d in agent.tool_definitions(deep)}
    assert "answer_subagent" not in names
    shallow = agent.sessions.create(kind="chat")
    assert "answer_subagent" in {d["function"]["name"] for d in agent.tool_definitions(shallow)}
