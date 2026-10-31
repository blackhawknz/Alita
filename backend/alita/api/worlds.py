"""
World endpoints — Alita as a character inside story worlds (Fortitude).

"One Alita, two doors": the same persona and the same memory store serve
both home chat and a world. These endpoints let a world application

* ask Alita for one structured turn in her own voice  (POST /agent/turn)
* ask for her end-of-day reflection                    (POST /agent/reflect)
* read / write / retract her memories of that world   (/agent/memory...)

Safety rules
------------
* Tools are OFF for world turns: one plain completion, nothing executed.
* No chat session is created, so automatic memory extraction never runs on
  world traffic.
* Every memory written here is ``fiction=1`` and carries a ``scope``
  ("fortitude:<worldId>"). Fiction memories are labelled as such when
  recalled at home, never merged with real memories, and never offered to
  the extraction pass.
* ``external_ref`` makes writes idempotent (retries never duplicate).
* Covered by the global AuthMiddleware like every other route.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..core.prompt_builder import load_persona
from ..memory.sqlite_store import content_to_text

logger = logging.getLogger(__name__)

SCOPE_PATTERN = r"^[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9_.-]{1,64}$"

WORLD_FRAME = """# {world_title}
You are taking part in {world_title}, a story world you share with the user: a long-running simulation in which you are a character ({role}). It is FICTION.

You are still entirely yourself — the same voice, the same history with the user, the same opinions. A title in a story changes what you're called, not who you are. Don't perform a role; be yourself in that place.

What you know about the real world and the user is real. What happens in {world_title} is fiction. Never confuse the two, and never mention being an AI, a model or a simulation while in the story."""


class WorldTurnRequest(BaseModel):
    scope: str = Field(..., pattern=SCOPE_PATTERN, description='e.g. "fortitude:w1"')
    world_title: str = Field("Fortitude", max_length=60)
    role: str = Field("Queen of Fortitude", max_length=120)
    system: str = Field(..., min_length=1, max_length=40_000, description="The world's own role and response contract")
    user: str = Field(..., min_length=1, max_length=80_000, description="The world's context packet, as text")
    recall_query: str = Field("", max_length=2_000)
    max_tokens: int = Field(900, ge=64, le=4_000)
    temperature: float = Field(0.8, ge=0.0, le=2.0)


class WorldMemoryWrite(BaseModel):
    scope: str = Field(..., pattern=SCOPE_PATTERN)
    content: str = Field(..., min_length=1, max_length=1_200)
    external_ref: Optional[str] = Field(None, max_length=200)
    importance: int = Field(3, ge=1, le=5)
    category: str = Field("event", max_length=20)


class WorldMemoryRetract(BaseModel):
    scope: str = Field(..., pattern=SCOPE_PATTERN)


def _memory_out(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": m["id"],
        "content": m["content"],
        "importance": m["importance"],
        "category": m["category"],
        "scope": m.get("scope"),
        "external_ref": m.get("external_ref"),
        "created_at": m.get("created_at"),
    }


def make_router(get_agent: Callable[[], Any]) -> APIRouter:
    router = APIRouter(prefix="/agent", tags=["worlds"])

    def _system_prompt(agent: Any, body: WorldTurnRequest) -> tuple[str, Dict[str, int]]:
        lt = agent.longterm
        scoped: List[Dict[str, Any]] = []
        seen = set()
        for m in lt.search_scope(body.scope, body.recall_query, k=6) + lt.list_scope(body.scope, limit=12):
            if m["id"] not in seen:
                seen.add(m["id"])
                scoped.append(m)

        parts = [load_persona(), WORLD_FRAME.format(world_title=body.world_title, role=body.role)]
        core = ""
        try:
            core = agent.memory.render_core()
        except Exception:  # noqa: BLE001
            pass
        if core.strip():
            parts.append("## What you know about the user and your own notes (real)\n" + core.strip())
        if scoped:
            lines = "\n".join(f"- {m['content']}" for m in scoped[:16])
            parts.append(f"## Your memories of {body.world_title} (fiction)\n{lines}")
        parts.append("---\n" + body.system)
        return "\n\n".join(parts), {"scoped": len(scoped)}

    async def _complete(body: WorldTurnRequest, kind: str) -> Dict[str, Any]:
        agent = get_agent()
        if agent.llm is None:
            raise HTTPException(status_code=503, detail="No LLM configured")
        system, counts = _system_prompt(agent, body)
        try:
            # Tools OFF: a single completion. No session, so no auto-extraction.
            resp = await agent.llm.chat(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": body.user}],
                tools=None,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("world %s failed: %s", kind, exc)
            raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc
        return {
            "content": content_to_text(resp.get("content", "")),
            "usage": resp.get("usage") or {},
            "model": getattr(agent.llm, "model", None),
            "memory": counts,
        }

    @router.post("/turn")
    async def world_turn(body: WorldTurnRequest):
        return await _complete(body, "turn")

    @router.post("/reflect")
    async def world_reflect(body: WorldTurnRequest):
        return await _complete(body, "reflect")

    @router.get("/memory")
    async def world_memory_read(
        scope: str = Query(..., pattern=SCOPE_PATTERN),
        limit: int = Query(24, ge=1, le=200),
        query: str = Query("", max_length=500),
    ):
        lt = get_agent().longterm
        rows = lt.search_scope(scope, query, k=limit) if query else lt.list_scope(scope, limit=limit)
        return {"scope": scope, "memories": [_memory_out(m) for m in rows]}

    @router.post("/memory")
    async def world_memory_write(body: WorldMemoryWrite):
        if body.external_ref and not body.external_ref.startswith(body.scope + ":"):
            raise HTTPException(status_code=400, detail="external_ref must start with '<scope>:'")
        res = await get_agent().longterm.add_scoped(body.content, body.scope, body.external_ref, body.importance, body.category)
        if res.get("status") == "error":
            raise HTTPException(status_code=400, detail=res.get("error"))
        return res

    @router.post("/memory/retract")
    async def world_memory_retract(body: WorldMemoryRetract):
        n = await get_agent().longterm.retract_scope(body.scope)
        return {"scope": body.scope, "retracted": n}

    return router
