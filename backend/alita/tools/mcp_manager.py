"""
MCP Manager -- loads MCP server configs and registers their tools.

Reads a JSON config file (default ``~/.alita/mcp_servers.json``) describing
one or more MCP servers, connects to them concurrently on agent startup, lists
the tools they expose, and wraps each as a standard Alita ``Tool`` registered
with the provided ``ToolRegistry``.

Config format (``mcp_servers.json``)::

    {
      "servers": {
        "filesystem": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        },
        "remote": {
          "transport": "sse",
          "url": "http://localhost:8080/sse",
          "headers": {"Authorization": "Bearer ..."}
        }
      }
    }

Optional per-server keys: ``env``, ``cwd``, ``timeout`` (SSE),
``connect_timeout`` (handshake + tool listing, default 30s), ``enabled``.

Tool naming: tools are registered as ``mcp__<server>__<tool_name>``, sanitised
to match ``^[a-zA-Z0-9_-]{1,64}$`` (invalid characters become ``_``; names
that are too long are truncated with a short hash suffix). Calls are routed
using the original remote tool name.

Failures are isolated: if one server fails to start, the others still load.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .mcp_client import (
    MCPConnectionError,
    MCPTool,
    SSEMCPClient,
    StdioMCPClient,
    _BaseMCPClient,
)
from .registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)

#: Default config location, overridable via ALITA_MCP_CONFIG env var.
DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".alita", "mcp_servers.json")

#: Prefix applied to MCP-sourced tool names to namespace them.
MCP_TOOL_PREFIX = "mcp__"

#: Maximum tool-name length accepted by OpenAI-compatible APIs.
MAX_TOOL_NAME_LEN = 64

_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_tool_name(name: str, max_len: int = MAX_TOOL_NAME_LEN) -> str:
    """Make ``name`` match ``^[a-zA-Z0-9_-]{1,max_len}$``.

    Invalid characters become ``_``. Names that are too long (or that had to be
    altered and would otherwise collide) are truncated and suffixed with an
    8-char hash of the original name, keeping them unique and deterministic.
    """
    cleaned = _INVALID_NAME_CHARS.sub("_", name or "") or "tool"
    if len(cleaned) <= max_len:
        return cleaned
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: max_len - 9]}_{digest}"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Registered (sanitised) name of an MCP tool."""
    return sanitize_tool_name(f"{MCP_TOOL_PREFIX}{server_name}__{tool_name}")


class MCPManager:
    """
    Owns the lifecycle of all configured MCP clients.

    Instantiate once, call :meth:`load_and_register` at startup, and
    :meth:`shutdown` when the agent stops.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        config_path: Optional[str] = None,
    ):
        self.registry = registry
        self.config_path = config_path or os.getenv("ALITA_MCP_CONFIG", DEFAULT_CONFIG_PATH)
        self._clients: Dict[str, _BaseMCPClient] = {}
        self._tools: Dict[str, Dict[str, MCPTool]] = {}  # server -> {remote tool name: MCPTool}
        #: server -> {registered name: remote tool name}
        self._registered: Dict[str, Dict[str, str]] = {}
        self._enabled = os.getenv("ALITA_MCP_ENABLED", "true").lower() != "false"

    # – Config loading ─────────────────────────────────────────────

    def load_config(self) -> Dict[str, Any]:
        """Load and validate the MCP server config file, returning {} if absent."""
        path = Path(self.config_path)
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("MCP config %s unreadable: %s", path, exc)
            return {}
        if not isinstance(cfg, dict):
            logger.warning("MCP config %s top-level must be an object", path)
            return {}
        servers = cfg.get("servers", {})
        if not isinstance(servers, dict):
            logger.warning("MCP config %s `servers` must be an object", path)
            return {}
        return servers

    # – Connection lifecycle ───────────────────────────────────────

    async def load_and_register(self) -> Dict[str, Any]:
        """
        Connect to all configured servers concurrently and register their tools.

        Returns a summary dict::

            {"connected": [server, ...], "failed": {server: error, ...},
             "tools": {server: [tool_name, ...], ...}}
        """
        if not self._enabled:
            return {"connected": [], "failed": {}, "tools": {}, "disabled": True}

        servers = self.load_config()
        if not servers:
            return {"connected": [], "failed": {}, "tools": {}}

        connected: List[str] = []
        failed: Dict[str, str] = {}
        tools_map: Dict[str, List[str]] = {}

        items: List[Tuple[str, Dict[str, Any]]] = []
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                failed[name] = "server spec must be an object"
                continue
            if spec.get("enabled", True) is False:
                continue
            items.append((name, spec))

        results = await asyncio.gather(
            *(self._connect_with_timeout(name, spec) for name, spec in items),
            return_exceptions=True,
        )
        for (name, _spec), result in zip(items, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failed[name] = str(result) or type(result).__name__
                logger.warning("MCP server %s failed to load: %s", name, failed[name])
            else:
                connected.append(name)
                tools_map[name] = [t.name for t in result]

        return {"connected": connected, "failed": failed, "tools": tools_map}

    async def _connect_with_timeout(self, name: str, spec: Dict[str, Any]) -> List[MCPTool]:
        timeout = _as_float(spec.get("connect_timeout"), 30.0)
        try:
            # Extra slack over the handshake timeout for process start-up and tools/list.
            return await asyncio.wait_for(self._connect_server(name, spec), timeout=timeout * 2 + 5)
        except asyncio.TimeoutError as exc:
            await self._disconnect_server(name)
            raise MCPConnectionError(f"timed out connecting to MCP server {name!r}") from exc

    async def _connect_server(self, name: str, spec: Dict[str, Any]) -> List[MCPTool]:
        transport = (spec.get("transport") or "stdio").lower()
        if transport == "stdio":
            if not spec.get("command"):
                raise MCPConnectionError(f"stdio server {name!r} requires 'command'")
            client: _BaseMCPClient = StdioMCPClient(
                name=name,
                command=spec["command"],
                args=[str(a) for a in spec.get("args", [])],
                env=spec.get("env"),
                cwd=spec.get("cwd"),
            )
        elif transport in ("sse", "http"):
            if not spec.get("url"):
                raise MCPConnectionError(f"SSE server {name!r} requires 'url'")
            client = SSEMCPClient(
                name=name,
                url=spec["url"],
                headers=spec.get("headers"),
                timeout=_as_float(spec.get("timeout"), 30.0),
            )
        else:
            raise MCPConnectionError(f"unknown transport {transport!r} for server {name!r}")

        connect_timeout = _as_float(spec.get("connect_timeout"), 30.0)
        await client.connect(timeout=connect_timeout)
        try:
            tools = await client.list_tools(timeout=connect_timeout)
        except BaseException:
            await client.close()
            raise
        self._clients[name] = client
        self._tools[name] = {t.name: t for t in tools}
        self._register_server_tools(name, tools)
        logger.info("MCP server %s connected: %d tool(s)", name, len(tools))
        return tools

    def _register_server_tools(self, server_name: str, tools: List[MCPTool]) -> None:
        registered = self._registered.setdefault(server_name, {})
        for tool in tools:
            if not tool.name:
                continue
            full_name = mcp_tool_name(server_name, tool.name)
            existing = self.registry.get(full_name)
            if existing is not None and full_name not in registered:
                logger.warning(
                    "MCP tool %s (server %s) collides with an existing tool; skipped",
                    full_name, server_name,
                )
                continue
            definition = tool.to_openai_function()
            definition["name"] = full_name  # rewrite the schema name to the registered one
            self.registry.register(Tool(
                name=full_name,
                description=tool.description or f"MCP tool {tool.name} (server: {server_name})",
                definition=definition,
                handler=self._make_handler(server_name, tool.name),  # routes by remote name
                tags=["mcp", f"mcp:{server_name}"],
            ))
            registered[full_name] = tool.name

    def _make_handler(self, server_name: str, tool_name: str):
        """Build an async handler that routes to the right client by remote tool name."""
        async def _handler(**kwargs) -> Dict[str, Any]:
            client = self._clients.get(server_name)
            if client is None:
                return {"success": False, "error": f"MCP server {server_name!r} not connected"}
            try:
                result = await client.call_tool(tool_name, kwargs)
            except MCPConnectionError as exc:
                return {"success": False, "error": f"MCP connection lost: {exc}"}
            except Exception as exc:  # noqa: BLE001 - surface to the model
                return {"success": False, "error": f"MCP call failed: {exc}"}
            d = result.to_dict()
            # Coerce text into a string field the conversation history expects.
            if isinstance(d.get("text"), str):
                d["result"] = d["text"]
            return d
        return _handler

    # – Introspection / management ─────────────────────────────────

    def list_servers(self) -> List[Dict[str, Any]]:
        out = []
        for name, client in self._clients.items():
            tools = self._tools.get(name, {})
            out.append({
                "name": name,
                "connected": getattr(client, "_initialized", False),
                "tools": list(tools.keys()),
                "registered_tools": list(self._registered.get(name, {}).keys()),
                "info": client.server_info,
            })
        return out

    def list_tool_names(self) -> List[str]:
        """All registered (namespaced, sanitised) MCP tool names."""
        names: List[str] = []
        for mapping in self._registered.values():
            names.extend(mapping.keys())
        return names

    def remote_tool_name(self, registered_name: str) -> Optional[Tuple[str, str]]:
        """Map a registered tool name back to ``(server, remote tool name)``."""
        for server, mapping in self._registered.items():
            if registered_name in mapping:
                return server, mapping[registered_name]
        return None

    async def reload_server(self, name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Disconnect and reconnect a single server by name (hot reload)."""
        await self._disconnect_server(name)
        try:
            tools = await self._connect_with_timeout(name, spec)
            return {"success": True, "name": name, "tools": [t.name for t in tools]}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    def _unregister_server_tools(self, name: str) -> None:
        for full_name in self._registered.pop(name, {}):
            self.registry.unregister(full_name)
        # Also cover tools recorded only in _tools (e.g. registered externally).
        for remote in self._tools.get(name, {}):
            self.registry.unregister(mcp_tool_name(name, remote))

    async def _disconnect_server(self, name: str) -> None:
        client = self._clients.pop(name, None)
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP server %s close failed: %s", name, exc)
        self._unregister_server_tools(name)
        self._tools.pop(name, None)

    async def shutdown(self) -> None:
        """Close every client and unregister their tools. Safe to call multiple times."""
        names = set(self._clients) | set(self._tools) | set(self._registered)
        clients = list(self._clients.values())
        self._clients.clear()
        if clients:
            await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
        for name in names:
            self._unregister_server_tools(name)
        self._tools.clear()
        self._registered.clear()


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
