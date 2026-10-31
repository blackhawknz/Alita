"""Tests for credential pool stability, todo store, MCP name sanitising, sandbox and database fixes."""

import asyncio
import os
import sqlite3

import pytest

from alita.llm.credential_pool import CredentialPool, pool_from_value
from alita.tools.database_tool import DatabaseTool
from alita.tools.mcp_manager import MCPManager, mcp_tool_name, sanitize_tool_name
from alita.tools.mcp_client import MCPTool
from alita.tools.registry import ToolRegistry
from alita.tools.sandbox import SandboxManager, cap_output
from alita.tools.terminal import TerminalTool
from alita.tools.todo_tool import TodoStore


# ─── Credential pool ──────────────────────────────────────────────


def test_get_available_is_stable():
    pool = CredentialPool(["k1", "k2", "k3"])
    assert [pool.get_available() for _ in range(5)] == ["k1"] * 5


def test_mark_exhausted_and_rotate_advance():
    pool = CredentialPool(["k1", "k2", "k3"])
    pool.mark_exhausted("k1", cooldown_seconds=60)
    assert pool.get_available() == "k2"
    assert pool.get_available() == "k2"
    assert pool.rotate() == "k3"
    assert pool.get_available() == "k3"
    assert pool.rotate() == "k2"  # k1 still cooling down


def test_all_exhausted_returns_none_and_cooldown_expiry():
    pool = CredentialPool(["k1", "k2"])
    pool.mark_exhausted("k1", cooldown_seconds=60)
    pool.mark_exhausted("k2", cooldown_seconds=0)
    assert pool.get_available() == "k2"  # zero cooldown expired immediately
    pool.mark_exhausted("k2", cooldown_seconds=60)
    assert pool.get_available() is None
    assert pool.available_count == 0


def test_pool_from_value():
    pool = pool_from_value(" a , b ,, ")
    assert pool.total == 2
    assert pool.get_available() == "a"


# ─── Todo store ───────────────────────────────────────────────────


def test_todo_status_validation_and_max_items():
    store = TodoStore(max_items=2)
    a = store.add("a")
    with pytest.raises(ValueError):
        store.update(a.id, status="bogus")
    store.add("b")
    with pytest.raises(ValueError):
        store.add("c")  # full, nothing completed to evict
    store.update(a.id, status="done")  # alias -> completed
    assert store.get(a.id).status == "completed"
    store.add("c")  # evicts the completed item
    assert [t.title for t in store.list()] == ["b", "c"]


def test_todo_replace_all_and_round_trip():
    store = TodoStore()
    items = store.replace_all([
        {"title": "plan", "status": "completed"},
        {"title": "build", "status": "in_progress"},
        {"title": "test"},
    ])
    assert [i.status for i in items] == ["completed", "in_progress", "pending"]
    with pytest.raises(ValueError):
        store.replace_all([{"title": "x", "status": "nope"}])
    assert store.count == 3  # unchanged after a failed replace

    data = store.to_dict_list()
    other = TodoStore()
    other.from_dict_list(data)
    assert other.to_dict_list() == data
    new = other.add("next")
    assert new.id == max(d["id"] for d in data) + 1


# ─── MCP naming / shutdown ────────────────────────────────────────


def test_sanitize_tool_name():
    assert sanitize_tool_name("mcp__fs__read.file v2") == "mcp__fs__read_file_v2"
    long = "mcp__server__" + "x" * 100
    out = sanitize_tool_name(long)
    assert len(out) == 64
    assert out != sanitize_tool_name(long + "y")
    import re
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", out)


async def test_mcp_registers_sanitized_names_and_routes_original(tmp_path):
    reg = ToolRegistry()
    mgr = MCPManager(reg, config_path=str(tmp_path / "none.json"))
    calls = []

    class FakeClient:
        _initialized = True
        server_info = {}

        async def call_tool(self, name, args):
            calls.append(name)

            class R:
                def to_dict(self):
                    return {"success": True, "text": "ok"}
            return R()

        async def close(self):
            pass

    mgr._clients["my.server"] = FakeClient()
    tools = [MCPTool(name="read file", description="", input_schema={})]
    mgr._tools["my.server"] = {t.name: t for t in tools}
    mgr._register_server_tools("my.server", tools)

    name = mcp_tool_name("my.server", "read file")
    assert name == "mcp__my_server__read_file"
    assert reg.get(name).definition["name"] == name
    assert mgr.list_tool_names() == [name]
    res = await reg.execute(name)
    assert res["success"] is True
    assert calls == ["read file"]

    await mgr.shutdown()
    assert reg.get(name) is None
    assert mgr.list_tool_names() == []


# ─── Sandbox / terminal ───────────────────────────────────────────


def test_cap_output_keeps_tail():
    out = cap_output("a" * 50 + "TAIL", limit=10)
    assert out.endswith("a" * 6 + "TAIL")
    assert "truncated" in out


def test_failed_docker_run_is_not_isolated(tmp_path, monkeypatch):
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = True
    monkeypatch.setattr(
        "alita.tools.sandbox.run_process",
        lambda *a, **k: {"success": False, "stdout": "", "stderr": "daemon down", "exit_code": 1},
    )
    sb = mgr.create_sandbox()
    assert sb["type"] == "subprocess"
    assert mgr.is_isolated("terminal") is False


async def test_terminal_runs_in_workspace(tmp_path):
    (tmp_path / "marker_file.txt").write_text("x", encoding="utf-8")
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    term = TerminalTool(mgr)
    cmd = "Get-ChildItem -Name" if os.name == "nt" else "ls"
    res = await term.execute(command=cmd, timeout=60, unexpected="ignored")
    assert res["success"] is True, res
    assert "marker_file.txt" in res["stdout"]
    mgr.destroy_all()


async def test_terminal_does_not_block_event_loop(tmp_path):
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    term = TerminalTool(mgr)
    cmd = "Start-Sleep -Milliseconds 1500" if os.name == "nt" else "sleep 1.5"
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    t = asyncio.create_task(ticker())
    await term.execute(command=cmd, timeout=30)
    t.cancel()
    assert ticks >= 5


def test_destroy_all_cleans_legacy_sandbox_dirs(tmp_path):
    legacy = tmp_path / ".sandboxes" / "abc"
    legacy.mkdir(parents=True)
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    mgr.create_sandbox()
    mgr.destroy_all()
    assert not (tmp_path / ".sandboxes").exists()


# ─── Database tool ────────────────────────────────────────────────


async def test_database_confined_and_query_readonly(tmp_path):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    tool = DatabaseTool(str(tmp_path))
    res = await tool.execute(action="query", db_path="app.db", sql="SELECT x FROM t", extra=1)
    assert res["success"] is True and res["rows"] == [{"x": 1}]

    res = await tool.execute(action="query", db_path="app.db", sql="DELETE FROM t")
    assert res["success"] is False

    res = await tool.execute(action="query", db_path="../other.db", sql="SELECT 1")
    assert res["success"] is False and "traversal" in res["error"].lower()

    res = await tool.execute(action="execute", db_path="app.db", sql="ATTACH DATABASE 'x.db' AS x")
    assert res["success"] is False
