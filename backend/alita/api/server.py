"""
FastAPI gateway for Alita.

The agent is created once in the app lifespan. Chat turns run as background
tasks; ``POST /chat/stream`` starts a turn and streams its events over SSE,
and ``GET /sessions/{id}/stream?after=N`` re-attaches to a running turn (for
reconnects or switching back to a busy session).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..core import config
from ..core.agent import AlitaAgent
from ..core.attachments import build_user_content, get_upload, save_upload
from ..core.session import Session
from ..memory.sqlite_store import content_to_text
from .security import AuthMiddleware, configured_token

logger = logging.getLogger(__name__)

_agent: Optional[AlitaAgent] = None


def get_agent() -> AlitaAgent:
    global _agent
    if _agent is None:
        config.reload_env()
        _agent = AlitaAgent()
    return _agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent = get_agent()
    await agent.start_services()
    yield
    global _agent
    if _agent is not None:
        await _agent.shutdown()
        _agent = None


app = FastAPI(title="Alita Agent API", version=__version__, lifespan=lifespan)

_origins = [o.strip() for o in os.getenv("ALITA_CORS_ORIGINS", "").split(",") if o.strip()] or [
    "http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "http://127.0.0.1:3001",
]
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization"],
)
app.add_middleware(AuthMiddleware, token=configured_token())

# Story-world endpoints (Fortitude): /agent/turn, /agent/reflect, /agent/memory…
from .worlds import make_router as _make_world_router  # noqa: E402

app.include_router(_make_world_router(lambda: get_agent()))

SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


async def _stream_run(run, after: int = 0, prelude: Optional[List[Dict[str, Any]]] = None) -> AsyncGenerator[str, None]:
    for ev in prelude or []:
        yield _sse(ev)
    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for ev in run.subscribe(after):
                await queue.put(ev)
        finally:
            await queue.put(None)

    task = asyncio.ensure_future(pump())
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if ev is None:
                break
            yield _sse(ev)
        yield _sse({"type": "stream_end"})
    finally:
        task.cancel()


def _session_or_404(session_id: str) -> Session:
    sess = get_agent().sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return sess


# ─── Models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = ""
    session_id: Optional[str] = None
    attachments: List[str] = Field(default_factory=list, description="Upload ids")
    edit_message_id: Optional[int] = None
    regenerate: bool = False
    stream: bool = True


class SessionCreate(BaseModel):
    title: str = ""


class SessionUpdate(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None


class UploadRequest(BaseModel):
    name: str
    mime: Optional[str] = None
    data: str = Field(..., description="Base64 (optionally a data: URL)")


class BlockUpdate(BaseModel):
    content: str = ""


class MemoryCreate(BaseModel):
    content: str
    category: str = "fact"
    importance: int = 3


class MemoryUpdate(BaseModel):
    content: Optional[str] = None
    category: Optional[str] = None
    importance: Optional[int] = None


class SkillCreate(BaseModel):
    name: str
    description: str
    content: str
    tags: List[str] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    description: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None


class PersonalityUpdate(BaseModel):
    persona: Optional[str] = None
    instructions: Optional[str] = None
    prompt: Optional[str] = None  # legacy alias for persona


class ToolExecuteRequest(BaseModel):
    tool: str
    params: dict = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    approval_id: str
    decision: bool = True


class SettingsUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_keys: Dict[str, str] = Field(default_factory=dict)
    models: Dict[str, str] = Field(default_factory=dict)
    base_urls: Dict[str, str] = Field(default_factory=dict)
    agent: Dict[str, Any] = Field(default_factory=dict)
    easy_diffusion_url: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_model: Optional[str] = None


class SpeechRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=200_000)


class VoiceUpdate(BaseModel):
    provider: Optional[str] = None
    voice_id: Optional[str] = None
    name: Optional[str] = None
    reason: Optional[str] = None
    public_owner_id: str = ""
    speed: Optional[float] = None
    stability: Optional[float] = None
    similarity_boost: Optional[float] = None
    style: Optional[float] = None
    use_speaker_boost: Optional[bool] = None
    instructions: Optional[str] = None


class VoicePreviewRequest(BaseModel):
    provider: Optional[str] = None
    voice_id: Optional[str] = None
    text: str = ""
    public_owner_id: str = ""


# ─── Root / health ────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"name": "Alita Agent API", "version": __version__, "status": "running", "docs": "/docs"}


@app.get("/health")
async def health_check():
    agent = get_agent()
    return {
        "status": "healthy",
        "version": __version__,
        "llm_provider": agent.llm_provider_name,
        "model": agent.llm_model_name,
        "context_window": agent.context_window,
        "vector_store": agent.vector_store.is_available(),
        "host_exec_allowed": agent.allow_host_exec,
        "running_sessions": [s.id for s in agent.sessions.running()],
    }


@app.get("/stats")
async def stats():
    agent = get_agent()
    row = agent.sqlite.query_one("SELECT COUNT(*) AS n FROM sessions WHERE kind = 'chat'")
    return {
        "sessions": row["n"] if row else 0,
        "memories": agent.longterm.stats(),
        "skills": len(agent.skills.list()),
        "tools": len(agent.tool_registry.list_names()),
        "running": [s.to_dict() for s in agent.sessions.running()],
    }


# ─── Chat ─────────────────────────────────────────────────────────────

async def _slash_response(agent: AlitaAgent, sess: Optional[Session], text: str):
    result = await agent.slash_registry.execute(text, {"session": sess})
    events = [
        {"type": "command", "command": text.split()[0], "message": result.get("message", ""),
         "level": result.get("type", "info"), "action": result.get("action"),
         "session_id": result.get("session_id") or (sess.id if sess else None)},
        {"type": "stream_end"},
    ]
    return result, events


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    agent = get_agent()
    text = (request.message or "").strip()
    if not text and not request.attachments and not request.regenerate:
        raise HTTPException(status_code=400, detail="Message is empty")

    if text.startswith("/") and agent.slash_registry.is_command(text) and not request.attachments:
        sess = agent.sessions.get(request.session_id) if request.session_id else None
        _, events = await _slash_response(agent, sess, text)

        async def gen():
            for ev in events:
                yield _sse(ev)
        return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)

    sess = agent.sessions.get(request.session_id) if request.session_id else None
    if request.session_id and sess is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if sess is None:
        sess = agent.sessions.create(kind="chat")
    if sess.is_running:
        raise HTTPException(status_code=409, detail="This chat is still working on a response.")

    content = build_user_content(text, request.attachments) if not request.regenerate else None
    try:
        run = agent.start_turn(sess, content, edit_message_id=request.edit_message_id, regenerate=request.regenerate)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    prelude = [{"type": "session", "session_id": sess.id, "turn_id": run.id, "title": sess.title}]
    return StreamingResponse(_stream_run(run, 0, prelude), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/chat")
async def chat(request: ChatRequest):
    """Non-streaming chat: runs the turn to completion and returns the reply."""
    agent = get_agent()
    text = (request.message or "").strip()
    sess = agent.sessions.get(request.session_id) if request.session_id else None
    if text.startswith("/") and agent.slash_registry.is_command(text):
        result, _ = await _slash_response(agent, sess, text)
        return {"response": result.get("message", ""), "session_id": result.get("session_id") or (sess.id if sess else None),
                "slash_command": True}
    if sess is None:
        sess = agent.sessions.create(kind="chat")
    if sess.is_running:
        raise HTTPException(status_code=409, detail="This chat is still working on a response.")
    result = await agent.run_conversation(build_user_content(text, request.attachments), session=sess)
    return {
        "response": result["final_response"], "session_id": sess.id, "error": result["error"],
        "api_call_count": result["api_call_count"], "tool_call_count": result["tool_call_count"],
        "interrupted": result["interrupted"],
    }


@app.get("/sessions/{session_id}/stream")
async def resume_stream(session_id: str, after: int = Query(0, ge=0)):
    sess = _session_or_404(session_id)
    if sess.turn is None:
        async def empty():
            yield _sse({"type": "idle", "session_id": session_id})
            yield _sse({"type": "stream_end"})
        return StreamingResponse(empty(), media_type="text/event-stream", headers=SSE_HEADERS)
    prelude = [{"type": "session", "session_id": sess.id, "turn_id": sess.turn.id, "title": sess.title, "resumed": True}]
    return StreamingResponse(_stream_run(sess.turn, after, prelude), media_type="text/event-stream", headers=SSE_HEADERS)


@app.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    agent = get_agent()
    _session_or_404(session_id)
    agent.interrupt(session_id)
    return {"success": True}


# ─── Sessions ─────────────────────────────────────────────────────────

def _session_summary(agent: AlitaAgent, row: Dict[str, Any]) -> Dict[str, Any]:
    cached = agent.sessions._cache.get(row["id"])
    return {
        "id": row["id"],
        "title": row.get("title") or "New chat",
        "kind": row.get("kind"),
        "pinned": row.get("pinned", False),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("created_at"),
        "message_count": row.get("message_count", 0),
        "is_running": bool(cached and cached.is_running),
        "compacted": bool(row.get("context_summary")),
        "parent_id": row.get("parent_id"),
    }


@app.get("/sessions")
async def list_sessions(kind: str = Query("chat"), q: str = Query(""), limit: int = Query(200, ge=1, le=1000),
                        offset: int = Query(0, ge=0)):
    agent = get_agent()
    rows = agent.sqlite.list_sessions(kind=None if kind == "all" else kind, limit=limit, offset=offset, query=q)
    return {"sessions": [_session_summary(agent, r) for r in rows]}


@app.post("/sessions")
@app.post("/sessions/new")
async def create_session(body: Optional[SessionCreate] = None):
    agent = get_agent()
    sess = agent.sessions.create(kind="chat", title=(body.title if body else "") or "")
    return {"session_id": sess.id, "session": sess.to_dict()}


@app.get("/sessions/search")
async def search_sessions(q: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=100)):
    agent = get_agent()
    rows = agent.sqlite.search_messages(q, limit=limit)
    return {"results": [{"session_id": r["session_id"], "title": r.get("title"), "role": r["role"],
                         "snippet": r.get("snippet"), "created_at": r.get("created_at"), "message_id": r["id"]}
                        for r in rows]}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    agent = get_agent()
    sess = _session_or_404(session_id)
    row = agent.sqlite.get_session(session_id) or {}
    return {**_session_summary(agent, row), **sess.to_dict(), "context_summary": sess.context_summary}


@app.patch("/sessions/{session_id}")
async def update_session(session_id: str, update: SessionUpdate):
    agent = get_agent()
    sess = _session_or_404(session_id)
    if update.title is not None:
        agent.sessions.set_title(sess, update.title.strip()[:120] or "New chat")
    if update.pinned is not None:
        agent.sqlite.update_session(session_id, pinned=update.pinned)
    return {"success": True}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    agent = get_agent()
    try:
        ok = agent.sessions.delete(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "session_id": session_id}


@app.post("/sessions/{session_id}/compact")
async def compact_session(session_id: str):
    agent = get_agent()
    sess = _session_or_404(session_id)
    if sess.is_running:
        raise HTTPException(status_code=409, detail="Session is running")
    ok = await agent.compact(sess)
    return {"success": ok}


def _ui_content(content: Any) -> Dict[str, Any]:
    """Split stored content into display text + attachment refs."""
    if isinstance(content, list):
        text = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        images = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = (p.get("image_url") or {}).get("url", "")
                if url.startswith("upload://"):
                    images.append({"url": f"/api/uploads/{url[len('upload://'):]}", "name": p.get("_name", "image")})
        return {"text": text, "images": images}
    return {"text": content or "", "images": []}


@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = Query(5000, ge=1, le=20000)):
    """Full transcript (including compacted turns) in display form."""
    agent = get_agent()
    sess = _session_or_404(session_id)
    rows = agent.sqlite.load_messages(session_id, limit=limit)
    out = []
    for m in rows:
        meta = m.get("_meta") or {}
        item: Dict[str, Any] = {"id": m["_id"], "role": m["role"], "created_at": m.get("_ts")}
        if m["role"] == "tool":
            raw = m.get("content") or ""
            try:
                result = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result = {"output": raw}
            item.update({"tool_call_id": m.get("tool_call_id"), "name": m.get("name"), "result": result,
                         "success": meta.get("success", True), "duration_ms": meta.get("duration_ms")})
        else:
            item.update(_ui_content(m.get("content")))
            if m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function") or {}
                    try:
                        params = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        params = {"raw": fn.get("arguments")}
                    calls.append({"id": tc.get("id"), "name": fn.get("name"), "params": params})
                item["tool_calls"] = calls
            if m.get("reasoning_content"):
                item["reasoning"] = m["reasoning_content"]
            if meta.get("interrupted"):
                item["interrupted"] = True
        if sess.compacted_upto and m["_id"] <= sess.compacted_upto:
            item["compacted"] = True
        out.append(item)
    return {"session_id": session_id, "title": sess.title, "messages": out, "count": len(out),
            "is_running": sess.is_running, "turn_id": sess.turn.id if sess.is_running else None,
            "todos": sess.todos.to_dict_list(), "usage": sess.last_usage}


@app.get("/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = Query("md")):
    agent = get_agent()
    sess = _session_or_404(session_id)
    rows = agent.sqlite.load_messages(session_id)
    if format == "json":
        return {"session": agent.sqlite.get_session(session_id),
                "messages": [{k: v for k, v in m.items() if k != "_meta"} for m in rows]}
    lines = [f"# {sess.title or 'Chat'}", ""]
    for m in rows:
        if m["role"] == "user":
            text = content_to_text(m.get("content")).split("\n\n<context>")[0]
            text = re.sub(r"<attachment name=\"([^\"]*)\"[^>]*>.*?</attachment>", r"[attachment: \1]", text, flags=re.DOTALL)
            text = re.sub(r"\[Attached (image|file): ([^\]\(]+?)[\s(\]].*", r"[attachment: \2]", text)
            lines += ["## You", "", text.strip(), ""]
        elif m["role"] == "assistant":
            text = content_to_text(m.get("content"))
            if text.strip():
                lines += ["## Alita", "", text, ""]
            for tc in m.get("tool_calls") or []:
                lines.append(f"> tool `{(tc.get('function') or {}).get('name')}`")
            if m.get("tool_calls"):
                lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


# ─── Uploads ──────────────────────────────────────────────────────────

@app.post("/uploads")
async def upload(req: UploadRequest):
    data_str = req.data
    mime = req.mime
    if data_str.startswith("data:"):
        header, _, data_str = data_str.partition(",")
        mime = mime or header[5:].split(";")[0]
    try:
        data = base64.b64decode(data_str, validate=False)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid base64 data")
    try:
        meta = save_upload(req.name, data, mime, workspace=get_agent().workspace_dir)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    return {**meta, "url": f"/api/uploads/{meta['id']}"}


@app.get("/uploads/{upload_id}")
async def get_upload_file(upload_id: str):
    found = get_upload(upload_id)
    if not found:
        raise HTTPException(status_code=404, detail="Upload not found")
    path, meta = found
    return FileResponse(str(path), media_type=meta.get("mime"), filename=meta.get("name"))


# ─── Memory ───────────────────────────────────────────────────────────

@app.get("/memory")
async def get_memory():
    agent = get_agent()
    return {"blocks": agent.core_memory.describe(), "stats": agent.longterm.stats()}


@app.put("/memory/blocks/{name}")
async def update_block(name: str, body: BlockUpdate):
    agent = get_agent()
    from ..memory.core_memory import CoreMemoryError
    try:
        text = agent.core_memory.set(name, body.content)
    except CoreMemoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "name": name, "chars": len(text)}


@app.get("/memory/entries")
async def list_memories(q: str = Query(""), category: str = Query(""), limit: int = Query(100, ge=1, le=1000),
                        offset: int = Query(0, ge=0)):
    agent = get_agent()
    if q:
        items = await agent.longterm.search(q, k=limit, category=category or None, touch=False)
    else:
        items = agent.longterm.list(limit=limit, offset=offset, category=category or None)
    return {"memories": items, "total": agent.longterm.count()}


@app.post("/memory/entries")
async def create_memory(body: MemoryCreate):
    res = await get_agent().longterm.add(body.content, body.category, body.importance, dedupe=True)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res


@app.patch("/memory/entries/{memory_id}")
async def update_memory(memory_id: int, body: MemoryUpdate):
    ok = await get_agent().longterm.update(memory_id, body.content, body.category, body.importance)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"success": True}


@app.delete("/memory/entries/{memory_id}")
async def delete_memory(memory_id: int):
    ok = await get_agent().longterm.delete(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"success": True}


@app.get("/memory/user")
async def get_user_memory():
    return {"user_md": get_agent().core_memory.get("user")}


@app.get("/memory/env")
async def get_env_memory():
    return {"memory_md": get_agent().core_memory.get("notes")}


# ─── Skills ───────────────────────────────────────────────────────────

@app.get("/skills")
async def list_skills():
    return {"skills": get_agent().skills.list()}


@app.get("/skills/{name}")
async def get_skill(name: str):
    skill = get_agent().skills.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.post("/skills")
async def create_skill(body: SkillCreate):
    try:
        return get_agent().skills.create(body.name, body.description, body.content, body.tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.put("/skills/{name}")
async def update_skill(name: str, body: SkillUpdate):
    try:
        return get_agent().skills.update(name, body.description, body.content, body.tags)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/skills/{name}")
async def delete_skill(name: str):
    if not get_agent().skills.delete(name):
        raise HTTPException(status_code=400, detail="Only user skills can be deleted")
    return {"success": True}


# ─── Persona ──────────────────────────────────────────────────────────

@app.get("/personality")
async def get_personality():
    from ..core.prompt_builder import DEFAULT_PERSONA, load_instructions, load_persona
    persona = load_persona()
    return {"persona": persona, "personality": persona, "instructions": load_instructions(), "default": DEFAULT_PERSONA,
            "is_default": not config.persona_path().exists()}


@app.put("/personality")
async def update_personality(update: PersonalityUpdate):
    persona = update.persona if update.persona is not None else update.prompt
    if persona is not None:
        if persona.strip():
            config.persona_path().write_text(persona.strip() + "\n", encoding="utf-8")
        else:
            config.persona_path().unlink(missing_ok=True)
    if update.instructions is not None:
        p = config.instructions_path()
        if update.instructions.strip():
            p.write_text(update.instructions.strip() + "\n", encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    return await get_personality()


@app.post("/personality/reset")
async def reset_personality():
    config.persona_path().unlink(missing_ok=True)
    return await get_personality()


@app.get("/personality/preview")
async def preview_system_prompt(session_id: Optional[str] = Query(None)):
    """The full system prompt the model receives (persona + guidelines + memory + skills + environment)."""
    agent = get_agent()
    sess = agent.sessions.get(session_id) if session_id else None
    if sess is None:
        sess = Session("preview", kind="chat")  # throwaway, never persisted
    prompt = agent._system_prompt(sess)
    from ..core.context import estimate_tokens
    return {"prompt": prompt, "chars": len(prompt),
            "approx_tokens": estimate_tokens([{"role": "system", "content": prompt}])}


# ─── Tools & approvals ────────────────────────────────────────────────

@app.get("/tools")
async def list_tools():
    agent = get_agent()
    tools = agent.tool_registry.list_tools()
    return {
        "tools": agent.tool_registry.list_definitions(),
        "tool_names": [t.name for t in tools],
        "details": [{"name": t.name, "description": t.definition.get("description", ""), "tags": t.tags} for t in tools],
    }


@app.post("/tools/execute")
async def execute_tool(request: ToolExecuteRequest):
    return await get_agent()._dispatch_tool(request.tool, request.params)


@app.get("/commands")
async def list_commands():
    return {"commands": get_agent().slash_registry.list()}


@app.get("/approvals/pending")
async def list_pending_approvals():
    agent = get_agent()
    return {"pending": agent.approval_coordinator.list_pending(), "count": agent.approval_coordinator.pending_count,
            "mode": agent.approval_policy.mode}


@app.post("/approve")
async def resolve_approval(request: ApprovalRequest):
    agent = get_agent()
    if not agent.approval_coordinator.resolve(request.approval_id, request.decision):
        raise HTTPException(status_code=404, detail="Approval not found or already resolved.")
    return {"approval_id": request.approval_id, "decision": "approved" if request.decision else "denied",
            "remaining": agent.approval_coordinator.pending_count}


# ─── Kanban ───────────────────────────────────────────────────────────

class KanbanCardCreate(BaseModel):
    title: str
    description: str = ""
    column: str = "idea"
    priority: int = 0
    tags: list = Field(default_factory=list)
    auto_advance: bool = True
    max_iterations: int = 20


class KanbanCardUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    column: Optional[str] = None
    priority: Optional[int] = None
    tags: Optional[list] = None
    auto_advance: Optional[bool] = None
    max_iterations: Optional[int] = None


@app.get("/kanban/cards")
async def list_kanban_cards(column: Optional[str] = Query(None)):
    agent = get_agent()
    cards = agent.kanban_store.list_cards(column=column) if column else agent.kanban_store.list_cards()
    return {"cards": [c.to_dict() for c in cards], "count": len(cards)}


@app.post("/kanban/cards")
async def create_kanban_card(card: KanbanCardCreate):
    created = get_agent().kanban_store.create_card(
        title=card.title, description=card.description, column=card.column, priority=card.priority,
        tags=card.tags, auto_advance=card.auto_advance, max_iterations=card.max_iterations)
    return {"success": True, "card": created.to_dict()}


@app.get("/kanban/cards/{card_id}")
async def get_kanban_card(card_id: str):
    card = get_agent().kanban_store.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return {"card": card.to_dict()}


@app.patch("/kanban/cards/{card_id}")
async def update_kanban_card(card_id: str, update: KanbanCardUpdate):
    agent = get_agent()
    fields = {k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None}
    card = agent.kanban_store.update_card(card_id, **fields) if fields else agent.kanban_store.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return {"success": True, "card": card.to_dict()}


@app.delete("/kanban/cards/{card_id}")
async def delete_kanban_card(card_id: str):
    if not get_agent().kanban_store.delete_card(card_id):
        raise HTTPException(status_code=404, detail="Card not found")
    return {"success": True, "card_id": card_id}


@app.post("/kanban/cards/{card_id}/retry")
async def retry_kanban_card(card_id: str):
    if not get_agent().kanban_executor.retry_card(card_id):
        raise HTTPException(status_code=400, detail="Card not found or not in 'failed' status")
    return {"success": True, "card_id": card_id}


@app.post("/kanban/cards/{card_id}/cancel")
async def cancel_kanban_card(card_id: str):
    if not get_agent().kanban_executor.cancel_card(card_id):
        raise HTTPException(status_code=400, detail="Card not found or not running")
    return {"success": True, "card_id": card_id}


@app.get("/kanban/columns")
async def get_kanban_columns():
    from ..kanban.store import COLUMNS, EXECUTING_COLUMN
    return {"columns": COLUMNS, "executing_column": EXECUTING_COLUMN}


# ─── Scheduled jobs ───────────────────────────────────────────────────

class JobCreate(BaseModel):
    name: str
    description: str = ""
    prompt: str
    cron_expr: str
    enabled: bool = True
    timezone: str = ""


class JobUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    cron_expr: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None


def _check_cron(expr: str, timezone: str = "") -> None:
    from ..scheduler.service import SchedulerService
    try:
        SchedulerService.validate_cron(expr, timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid schedule: {exc}")


def _check_timezone(name: str) -> None:
    from ..scheduler.service import SchedulerService
    try:
        SchedulerService.validate_timezone(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid timezone: {exc}")


@app.get("/jobs")
async def list_jobs():
    jobs = get_agent().scheduler_store.list_jobs()
    return {"jobs": [j.to_dict() for j in jobs], "count": len(jobs)}


@app.post("/jobs")
async def create_job(job: JobCreate):
    _check_timezone(job.timezone)
    _check_cron(job.cron_expr, job.timezone)
    created = get_agent().scheduler_service.add_job(
        name=job.name, prompt=job.prompt, cron_expr=job.cron_expr,
        description=job.description, enabled=job.enabled, timezone=job.timezone,
    )
    return {"success": True, "job": created.to_dict()}


@app.get("/jobs/runs/all")
async def list_all_runs(limit: int = Query(50, ge=1, le=200)):
    runs = get_agent().scheduler_store.list_runs(limit=limit)
    return {"runs": [r.to_dict() for r in runs], "count": len(runs)}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = get_agent().scheduler_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job.to_dict()}


@app.patch("/jobs/{job_id}")
async def update_job(job_id: str, update: JobUpdate):
    agent = get_agent()
    fields = {k: v for k, v in update.model_dump(exclude_unset=True).items() if v is not None}
    if "timezone" in fields:
        _check_timezone(fields["timezone"])
    if "cron_expr" in fields:
        _check_cron(fields["cron_expr"], fields.get("timezone", ""))
    job = agent.scheduler_service.update_job(job_id, **fields) if fields else agent.scheduler_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job": job.to_dict()}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if not get_agent().scheduler_service.remove_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job_id": job_id}


@app.post("/jobs/{job_id}/run")
async def run_job_now(job_id: str):
    if get_agent().scheduler_service.run_job_now(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "job_id": job_id, "message": "Job started"}


@app.get("/jobs/{job_id}/runs")
async def list_job_runs(job_id: str, limit: int = Query(20, ge=1, le=200)):
    runs = get_agent().scheduler_store.list_runs(job_id=job_id, limit=limit)
    return {"runs": [r.to_dict() for r in runs], "count": len(runs)}


# ─── Images ───────────────────────────────────────────────────────────

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@app.get("/images")
async def list_images():
    outputs_dir = get_agent().easy_diffusion.outputs_dir
    if not outputs_dir.exists():
        return {"images": [], "count": 0}
    images = []
    for fpath in outputs_dir.iterdir():
        if fpath.suffix.lower() not in _IMAGE_EXTS:
            continue
        metadata: Dict[str, Any] = {}
        sidecar = fpath.with_suffix(".json")
        if sidecar.exists():
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        stat = fpath.stat()
        images.append({
            "filename": fpath.name, "size_bytes": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "url": f"/api/images/{fpath.name}/file",
            "prompt": metadata.get("prompt", ""), "negative_prompt": metadata.get("negative_prompt", ""),
            "seed": metadata.get("seed"), "sampler": metadata.get("sampler", ""),
            "steps": metadata.get("steps", 0), "cfg_scale": metadata.get("cfg_scale", 0),
            "width": metadata.get("width", 0), "height": metadata.get("height", 0),
            "task_id": metadata.get("task_id", ""),
        })
    images.sort(key=lambda x: x["created_at"], reverse=True)
    return {"images": images, "count": len(images)}


def _safe_image(filename: str):
    outputs_dir = get_agent().easy_diffusion.outputs_dir.resolve()
    safe_path = (outputs_dir / filename).resolve()
    if not safe_path.is_file() or safe_path.parent != outputs_dir:
        raise HTTPException(status_code=404, detail="Image not found")
    return safe_path


@app.get("/images/{filename}/file")
async def serve_image(filename: str):
    path = _safe_image(filename)
    return FileResponse(str(path), media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")


@app.delete("/images/{filename}")
async def delete_image(filename: str):
    path = _safe_image(filename)
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)
    return {"success": True, "filename": filename}


# ─── Voice ────────────────────────────────────────────────────────────

def _voice_http_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status", 502), detail=str(exc))


@app.get("/voice/status")
async def voice_status(usage: bool = False):
    voice = get_agent().voice
    payload = voice.status()
    if usage and voice.providers["elevenlabs"].available():
        payload["usage"] = await voice.usage()
    return payload


@app.get("/voice/providers")
async def voice_providers():
    voice = get_agent().voice
    return {"active": voice.status()["provider"], "providers": [p.info() for p in voice.providers.values()]}


@app.get("/voice/voices")
async def voice_list(provider: Optional[str] = Query(None), search: str = Query(""),
                     locale: str = Query(""), gender: str = Query(""), source: str = Query("account"),
                     limit: int = Query(200, ge=1, le=400)):
    from ..voice import VoiceError
    try:
        return await get_agent().voice.list_voices(
            provider=provider, search=search, locale=locale, gender=gender, limit=limit, source=source)
    except VoiceError as exc:
        raise _voice_http_error(exc)


@app.put("/voice")
async def voice_update(body: VoiceUpdate):
    """Choose the voice / tune it (used by the voice picker; Alita uses the tool)."""
    from ..voice import VoiceError
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    provider = fields.pop("provider", None)
    voice_id = fields.pop("voice_id", None)
    name = fields.pop("name", None)
    reason = fields.pop("reason", None)
    owner = fields.pop("public_owner_id", "") or ""
    try:
        result = await get_agent().voice.set_voice(
            provider=provider, voice_id=voice_id, name=name, reason=reason,
            public_owner_id=owner, allow_empty=True, **fields)
    except VoiceError as exc:
        raise _voice_http_error(exc)
    return {**result, "status": get_agent().voice.status()}


@app.post("/voice/preview")
async def voice_preview(body: VoicePreviewRequest):
    from ..voice import VoiceError
    try:
        return await get_agent().voice.preview(
            provider=body.provider, voice_id=body.voice_id, text=body.text,
            public_owner_id=body.public_owner_id)
    except VoiceError as exc:
        raise _voice_http_error(exc)


@app.post("/voice/speech")
async def prepare_speech(body: SpeechRequest):
    """Register reply text for speaking; returns a URL an <audio> element can stream."""
    from ..voice import VoiceError
    try:
        return get_agent().voice.prepare(body.text)
    except VoiceError as exc:
        raise _voice_http_error(exc)


@app.get("/voice/speech/{utterance_id}.mp3")
async def speech_audio(utterance_id: str):
    from ..voice import VoiceError
    voice = get_agent().voice
    cached = voice.cached_path(utterance_id)
    if cached:
        return FileResponse(str(cached), media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})
    try:
        stream = await voice.stream(utterance_id)
    except VoiceError as exc:
        raise _voice_http_error(exc)
    return StreamingResponse(stream, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/voice/speech/{utterance_id}/status")
async def speech_status(utterance_id: str):
    return get_agent().voice.speech_state(utterance_id)


# ─── Gmail ────────────────────────────────────────────────────────────

@app.get("/gmail/status")
async def gmail_status():
    from ..tools.gmail import _client_secret_path
    agent = get_agent()
    return {"authorized": agent.gmail.is_authorized(), "has_client_secret": _client_secret_path().exists()}


@app.get("/gmail/auth")
async def gmail_auth():
    url = get_agent().gmail.get_auth_url()
    if url is None:
        raise HTTPException(status_code=400, detail="Gmail client secret not found. Save your OAuth credentials JSON "
                                                    "to ~/.alita/gmail_client_secret.json or set GMAIL_CLIENT_SECRET_PATH.")
    return RedirectResponse(url)


@app.get("/gmail/callback")
async def gmail_callback(code: str = "", error: str = ""):
    if error:
        return {"success": False, "error": error}
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    if get_agent().gmail.exchange_code(code):
        return {"success": True, "message": "Gmail authorised successfully! You can close this tab."}
    raise HTTPException(status_code=400, detail="Failed to exchange authorization code")


# ─── Settings ─────────────────────────────────────────────────────────

_AGENT_ENV = {
    "allow_host_exec": ("ALITA_ALLOW_HOST_EXEC", bool),
    "max_iterations": ("ALITA_MAX_ITERATIONS", int),
    "temperature": ("ALITA_TEMPERATURE", float),
    "context_window": ("ALITA_CONTEXT_WINDOW", int),
    "memory_auto_extract": ("ALITA_MEMORY_AUTO_EXTRACT", bool),
    "memory_auto_recall": ("ALITA_MEMORY_AUTO_RECALL", bool),
    "skills_auto_create": ("ALITA_SKILLS_AUTO_CREATE", bool),
    "auto_title": ("ALITA_AUTO_TITLE", bool),
    "approval_mode": ("ALITA_APPROVAL_MODE", str),
    "headless_approval": ("ALITA_HEADLESS_APPROVAL", str),
}


def _settings_payload(agent: AlitaAgent) -> Dict[str, Any]:
    from ..llm.provider import list_providers
    from ..llm.liquid_local import liquid_status
    s = agent.settings
    return {
        "provider": agent.llm_provider_name,
        "model": agent.llm_model_name,
        "context_window": agent.context_window,
        "providers": list_providers(),
        "liquid": liquid_status(),
        "agent": {
            "allow_host_exec": agent.allow_host_exec,
            "max_iterations": s.max_iterations,
            "temperature": s.temperature,
            "context_window": s.context_window,
            "memory_auto_extract": s.memory_auto_extract,
            "memory_auto_recall": s.memory_auto_recall,
            "skills_auto_create": s.skills_auto_create,
            "auto_title": s.auto_title,
            "approval_mode": agent.approval_policy.mode,
            "headless_approval": s.headless_approval,
        },
        "easy_diffusion_url": agent.easy_diffusion.base_url,
        "voice": agent.voice.status(),
        "workspace": agent.workspace_dir,
        "home": str(agent.home),
        "docker_available": agent.sandbox_mgr.docker_available,
    }


def _apply_runtime_settings(agent: AlitaAgent) -> None:
    from ..core.approval import ApprovalPolicy
    agent.settings = config.AgentSettings()
    agent.allow_host_exec = config.env_bool("ALITA_ALLOW_HOST_EXEC", False)
    agent.approval_policy = ApprovalPolicy(workspace_dir=agent.workspace_dir)
    url = os.getenv("EASY_DIFFUSION_URL")
    if url:
        agent.easy_diffusion.base_url = url.rstrip("/")


@app.get("/settings")
async def get_settings():
    return _settings_payload(get_agent())


@app.get("/settings/providers")
async def list_available_providers():
    from ..llm.provider import list_providers
    return {"providers": list_providers()}


@app.get("/models/liquid/status")
async def liquid_model_status():
    from ..llm.liquid_local import liquid_status
    return liquid_status()


@app.post("/models/liquid/prepare")
async def prepare_liquid_model():
    from ..llm.liquid_local import start_prepare
    return start_prepare()


@app.put("/settings")
async def update_settings(update: SettingsUpdate):
    from ..llm.provider import PROVIDER_REGISTRY
    agent = get_agent()
    env: Dict[str, Optional[str]] = {}
    provider = (update.provider or agent.llm_provider_name).lower()
    if provider not in PROVIDER_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'")
    if update.provider:
        env["LLM_PROVIDER"] = provider
    if update.model:
        env[PROVIDER_REGISTRY[provider]["env_model"]] = update.model.strip()
    for pid, model in update.models.items():
        if pid in PROVIDER_REGISTRY and model.strip():
            env[PROVIDER_REGISTRY[pid]["env_model"]] = model.strip()
    for pid, key in update.api_keys.items():
        info = PROVIDER_REGISTRY.get(pid)
        if info and info.get("env_key"):
            env[info["env_key"]] = key.strip()
    for pid, url in update.base_urls.items():
        info = PROVIDER_REGISTRY.get(pid)
        if info and info.get("env_base_url"):
            env[info["env_base_url"]] = url.strip()
    for key, value in update.agent.items():
        if key not in _AGENT_ENV:
            continue
        env_name, typ = _AGENT_ENV[key]
        if value is None or value == "":
            env[env_name] = ""
        elif typ is bool:
            env[env_name] = "true" if value in (True, "true", "1", 1) else "false"
        else:
            try:
                env[env_name] = str(typ(value))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Invalid value for {key}")
    if update.easy_diffusion_url is not None:
        env["EASY_DIFFUSION_URL"] = update.easy_diffusion_url.strip()
    if update.elevenlabs_api_key and update.elevenlabs_api_key.strip():
        env["ELEVENLABS_API_KEY"] = update.elevenlabs_api_key.strip()
    if update.elevenlabs_model is not None:
        model = update.elevenlabs_model.strip()
        if model and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", model):
            raise HTTPException(status_code=400, detail="Invalid ElevenLabs model id")
        env["ELEVENLABS_MODEL"] = model

    config.update_env_file(env)
    _apply_runtime_settings(agent)
    try:
        agent.set_llm(os.getenv("LLM_PROVIDER", provider))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Saved, but could not activate provider: {exc}")
    return {"message": "Settings saved and applied.", **_settings_payload(agent)}


@app.post("/settings/reload")
async def reload_settings():
    agent = get_agent()
    config.reload_env(override=True)
    _apply_runtime_settings(agent)
    agent.set_llm(os.getenv("LLM_PROVIDER", "ollama"))
    return {"message": "Settings reloaded", "provider": agent.llm_provider_name, "model": agent.llm_model_name}


@app.post("/settings/test")
async def test_llm():
    agent = get_agent()
    import time as _time
    t0 = _time.time()
    try:
        resp = await asyncio.wait_for(
            agent.llm.chat(messages=[{"role": "user", "content": "Reply with exactly: OK"}], temperature=0, max_tokens=10),
            timeout=60)
        return {"ok": True, "latency_ms": int((_time.time() - t0) * 1000), "reply": content_to_text(resp.get("content"))[:100],
                "provider": agent.llm_provider_name, "model": agent.llm_model_name}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "provider": agent.llm_provider_name, "model": agent.llm_model_name}
