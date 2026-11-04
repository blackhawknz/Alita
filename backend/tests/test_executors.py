"""Executor protocol and the native adapter over SubAgentManager."""

import asyncio

from alita.core.runtime import current_session
from alita.executors import ExecutorEvent, NativeExecutor


async def _with_parent(agent):
    parent = agent.sessions.create(kind="chat")
    token = current_session.set(parent)
    return parent, token


async def test_native_start_returns_a_handle(agent):
    agent.llm.script = [{"content": "hello"}]
    executor = NativeExecutor(agent.subagent_manager)
    parent, token = await _with_parent(agent)
    try:
        handle = await executor.start("do the thing")
    finally:
        current_session.reset(token)
    assert isinstance(handle, str) and handle
    assert executor.has_handle(handle)
    await executor.close(handle)
    assert not executor.has_handle(handle)


async def test_native_send_yields_done_with_final_text(agent):
    agent.llm.script = [{"content": "final answer"}]
    executor = NativeExecutor(agent.subagent_manager)
    parent, token = await _with_parent(agent)
    try:
        handle = await executor.start("work")
        events = [event async for event in executor.send(handle, "")]
    finally:
        current_session.reset(token)

    assert isinstance(events[-1], ExecutorEvent)
    assert events[-1].kind == "done"
    assert events[-1].text == "final answer"
    assert events[-1].data["result"]["response"] == "final answer"


async def test_native_second_send_is_an_error(agent):
    agent.llm.script = [{"content": "once"}]
    executor = NativeExecutor(agent.subagent_manager)
    parent, token = await _with_parent(agent)
    try:
        handle = await executor.start("work")
        first = [event async for event in executor.send(handle, "")]
        second = [event async for event in executor.send(handle, "again")]
    finally:
        current_session.reset(token)

    assert first[-1].kind == "done"
    assert second[0].kind == "error"
    assert "single-shot" in second[0].text


async def test_native_interrupt_sets_child_flag(agent):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)

    agent.run_conversation = slow
    executor = NativeExecutor(agent.subagent_manager)
    handle = await executor.start("slow work")
    await started.wait()

    await executor.interrupt(handle)
    child = next(iter(agent.subagent_manager._active.values()))
    assert child.interrupt_requested is True

    await executor.close(handle)
    assert not executor.has_handle(handle)


async def test_native_close_removes_handle(agent):
    agent.llm.script = [{"content": "done"}]
    executor = NativeExecutor(agent.subagent_manager)
    handle = await executor.start("work")
    assert executor.has_handle(handle)
    await executor.close(handle)
    assert not executor.has_handle(handle)


def test_agent_registers_native_executor(agent):
    assert agent.get_executor("native") is agent.executors["native"]
    assert agent.get_executor("does-not-exist") is None
