"""
Git Tools -- git operations for the Alita agent.

Exposes a single 'git' tool with an action parameter so the LLM has a clean,
predictable interface rather than constructing raw commands. Commands run in
a worker thread, never through a shell, inside the workspace, and without
interactive prompts.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from .file_ops import resolve_within
from .sandbox import run_process

_MAX_OUTPUT = 20_000

#: Options that could write outside the workspace or execute arbitrary programs.
_FORBIDDEN_OPTION_PREFIXES = (
    "--output", "--exec", "--upload-pack", "--receive-pack", "--git-dir",
    "--work-tree", "--ext-diff", "--open-files-in-pager", "--config", "--template",
)


class GitTools:
    """Git operations via subprocess."""

    definition = {
        "name": "git",
        "description": (
            "Perform git operations in the workspace: "
            "status, diff, log, add, commit, branch, checkout, pull, push, stash. "
            "Use 'args' for extra flags or values (e.g. commit message, branch name, file path). "
            "For 'commit', args is the commit message unless it starts with '-'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status", "diff", "log", "add", "commit",
                        "branch", "checkout", "pull", "push", "stash",
                    ],
                    "description": "Git subcommand to run.",
                },
                "args": {
                    "type": "string",
                    "description": (
                        "Extra arguments: commit message for 'commit', "
                        "branch name for 'checkout'/'branch', "
                        "file path(s) for 'add'/'diff'. Quote values containing spaces."
                    ),
                    "default": "",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to the workspace root (default: root).",
                },
            },
            "required": ["action"],
        },
    }

    # Maps action -> base git subcommand args (before user args are appended)
    _COMMANDS: Dict[str, list] = {
        "status": ["git", "status", "--short", "--branch"],
        "diff": ["git", "diff"],
        "log": ["git", "log", "--oneline", "--decorate", "-15"],
        "add": ["git", "add"],
        "commit": ["git", "commit"],
        "branch": ["git", "branch", "--list"],
        "checkout": ["git", "checkout"],
        "pull": ["git", "pull", "--ff-only"],
        "push": ["git", "push"],
        "stash": ["git", "stash"],
    }

    _SLOW_ACTIONS = {"pull", "push"}

    def __init__(self, workspace: str = "."):
        self.workspace = workspace

    @staticmethod
    def _split_args(args: str) -> List[str]:
        # Forward slashes keep Windows paths intact under POSIX shlex rules.
        try:
            return shlex.split(args.replace("\\", "/"))
        except ValueError:
            return args.split()

    def _build(self, action: str, args: str) -> List[str]:
        base = list(self._COMMANDS[action])
        cmd = [base[0], "--no-pager", "-c", "core.quotepath=false", *base[1:]]
        args = (args or "").strip()
        if not args:
            return cmd
        if action == "commit" and not args.startswith("-"):
            message = args
            if len(message) >= 2 and message[0] == message[-1] and message[0] in "\"'":
                message = message[1:-1]
            return cmd + ["-m", message]
        if action == "branch":
            # `--list` only applies to bare listing; with args, pass them through as-is.
            cmd = [c for c in cmd if c != "--list"]
        return cmd + self._split_args(args)

    async def execute(
        self,
        action: str = "",
        args: str = "",
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run a git action; unexpected kwargs are ignored."""
        if action not in self._COMMANDS:
            return {"success": False, "error": f"Unknown git action '{action}'."}
        if not isinstance(args, str):
            args = " ".join(str(a) for a in args) if isinstance(args, list) else str(args or "")

        try:
            work_dir = resolve_within(Path(self.workspace), cwd or "")
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not work_dir.is_dir():
            return {"success": False, "error": f"cwd is not a directory: {cwd}"}

        cmd = self._build(action, args)
        bad = [t for t in cmd[4:] if t.lower().startswith(_FORBIDDEN_OPTION_PREFIXES)]
        if bad:
            return {"success": False, "error": f"Option not allowed: {bad[0]}"}

        env = os.environ.copy()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GCM_INTERACTIVE": "never"})
        timeout = 120 if action in self._SLOW_ACTIONS else 30
        result = await asyncio.to_thread(
            run_process, cmd, cwd=str(work_dir), timeout=timeout, env=env, max_output=10 * _MAX_OUTPUT
        )
        if result.get("error") == "timeout":
            return {"success": False, "error": f"git command timed out after {timeout}s."}
        if result.get("error", "").startswith("Executable not found"):
            return {"success": False, "error": "git not found -- is git installed and on PATH?"}

        output = ((result.get("stdout") or "") + (result.get("stderr") or "")).strip()
        truncated = len(output) > _MAX_OUTPUT
        if truncated:
            output = output[:_MAX_OUTPUT] + f"\n[... truncated {len(output) - _MAX_OUTPUT} characters ...]"
        return {
            "success": bool(result.get("success")),
            "return_code": result.get("exit_code", -1),
            "output": output,
            "truncated": truncated,
        }
