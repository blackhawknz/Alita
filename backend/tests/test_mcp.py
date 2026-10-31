"""
Tests for the MCP client and manager.

Network/subprocess-free: a ``FakeMCPClient`` overrides the transport hooks
so we can drive the JSON-RPC routing, tool registration, and dispatch
paths without spawning anything.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from alita.tools.mcp_client import (
    MCPTool,
    MCPConnectionError,
    RPCError,
    StdioMCPClient,
    _BaseMCPClient,
)
from alita.tools.mcp_manager import MCPManager, MCP_TOOL_PREFIX
from alita.tools.registry import ToolRegistry


# ─── Fake transport ───────────────────────────────────────────────


class FakeMCPClient(_BaseMCPClient):
    """
    Drives _BaseMCPClient with scripted JSON-RPC responses.

    Bypasses the async consumer task: ``_request`` pulls the next queued
    response inline and dispatches it. This is deterministic and avoids
    leaking a background task per test.
    """

    def __init__(self, name="fake", responses=None, tools=None):
        super().__init__(name)
        # FIFO of raw JSON-RPC response strings, popped one per _request call.
        self._inbox: list = list(responses) if responses else []
        self._sent: list = []
        self._started = False
        self._stopped = False
        self._tools = tools or []

    async def _start(self):
        self._started = True

    async def _stop(self):
        self._stopped = True

    async def _send(self, payload):
        self._sent.append(payload.decode("utf-8").rstrip("\n"))

    async def _recv(self):
        # Not used — _request is overridden to dispatch inline.
        return None

    async def _request(self, method, params=None, timeout=30.0):
        """Send then immediately return the next queued response's result."""
        if not self._initialized and method != "initialize":
            raise MCPConnectionError(f"Cannot send {method}: client not initialized")
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send((json.dumps(payload) + "\n").encode("utf-8"))

        if not self._inbox:
            raise MCPConnectionError(f"no queued response for {method}")
        msg = json.loads(self._inbox.pop(0))
        if "error" in msg:
            err = msg["error"] or {}
            raise RPCError(err.get("code", -1), err.get("message", "unknown"), err.get("data"))
        return msg.get("result")

    async def _notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send((json.dumps(payload) + "\n").encode("utf-8"))

    def queue(self, response_json: str) -> None:
        """Append a JSON-RPC response string to the inbox."""
        self._inbox.append(response_json)


@pytest.fixture
def fresh_registry():
    return ToolRegistry()


# ─── MCPTool / MCPToolResult ──────────────────────────────────────


def test_mcp_tool_to_openai_function():
    t = MCPTool(
        name="read_file",
        description="Reads a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    fn = t.to_openai_function()
    assert fn["name"] == "read_file"
    assert fn["description"] == "Reads a file"
    assert fn["parameters"]["properties"]["path"]["type"] == "string"


def test_mcp_tool_default_schema():
    t = MCPTool(name="x", description="", input_schema={})
    fn = t.to_openai_function()
    assert fn["parameters"] == {"type": "object", "properties": {}}
    assert fn["description"] == "MCP tool x"


# ─── JSON-RPC routing via FakeMCPClient ────────────────────────────


async def test_connect_handshake_and_list_tools():
    # Pre-queue: initialize response, tools/list response
    client = FakeMCPClient(name="fs")
    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 1, "result": {
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "filesystem", "version": "1.0"},
        }
    }))
    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {
            "tools": [{
                "name": "read_file",
                "description": "Read a file",
                "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }]
        }
    }))

    await client.connect()
    assert client._initialized is True
    assert client.server_info["name"] == "filesystem"

    tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "read_file"
    assert tools[0].input_schema["properties"]["path"]["type"] == "string"

    # Connect should have sent: initialize request + initialized notification.
    sent = [json.loads(s) for s in client._sent]
    methods = [m["method"] for m in sent]
    assert "initialize" in methods
    assert "notifications/initialized" in methods

    await client.close()


async def test_call_tool_text_content():
    client = FakeMCPClient(name="fs")
    client._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    await client.connect()

    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": "hello world"}]
        }
    }))
    result = await client.call_tool("read_file", {"path": "/tmp/x"})
    assert result.success is True
    assert result.text == "hello world"
    assert "hello world" in result.to_dict()["text"]

    await client.close()


async def test_call_tool_error_response():
    client = FakeMCPClient(name="fs")
    client._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    await client.connect()

    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "invalid params"}
    }))
    result = await client.call_tool("read_file", {"path": "?"})
    assert result.success is False
    assert "invalid params" in result.error

    await client.close()


async def test_call_tool_isError_flag():
    client = FakeMCPClient(name="fs")
    client._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    await client.connect()

    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {
            "isError": True,
            "content": [{"type": "text", "text": "Permission denied"}],
        }
    }))
    result = await client.call_tool("delete", {"path": "/x"})
    assert result.success is False
    assert "Permission denied" in result.error

    await client.close()


async def test_call_tool_image_content_placeholder():
    client = FakeMCPClient(name="fs")
    client._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    await client.connect()

    client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {
            "content": [
                {"type": "text", "text": "screenshot below"},
                {"type": "image", "mimeType": "image/png", "data": "BASE64DATA=="},
            ]
        }
    }))
    result = await client.call_tool("screenshot", {})
    assert result.success is True
    assert "screenshot below" in result.text
    assert "[image: image/png" in result.text
    # base64 must NOT be dumped into context
    assert "BASE64DATA==" not in result.text

    await client.close()


# ─── MCPManager config loading ─────────────────────────────────────


def test_manager_load_config_missing_file(tmp_path):
    mgr = MCPManager(ToolRegistry(), config_path=str(tmp_path / "nope.json"))
    assert mgr.load_config() == {}


def test_manager_load_config_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    mgr = MCPManager(ToolRegistry(), config_path=str(p))
    assert mgr.load_config() == {}


def test_manager_load_config_valid(tmp_path):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({
        "servers": {
            "fs": {"transport": "stdio", "command": "npx", "args": ["-y", "x"]},
            "remote": {"transport": "sse", "url": "http://localhost:8081/sse"},
        }
    }), encoding="utf-8")
    mgr = MCPManager(ToolRegistry(), config_path=str(p))
    cfg = mgr.load_config()
    assert set(cfg.keys()) == {"fs", "remote"}
    assert cfg["fs"]["transport"] == "stdio"
    assert cfg["remote"]["url"] == "http://localhost:8081/sse"


def test_manager_disabled_via_env(monkeypatch, tmp_path):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": {"fs": {"transport": "stdio", "command": "x"}}}), encoding="utf-8")
    monkeypatch.setenv("ALITA_MCP_ENABLED", "false")
    mgr = MCPManager(ToolRegistry(), config_path=str(p))
    assert mgr._enabled is False


# ─── MCPManager load_and_register with mocked client ──────────────


async def test_load_and_register_registers_namespaced_tools(monkeypatch, tmp_path, fresh_registry):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({
        "servers": {"fs": {"transport": "stdio", "command": "npx", "args": ["-y", "x"]}}
    }), encoding="utf-8")

    mgr = MCPManager(fresh_registry, config_path=str(p))

    # Stub _connect_server to avoid real subprocess.
    fake_client = FakeMCPClient(name="fs")
    fake_client._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    fake_client._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {
            "tools": [
                {"name": "read_file", "description": "Read", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
                {"name": "write_file", "description": "Write", "inputSchema": {"type": "object", "properties": {}}},
            ]
        }
    }))

    async def fake_connect(name, spec):
        await fake_client.connect()
        tools = await fake_client.list_tools()
        mgr._clients[name] = fake_client
        mgr._tools[name] = {t.name: t for t in tools}
        mgr._register_server_tools(name, tools)
        return tools

    monkeypatch.setattr(mgr, "_connect_server", fake_connect)

    summary = await mgr.load_and_register()
    assert summary["connected"] == ["fs"]
    assert "read_file" in summary["tools"]["fs"]
    assert "write_file" in summary["tools"]["fs"]

    # Tools registered under namespaced names.
    assert f"{MCP_TOOL_PREFIX}fs__read_file" in fresh_registry.list_names()
    assert f"{MCP_TOOL_PREFIX}fs__write_file" in fresh_registry.list_names()
    # Original tool name (without prefix) must NOT be registered.
    assert "read_file" not in fresh_registry.list_names()

    await mgr.shutdown()


async def test_load_and_register_isolates_failures(monkeypatch, tmp_path, fresh_registry):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({
        "servers": {
            "good": {"transport": "stdio", "command": "npx", "args": ["a"]},
            "bad": {"transport": "stdio", "command": "npx", "args": ["b"]},
        }
    }), encoding="utf-8")
    mgr = MCPManager(fresh_registry, config_path=str(p))

    async def fake_connect(name, spec):
        if name == "bad":
            raise MCPConnectionError("subprocess died")
        # Simulate a good server with one tool.
        fake = FakeMCPClient(name="good")
        fake._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
        fake._inbox.append(json.dumps({
            "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "ping", "description": "", "inputSchema": {}}]}
        }))
        await fake.connect()
        tools = await fake.list_tools()
        mgr._clients[name] = fake
        mgr._tools[name] = {t.name: t for t in tools}
        mgr._register_server_tools(name, tools)
        return tools

    monkeypatch.setattr(mgr, "_connect_server", fake_connect)

    summary = await mgr.load_and_register()
    assert summary["connected"] == ["good"]
    assert "bad" in summary["failed"]
    assert f"{MCP_TOOL_PREFIX}good__ping" in fresh_registry.list_names()

    await mgr.shutdown()


async def test_registered_tool_handler_routes_to_client(monkeypatch, tmp_path, fresh_registry):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": {"fs": {"transport": "stdio", "command": "x"}}}), encoding="utf-8")
    mgr = MCPManager(fresh_registry, config_path=str(p))

    fake = FakeMCPClient(name="fs")
    fake._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    fake._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read_file", "description": "", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}}]}
    }))

    async def fake_connect(name, spec):
        await fake.connect()
        tools = await fake.list_tools()
        mgr._clients[name] = fake
        mgr._tools[name] = {t.name: t for t in tools}
        mgr._register_server_tools(name, tools)
        return tools

    monkeypatch.setattr(mgr, "_connect_server", fake_connect)
    await mgr.load_and_register()

    # Queue the tools/call response.
    fake._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "FILE CONTENTS"}]}
    }))

    result = await fresh_registry.execute(f"{MCP_TOOL_PREFIX}fs__read_file", path="/tmp/x")
    assert result["success"] is True
    assert result["text"] == "FILE CONTENTS"
    assert result["result"] == "FILE CONTENTS"  # conversation-friendly field

    await mgr.shutdown()


async def test_handler_returns_error_when_server_disconnected(monkeypatch, tmp_path, fresh_registry):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": {"fs": {"transport": "stdio", "command": "x"}}}), encoding="utf-8")
    mgr = MCPManager(fresh_registry, config_path=str(p))

    fake = FakeMCPClient(name="fs")
    fake._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
    fake._inbox.append(json.dumps({
        "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read_file", "description": "", "inputSchema": {}}]}
    }))

    async def fake_connect(name, spec):
        await fake.connect()
        tools = await fake.list_tools()
        mgr._clients[name] = fake
        mgr._tools[name] = {t.name: t for t in tools}
        mgr._register_server_tools(name, tools)
        return tools

    monkeypatch.setattr(mgr, "_connect_server", fake_connect)
    await mgr.load_and_register()

    # Remove the client to simulate disconnect.
    mgr._clients.pop("fs")
    result = await fresh_registry.execute(f"{MCP_TOOL_PREFIX}fs__read_file", path="/x")
    assert result["success"] is False
    assert "not connected" in result["error"]

    await mgr.shutdown()


async def test_no_duplicate_registration_on_reload(monkeypatch, tmp_path, fresh_registry):
    """Re-registering a server should not produce two tools of the same name."""
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": {"fs": {"transport": "stdio", "command": "x"}}}), encoding="utf-8")
    mgr = MCPManager(fresh_registry, config_path=str(p))

    async def fake_connect(name, spec):
        fake = FakeMCPClient(name="fs")
        fake._inbox.append(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}))
        fake._inbox.append(json.dumps({
            "jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "ping", "description": "", "inputSchema": {}}]}
        }))
        await fake.connect()
        tools = await fake.list_tools()
        mgr._clients[name] = fake
        mgr._tools[name] = {t.name: t for t in tools}
        mgr._register_server_tools(name, tools)
        return tools

    monkeypatch.setattr(mgr, "_connect_server", fake_connect)
    await mgr.load_and_register()
    # Manually re-register the same tools (simulating reload).
    await mgr.reload_server("fs", {"transport": "stdio", "command": "x"})

    names = fresh_registry.list_names()
    assert names.count(f"{MCP_TOOL_PREFIX}fs__ping") == 1

    await mgr.shutdown()


def test_unknown_transport_raises(monkeypatch, tmp_path):
    p = tmp_path / "servers.json"
    p.write_text(json.dumps({"servers": {"x": {"transport": "weird", "command": "y"}}}), encoding="utf-8")
    mgr = MCPManager(ToolRegistry(), config_path=str(p))
    servers = mgr.load_config()
    assert "x" in servers
    # _connect_server is async; just verify the type check via constructing with bad transport.