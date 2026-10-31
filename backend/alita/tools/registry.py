"""
Tool registry — registration, discovery and dispatch of agent tools.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union


@dataclass
class Tool:
    """A registered tool: OpenAI function schema + async (or sync) handler."""

    name: str
    description: str
    definition: Dict[str, Any]
    handler: Callable[..., Union[Awaitable[Dict[str, Any]], Dict[str, Any]]]
    tags: List[str] = field(default_factory=list)


def _accepts_var_kwargs(fn: Callable[..., Any]) -> bool:
    try:
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True


class ToolRegistry:
    """Central registry for all tools (built-in, agent-level and MCP)."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        # Keep the schema name in sync with the registry key.
        if isinstance(tool.definition, dict) and tool.definition.get("name") != tool.name:
            tool.definition = {**tool.definition, "name": tool.name}
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_definitions(self) -> List[Dict[str, Any]]:
        """All tool definitions in OpenAI ``{"type": "function", ...}`` format."""
        return [{"type": "function", "function": t.definition} for t in self._tools.values()]

    def list_names(self) -> List[str]:
        return list(self._tools.keys())

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    async def execute(self, tool_name: str, /, **kwargs: Any) -> Dict[str, Any]:
        """Execute a tool. ``tool_name`` is positional-only so tools may take a ``name`` argument."""
        tool = self._tools.get(tool_name)
        if not tool:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        if not _accepts_var_kwargs(tool.handler):
            allowed = set(inspect.signature(tool.handler).parameters)
            kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        result = tool.handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def get_tools_by_tag(self, tag: str) -> List[Tool]:
        return [t for t in self._tools.values() if tag in t.tags]
