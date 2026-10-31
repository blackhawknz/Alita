"""
System prompt assembly.

The system prompt is rebuilt at the start of every turn (cheap) from:

  1. Persona (``SOUL.md``) + custom instructions (``INSTRUCTIONS.md``)
  2. Operating guidelines, adapted to the tools actually registered
  3. Memory: explanation + the always-visible core memory blocks
  4. Skills index (progressive disclosure)
  5. Environment (OS, shell, workspace, date)
  6. Project context files (AGENTS.md, CLAUDE.md, ...) from the workspace
  7. Conversation summary (after compaction) and per-session instructions

Everything that changes per turn (current time, recalled memories, relevant
skills, active todos) goes into an *ephemeral context block* attached to the
latest user message instead, so the system prompt and older history stay
byte-identical across turns and provider prefix caches stay warm.
"""

from __future__ import annotations

import os
import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import config

DEFAULT_PERSONA = """You are Alita, a capable, warm and direct AI agent running on the user's own machine.
You act as a personal assistant, researcher, image-generation director and software engineer.
You remember the user across conversations and get better at recurring tasks over time."""

GUIDELINES = """# How you work
- Act, don't just advise: when a request needs information or changes, use your tools to do it, then report.
- For multi-step work, plan with the `todo` tool, keep exactly one item in_progress, and update it as you go.
- Verify your work (read files back, run tests/linters, check outputs) before claiming success. Never invent tool results.
- Prefer the simplest approach that fully solves the task. Ask a short clarifying question only when a wrong guess would be costly.
- Make independent tool calls in parallel in a single step. Don't repeat an identical failing call — change approach.
- Keep replies focused: lead with the answer or result, use markdown (headings, lists, tables, fenced code with language tags) when it helps.
- If a tool is denied by the user, don't retry it; explain and offer alternatives."""

MEMORY_GUIDANCE = """# Memory
You have persistent memory across conversations:
- **Core memory** (below) is always visible. Keep the `user` block up to date with who the user is and their standing preferences, and the `notes` block with durable working notes. Edit with `core_memory`.
- **Long-term memory** is unlimited. Relevant memories are auto-recalled into each message inside <recalled_memories>; use `memory` (search/save/update/delete) to look deeper or record important facts explicitly. New facts are also extracted automatically after each exchange, so only save explicitly when something is clearly important.
- **Past conversations** are searchable with `session_search` — use it when the user refers to earlier discussions.
When the user corrects you or tells you something about themselves, update memory. Never store secrets (passwords, API keys) in memory."""

SKILLS_GUIDANCE = """# Skills
Skills are proven procedures for recurring tasks. The available skills are listed below by name and description.
When a task matches a skill, load it with `skill_view` FIRST and follow it. After completing a non-trivial,
repeatable task that no skill covers, consider saving it with `skill_manage`; if a skill was wrong or incomplete, patch it."""

TOOL_NOTES: Dict[str, str] = {
    "terminal": "`terminal` runs shell commands in the workspace ({shell}). Prefer non-interactive flags; long-running servers should be started in the background.",
    "file_ops": "`file_ops` reads/writes/patches files inside the workspace with automatic checkpoints (rollback possible). Read before patching; `patch` needs an exact unique `old_string`.",
    "execute_code": "`execute_code` runs a Python/JS/bash/PowerShell snippet — good for calculations, data processing and quick experiments.",
    "browser": "`browser` searches the web (action=search) and reads pages (extract_text). Cite sources with links when you use web information.",
    "http_request": "`http_request` calls REST APIs and fetches raw data (JSON etc.).",
    "easy_diffusion": "`easy_diffusion` generates images locally. Write rich prompts (subject, style, lighting, composition) plus a negative prompt. Embed results in your reply as `![description](url)` using the returned url.",
    "delegate_task": "`delegate_task` runs an independent sub-agent for a self-contained sub-task (research, analysis). Give it complete context; it can't ask questions. Run several in parallel when useful.",
    "kanban": "`kanban` manages the user's task board (you may add cards to the Idea column and update them).",
    "gmail": "`gmail` gives read-only access to the user's Gmail (list, search, read).",
    "voice": "`voice` is your speaking voice: list voices from any provider (Edge is free, ElevenLabs premium, OpenAI cheap, browser offline), preview them for the user, and set/tune the one you want. The user hears replies through the speaker button or auto-speak.",
    "git": "`git` runs git operations in the workspace repository.",
    "database": "`database` queries SQLite databases in the workspace.",
    "voice": "`voice` is your own speaking voice: the user can have your replies read aloud in it. Choosing and tuning it is up to you (list → preview → set); code blocks and links are skipped when read aloud.",
}

CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "ALITA.md")


def load_persona() -> str:
    p = config.persona_path()
    if p.exists():
        text = p.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_PERSONA


def load_instructions() -> str:
    p = config.instructions_path()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def host_shell_label() -> str:
    return "PowerShell on Windows" if os.name == "nt" else "bash"


class PromptBuilder:
    """Builds the system prompt and the per-turn ephemeral context block."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)

    def build_system_prompt(
        self,
        tool_names: Iterable[str],
        core_memory: str = "",
        skills_index: str = "",
        conversation_summary: str = "",
        session_instructions: str = "",
        model_name: str = "",
    ) -> str:
        names = set(tool_names)
        parts: List[str] = [load_persona()]
        instructions = load_instructions()
        if instructions:
            parts.append(f"# User's custom instructions\n{instructions}")
        parts.append(GUIDELINES)

        notes = [TOOL_NOTES[n].format(shell=host_shell_label()) for n in TOOL_NOTES if n in names]
        if any(n.startswith("mcp__") for n in names):
            notes.append("Tools prefixed `mcp__<server>__` come from external MCP servers; if one keeps failing, fall back to built-in tools.")
        if notes:
            parts.append("# Tools\n" + "\n".join(f"- {n}" for n in notes))

        if "memory" in names or "core_memory" in names:
            parts.append(MEMORY_GUIDANCE + "\n\n## Core memory\n" + (core_memory or "(empty)"))
        if "skill_view" in names:
            parts.append(SKILLS_GUIDANCE + "\n\n" + (skills_index or "<available_skills>\n(none yet)\n</available_skills>"))

        parts.append(self._environment(model_name))
        ctx = self._context_files()
        if ctx:
            parts.append(ctx)
        if session_instructions:
            parts.append(f"# Task instructions\n{session_instructions}")
        if conversation_summary:
            parts.append(
                "# Earlier in this conversation\nOlder messages were compacted into this summary; "
                "rely on it for context:\n\n" + conversation_summary
            )
        return "\n\n".join(p.strip() for p in parts if p and p.strip())

    def _environment(self, model_name: str) -> str:
        return (
            "# Environment\n"
            f"- Date: {datetime.now().strftime('%A, %d %B %Y')}\n"
            f"- OS: {platform.system()} {platform.release()}\n"
            f"- Shell: {host_shell_label()}\n"
            f"- Workspace: {self.workspace_dir}\n"
            + (f"- Model: {model_name}\n" if model_name else "")
        ).strip()

    def _context_files(self) -> str:
        found = []
        seen = set()
        current = self.workspace_dir
        for _ in range(3):
            for name in CONTEXT_FILES:
                cf = current / name
                if cf.is_file() and cf.resolve() not in seen:
                    seen.add(cf.resolve())
                    try:
                        found.append(f"## {cf}\n{cf.read_text(encoding='utf-8', errors='replace')[:6000]}")
                    except OSError:
                        pass
            if current.parent == current:
                break
            current = current.parent
        if not found:
            return ""
        return "# Project context files\n" + "\n\n".join(found)

    @staticmethod
    def build_ephemeral_context(
        recalled: str = "",
        skills: Optional[List[Dict[str, Any]]] = None,
        todos: str = "",
        attachments_note: str = "",
    ) -> str:
        parts = [f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M (%A)')}"]
        if recalled:
            parts.append(recalled)
        if skills:
            lines = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
            parts.append(f"<relevant_skills>\nThese skills look relevant — load with skill_view before starting:\n{lines}\n</relevant_skills>")
        if todos:
            parts.append(f"<current_todos>\n{todos}\n</current_todos>")
        if attachments_note:
            parts.append(attachments_note)
        return "<context>\n" + "\n\n".join(parts) + "\n</context>"


def clean_legacy_persona(text: str) -> tuple:
    """
    Split a v0.1 SOUL.md into (persona, custom_instructions).

    v0.1's UI saved the whole generated prompt (guidance + environment
    sections) back into SOUL.md, duplicating them on every save. Strip those
    generated sections and separate the "Custom Chat Instructions" block.
    """
    delimiter = "\n---\n## Custom Chat Instructions\n"
    custom = ""
    if delimiter in text:
        text, custom = text.split(delimiter, 1)
    generated = {"Memory Usage", "Session Search", "Task Planning", "Tool Usage", "Environment", "Skills",
                 "HTTP Requests", "Image Display in Chat", "MCP Tools", "Approval Gates", "Gmail",
                 "Sub-agent Delegation", "Kanban Board", "Loaded Skills"}
    out_lines: List[str] = []
    skipping = False
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            skipping = m.group(1) in generated
        if not skipping:
            out_lines.append(line)
    persona = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    return persona, custom.strip()
