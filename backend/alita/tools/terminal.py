"""
Terminal Execution Tool -- runs shell commands in the workspace.

Uses a Docker sandbox (workspace mounted at /workspace) when available and
falls back to the host shell (PowerShell on Windows, bash elsewhere).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .sandbox import MAX_OUTPUT_CHARS, SandboxManager, host_shell_name


class TerminalTool:
    """Execute terminal commands via the SandboxManager without blocking the loop."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox = sandbox_manager
        self._current_sandbox_id: Optional[str] = None
        # Warm the cached Docker probe in the background so the first
        # is_isolated()/execute() call does not wait on `docker info`.
        prefetch = getattr(self.sandbox, "prefetch_docker_status", None)
        if callable(prefetch):
            prefetch()

    @property
    def definition(self) -> Dict[str, Any]:
        host_shell = host_shell_name()
        return {
            "name": "terminal",
            "description": (
                "Execute a shell command in the project workspace directory. "
                "When Docker is available the command runs with `sh` in a Linux "
                "container with the workspace mounted at /workspace; otherwise it "
                f"runs on the host using {host_shell}"
                + (" (use PowerShell syntax, e.g. Get-ChildItem, Select-String, `;` to chain)"
                   if host_shell == "Windows PowerShell" else "")
                + ". Use it for running programs, installing packages, linting and "
                "testing. Prefer file_ops for reading/editing files. "
                f"Output is capped to the last {MAX_OUTPUT_CHARS} characters per stream."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 120, max 1800).",
                        "default": 120,
                    },
                    "work_dir": {
                        "type": "string",
                        "description": "Working directory relative to the workspace root (default: root).",
                    },
                },
                "required": ["command"],
            },
        }

    async def execute(
        self,
        command: str = "",
        timeout: int = 120,
        work_dir: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a command; ``kwargs`` are accepted and ignored."""
        if not isinstance(command, str) or not command.strip():
            return {"success": False, "error": "command is required", "exit_code": -1}
        try:
            timeout = max(1, min(int(timeout or 120), 1800))
        except (TypeError, ValueError):
            timeout = 120

        start_time = time.time()
        try:
            result = await self._run(command, timeout, work_dir)
            if result.get("sandbox_lost"):
                self.reset_sandbox()
                result = await self._run(command, timeout, work_dir)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "exit_code": -1}

        out: Dict[str, Any] = {
            "success": bool(result.get("success")),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "exit_code": result.get("exit_code", -1),
            "duration_ms": int((time.time() - start_time) * 1000),
            "timed_out": bool(result.get("timed_out", False)),
        }
        if result.get("error"):
            out["error"] = result["error"]
        return out

    async def _run(self, command: str, timeout: int, work_dir: Optional[str]) -> Dict[str, Any]:
        if self._current_sandbox_id is None:
            create = getattr(self.sandbox, "acreate_sandbox", None)
            sandbox = await create() if create else self.sandbox.create_sandbox()
            self._current_sandbox_id = sandbox["sandbox_id"]
        run = getattr(self.sandbox, "aexecute", None)
        if run is not None:
            return await run(self._current_sandbox_id, command, timeout=timeout, work_dir=work_dir)
        return self.sandbox.execute(
            self._current_sandbox_id, command, timeout=timeout, work_dir=work_dir
        )

    def reset_sandbox(self) -> None:
        """Destroy the current sandbox; a fresh one is created on next use."""
        if self._current_sandbox_id:
            self.sandbox.destroy_sandbox(self._current_sandbox_id)
            self._current_sandbox_id = None
