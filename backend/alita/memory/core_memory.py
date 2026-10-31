"""
Core memory — small, always-in-context memory blocks the agent edits itself.

Modelled on Letta/MemGPT "core memory": a handful of named text blocks that
are rendered into every system prompt. Each block has a character budget;
edits that would overflow it are rejected with guidance to condense, so the
agent (not a truncation heuristic) decides what to keep.

Blocks are stored as plain markdown files in ``~/.alita/memory/`` so users
can read and edit them directly.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class BlockSpec:
    name: str
    label: str
    description: str
    limit: int


BLOCKS: Dict[str, BlockSpec] = {
    "user": BlockSpec(
        "user",
        "About the user",
        "Who the user is: name, role, location/timezone, preferences, communication style, "
        "long-term goals. Facts that should shape every reply.",
        3000,
    ),
    "notes": BlockSpec(
        "notes",
        "Working notes",
        "Durable notes for yourself: environment details, active projects, conventions, "
        "decisions and lessons learned that apply across conversations.",
        4000,
    ),
}


class CoreMemoryError(ValueError):
    """Raised for invalid core-memory edits (message is shown to the agent)."""


class CoreMemory:
    """File-backed named text blocks with per-block character limits."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        if name not in BLOCKS:
            raise CoreMemoryError(f"Unknown memory block '{name}'. Valid blocks: {', '.join(BLOCKS)}")
        return self.dir / f"{name}.md"

    # ─── Read ─────────────────────────────────────────────────────

    def get(self, name: str) -> str:
        p = self._path(name)
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def all(self) -> Dict[str, str]:
        return {name: self.get(name) for name in BLOCKS}

    def describe(self) -> List[Dict[str, object]]:
        out = []
        for name, spec in BLOCKS.items():
            text = self.get(name)
            out.append({
                "name": name, "label": spec.label, "description": spec.description,
                "limit": spec.limit, "chars": len(text), "content": text,
            })
        return out

    def render(self) -> str:
        """Render all non-empty blocks for the system prompt."""
        parts = []
        for name, spec in BLOCKS.items():
            text = self.get(name)
            parts.append(
                f'<memory_block name="{name}" chars="{len(text)}/{spec.limit}">\n'
                f"{text or '(empty)'}\n</memory_block>"
            )
        return "\n".join(parts)

    # ─── Write ────────────────────────────────────────────────────

    def set(self, name: str, text: str, *, enforce_limit: bool = True) -> str:
        spec = BLOCKS.get(name)
        if spec is None:
            self._path(name)  # raises
        text = (text or "").strip()
        if enforce_limit and len(text) > spec.limit:
            raise CoreMemoryError(
                f"Block '{name}' would be {len(text)} chars (limit {spec.limit}). "
                "Condense or remove less important lines, or move details to long-term memory."
            )
        with self._lock:
            self._path(name).write_text(text + ("\n" if text else ""), encoding="utf-8")
        return text

    def append(self, name: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise CoreMemoryError("Nothing to append.")
        current = self.get(name)
        if _normalise(text) in _normalise(current):
            return current  # already present
        line = text if text.startswith(("-", "*", "#")) else f"- {text}"
        return self.set(name, f"{current}\n{line}" if current else line)

    def replace(self, name: str, old: str, new: str) -> str:
        current = self.get(name)
        if not old:
            raise CoreMemoryError("old_text is required for replace.")
        count = current.count(old)
        if count == 0:
            raise CoreMemoryError(f"old_text not found in block '{name}'. View the block and copy the text exactly.")
        if count > 1:
            raise CoreMemoryError(f"old_text matches {count} places in block '{name}'; include more context.")
        return self.set(name, current.replace(old, new or "", 1))

    # ─── Migration ────────────────────────────────────────────────

    def import_legacy(self, legacy_dir: Path) -> None:
        """Seed empty blocks from v0.1 ``USER.md`` / ``MEMORY.md`` bullet lists."""
        mapping = {"user": "USER.md", "notes": "MEMORY.md"}
        for block, filename in mapping.items():
            if self.get(block):
                continue
            src = Path(legacy_dir) / filename
            if not src.exists():
                continue
            lines = []
            for raw in src.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\s*[-*]\s*(?:\[\w+\]\s*)?(.+)$", raw)
                if m and m.group(1).strip():
                    lines.append(f"- {m.group(1).strip()}")
            if lines:
                text = "\n".join(lines)
                self.set(block, text[: BLOCKS[block].limit], enforce_limit=False)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip(" -*")
