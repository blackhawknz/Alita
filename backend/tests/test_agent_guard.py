"""
Tests for the host-exec guard integrated into AlitaAgent._dispatch_tool.

These construct a bare AlitaAgent via object.__new__ and wire only the three
attributes the guard reads (tool_registry, sandbox_mgr, allow_host_exec), so we
exercise the real dispatch method without building the full agent (no chromadb,
SQLite, or LLM provider needed).
"""

import pytest

from alita.core.agent import AlitaAgent
from alita.core.approval import ApprovalCoordinator, ApprovalPolicy
from alita.tools.registry import Tool, ToolRegistry
from alita.tools.sandbox import SandboxManager


async def _ok_handler(**kw):
    return {"success": True, "ran": True}


def _tool(name, tags):
    return Tool(
        name=name,
        description="",
        definition={"name": name, "parameters": {"type": "object", "properties": {}}},
        handler=_ok_handler,
        tags=tags,
    )


def _make_agent(tmp_path, *, docker_available, allow_host_exec):
    agent = object.__new__(AlitaAgent)
    agent.sandbox_mgr = SandboxManager(workspace_dir=str(tmp_path))
    agent.sandbox_mgr._docker_available = docker_available
    agent.allow_host_exec = allow_host_exec
    agent.tool_registry = ToolRegistry()
    # Approval gate is a no-op for these host-exec tests.
    agent.approval_policy = ApprovalPolicy(mode="never")
    agent.approval_coordinator = ApprovalCoordinator(timeout=1.0)
    agent.mutation_verifier = None
    return agent


async def test_terminal_blocked_without_docker(tmp_path):
    agent = _make_agent(tmp_path, docker_available=False, allow_host_exec=False)
    agent.tool_registry.register(_tool("terminal", ["host_exec"]))
    result = await agent._dispatch_tool("terminal", {"command": "ls"})
    assert result["success"] is False
    assert "Host execution disabled" in result["error"]


async def test_execute_code_blocked_even_with_docker(tmp_path):
    # execute_code never uses the sandbox, so it is never isolated.
    agent = _make_agent(tmp_path, docker_available=True, allow_host_exec=False)
    agent.tool_registry.register(_tool("execute_code", ["host_exec"]))
    result = await agent._dispatch_tool("execute_code", {"code": "print(1)"})
    assert result["success"] is False
    assert "Host execution disabled" in result["error"]


async def test_terminal_allowed_when_docker_present(tmp_path):
    agent = _make_agent(tmp_path, docker_available=True, allow_host_exec=False)
    agent.tool_registry.register(_tool("terminal", ["host_exec"]))
    result = await agent._dispatch_tool("terminal", {"command": "ls"})
    assert result == {"success": True, "ran": True}


async def test_host_exec_allowed_when_operator_opts_in(tmp_path):
    # No Docker, but ALITA_ALLOW_HOST_EXEC=true → permitted.
    agent = _make_agent(tmp_path, docker_available=False, allow_host_exec=True)
    agent.tool_registry.register(_tool("terminal", ["host_exec"]))
    result = await agent._dispatch_tool("terminal", {"command": "ls"})
    assert result == {"success": True, "ran": True}


async def test_non_host_exec_tool_unaffected(tmp_path):
    # A read-only tool is never gated, regardless of Docker/host-exec policy.
    agent = _make_agent(tmp_path, docker_available=False, allow_host_exec=False)
    agent.tool_registry.register(_tool("lookup", ["research"]))
    result = await agent._dispatch_tool("lookup", {"query": "x"})
    assert result == {"success": True, "ran": True}


async def test_unknown_tool_without_tags_does_not_crash(tmp_path):
    agent = _make_agent(tmp_path, docker_available=False, allow_host_exec=False)
    # Not registered at all → tool_registry.execute returns an error dict
    result = await agent._dispatch_tool("nope", {})
    assert result["success"] is False
    assert "Unknown tool" in result["error"]
