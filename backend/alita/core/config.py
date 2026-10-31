"""
Runtime configuration.

Single source of truth for paths and tunables. Values come from (highest
priority first):

  1. Process environment (including ``backend/.env``, loaded at startup)
  2. Compiled-in defaults below

The settings UI writes to ``backend/.env`` via :func:`update_env_file`, then
calls :func:`reload_env` so changes apply without a restart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BACKEND_DIR / ".env"


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


# ─── Paths ────────────────────────────────────────────────────────────

def alita_home() -> Path:
    """Root data directory (``~/.alita`` unless ALITA_HOME is set)."""
    home = Path(os.path.expanduser(_env("ALITA_HOME", "~/.alita")))
    home.mkdir(parents=True, exist_ok=True)
    return home


def workspace_dir() -> Path:
    """Directory the agent's file/terminal tools operate in."""
    raw = _env("ALITA_WORKSPACE", "")
    if raw:
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = (BACKEND_DIR / p).resolve()
    else:
        p = BACKEND_DIR / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return alita_home() / "state.db"


def memory_dir() -> Path:
    p = alita_home() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_skills_dir() -> Path:
    p = alita_home() / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def builtin_skills_dir() -> Path:
    return BACKEND_DIR / "skills"


def uploads_dir() -> Path:
    p = alita_home() / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def persona_path() -> Path:
    """SOUL.md — the agent's identity / persona (user editable)."""
    p = alita_home() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / "SOUL.md"


def instructions_path() -> Path:
    """Custom user instructions appended after the persona."""
    p = alita_home() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / "INSTRUCTIONS.md"


# ─── Agent tunables ───────────────────────────────────────────────────

class AgentSettings:
    """Snapshot of agent tunables read from the environment."""

    def __init__(self) -> None:
        self.max_iterations = env_int("ALITA_MAX_ITERATIONS", 50)
        self.subagent_max_iterations = env_int("ALITA_SUBAGENT_MAX_ITERATIONS", 25)
        self.subagent_max_depth = env_int("ALITA_SUBAGENT_MAX_DEPTH", 2)
        self.temperature = env_float("ALITA_TEMPERATURE", 0.7)
        self.max_output_tokens = env_int("ALITA_MAX_OUTPUT_TOKENS", 8192)
        # 0 = use the provider/model default
        self.context_window = env_int("ALITA_CONTEXT_WINDOW", 0)
        self.compaction_threshold = env_float("ALITA_COMPACTION_THRESHOLD", 0.75)
        self.memory_auto_extract = env_bool("ALITA_MEMORY_AUTO_EXTRACT", True)
        self.memory_auto_recall = env_bool("ALITA_MEMORY_AUTO_RECALL", True)
        self.memory_recall_k = env_int("ALITA_MEMORY_RECALL_K", 6)
        self.skills_auto_create = env_bool("ALITA_SKILLS_AUTO_CREATE", True)
        self.skills_min_tool_calls = env_int("ALITA_SKILLS_MIN_TOOL_CALLS", 6)
        self.auto_title = env_bool("ALITA_AUTO_TITLE", True)
        # What headless runs (sub-agents, jobs, kanban) do with calls that
        # need approval: "deny" (default) or "allow".
        self.headless_approval = _env("ALITA_HEADLESS_APPROVAL", "deny").lower()
        self.tool_result_max_chars = env_int("ALITA_TOOL_RESULT_MAX_CHARS", 24000)


# ─── .env management ──────────────────────────────────────────────────

def reload_env(override: bool = False) -> None:
    """
    Load backend/.env into the process environment.

    At startup real environment variables win (``override=False``); the
    explicit "reload settings" action passes ``override=True`` so edits made
    to the file by hand take effect.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=override)


def update_env_file(updates: Dict[str, Optional[str]], path: Optional[Path] = None) -> None:
    """
    Set (or clear, when value is "" / None) keys in the .env file.

    Existing lines are replaced in place (including commented-out
    ``# KEY=`` placeholders); new keys are appended. The process
    environment is updated to match.
    """
    path = path or ENV_PATH  # resolved at call time (tests monkeypatch ENV_PATH)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = re.match(r"^\s*#?\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            value = remaining.pop(key)
            out.append(f"{key}={value}" if value else f"# {key}=")
        else:
            out.append(line)
    for key, value in remaining.items():
        if value:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
