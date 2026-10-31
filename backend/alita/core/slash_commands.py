"""
Slash commands — ``/command`` messages handled without an LLM call.

Handlers receive ``(args, ctx)`` where ``ctx["session"]`` is the current
:class:`~alita.core.session.Session` and return
``{"handled": True, "type": "info"|"error", "message": str, ...}``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class SlashCommand:
    name: str
    description: str
    usage: str
    handler: Callable


class SlashCommandRegistry:
    def __init__(self) -> None:
        self._cmds: Dict[str, SlashCommand] = {}

    def register(self, name: str, description: str, handler: Callable, usage: str = "") -> None:
        self._cmds[name.lower()] = SlashCommand(name.lower(), description, usage or f"/{name}", handler)

    def parse(self, text: str) -> Optional[Tuple[str, str]]:
        stripped = (text or "").strip()
        m = re.match(r"^/([a-zA-Z][\w-]*)\s*(.*)$", stripped, re.DOTALL)
        if not m:
            return None
        return m.group(1).lower(), m.group(2).strip()

    def is_command(self, text: str) -> bool:
        parsed = self.parse(text)
        return parsed is not None and parsed[0] in self._cmds

    async def execute(self, text: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        parsed = self.parse(text)
        if parsed is None:
            return None
        name, args = parsed
        cmd = self._cmds.get(name)
        if cmd is None:
            return None  # not a known command: treat as a normal message (e.g. a path like /usr/bin)
        try:
            return await cmd.handler(args, context)
        except Exception as exc:  # noqa: BLE001
            return {"handled": True, "type": "error", "message": f"/{name} failed: {exc}"}

    def list(self) -> list:
        return [{"name": c.name, "description": c.description, "usage": c.usage} for c in sorted(self._cmds.values(), key=lambda c: c.name)]

    def help_text(self) -> str:
        lines = ["**Slash commands**", ""]
        for c in self.list():
            lines.append(f"- `{c['usage']}` — {c['description']}")
        return "\n".join(lines)


def _info(message: str, **extra: Any) -> Dict[str, Any]:
    return {"handled": True, "type": "info", "message": message, **extra}


def _err(message: str) -> Dict[str, Any]:
    return {"handled": True, "type": "error", "message": message}


def build_slash_registry(agent: Any) -> SlashCommandRegistry:
    reg = SlashCommandRegistry()

    async def _help(args: str, ctx: dict) -> dict:
        return _info(reg.help_text())

    async def _model(args: str, ctx: dict) -> dict:
        if not args:
            return _info(f"Active model: `{agent.llm_model_name}` via `{agent.llm_provider_name}` "
                         f"(context window ~{agent.context_window:,} tokens)")
        from ..llm.provider import PROVIDER_REGISTRY
        provider, model = agent.llm_provider_name, args
        prefix, _, rest = args.partition(":")
        if rest and prefix.lower() in PROVIDER_REGISTRY:
            provider, model = prefix.lower(), rest
        try:
            agent.set_llm(provider.strip(), model.strip())
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not switch model: {exc}")
        return _info(f"Switched to `{agent.llm_model_name}` via `{agent.llm_provider_name}` (until restart; "
                     "use Settings to make it permanent).")

    async def _compact(args: str, ctx: dict) -> dict:
        sess = ctx.get("session")
        if sess is None:
            return _err("No active session.")
        ok = await agent.compact(sess)
        return _info("Conversation compacted — older turns are now summarised." if ok
                     else "Nothing to compact yet (need at least two exchanges).")

    async def _usage(args: str, ctx: dict) -> dict:
        sess = ctx.get("session")
        if sess is None:
            return _err("No active session.")
        u = sess.last_usage or {}
        turns = sess.user_turn_count()
        tools = sum(1 for m in sess.messages if m.get("role") == "tool")
        return _info(
            "**Session usage**\n"
            f"- Turns in context: {turns}\n- Tool calls in context: {tools}\n"
            f"- Context: {u.get('context_tokens', 0):,} / {agent.context_window:,} tokens\n"
            f"- Compacted: {'yes' if sess.context_summary else 'no'}\n- Session: `{sess.id}`"
        )

    async def _memory(args: str, ctx: dict) -> dict:
        stats = agent.longterm.stats()
        blocks = agent.core_memory.all()
        parts = [f"**Long-term memories:** {stats['total']} "
                 f"(semantic search {'on' if stats['semantic_search'] else 'off'})"]
        for name, text in blocks.items():
            parts.append(f"\n**Core · {name}**\n{text or '_(empty)_'}")
        return _info("\n".join(parts))

    async def _remember(args: str, ctx: dict) -> dict:
        if not args:
            return _err("Usage: `/remember <fact>`")
        sess = ctx.get("session")
        res = await agent.longterm.add(args, category="fact", importance=4, source_session=sess.id if sess else None)
        return _info(f"Remembered (#{res.get('id')}, {res.get('status')}).")

    async def _forget(args: str, ctx: dict) -> dict:
        if not args.strip().lstrip("#").isdigit():
            return _err("Usage: `/forget <memory id>` (find ids with `/recall <query>`)")
        ok = await agent.longterm.delete(int(args.strip().lstrip("#")))
        return _info("Forgotten." if ok else "No such memory.")

    async def _recall(args: str, ctx: dict) -> dict:
        if not args:
            return _err("Usage: `/recall <query>`")
        hits = await agent.longterm.search(args, k=10, touch=False)
        if not hits:
            return _info("No matching memories.")
        return _info("\n".join(f"- `#{h['id']}` [{h['category']}] {h['content']}  _(score {h['relevance']:.2f})_"
                               for h in hits))

    async def _skills(args: str, ctx: dict) -> dict:
        skills = agent.skills.list()
        if not skills:
            return _info("No skills yet. Alita creates skills automatically after complex tasks, or ask her to save one.")
        return _info(f"**Skills ({len(skills)})**\n" + "\n".join(
            f"- `{s['name']}` — {s['description'][:120]}" for s in skills[:50]))

    async def _new(args: str, ctx: dict) -> dict:
        sess = agent.sessions.create(kind="chat", title=args or "")
        return _info("Started a new chat.", action="switch_session", session_id=sess.id)

    async def _title(args: str, ctx: dict) -> dict:
        sess = ctx.get("session")
        if sess is None or not args:
            return _err("Usage: `/title <new title>`")
        agent.sessions.set_title(sess, args[:100])
        return _info(f"Renamed to **{args[:100]}**.", action="refresh_sessions")

    async def _approve(args: str, ctx: dict, approved: bool = True) -> dict:
        approval_id = args.strip()
        pending = agent.approval_coordinator.list_pending()
        if not approval_id and len(pending) == 1:
            approval_id = pending[0]["approval_id"]
        if not approval_id:
            return _err("Usage: `/approve <id>` — see `/pending`.")
        if not agent.approval_coordinator.resolve(approval_id, approved=approved):
            return _err(f"Approval `{approval_id}` not found or already resolved.")
        return _info(f"{'Approved' if approved else 'Denied'} `{approval_id}`.")

    async def _deny(args: str, ctx: dict) -> dict:
        return await _approve(args, ctx, approved=False)

    async def _pending(args: str, ctx: dict) -> dict:
        items = agent.approval_coordinator.list_pending()
        if not items:
            return _info("No pending approvals.")
        return _info("\n".join(f"- `{p['approval_id']}` `{p['tool_name']}` ({p['severity']}): {p['reason']}" for p in items))

    async def _persona(args: str, ctx: dict) -> dict:
        from . import config
        from .prompt_builder import load_instructions, load_persona
        if not args:
            custom = config.persona_path().exists()
            instr = load_instructions()
            return _info(
                f"**Persona** ({'custom' if custom else 'default'}) — edit it on the Persona page or with "
                f"`/persona <new text>`; `/persona reset` restores the default.\n\n```\n{load_persona()[:3000]}\n```"
                + (f"\n\n**Custom instructions**\n```\n{instr[:2000]}\n```" if instr else "")
            )
        if args.strip().lower() == "reset":
            config.persona_path().unlink(missing_ok=True)
            return _info("Persona reset to the default. It applies from your next message.")
        config.persona_path().write_text(args.strip() + "\n", encoding="utf-8")
        return _info("Persona updated. It applies from your next message.")

    async def _mcp(args: str, ctx: dict) -> dict:
        servers = agent.mcp_manager.list_servers()
        if not servers:
            return _info("No MCP servers connected. Configure them in `~/.alita/mcp_servers.json`.")
        return _info("\n".join(f"- `{s['name']}` — {len(s.get('tools', []))} tools" for s in servers))

    reg.register("help", "List slash commands", _help)
    reg.register("model", "Show or switch model", _model, "/model [provider:]model")
    reg.register("compact", "Summarise older turns to free context", _compact)
    reg.register("usage", "Context and session stats", _usage)
    reg.register("persona", "Show, replace or reset Alita's persona (system prompt)", _persona,
                 "/persona [new text | reset]")
    reg.register("memory", "Show core memory and memory stats", _memory)
    reg.register("remember", "Save a fact to long-term memory", _remember, "/remember <fact>")
    reg.register("recall", "Search long-term memory", _recall, "/recall <query>")
    reg.register("forget", "Delete a long-term memory", _forget, "/forget <id>")
    reg.register("skills", "List skills", _skills)
    reg.register("new", "Start a new chat", _new, "/new [title]")
    reg.register("title", "Rename this chat", _title, "/title <title>")
    reg.register("approve", "Approve a pending tool call", _approve, "/approve [id]")
    reg.register("deny", "Deny a pending tool call", _deny, "/deny [id]")
    reg.register("pending", "List pending approvals", _pending)
    reg.register("mcp", "List MCP servers", _mcp)
    return reg
