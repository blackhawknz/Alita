"""
End-to-end tests of the agent loop with a scripted LLM.

Covers persistence fidelity, multi-tool steps, session isolation and reload,
approvals (interactive and headless), interrupts, compaction, regenerate/edit
and the memory recall injection.
"""

import asyncio
import json

import pytest

from alita.core.session import SessionManager, repair_history
from tests.conftest import FakeLLM, drain, tool_call


async def collect(agent, sess, content, **kw):
    events = []

    async def emit(ev):
        events.append(ev)

    await agent._run_turn(sess, content, emit, **kw)
    return events


async def test_simple_turn_persists_and_titles(agent):
    agent.llm.script = [{"content": "Hello there!"}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "Hi Alita, what's up?")
    types = [e["type"] for e in events]
    assert types[0] == "turn_start"
    assert "text" in types and types[-1] == "done"
    done = events[-1]
    assert done["final_response"] == "Hello there!"
    assert done["error"] is None
    rows = agent.sqlite.load_messages(sess.id)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert sess.title == "Hi Alita, what's up?"


async def test_multi_tool_step_keeps_every_result(agent):
    """Regression: v0.1 dropped the 2nd+ tool results of a batch from context."""
    calls = [tool_call("todo", {"todos": [{"title": "a", "status": "in_progress"}]}, "c1"),
             tool_call("session_search", {"query": "anything"}, "c2"),
             tool_call("skill_view", {}, "c3")]
    agent.llm.script = [{"content": "", "tool_calls": calls}, {"content": "All done."}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "Do three things")
    assert events[-1]["final_response"] == "All done."
    # Second request must contain the assistant tool_calls message and all 3 tool results.
    second = agent.llm.requests[1]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]
    assert any(m.get("tool_calls") for m in second if m["role"] == "assistant")
    # And it all survives a reload from SQLite.
    fresh = SessionManager(agent.sqlite).get(sess.id)
    roles = [m["role"] for m in fresh.messages]
    assert roles == ["user", "assistant", "tool", "tool", "tool", "assistant"]
    assert fresh.todos.list()[0].title == "a"


async def test_sessions_are_isolated(agent):
    agent.llm.script = [{"content": "one"}, {"content": "two"}]
    s1 = agent.sessions.create(kind="chat")
    s2 = agent.sessions.create(kind="chat")
    await collect(agent, s1, "first session message")
    await collect(agent, s2, "second session message")
    sent_to_s2 = json.dumps(agent.llm.requests[1])
    assert "first session message" not in sent_to_s2
    assert len(s1.messages) == 2 and len(s2.messages) == 2


async def test_invalid_tool_json_is_reported_not_crashed(agent):
    bad = {"id": "x1", "type": "function", "function": {"name": "todo", "arguments": "{not json"}}
    agent.llm.script = [{"content": "", "tool_calls": [bad]}, {"content": "recovered"}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "go")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["success"] is False and "Invalid JSON" in result["result"]["error"]
    assert events[-1]["final_response"] == "recovered"


async def test_unknown_tool(agent):
    agent.llm.script = [{"content": "", "tool_calls": [tool_call("nope", {})]}, {"content": "ok"}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "go")
    result = next(e for e in events if e["type"] == "tool_result")
    assert "Unknown tool" in result["result"]["error"]


async def test_interactive_approval_approve(agent, tmp_path):
    target = tmp_path / "workspace" / "existing.txt"
    target.write_text("old")
    call = tool_call("file_ops", {"action": "write", "path": "existing.txt", "content": "new"}, "w1")
    agent.llm.script = [{"content": "", "tool_calls": [call]}, {"content": "written"}]
    sess = agent.sessions.create(kind="chat")
    run = agent.start_turn(sess, "overwrite it")
    approval_id = None
    async for ev in run.subscribe():
        if ev["type"] == "approval_requested":
            approval_id = ev["approval_id"]
            assert ev["tool_call_id"] == "w1"
            agent.approval_coordinator.resolve(approval_id, True)
    assert approval_id
    assert target.read_text() == "new"
    assert run.events[-1]["final_response"] == "written"


async def test_interactive_approval_deny(agent, tmp_path):
    target = tmp_path / "workspace" / "keep.txt"
    target.write_text("keep")
    call = tool_call("file_ops", {"action": "write", "path": "keep.txt", "content": "x"})
    agent.llm.script = [{"content": "", "tool_calls": [call]}, {"content": "ok, not touching it"}]
    sess = agent.sessions.create(kind="chat")
    run = agent.start_turn(sess, "overwrite it")
    async for ev in run.subscribe():
        if ev["type"] == "approval_requested":
            agent.approval_coordinator.resolve(ev["approval_id"], False)
    assert target.read_text() == "keep"
    result = next(e for e in run.events if e["type"] == "tool_result")
    assert result["result"]["denied"] is True


async def test_headless_session_denies_destructive(agent, tmp_path):
    target = tmp_path / "workspace" / "job.txt"
    target.write_text("orig")
    call = tool_call("file_ops", {"action": "write", "path": "job.txt", "content": "x"})
    agent.llm.script = [{"content": "", "tool_calls": [call]}, {"content": "reported"}]
    result = await agent.run_conversation("scheduled thing", kind="job")
    assert result["final_response"] == "reported"
    assert target.read_text() == "orig"
    assert result["tool_trace"][0]["success"] is False
    job_sess = agent.sqlite.get_session(result["session_id"])
    assert job_sess["kind"] == "job"


async def test_interrupt_stops_turn(agent):
    agent.llm.delay = 0.01
    agent.llm.script = [{"content": "x" * 400}]
    sess = agent.sessions.create(kind="chat")
    run = agent.start_turn(sess, "long answer please")
    await asyncio.sleep(0.05)
    agent.interrupt(sess.id)
    await run.task
    done = run.events[-1]
    assert done["type"] == "done" and done["interrupted"] is True
    stored = agent.sqlite.load_messages(sess.id)
    assert stored[-1]["role"] == "assistant"
    assert stored[-1]["_meta"].get("interrupted") is True
    assert not sess.is_running


async def test_resubscribe_replays_events(agent):
    agent.llm.script = [{"content": "replayable answer"}]
    sess = agent.sessions.create(kind="chat")
    run = agent.start_turn(sess, "hi")
    await run.task
    replay = [ev async for ev in run.subscribe(after=0)]
    assert replay[-1]["type"] == "done"
    tail = [ev async for ev in run.subscribe(after=len(run.events) - 1)]
    assert len(tail) == 1


async def test_concurrent_turn_rejected(agent):
    agent.llm.delay = 0.01
    agent.llm.script = [{"content": "y" * 200}]
    sess = agent.sessions.create(kind="chat")
    run = agent.start_turn(sess, "hi")
    with pytest.raises(RuntimeError):
        agent.start_turn(sess, "again")
    agent.interrupt(sess.id)
    await run.task


async def test_compaction_when_window_small(agent):
    agent.llm = FakeLLM(window=12000)
    agent.settings.max_output_tokens = 1000
    sess = agent.sessions.create(kind="chat")
    big = "lorem ipsum dolor sit amet " * 400  # ~10.8k chars ≈ 3k tokens per turn
    for i in range(4):
        agent.llm.script.append({"content": f"reply {i} " + big})
        await collect(agent, sess, f"question {i} " + big)
    assert sess.context_summary.startswith("## User goals")
    assert sess.compacted_upto > 0
    # Summary is in the system prompt of later requests; full transcript still in SQLite.
    assert "SUMMARY OF EARLIER TURNS" in agent.llm.requests[-1][0]["content"]
    assert len(agent.sqlite.load_messages(sess.id)) == 8
    reloaded = SessionManager(agent.sqlite).get(sess.id)
    assert reloaded.context_summary == sess.context_summary
    assert len(reloaded.messages) < 8


async def test_regenerate_replaces_last_answer(agent):
    agent.llm.script = [{"content": "first answer"}, {"content": "second answer"}]
    sess = agent.sessions.create(kind="chat")
    await collect(agent, sess, "question")
    await collect(agent, sess, None, regenerate=True)
    rows = agent.sqlite.load_messages(sess.id)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[-1]["content"] == "second answer"


async def test_edit_message_truncates_history(agent):
    agent.llm.script = [{"content": "a1"}, {"content": "a2"}, {"content": "a2-edited"}]
    sess = agent.sessions.create(kind="chat")
    await collect(agent, sess, "q1")
    await collect(agent, sess, "q2")
    q2_id = agent.sqlite.load_messages(sess.id)[2]["_id"]
    await collect(agent, sess, "q2 edited", edit_message_id=q2_id)
    rows = agent.sqlite.load_messages(sess.id)
    assert [r["content"] for r in rows] == ["q1", "a1", "q2 edited", "a2-edited"]


async def test_recalled_memories_are_injected_ephemerally(agent):
    await agent.longterm.add("User's favourite programming language is Rust", category="preference", importance=4)
    agent.llm.script = [{"content": "Rust, of course."}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "Which programming language should I use for my new CLI tool?")
    assert any(e["type"] == "memory_recall" for e in events)
    last_user = [m for m in agent.llm.requests[0] if m["role"] == "user"][-1]
    assert "<recalled_memories>" in last_user["content"] and "Rust" in last_user["content"]
    # Not persisted into the stored user message.
    stored = agent.sqlite.load_messages(sess.id)[0]
    assert "<recalled_memories>" not in stored["content"]


async def test_core_memory_in_system_prompt(agent):
    agent.core_memory.set("user", "- Name: Sam\n- Prefers metric units")
    agent.llm.script = [{"content": "hi Sam"}]
    sess = agent.sessions.create(kind="chat")
    await collect(agent, sess, "hello")
    system = agent.llm.requests[0][0]["content"]
    assert "Name: Sam" in system and "<available_skills>" in system


async def test_memory_extraction_after_turn(agent):
    agent.settings.memory_auto_extract = True
    agent.llm.extraction = {"memories": [{"op": "add", "content": "User is planning a trip to Japan in May 2027",
                                          "category": "event", "importance": 4}],
                            "profile_add": ["Name: Sam"]}
    agent.llm.script = [{"content": "Sounds fun!"}]
    sess = agent.sessions.create(kind="chat")
    await collect(agent, sess, "I'm Sam and I'm going to Japan next May")
    await drain(agent)
    mems = agent.longterm.list()
    assert any("Japan" in m["content"] for m in mems)
    assert "Name: Sam" in agent.core_memory.get("user")


async def test_step_limit_forces_final_answer(agent):
    loop_call = lambda msgs: {"content": "", "tool_calls": [tool_call("skill_view", {"name": "x"})]}  # noqa: E731
    agent.llm.script = [loop_call, loop_call, {"content": "Here is where I got to."}]
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "loop", max_iterations=2)
    assert events[-1]["final_response"] == "Here is where I got to."
    last_req_user = [m for m in agent.llm.requests[-1] if m["role"] == "user"][-1]["content"]
    assert "Do NOT call any tools" in last_req_user


async def test_llm_error_surfaces_as_event(agent):
    from alita.llm.provider import LLMError

    class Broken(FakeLLM):
        async def chat_stream(self, *a, **k):
            raise LLMError("deepseek error 401: invalid key")
            yield {}

    agent.llm = Broken()
    sess = agent.sessions.create(kind="chat")
    events = await collect(agent, sess, "hi")
    assert any(e["type"] == "error" and "invalid key" in e["message"] for e in events)
    assert events[-1]["error"]


def test_repair_history_adds_missing_results_and_drops_orphans():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "zzz", "content": "orphan"},
        {"role": "assistant", "content": "", "tool_calls": [tool_call("a", {}, "t1"), tool_call("b", {}, "t2")]},
        {"role": "tool", "tool_call_id": "t2", "content": "B"},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "final"},
    ]
    out = repair_history(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "assistant"]
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["t1", "t2"]
    assert out[2].get("_synthetic") is True
