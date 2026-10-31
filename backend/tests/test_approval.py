"""
Tests for the approval gate (ApprovalPolicy + ApprovalCoordinator).

The policy is a pure helper — no agent, no event loop required for the
classification tests. The coordinator uses asyncio futures, so those tests
run inside the auto asyncio event loop provided by pytest-asyncio.
"""

import asyncio

import pytest

from alita.core.approval import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalPolicy,
)


# ─── ApprovalPolicy.classify ───────────────────────────────────────


def test_mode_never_allows_everything():
    p = ApprovalPolicy(mode="never")
    d = p.classify("terminal", {"command": "rm -rf /"})
    assert d.needs_approval is False


def test_mode_always_gates_everything():
    p = ApprovalPolicy(mode="always")
    d = p.classify("browser", {"action": "fetch", "url": "https://example.com"})
    assert d.needs_approval is True
    assert "always" in d.reason.lower()


def test_allowlist_skips_gate():
    p = ApprovalPolicy(mode="auto", allowlist=["terminal"])
    d = p.classify("terminal", {"command": "rm -rf /"})
    assert d.needs_approval is False


def test_readonly_file_ops_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("file_ops", {"action": "read", "path": "/tmp/x"})
    assert d.needs_approval is False


@pytest.mark.parametrize("action", ["write", "patch", "create", "overwrite", "delete", "move"])
def test_file_write_actions_gated(action):
    p = ApprovalPolicy(mode="auto")
    d = p.classify("file_ops", {"action": action, "path": "/tmp/x"})
    assert d.needs_approval is True
    assert action in d.reason
    assert "/tmp/x" in d.reason
    assert d.severity == "warning"


@pytest.mark.parametrize("cmd,label", [
    ("rm -rf /tmp", "rm"),
    ("del foo.txt", "del"),
    ("sudo apt install x", "sudo"),
    ("format C:", "format"),
    ("dd if=/dev/zero of=/dev/sda", "dd"),
    ("mkfs.ext4 /dev/sda1", "mkfs"),
    ("shutdown -h now", "shutdown"),
])
def test_terminal_destructive_commands_gated(cmd, label):
    p = ApprovalPolicy(mode="auto")
    d = p.classify("terminal", {"command": cmd})
    assert d.needs_approval is True
    assert label in d.reason.lower() or label in cmd.lower()
    assert d.severity == "danger"


def test_terminal_benign_command_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("terminal", {"command": "ls -la /tmp"})
    assert d.needs_approval is False


def test_terminal_empty_command_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("terminal", {"command": ""})
    assert d.needs_approval is False


def test_git_push_gated():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("git", {"subcommand": "push"})
    assert d.needs_approval is True
    assert "push" in d.reason


def test_git_force_push_gated_as_danger():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("git", {"subcommand": "push", "args": "--force origin main"})
    assert d.needs_approval is True
    assert "force" in d.reason.lower()
    assert d.severity == "danger"


def test_git_reset_gated():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("git", {"subcommand": "reset"})
    assert d.needs_approval is True


def test_git_status_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("git", {"subcommand": "status"})
    assert d.needs_approval is False


@pytest.mark.parametrize("sql", [
    "DROP TABLE users",
    "DELETE FROM users WHERE 1=1",
    "UPDATE users SET x=1",
    "TRUNCATE TABLE logs",
    "ALTER TABLE users ADD COLUMN x",
])
def test_database_destructive_sql_gated(sql):
    p = ApprovalPolicy(mode="auto")
    d = p.classify("database", {"action": "execute", "sql": sql})
    assert d.needs_approval is True


def test_database_select_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("database", {"action": "query", "sql": "SELECT 1"})
    assert d.needs_approval is False


def test_database_schema_inspect_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("database", {"action": "schema"})
    assert d.needs_approval is False


def test_execute_code_with_rm_gated():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("execute_code", {"code": "import os; os.system('rm -rf /tmp/x')"})
    assert d.needs_approval is True
    assert d.severity == "danger"


def test_execute_code_benign_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("execute_code", {"code": "print('hello')"})
    assert d.needs_approval is False


def test_browser_fetch_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("browser", {"action": "fetch", "url": "https://example.com"})
    assert d.needs_approval is False


def test_http_request_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("http_request", {"url": "https://api.example.com"})
    assert d.needs_approval is False


def test_mcp_tools_allowed_by_default():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("mcp__filesystem__write_file", {"path": "/x"})
    assert d.needs_approval is False


def test_unknown_tool_allowed():
    p = ApprovalPolicy(mode="auto")
    d = p.classify("custom_tool", {})
    assert d.needs_approval is False


def test_env_mode_resolved(monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_MODE", "never")
    p = ApprovalPolicy()
    assert p.mode == "never"
    assert p.classify("terminal", {"command": "rm -rf /"}).needs_approval is False


def test_env_allowlist_resolved(monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_ALLOWLIST", "file_ops, terminal")
    p = ApprovalPolicy()
    assert "file_ops" in p.allowlist
    assert "terminal" in p.allowlist


def test_env_timeout_resolved(monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_TIMEOUT", "42")
    p = ApprovalPolicy()
    assert p.timeout == 42.0


def test_invalid_env_mode_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_MODE", "bogus")
    p = ApprovalPolicy()
    assert p.mode == "auto"


# ─── ApprovalCoordinator ───────────────────────────────────────────


async def test_request_and_approve():
    coord = ApprovalCoordinator(timeout=5.0)
    approval_id, future = await coord.request(
        tool_name="terminal", params={"command": "rm /x"},
        reason="rm removes files", severity="danger",
    )
    assert approval_id
    assert coord.pending_count == 1

    # Resolve from the responder side (simulates /approve endpoint).
    ok = coord.resolve(approval_id, approved=True)
    assert ok is True

    approved = await coord.wait(approval_id, future)
    assert approved is True
    assert coord.pending_count == 0  # cleaned up


async def test_request_and_deny():
    coord = ApprovalCoordinator(timeout=5.0)
    approval_id, future = await coord.request(
        tool_name="file_ops", params={"action": "write"},
        reason="file_ops 'write' on /x", severity="warning",
    )
    coord.resolve(approval_id, approved=False)
    approved = await coord.wait(approval_id, future)
    assert approved is False
    assert coord.pending_count == 0


async def test_timeout_auto_denies():
    coord = ApprovalCoordinator(timeout=0.05)
    approval_id, future = await coord.request(
        tool_name="git", params={"subcommand": "push"},
        reason="git push publishes commits", severity="warning",
    )
    approved = await coord.wait(approval_id, future, timeout=0.05)
    assert approved is False
    assert coord.pending_count == 0


async def test_resolve_unknown_id_returns_false():
    coord = ApprovalCoordinator()
    assert coord.resolve("nonexistent", approved=True) is False


async def test_resolve_already_resolved_returns_false():
    coord = ApprovalCoordinator(timeout=5.0)
    approval_id, future = await coord.request(
        "terminal", {"command": "rm x"}, "r", "danger"
    )
    assert coord.resolve(approval_id, True) is True
    # Second resolve on the same id must fail — the future is already done.
    assert coord.resolve(approval_id, True) is False
    await coord.wait(approval_id, future)


async def test_list_pending_snapshot():
    coord = ApprovalCoordinator(timeout=10.0)
    id1, _ = await coord.request("terminal", {"command": "rm a"}, "r1", "danger")
    id2, _ = await coord.request("file_ops", {"action": "write"}, "r2", "warning")
    pending = coord.list_pending()
    assert len(pending) == 2
    ids = {p["approval_id"] for p in pending}
    assert ids == {id1, id2}
    # Each entry exposes tool/params/reason/severity.
    by_id = {p["approval_id"]: p for p in pending}
    assert by_id[id1]["tool_name"] == "terminal"
    assert by_id[id2]["tool_name"] == "file_ops"
    assert by_id[id1]["severity"] == "danger"


async def test_cancel_pending():
    coord = ApprovalCoordinator(timeout=10.0)
    approval_id, future = await coord.request(
        "terminal", {"command": "rm x"}, "r", "danger"
    )
    assert coord.cancel(approval_id) is True
    assert coord.pending_count == 0
    # Awaiting the cancelled future must not hang; it raises CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await coord.wait(approval_id, future)


async def test_clear_denies_all():
    coord = ApprovalCoordinator(timeout=10.0)
    id1, f1 = await coord.request("a", {}, "r", "danger")
    id2, f2 = await coord.request("b", {}, "r", "danger")
    coord.clear()
    assert coord.pending_count == 0
    # Resolved as denied (not cancelled) so in-flight turns are not killed.
    assert not f1.cancelled() and f1.result() is False
    assert not f2.cancelled() and f2.result() is False
    assert await coord.wait(id1, f1) is False


async def test_wait_with_zero_timeout_disables_auto_deny():
    # timeout=0 means wait forever for an explicit decision (no auto-deny).
    coord = ApprovalCoordinator(timeout=0)
    approval_id, future = await coord.request("a", {}, "r", "danger")
    # Approve quickly so the test doesn't actually hang.
    coord.resolve(approval_id, True)
    approved = await coord.wait(approval_id, future)
    assert approved is True


# ─── Agent integration (smoke) ────────────────────────────────────


async def test_agent_denies_blocked_dispatch(tmp_path):
    """
    Smoke test: a destructive file_ops write through _dispatch_tool (non-stream
    path) is denied by default because no one resolves the approval before the
    timeout. We use a short timeout to keep the test fast.
    """
    from alita.core.agent import AlitaAgent
    from alita.tools.registry import Tool, ToolRegistry

    agent = object.__new__(AlitaAgent)
    agent.allow_host_exec = True
    agent.sandbox_mgr = None  # not used by file_ops in this path
    agent.tool_registry = ToolRegistry()
    agent.mutation_verifier = None

    # Insert a fake file_ops that records if it ran.
    ran = []

    async def fake_file_ops_handler(**kw):
        ran.append(kw)
        return {"success": True}

    agent.tool_registry.register(Tool(
        name="file_ops",
        description="",
        definition={"name": "file_ops", "parameters": {"type": "object", "properties": {}}},
        handler=fake_file_ops_handler,
        tags=[],
    ))

    # Use a tiny approval timeout so the test resolves fast.
    agent.approval_policy = ApprovalPolicy(mode="auto", timeout=0.05)
    agent.approval_coordinator = ApprovalCoordinator(timeout=0.05)

    # Override the host-exec guard helper to skip the sandbox check.
    from alita.tools import sandbox as sandbox_mod
    agent.sandbox_mgr = type("S", (), {"is_isolated": lambda self, name: True})()

    result = await agent._dispatch_tool("file_ops", {"action": "write", "path": "/tmp/x"})
    assert result["success"] is False
    assert result.get("denied") is True
    assert ran == []  # the tool never ran


async def test_agent_allows_readonly_dispatch(tmp_path):
    """Read-only file_ops (action=read) bypasses the gate and runs."""
    from alita.core.agent import AlitaAgent
    from alita.tools.registry import Tool, ToolRegistry

    agent = object.__new__(AlitaAgent)
    agent.allow_host_exec = True
    agent.tool_registry = ToolRegistry()

    ran = []

    async def fake_file_ops_handler(**kw):
        ran.append(kw)
        return {"success": True, "content": "file contents"}

    agent.tool_registry.register(Tool(
        name="file_ops",
        description="",
        definition={"name": "file_ops", "parameters": {"type": "object", "properties": {}}},
        handler=fake_file_ops_handler,
        tags=[],
    ))
    agent.approval_policy = ApprovalPolicy(mode="auto")
    agent.approval_coordinator = ApprovalCoordinator(timeout=1.0)
    agent.mutation_verifier = None
    agent.sandbox_mgr = type("S", (), {"is_isolated": lambda self, name: True})()

    result = await agent._dispatch_tool("file_ops", {"action": "read", "path": "/tmp/x"})
    assert result["success"] is True
    assert ran  # the tool actually ran