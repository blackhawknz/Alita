"""
Alita agent core.

``AlitaAgent`` owns the shared runtime — LLM provider, tools, memory, skills,
stores and background services — while all conversation state lives in
per-session :class:`~alita.core.session.Session` objects. One event-driven
loop (:meth:`AlitaAgent._run_turn`) serves every caller:

* interactive chat (SSE streaming, resumable via :class:`TurnRun`)
* non-streaming calls, scheduled jobs, kanban cards and sub-agents
  (:meth:`AlitaAgent.run_conversation`)

Turn lifecycle
--------------
1. Persist the user message (immediately — history survives crashes).
2. Auto-recall long-term memories and relevant skills for the message.
3. Loop: build system prompt + context → compact if near the window →
   stream the model → persist the assistant message → run tool calls
   (approval gates, parallel where safe) → persist each tool result.
4. Emit ``done``; background tasks then extract memories, consider creating
   a skill, and title new chats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Set

from . import config
from .approval import ApprovalCoordinator, ApprovalPolicy
from .attachments import resolve_for_llm
from .context import build_payload, estimate_tokens, hard_trim, plan_compaction, render_transcript, summarise
from .prompt_builder import PromptBuilder, clean_legacy_persona
from .runtime import current_session
from .session import Session, SessionManager, TurnRun
from .stream_scrubber import ThinkBlockScrubber
from .tool_guardrails import ToolGuardrails
from ..api.security import should_block_host_exec
from ..llm.provider import LLMError, LLMProvider, create_provider
from ..memory.core_memory import CoreMemory
from ..memory.longterm import LongTermMemory
from ..memory.manager import CORE_MEMORY_TOOL_SCHEMA, MEMORY_TOOL_SCHEMA, MemoryManager
from ..memory.sqlite_store import SQLiteStore, content_to_text, fallback_title
from ..memory.vector_store import VectorStore
from ..skills.manager import SKILL_MANAGE_SCHEMA, SKILL_VIEW_SCHEMA, SkillManager
from ..tools.registry import Tool, ToolRegistry

logger = logging.getLogger(__name__)

Emit = Callable[[Dict[str, Any]], Awaitable[None]]

# Tools that are safe to run concurrently within one step. ``None`` means every
# action of that tool is safe; otherwise only the listed read-only actions are.
_PARALLEL_SAFE_ACTIONS: Dict[str, Optional[Set[str]]] = {
    "browser": None,
    "http_request": None,
    "gmail": None,
    "session_search": None,
    "memory": None,
    "skill_view": None,
    "delegate_task": None,
    "file_ops": {"read", "list", "search", "list_checkpoints"},
    "database": {"query", "schema"},
    "git": {"status", "diff", "log"},
    "easy_diffusion": {"status", "models"},
}

TODO_TOOL_SCHEMA = {
    "name": "todo",
    "description": (
        "Plan and track multi-step work. Send the COMPLETE list every time (it replaces the previous list). "
        "Keep exactly one item 'in_progress'; mark items 'completed' as soon as they're done. "
        "Call with no todos to read the current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    },
                    "required": ["title", "status"],
                },
            },
        },
    },
}

SESSION_SEARCH_SCHEMA = {
    "name": "session_search",
    "description": "Full-text search over past conversations (all sessions). Returns matching snippets with session titles.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for."},
            "session_id": {"type": "string", "description": "Restrict to one session (optional)."},
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
        "required": ["query"],
    },
}

TITLE_PROMPT = (
    "Write a short title (3-6 words, no quotes, no trailing punctuation) for a conversation that starts with "
    "this user message. Reply with the title only.\n\nMessage:\n{msg}"
)


def _truncate_result(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n…[truncated {len(text) - limit} chars; ask for a narrower range if you need more]…\n{text[-tail:]}"


class AlitaAgent:
    """Shared agent runtime. Conversation state lives in :class:`Session`."""

    def __init__(
        self,
        llm_provider: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        easy_diffusion_url: Optional[str] = None,
        max_iterations: Optional[int] = None,
        model_context_length: Optional[int] = None,
        allow_host_exec: Optional[bool] = None,
        db_path: Optional[str] = None,
        vector_store: Optional[VectorStore] = None,
    ):
        self.settings = config.AgentSettings()
        if max_iterations:
            self.settings.max_iterations = max_iterations
        if model_context_length:
            self.settings.context_window = model_context_length
        self.workspace_dir = str(workspace_dir or config.workspace_dir())
        self.home = config.alita_home()
        self.allow_host_exec = (
            allow_host_exec if allow_host_exec is not None else config.env_bool("ALITA_ALLOW_HOST_EXEC", False)
        )

        # ─ Storage & memory ─
        self.sqlite = SQLiteStore(db_path)
        self.sessions = SessionManager(self.sqlite)
        self.vector_store = vector_store or VectorStore()
        self.core_memory = CoreMemory(config.memory_dir())
        self.longterm = LongTermMemory(self.sqlite, self.vector_store)
        self.memory = MemoryManager(self.sqlite, self.core_memory, self.longterm, lambda: self.llm)
        self.skills = SkillManager(config.user_skills_dir(), config.builtin_skills_dir(), self.sqlite, self.vector_store)
        self.prompt_builder = PromptBuilder(self.workspace_dir)
        self._migrate_legacy()

        # ─ LLM ─
        self.llm: LLMProvider
        self.set_llm(llm_provider or os.getenv("LLM_PROVIDER", "ollama"), llm_model, llm_api_key)

        # ─ Tools ─
        self.tool_registry = ToolRegistry()
        self._register_builtin_tools(easy_diffusion_url or os.getenv("EASY_DIFFUSION_URL", "http://localhost:9000"))

        from ..tools.subagent import SubAgentManager, make_subagent_tool
        from ..tools.kanban_tool import make_kanban_tool
        from ..kanban.store import KanbanStore
        from ..kanban.executor import KanbanExecutor
        from ..scheduler.store import SchedulerStore
        from ..scheduler.service import SchedulerService
        from ..tools.mcp_manager import MCPManager
        from .slash_commands import build_slash_registry

        self.subagent_manager = SubAgentManager(self, default_max_iterations=self.settings.subagent_max_iterations)
        self.tool_registry.register(make_subagent_tool(self.subagent_manager))
        self.kanban_store = KanbanStore(str(self.sqlite.db_path))
        self.kanban_executor = KanbanExecutor(self.kanban_store, self.subagent_manager)
        self.tool_registry.register(make_kanban_tool(self.kanban_store))
        self.scheduler_store = SchedulerStore(str(self.sqlite.db_path))
        self.scheduler_service = SchedulerService(self, self.scheduler_store)
        self.mcp_manager = MCPManager(self.tool_registry)

        self.approval_policy = ApprovalPolicy(workspace_dir=self.workspace_dir)
        self.approval_coordinator = ApprovalCoordinator(timeout=self.approval_policy.timeout)
        self.slash_registry = build_slash_registry(self)

        self._background: Set[asyncio.Task] = set()
        self._services_started = False

    # ─── Configuration ────────────────────────────────────────────

    def set_llm(self, provider: str, model: Optional[str] = None, api_key: Optional[str] = None) -> None:
        """Hot-swap the LLM provider (running turns keep their provider)."""
        self.llm = create_provider(provider, model=model, api_key=api_key)
        self.llm_provider_name = provider
        self.llm_model_name = self.llm.model

    @property
    def context_window(self) -> int:
        return self.settings.context_window or self.llm.context_window

    @property
    def easy_diffusion_url(self) -> str:
        return self.easy_diffusion.base_url

    def _migrate_legacy(self) -> None:
        """One-time import of v0.1 memory files and persona clean-up."""
        legacy_data = config.BACKEND_DIR / "data"
        try:
            self.core_memory.import_legacy(legacy_data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("legacy memory import failed: %s", exc)
        marker = self.home / ".persona_migrated"
        soul = config.persona_path()
        if soul.exists() and not marker.exists():
            try:
                text = soul.read_text(encoding="utf-8")
                persona, custom = clean_legacy_persona(text)
                if persona != text.strip() or custom:
                    (self.home / "data" / "SOUL.md.v1.bak").write_text(text, encoding="utf-8")
                    soul.write_text(persona + "\n", encoding="utf-8")
                    instr = config.instructions_path()
                    if custom and not instr.exists():
                        instr.write_text(custom + "\n", encoding="utf-8")
                marker.write_text("1", encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                logger.warning("persona migration failed: %s", exc)

    # ─── Tool registration ────────────────────────────────────────

    def _register_builtin_tools(self, easy_diffusion_url: str) -> None:
        from ..tools.browser import BrowserTool
        from ..tools.code_execution import CodeExecutionTool
        from ..tools.database_tool import DatabaseTool
        from ..tools.easydiffusion import EasyDiffusionTool
        from ..tools.file_ops import FileOps
        from ..tools.git_tools import GitTools
        from ..tools.gmail import GmailTool
        from ..tools.http_request import HttpRequestTool
        from ..tools.sandbox import SandboxManager
        from ..tools.terminal import TerminalTool
        from ..voice import VoiceService

        self.sandbox_mgr = SandboxManager(self.workspace_dir)
        self.terminal = TerminalTool(self.sandbox_mgr)
        self.file_ops = FileOps(self.workspace_dir)
        self.browser = BrowserTool()
        self.easy_diffusion = EasyDiffusionTool(easy_diffusion_url)
        self.code_execution = CodeExecutionTool()
        self.git_tools = GitTools(self.workspace_dir)
        self.database_tool = DatabaseTool(self.workspace_dir)
        self.http_request = HttpRequestTool()
        self.gmail = GmailTool()
        self.voice = VoiceService()

        reg = self.tool_registry.register
        reg(Tool("terminal", "Run shell commands", self.terminal.definition, self.terminal.execute,
                 ["cli", "execution", "coding", "host_exec"]))
        reg(Tool("file_ops", "Read/write/patch files", self.file_ops.definition, self.file_ops.execute,
                 ["file_management", "coding"]))
        reg(Tool("browser", "Web search and page reading", self.browser.definition, self.browser.execute,
                 ["web", "research"]))
        reg(Tool("http_request", "HTTP API calls", self.http_request.definition, self.http_request.execute,
                 ["web", "network"]))
        reg(Tool("execute_code", "Run code snippets (unsandboxed on host)",
                 self.code_execution.definition, self.code_execution.execute,
                 ["coding", "execution", "host_exec"]))
        reg(Tool("git", "Git operations", self.git_tools.definition, self.git_tools.execute, ["git", "coding"]))
        reg(Tool("database", "SQLite queries", self.database_tool.definition, self.database_tool.execute,
                 ["database"]))
        reg(Tool("easy_diffusion", "Image generation", self.easy_diffusion.definition, self.easy_diffusion.execute,
                 ["image_generation"]))
        reg(Tool("gmail", "Read Gmail", self.gmail.definition, self.gmail.execute, ["email"]))
        reg(Tool("voice", "Choose and tune your speaking voice", self.voice.definition, self.voice.execute, ["voice"]))

        # Session-aware tools (read the current session from a context var).
        reg(Tool("todo", "Task list", TODO_TOOL_SCHEMA, self._tool_todo, ["planning"]))
        reg(Tool("memory", "Long-term memory", MEMORY_TOOL_SCHEMA, self._tool_memory, ["memory"]))
        reg(Tool("core_memory", "Core memory blocks", CORE_MEMORY_TOOL_SCHEMA, self.memory.tool_core_memory, ["memory"]))
        reg(Tool("session_search", "Search past conversations", SESSION_SEARCH_SCHEMA, self._tool_session_search,
                 ["memory", "search"]))
        reg(Tool("skill_view", "Load a skill", SKILL_VIEW_SCHEMA, self.skills.tool_view, ["skills"]))
        reg(Tool("skill_manage", "Create/edit skills", SKILL_MANAGE_SCHEMA, self.skills.tool_manage, ["skills"]))

    async def _tool_todo(self, todos: Optional[List[Dict[str, Any]]] = None, **_: Any) -> Dict[str, Any]:
        sess: Optional[Session] = current_session.get()
        if sess is None:
            return {"success": False, "error": "todo is only available inside a conversation"}
        if todos is not None:
            try:
                sess.todos.replace_all(todos)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            self.sessions.save_todos(sess)
        return {"success": True, "todos": [{"title": t.title, "status": t.status} for t in sess.todos.list()]}

    async def _tool_memory(self, **params: Any) -> Dict[str, Any]:
        sess = current_session.get()
        return await self.memory.tool_memory(_session_id=sess.id if sess else None, **params)

    async def _tool_session_search(self, query: str = "", session_id: str = "", limit: int = 10, **_: Any) -> Dict[str, Any]:
        sess = current_session.get()
        try:
            rows = self.sqlite.search_messages(
                query, limit=max(1, min(int(limit or 10), 30)), session_id=session_id or None,
                exclude_session=None if session_id else (sess.id if sess else None),
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
        return {"success": True, "count": len(rows), "results": [
            {"session_id": r["session_id"], "session_title": r.get("title"), "role": r["role"],
             "date": r.get("created_at"), "snippet": r.get("snippet")} for r in rows
        ]}

    def tool_definitions(self, sess: Optional[Session] = None) -> List[Dict[str, Any]]:
        defs = self.tool_registry.list_definitions()
        if sess is not None and sess.depth >= self.settings.subagent_max_depth:
            defs = [d for d in defs if d["function"]["name"] != "delegate_task"]
        if sess is not None and sess.kind == "subagent":
            defs = [d for d in defs if d["function"]["name"] not in ("kanban", "skill_manage", "core_memory")]
        if sess is not None and sess.kind != "chat":
            # Her voice is a personal choice made in conversation, not by jobs or sub-agents.
            defs = [d for d in defs if d["function"]["name"] != "voice"]
        return defs

    # ─── Services ─────────────────────────────────────────────────

    async def start_services(self) -> None:
        """Start background services (call from inside the event loop)."""
        if self._services_started:
            return
        self._services_started = True
        try:
            self.sqlite.prune_empty_sessions()
        except Exception as exc:  # noqa: BLE001
            logger.debug("prune failed: %s", exc)
        self.sandbox_mgr.prefetch_docker_status()
        self.kanban_executor.start()
        self.scheduler_service.start()
        self.spawn(self._load_mcp(), name="load-mcp")
        self.spawn(self._warm_vectors(), name="warm-vectors")

    async def _load_mcp(self) -> None:
        try:
            await self.mcp_manager.load_and_register()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP load failed: %s", exc)

    async def _warm_vectors(self) -> None:
        if not self.vector_store.is_available():
            return
        ok = await asyncio.to_thread(self.vector_store.warm_up)
        if ok:
            await self.longterm.reindex()
            await self.skills.sync_index()

    def spawn(
        self,
        coro: Awaitable[Any],
        name: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
        no_timeout: bool = False,
    ) -> asyncio.Task:
        """Run a bounded background task and log its outcome.

        ``timeout`` defaults to ``ALITA_BACKGROUND_TIMEOUT`` (120s). Pass
        ``no_timeout=True`` for work that legitimately runs long, such as a
        delegated sub-agent; do not silently use it to hide a hang.
        """
        label = name or getattr(coro, "__name__", "background")
        limit = None if no_timeout else (timeout if timeout is not None else self.settings.background_timeout)
        if limit and limit > 0:
            coro = asyncio.wait_for(coro, timeout=float(limit))
        task = asyncio.ensure_future(coro)
        task.set_name(label)
        self._background.add(task)
        task.add_done_callback(self._on_background_done)
        return task

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        name = task.get_name()
        if task.cancelled():
            logger.info("background task cancelled: %s", name)
            return
        exc = task.exception()
        if exc is None:
            return
        if isinstance(exc, asyncio.TimeoutError):
            logger.warning("background task timed out and was cancelled: %s", name)
            return
        logger.warning(
            "background task failed: %s (%s)", name, exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _persist_usage(
        self,
        *,
        session_id: str,
        job_id: Optional[str],
        source: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int,
        estimated: bool,
    ) -> None:
        """Write a usage event off the turn's critical path."""
        try:
            await asyncio.to_thread(
                self.sqlite.record_usage,
                session_id=session_id,
                job_id=job_id,
                source=source,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                estimated=estimated,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not persist token usage for %s: %s", session_id, exc)

    async def shutdown(self) -> None:
        for sess in self.sessions.running():
            self.interrupt(sess.id)
        pending = list(self._background)
        if pending:
            logger.info("cancelling %d background task(s) at shutdown", len(pending))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            await self.kanban_executor.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.scheduler_service.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.mcp_manager.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.approval_coordinator.clear()
        try:
            await self.voice.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sandbox_mgr.destroy_all()
        except Exception:  # noqa: BLE001
            pass
        self.sqlite.close()

    # ─── Public turn API ──────────────────────────────────────────

    def start_turn(
        self,
        sess: Session,
        content: Any,
        *,
        edit_message_id: Optional[int] = None,
        regenerate: bool = False,
        session_instructions: str = "",
        max_iterations: Optional[int] = None,
    ) -> TurnRun:
        """Start a turn in the background; returns its resumable event buffer."""
        if sess.is_running:
            raise RuntimeError("This session is already working on a response.")
        run = TurnRun(sess.id)
        sess.turn = run

        async def _task() -> None:
            try:
                await self._run_turn(sess, content, run.emit, edit_message_id=edit_message_id,
                                     regenerate=regenerate, session_instructions=session_instructions,
                                     max_iterations=max_iterations)
            except Exception as exc:  # noqa: BLE001
                logger.exception("turn crashed")
                await run.emit({"type": "error", "message": f"Internal error: {exc}"})
                await run.emit({"type": "done", "interrupted": False, "error": str(exc)})
            finally:
                await run.finish()

        run.task = asyncio.ensure_future(_task())
        return run

    async def run_conversation(
        self,
        user_message: Any,
        system_message: Optional[str] = None,
        session: Optional[Session] = None,
        max_iterations: Optional[int] = None,
        kind: str = "job",
        title: str = "",
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        on_event: Optional[Emit] = None,
        headless_approval: Optional[str] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """Run one turn to completion (non-streaming). Creates a session if needed."""
        sess = session or self.sessions.create(kind=kind, title=title or fallback_title(content_to_text(user_message)),
                                               parent_id=parent_id, metadata=metadata)
        trace: List[Dict[str, Any]] = []
        outcome: Dict[str, Any] = {}
        errors: List[str] = []
        calls: Dict[str, Dict[str, Any]] = {}

        async def emit(ev: Dict[str, Any]) -> None:
            t = ev.get("type")
            if t == "tool_call":
                calls[ev["id"]] = {"tool_name": ev["tool"], "input_params": ev.get("params", {})}
            elif t == "tool_result":
                entry = calls.get(ev["id"], {"tool_name": ev.get("tool")})
                entry.update({"success": ev.get("success", True), "output_result": ev.get("result"),
                              "duration_ms": ev.get("duration_ms")})
                trace.append(entry)
            elif t == "error":
                errors.append(ev.get("message", ""))
            elif t == "done":
                outcome.update(ev)
            if on_event:
                await on_event(ev)

        async with sess.lock:
            await self._run_turn(sess, user_message, emit, session_instructions=system_message or "",
                                 max_iterations=max_iterations, headless_approval=headless_approval)
        return {
            "final_response": outcome.get("final_response", ""),
            "api_call_count": outcome.get("api_calls", 0),
            "tool_call_count": outcome.get("tool_calls", 0),
            "interrupted": outcome.get("interrupted", False),
            "error": "; ".join(e for e in errors if e) or None,
            "session_id": sess.id,
            "tool_trace": trace,
        }

    def interrupt(self, session_id: str) -> bool:
        sess = self.sessions.get(session_id)
        if not sess:
            return False
        sess.interrupt_requested = True
        for approval_id in list(sess.pending_approvals):
            self.approval_coordinator.resolve(approval_id, False)
        for child in self.sessions.running():
            if child.parent_id == session_id:
                child.interrupt_requested = True
        return True

    # ─── The loop ─────────────────────────────────────────────────

    async def _run_turn(
        self,
        sess: Session,
        content: Any,
        emit: Emit,
        *,
        edit_message_id: Optional[int] = None,
        regenerate: bool = False,
        session_instructions: str = "",
        max_iterations: Optional[int] = None,
        headless_approval: Optional[str] = None,
    ) -> None:
        token = current_session.set(sess)
        llm = self.llm  # a settings change mid-turn shouldn't swap providers under us
        sess.interrupt_requested = False
        started = time.time()
        api_calls = tool_calls_total = 0
        final_text = ""
        interrupted = False
        error: Optional[str] = None
        turn_start_idx = len(sess.messages)
        try:
            if edit_message_id:
                self.sessions.truncate_from(sess, int(edit_message_id))
                turn_start_idx = len(sess.messages)
            if regenerate:
                last_user = max((i for i, m in enumerate(sess.messages) if m.get("role") == "user"), default=-1)
                if last_user < 0:
                    raise RuntimeError("Nothing to regenerate.")
                after = sess.messages[last_user + 1:]
                if after and after[0].get("_id"):
                    self.sessions.truncate_from(sess, after[0]["_id"])
                content = sess.messages[last_user]["content"]
                turn_start_idx = last_user
                user_msg = sess.messages[last_user]
            else:
                user_msg = self.sessions.append(sess, {"role": "user", "content": content})

            query = content_to_text(content)
            if sess.kind == "chat" and not sess.title:
                self.sessions.set_title(sess, fallback_title(query))
                if self.settings.auto_title:
                    self.spawn(self._generate_title(sess, query, emit), name=f"generate-title:{sess.id}")
            await emit({"type": "turn_start", "session_id": sess.id, "user_message_id": user_msg.get("_id"),
                        "title": sess.title})

            # Auto-recall: memories + relevant skills, attached ephemerally.
            recalled_txt = ""
            relevant_skills: List[Dict[str, Any]] = []
            if self.settings.memory_auto_recall:
                recall_query = query
                if len(query) < 40 and turn_start_idx > 0:
                    prev = next((content_to_text(m.get("content")) for m in reversed(sess.messages[:turn_start_idx])
                                 if m.get("role") == "assistant" and m.get("content")), "")
                    recall_query = f"{query}\n{prev[-400:]}"
                items = await self.memory.recall(recall_query, k=self.settings.memory_recall_k)
                if items:
                    recalled_txt = self.memory.format_recall(items)
                    await emit({"type": "memory_recall", "items": [
                        {"id": m["id"], "content": m["content"], "category": m["category"]} for m in items]})
            try:
                relevant_skills = await self.skills.find_relevant(query, k=3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("skill relevance failed: %s", exc)
            if relevant_skills:
                await emit({"type": "skills_suggested", "skills": relevant_skills})

            limit = max_iterations or (self.settings.max_iterations if sess.kind == "chat"
                                       else self.settings.subagent_max_iterations)
            guard = ToolGuardrails(warn_threshold=2, halt_threshold=4)
            tools = self.tool_definitions(sess)
            force_final = False
            iteration = 0

            while True:
                if sess.interrupt_requested:
                    interrupted = True
                    break
                todos_txt = "" if not sess.todos.list() else sess.todos.to_context_string()
                note = ""
                if force_final:
                    note = ("<system_note>Step limit reached or tools stopped making progress. Do NOT call any tools. "
                            "Reply to the user now: summarise what was done, the current state, and what remains."
                            "</system_note>")
                ephemeral = self.prompt_builder.build_ephemeral_context(recalled_txt, relevant_skills, todos_txt, note)
                system_prompt = self._system_prompt(sess, session_instructions)
                payload = build_payload(system_prompt, sess.messages, ephemeral)
                payload_tokens = estimate_tokens(payload, tools)
                budget = int((self.context_window - self.settings.max_output_tokens) * self.settings.compaction_threshold)
                if payload_tokens > budget:
                    if await self._compact(sess, llm, emit):
                        system_prompt = self._system_prompt(sess, session_instructions)
                        payload = build_payload(system_prompt, sess.messages, ephemeral)
                        payload_tokens = estimate_tokens(payload, tools)
                    if payload_tokens > budget:
                        hard_trim(payload)
                        payload_tokens = estimate_tokens(payload, tools)

                resolve_for_llm(payload)
                text_parts: List[str] = []
                reasoning_parts: List[str] = []
                tool_calls: List[Dict[str, Any]] = []
                usage: Dict[str, Any] = {}
                usage_seen = False
                response_model = getattr(llm, "model", "") or self.llm_model_name
                scrubber = ThinkBlockScrubber()
                await emit({"type": "step_start", "step": iteration + 1})
                try:
                    async for ev in llm.chat_stream(payload, tools=tools, temperature=self.settings.temperature,
                                                    max_tokens=self.settings.max_output_tokens):
                        if sess.interrupt_requested:
                            interrupted = True
                            break
                        if "model" in ev:
                            response_model = str(ev["model"] or response_model)
                        if "content" in ev:
                            safe = scrubber.feed(ev["content"])
                            if safe:
                                text_parts.append(safe)
                                await emit({"type": "text", "content": safe})
                        elif "reasoning" in ev:
                            reasoning_parts.append(ev["reasoning"])
                            await emit({"type": "reasoning", "content": ev["reasoning"]})
                        elif "tool_calls" in ev:
                            tool_calls = ev["tool_calls"]
                        elif "usage" in ev:
                            usage = ev["usage"]
                            usage_seen = True
                    tail = scrubber.flush()
                    if tail:
                        text_parts.append(tail)
                        await emit({"type": "text", "content": tail})
                except LLMError as exc:
                    error = str(exc)
                except asyncio.CancelledError:
                    interrupted = True
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception("LLM call failed")
                    error = f"{type(exc).__name__}: {exc}"
                api_calls += 1

                text = "".join(text_parts)
                if interrupted or error or force_final:
                    tool_calls = []  # never execute calls from a cut-off / forced-final response
                assistant: Dict[str, Any] = {"role": "assistant", "content": text}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                if reasoning_parts:
                    assistant["reasoning_content"] = "".join(reasoning_parts)
                if interrupted:
                    assistant["_meta"] = {"interrupted": True}
                if text or tool_calls:
                    self.sessions.append(sess, assistant)
                    await emit({"type": "message", "message_id": assistant.get("_id"), "role": "assistant"})

                measured_prompt = int(usage.get("prompt_tokens") or 0)
                ctx_tokens = measured_prompt or payload_tokens
                completion_tokens = int(usage.get("completion_tokens") or 0)
                cached_tokens = int(usage.get("cached_tokens") or 0)
                sess.last_usage = {
                    "context_tokens": ctx_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                    "context_window": self.context_window,
                }
                await emit({"type": "usage", **sess.last_usage})
                if usage_seen and (ctx_tokens or completion_tokens or cached_tokens):
                    self.spawn(self._persist_usage(
                        session_id=sess.id,
                        job_id=(sess.metadata or {}).get("job_id"),
                        source=sess.kind,
                        model=response_model,
                        prompt_tokens=ctx_tokens,
                        completion_tokens=completion_tokens,
                        cached_tokens=cached_tokens,
                        estimated=not bool(measured_prompt),
                    ), name=f"persist-usage:{sess.id}")

                if error:
                    await emit({"type": "error", "message": error})
                    break
                if interrupted:
                    break
                if not tool_calls:
                    final_text = text
                    break

                await self._execute_tool_calls(sess, tool_calls, emit, guard,
                                               headless_approval=headless_approval)
                tool_calls_total += len(tool_calls)
                iteration += 1
                if sess.interrupt_requested:
                    interrupted = True
                    break
                if guard.is_halted or iteration >= limit:
                    force_final = True
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("turn failed")
            error = str(exc) or type(exc).__name__
            await emit({"type": "error", "message": error})
        finally:
            current_session.reset(token)
            sess.interrupt_requested = False
            await emit({
                "type": "done", "interrupted": interrupted, "error": error, "final_response": final_text,
                "api_calls": api_calls, "tool_calls": tool_calls_total,
                "duration_ms": int((time.time() - started) * 1000), "title": sess.title,
            })

        if not interrupted and not error and final_text:
            self._after_turn(sess, content, final_text, turn_start_idx, tool_calls_total)

    def _system_prompt(self, sess: Session, session_instructions: str = "") -> str:
        names = [d["function"]["name"] for d in self.tool_definitions(sess)]
        return self.prompt_builder.build_system_prompt(
            names,
            core_memory=self.memory.render_core(),
            skills_index=self.skills.render_index(),
            conversation_summary=sess.context_summary,
            session_instructions=session_instructions or sess.metadata.get("instructions", ""),
            model_name=self.llm_model_name,
        )

    # ─── Tool execution ───────────────────────────────────────────

    def _parallel_safe(self, name: str, params: Dict[str, Any]) -> bool:
        if name not in _PARALLEL_SAFE_ACTIONS:
            return False
        actions = _PARALLEL_SAFE_ACTIONS[name]
        return actions is None or params.get("action") in actions

    async def _execute_tool_calls(self, sess: Session, tool_calls: List[Dict[str, Any]], emit: Emit,
                                  guard: ToolGuardrails,
                                  headless_approval: Optional[str] = None) -> None:
        prepared = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            parse_error = None
            try:
                params = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                if not isinstance(params, dict):
                    params, parse_error = {}, "Tool arguments must be a JSON object."
            except (json.JSONDecodeError, TypeError) as exc:
                params, parse_error = {}, f"Invalid JSON in tool arguments: {exc}. Re-issue the call with valid JSON."
            prepared.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:10]}", "name": name, "params": params,
                             "error": parse_error})
            await emit({"type": "tool_call", "id": prepared[-1]["id"], "tool": name, "params": params})

        # Approval phase (sequential, before anything runs).
        for p in prepared:
            if p["error"]:
                continue
            decision = self.approval_policy.classify(p["name"], p["params"])
            if not decision.needs_approval:
                continue
            if sess.interrupt_requested:
                p["error"] = "Skipped: interrupted by user."
                continue
            if sess.headless:
                approval_mode = headless_approval or self.settings.headless_approval
                if approval_mode != "allow":
                    p["denied"] = True
                    p["error"] = (f"Denied: '{p['name']}' needs user approval ({decision.reason}), which isn't available "
                                  "in background runs. Use a non-destructive approach or report back.")
                continue
            approval_id, future = await self.approval_coordinator.request(
                tool_name=p["name"], params=p["params"], reason=decision.reason, severity=decision.severity)
            pending = sess.pending_approvals
            pending.append(approval_id)
            await emit({"type": "approval_requested", "approval_id": approval_id, "tool_call_id": p["id"],
                        "tool": p["name"], "params": p["params"], "reason": decision.reason,
                        "severity": decision.severity})
            try:
                approved = await self.approval_coordinator.wait(approval_id, future)
            except asyncio.CancelledError:
                approved = False
            finally:
                if approval_id in pending:
                    pending.remove(approval_id)
            await emit({"type": "approval", "approval_id": approval_id, "tool_call_id": p["id"], "approved": approved})
            if not approved:
                p["denied"] = True
                p["error"] = "The user denied this tool call. Do not retry it; ask how they'd like to proceed."

        async def run_one(p: Dict[str, Any]) -> Dict[str, Any]:
            if p.get("error"):
                return {"success": False, "error": p["error"], **({"denied": True} if p.get("denied") else {})}
            if sess.interrupt_requested:
                return {"success": False, "error": "Skipped: interrupted by user."}
            return await self._invoke_tool(p["name"], p["params"])

        runnable = [p for p in prepared if not p.get("error")]
        if len(runnable) > 1 and all(self._parallel_safe(p["name"], p["params"]) for p in runnable):
            starts = {p["id"]: time.time() for p in prepared}
            results = await asyncio.gather(*(run_one(p) for p in prepared), return_exceptions=True)
            outcomes = []
            for p, r in zip(prepared, results):
                if isinstance(r, BaseException):
                    r = {"success": False, "error": f"Tool raised: {r}"}
                outcomes.append((p, r, int((time.time() - starts[p["id"]]) * 1000)))
            for p, r, ms in outcomes:
                await self._record_tool_result(sess, p, r, ms, emit, guard)
        else:
            for p in prepared:
                t0 = time.time()
                try:
                    r = await run_one(p)
                except Exception as exc:  # noqa: BLE001
                    r = {"success": False, "error": f"Tool raised: {exc}"}
                await self._record_tool_result(sess, p, r, int((time.time() - t0) * 1000), emit, guard)

    async def _record_tool_result(self, sess: Session, p: Dict[str, Any], result: Any, duration_ms: int,
                                  emit: Emit, guard: ToolGuardrails) -> None:
        if not isinstance(result, dict):
            result = {"success": True, "result": result}
        success = bool(result.get("success", True))
        decision = guard.after_call(p["name"], p["params"], result, failed=not success)
        text = json.dumps(result, ensure_ascii=False, default=str)
        text = _truncate_result(text, self.settings.tool_result_max_chars)
        if decision.should_warn:
            text += decision.message.replace("⚠️ ", "")
        msg = {"role": "tool", "tool_call_id": p["id"], "name": p["name"], "content": text,
               "_meta": {"success": success, "duration_ms": duration_ms}}
        self.sessions.append(sess, msg)
        ui_result = result
        if len(text) > 30000:
            ui_result = {"success": success, "truncated": True, "preview": text[:30000]}
        await emit({"type": "tool_result", "id": p["id"], "tool": p["name"], "success": success,
                    "result": ui_result, "duration_ms": duration_ms})

    async def _invoke_tool(self, name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Host-exec gate + registry dispatch (no approval — callers handle that)."""
        tool = self.tool_registry.get(name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {name}"}
        blocked = should_block_host_exec(
            tool_tags=getattr(tool, "tags", []),
            isolated=self.sandbox_mgr.is_isolated(name) if self.sandbox_mgr else False,
            allow_host_exec=self.allow_host_exec,
        )
        if blocked:
            return {"success": False, "error": blocked + " (Enable it in Settings → Agent → 'Allow host command execution'.)"}
        try:
            return await self.tool_registry.execute(name, **params)
        except TypeError as exc:
            return {"success": False, "error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool %s failed: %s", name, exc)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _dispatch_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Direct tool execution (``/tools/execute``): host gate → approval → run."""
        tool = self.tool_registry.get(tool_name)
        blocked = should_block_host_exec(
            tool_tags=getattr(tool, "tags", []) if tool else [],
            isolated=self.sandbox_mgr.is_isolated(tool_name) if self.sandbox_mgr else False,
            allow_host_exec=self.allow_host_exec,
        )
        if blocked:
            return {"success": False, "error": blocked}
        decision = self.approval_policy.classify(tool_name, params)
        if decision.needs_approval:
            approval_id, future = await self.approval_coordinator.request(
                tool_name=tool_name, params=params, reason=decision.reason, severity=decision.severity)
            if not await self.approval_coordinator.wait(approval_id, future):
                return {"success": False, "denied": True, "reason": decision.reason,
                        "error": "Tool call was denied or timed out awaiting approval."}
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}
        try:
            return await self.tool_registry.execute(tool_name, **params)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}

    # ─── Compaction ───────────────────────────────────────────────

    async def compact(self, sess: Session, emit: Optional[Emit] = None) -> bool:
        async def _noop(_: Dict[str, Any]) -> None:
            return None
        return await self._compact(sess, self.llm, emit or _noop, force=True)

    async def _compact(self, sess: Session, llm: LLMProvider, emit: Emit, force: bool = False) -> bool:
        budget = int(self.context_window * 0.35)
        keep_from = plan_compaction(sess.messages, budget)
        if keep_from is None:
            return False
        old = sess.messages[:keep_from]
        kept = sess.messages[keep_from:]
        ids = [m["_id"] for m in old if m.get("_id")]
        if not ids:
            return False
        await emit({"type": "compaction", "status": "started", "messages": len(old)})
        try:
            summary = await summarise(llm, old, sess.context_summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("compaction failed: %s", exc)
            await emit({"type": "compaction", "status": "failed", "error": str(exc)})
            return False
        if not summary:
            return False
        self.sessions.set_compaction(sess, summary, max(ids), kept)
        await emit({"type": "compaction", "status": "done", "messages": len(old), "summary_chars": len(summary)})
        return True

    # ─── After-turn background work ───────────────────────────────

    def _after_turn(self, sess: Session, content: Any, final_text: str, start_idx: int, tool_calls: int) -> None:
        if sess.kind == "chat" and self.settings.memory_auto_extract:
            tools_used = sorted({m.get("name", "") for m in sess.messages[start_idx:] if m.get("role") == "tool"})
            self.spawn(
                self._extract_memories(sess.id, content, final_text, ", ".join(t for t in tools_used if t)),
                name=f"extract-memories:{sess.id}",
            )
        if (sess.kind in ("chat", "job") and self.settings.skills_auto_create
                and tool_calls >= self.settings.skills_min_tool_calls):
            turn_msgs = sess.messages[start_idx:]
            used_skill_tools = any(m.get("name") == "skill_manage" for m in turn_msgs if m.get("role") == "tool")
            failures = sum(1 for m in turn_msgs if m.get("role") == "tool" and not (m.get("_meta") or {}).get("success", True))
            if not used_skill_tools and failures <= tool_calls // 2:
                self.spawn(self._reflect_skill(content, turn_msgs, final_text), name=f"reflect-skill:{sess.id}")

    async def _extract_memories(self, session_id: str, content: Any, final_text: str, tools_used: str) -> None:
        try:
            await self.memory.extract_from_exchange(content, final_text, session_id=session_id, tool_summary=tools_used)
        except Exception as exc:  # noqa: BLE001
            logger.info("memory extraction failed: %s", exc)

    async def _reflect_skill(self, content: Any, turn_msgs: List[Dict[str, Any]], final_text: str) -> None:
        try:
            await self.skills.reflect(self.llm, content_to_text(content), render_transcript(turn_msgs, 12000), final_text)
        except Exception as exc:  # noqa: BLE001
            logger.info("skill reflection failed: %s", exc)

    async def _generate_title(self, sess: Session, query: str, emit: Emit) -> None:
        try:
            resp = await asyncio.wait_for(
                self.llm.chat(messages=[{"role": "user", "content": TITLE_PROMPT.format(msg=query[:2000])}],
                              temperature=0.3, max_tokens=30),
                timeout=30,
            )
            title = content_to_text(resp.get("content", "")).strip().strip('"\'').splitlines()[0][:80].rstrip(". ")
            if title:
                self.sessions.set_title(sess, title)
                if sess.is_running:
                    await emit({"type": "title", "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.debug("title generation failed: %s", exc)
