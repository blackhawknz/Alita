"""
Memory manager — orchestrates Alita's layered memory.

Layers
------
1. **Persona** (SOUL.md) and **custom instructions** — static identity.
2. **Core memory** blocks (``user``, ``notes``) — always in the system
   prompt; edited by the agent via the ``core_memory`` tool.
3. **Long-term memory** — unbounded atomic memories with hybrid retrieval.
   * *Auto-recall*: before each turn the most relevant memories are
     retrieved for the user's message and injected (ephemerally) into the
     request, so the model sees them without spending a tool call.
   * *Auto-extraction*: after each turn a background LLM pass extracts new
     durable facts, updates outdated ones and deletes contradicted ones
     (Mem0-style add/update/delete), de-duplicating against existing rows.
4. **Transcripts** — every session is stored in SQLite with FTS5 search
   (``session_search`` tool).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .core_memory import BLOCKS, CoreMemory, CoreMemoryError
from .longterm import CATEGORIES, LongTermMemory
from .sqlite_store import SQLiteStore, content_to_text

logger = logging.getLogger(__name__)

_RECALL_MIN_RELEVANCE = 0.32

EXTRACTION_PROMPT = """You maintain the long-term memory of an AI assistant named Alita.
Today is {today}.

Read the latest exchange and decide what, if anything, is worth remembering
across future conversations. Good memories are durable, specific and useful
later: the user's identity, preferences, goals, relationships, recurring
projects, environment/setup details, decisions, and commitments. Do NOT store:
transient task details, things only relevant to this one request, generic
knowledge, the assistant's own opinions, or anything already captured below.
Events, people and places from stories, games or simulations the user and
Alita share are FICTION: never store them as real-world facts.

Write each memory as one short, self-contained, third-person statement
(e.g. "User's name is Sam", "User prefers concise answers with code first").
Convert relative dates to absolute dates.

If the exchange contradicts or refines an EXISTING memory, update it (by id)
instead of adding a new one. If it shows an existing memory is now false,
delete it.

Facts so central they should shape EVERY future reply (name, how they want to
be addressed, strong standing preferences) may also be added to the
always-visible user profile via "profile_add" (keep each line short; skip if
already present in the profile).

## Always-visible user profile
{profile}

## Existing related memories
{existing}

## Latest exchange
{exchange}

Respond with ONLY a JSON object, no prose:
{{"memories": [{{"op": "add", "content": "...", "category": "{categories}", "importance": 1-5}},
               {{"op": "update", "id": 0, "content": "..."}},
               {{"op": "delete", "id": 0}}],
 "profile_add": ["..."]}}
Use {{"memories": [], "profile_add": []}} when nothing is worth remembering (this is the common case)."""


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class MemoryManager:
    """Facade over core memory, long-term memory and transcript search."""

    def __init__(
        self,
        db: SQLiteStore,
        core: CoreMemory,
        longterm: LongTermMemory,
        llm_getter: Callable[[], Any],
    ):
        self.db = db
        self.core = core
        self.longterm = longterm
        self._llm = llm_getter

    # ─── Prompt rendering ─────────────────────────────────────────

    def render_core(self) -> str:
        return self.core.render()

    # ─── Auto-recall ──────────────────────────────────────────────

    async def recall(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        if not query or len(query.strip()) < 3:
            return []
        try:
            hits = await self.longterm.search(query, k=k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory recall failed: %s", exc)
            return []
        return [h for h in hits if h["relevance"] >= _RECALL_MIN_RELEVANCE or h.get("keyword_hit") and h["semantic"] >= 0.2]

    @staticmethod
    def format_recall(items: List[Dict[str, Any]]) -> str:
        if not items:
            return ""
        def line(m: Dict[str, Any]) -> str:
            if m.get("fiction"):
                world = str(m.get("scope") or "a story world").split(":")[0].capitalize()
                return f"- [#{m['id']} FICTION · {world}] {m['content']}"
            return f"- [#{m['id']} {m['category']}] {m['content']}"

        lines = [line(m) for m in items]
        note = ""
        if any(m.get("fiction") for m in items):
            note = ("Entries marked FICTION happened inside a story world you take part in (e.g. an "
                    "interactive fiction session). They are your memories of that story — never real-world facts.\n")
        return (
            "<recalled_memories>\n"
            "Possibly relevant long-term memories (auto-retrieved; may be outdated — "
            "use only if relevant, and correct them with the memory tool if wrong):\n"
            + note
            + "\n".join(lines)
            + "\n</recalled_memories>"
        )

    # ─── Auto-extraction ──────────────────────────────────────────

    async def extract_from_exchange(
        self,
        user_message: Any,
        assistant_response: str,
        session_id: Optional[str] = None,
        tool_summary: str = "",
    ) -> Dict[str, Any]:
        """Run the extraction pass; returns a summary of applied operations."""
        user_text = content_to_text(user_message).strip()
        if len(user_text) < 8 and not tool_summary:
            return {"applied": []}
        llm = self._llm()
        if llm is None:
            return {"applied": []}

        # Fiction memories (story worlds) are never offered to extraction, so it
        # can neither update nor delete them, nor treat them as real facts.
        related = [m for m in await self.longterm.search(user_text + "\n" + assistant_response[:500], k=10, touch=False)
                   if not m.get("fiction")]
        existing = "\n".join(f"- id={m['id']} [{m['category']}] {m['content']}" for m in related) or "(none)"
        exchange = f"USER: {user_text[:4000]}\n\nASSISTANT: {assistant_response[:3000]}"
        if tool_summary:
            exchange += f"\n\n(Tools used: {tool_summary[:600]})"
        prompt = EXTRACTION_PROMPT.format(
            today=datetime.now().strftime("%Y-%m-%d (%A)"),
            profile=self.core.get("user") or "(empty)",
            existing=existing,
            exchange=exchange,
            categories="|".join(CATEGORIES),
        )
        try:
            resp = await llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=900,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("memory extraction call failed: %s", exc)
            return {"applied": [], "error": str(exc)}
        data = _parse_json_object(content_to_text(resp.get("content", "")))
        if not data:
            return {"applied": []}

        valid_ids = {m["id"] for m in related}
        applied: List[Dict[str, Any]] = []
        for op in (data.get("memories") or [])[:8]:
            if not isinstance(op, dict):
                continue
            kind = op.get("op")
            try:
                if kind == "add" and op.get("content"):
                    res = await self.longterm.add(
                        str(op["content"]),
                        category=str(op.get("category", "fact")),
                        importance=int(op.get("importance", 3) or 3),
                        source_session=session_id,
                    )
                    applied.append({"op": res["status"], "id": res.get("id"), "content": op["content"]})
                elif kind == "update" and int(op.get("id", 0)) in valid_ids and op.get("content"):
                    if await self.longterm.update(int(op["id"]), content=str(op["content"])):
                        applied.append({"op": "updated", "id": int(op["id"]), "content": op["content"]})
                elif kind == "delete" and int(op.get("id", 0)) in valid_ids:
                    if await self.longterm.delete(int(op["id"])):
                        applied.append({"op": "deleted", "id": int(op["id"])})
            except (TypeError, ValueError) as exc:
                logger.debug("bad memory op %s: %s", op, exc)
        for line in (data.get("profile_add") or [])[:3]:
            if isinstance(line, str) and line.strip():
                try:
                    self.core.append("user", line.strip()[:200])
                    applied.append({"op": "profile", "content": line.strip()[:200]})
                except CoreMemoryError:
                    break
        if applied:
            logger.info("memory extraction applied %d op(s)", len(applied))
        return {"applied": applied}

    # ─── Agent tools ──────────────────────────────────────────────

    async def tool_memory(self, action: str = "search", **params: Any) -> Dict[str, Any]:
        """Handler for the ``memory`` (long-term) tool."""
        session_id = params.get("_session_id")
        try:
            if action == "save":
                content = params.get("content", "")
                res = await self.longterm.add(
                    content,
                    category=params.get("category", "fact"),
                    importance=params.get("importance", 3),
                    source_session=session_id,
                )
                if res["status"] == "error":
                    return {"success": False, "error": res["error"]}
                return {"success": True, **res}
            if action == "search":
                hits = await self.longterm.search(params.get("query", ""), k=int(params.get("limit", 8) or 8),
                                                  category=params.get("category") or None)
                return {"success": True, "results": [
                    {"id": h["id"], "content": h["content"], "category": h["category"],
                     "importance": h["importance"], "relevance": h["relevance"], "updated_at": h["updated_at"]}
                    for h in hits
                ]}
            if action == "update":
                ok = await self.longterm.update(int(params.get("id", 0)), content=params.get("content") or None,
                                                category=params.get("category") or None,
                                                importance=params.get("importance") or None)
                return {"success": ok, **({} if ok else {"error": "memory not found"})}
            if action == "delete":
                ok = await self.longterm.delete(int(params.get("id", 0)))
                return {"success": ok, **({} if ok else {"error": "memory not found"})}
            if action == "list":
                rows = self.longterm.list(limit=int(params.get("limit", 30) or 30), category=params.get("category") or None)
                return {"success": True, "memories": [
                    {"id": r["id"], "content": r["content"], "category": r["category"], "importance": r["importance"]}
                    for r in rows
                ], "total": self.longterm.count()}
            return {"success": False, "error": f"Unknown action '{action}'. Use save, search, update, delete, list."}
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": f"Invalid parameters: {exc}"}

    async def tool_core_memory(self, action: str = "view", **params: Any) -> Dict[str, Any]:
        """Handler for the ``core_memory`` tool."""
        block = params.get("block", "")
        try:
            if action == "view":
                if block:
                    return {"success": True, "block": block, "content": self.core.get(block),
                            "limit": BLOCKS[block].limit if block in BLOCKS else None}
                return {"success": True, "blocks": self.core.all()}
            if action == "append":
                text = self.core.append(block, params.get("text", ""))
            elif action == "replace":
                text = self.core.replace(block, params.get("old_text", ""), params.get("new_text", ""))
            elif action == "rewrite":
                text = self.core.set(block, params.get("text", ""))
            else:
                return {"success": False, "error": f"Unknown action '{action}'. Use view, append, replace, rewrite."}
            return {"success": True, "block": block, "chars": len(text), "limit": BLOCKS[block].limit}
        except CoreMemoryError as exc:
            return {"success": False, "error": str(exc)}


MEMORY_TOOL_SCHEMA = {
    "name": "memory",
    "description": (
        "Long-term memory that persists across all conversations (unlimited size). "
        "Relevant memories are auto-recalled each turn, but use 'search' to look deeper. "
        "Use 'save' for durable facts worth remembering later (preferences, people, projects, decisions) — "
        "one atomic, self-contained statement per memory. Use 'update'/'delete' to fix outdated memories."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["save", "search", "update", "delete", "list"]},
            "content": {"type": "string", "description": "Memory text (save/update)."},
            "query": {"type": "string", "description": "What to search for (search)."},
            "id": {"type": "integer", "description": "Memory id (update/delete)."},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5,
                           "description": "1 = trivia, 3 = useful, 5 = essential."},
            "limit": {"type": "integer", "description": "Max results (search/list)."},
        },
        "required": ["action"],
    },
}

CORE_MEMORY_TOOL_SCHEMA = {
    "name": "core_memory",
    "description": (
        "Edit your always-visible core memory blocks (shown in every conversation). "
        "Blocks: 'user' (who the user is, standing preferences) and 'notes' (your durable working notes). "
        "They have character limits — keep them dense; move details to long-term `memory` instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["view", "append", "replace", "rewrite"]},
            "block": {"type": "string", "enum": list(BLOCKS)},
            "text": {"type": "string", "description": "Text to append, or full new content for rewrite."},
            "old_text": {"type": "string", "description": "Exact existing text to replace."},
            "new_text": {"type": "string", "description": "Replacement text (empty to delete)."},
        },
        "required": ["action"],
    },
}
