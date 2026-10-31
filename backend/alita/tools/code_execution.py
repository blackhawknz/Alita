"""
Code Execution Tool -- run Python, JavaScript, TypeScript, bash or PowerShell
snippets in a host subprocess.

Code runs in a temp directory with a hard timeout (max 120s) and the process
tree is killed on timeout. Output is capped to keep the context window small.
Execution happens in a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .sandbox import IS_WINDOWS, run_process

_MAX_OUTPUT = 8_000
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

#: language alias -> canonical language
_ALIASES: Dict[str, str] = {
    "python": "python",
    "python3": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "bash": "bash",
    "shell": "bash",
    "sh": "bash",
    "powershell": "powershell",
    "ps1": "powershell",
    "pwsh": "powershell",
}

_EXTENSIONS: Dict[str, str] = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "bash": ".sh",
    "powershell": ".ps1",
}


def find_git_bash() -> Optional[str]:
    """Locate Git for Windows' bash.exe (never WSL's System32 bash)."""
    candidates: List[Path] = []
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <Git>/cmd/git.exe -> <Git>
        candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    for env_var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_var)
        if not base:
            continue
        candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
        candidates.append(Path(base) / "Programs" / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _resolve_bash() -> Optional[str]:
    if IS_WINDOWS:
        return find_git_bash()
    return shutil.which("bash") or ("/bin/sh" if os.path.exists("/bin/sh") else None)


def _build_command(lang: str, file_path: str) -> Dict[str, Any]:
    """Return {"cmd": [...]} or {"error": "..."} for a canonical language."""
    if lang == "python":
        return {"cmd": [sys.executable, file_path]}
    if lang == "javascript":
        node = shutil.which("node")
        if not node:
            return {"error": "Runtime 'node' not found -- install Node.js and ensure it is on PATH."}
        return {"cmd": [node, file_path]}
    if lang == "typescript":
        npx = shutil.which("npx")
        if not npx:
            return {"error": "'npx' not found -- install Node.js to run TypeScript."}
        return {"cmd": [npx, "--yes", "ts-node", file_path]}
    if lang == "bash":
        bash = _resolve_bash()
        if not bash:
            if IS_WINDOWS:
                return {
                    "error": (
                        "Git Bash not found. Install Git for Windows, or use "
                        "language='powershell' on this host."
                    )
                }
            return {"error": "bash not found on PATH."}
        return {"cmd": [bash, file_path]}
    if lang == "powershell":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return {"error": "PowerShell not found on PATH."}
        return {
            "cmd": [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", file_path]
        }
    return {"error": f"Unsupported language {lang!r}"}


class CodeExecutionTool:
    """Execute code snippets in a controlled host subprocess."""

    definition = {
        "name": "execute_code",
        "description": (
            "Execute a code snippet in a host subprocess (temp directory) and return "
            "stdout/stderr. WARNING: this runs code directly on your machine, outside "
            "the sandbox, with your full user permissions. Supports Python, JavaScript "
            "(node), TypeScript (npx ts-node), bash (Git Bash on Windows) and "
            f"PowerShell. Output is capped at {_MAX_OUTPUT} characters per stream. "
            f"Max timeout is {_MAX_TIMEOUT} seconds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The source code to execute.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript", "typescript", "bash", "shell", "powershell"],
                    "description": "Language runtime to use (default: python).",
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (default {_DEFAULT_TIMEOUT}, max {_MAX_TIMEOUT}).",
                    "default": _DEFAULT_TIMEOUT,
                },
            },
            "required": ["code"],
        },
    }

    async def execute(
        self,
        code: str = "",
        language: str = "python",
        timeout: int = _DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run ``code``; unexpected ``kwargs`` are ignored."""
        if not isinstance(code, str) or not code.strip():
            return {"success": False, "error": "code is required"}
        lang = _ALIASES.get(str(language or "python").lower().strip())
        if not lang:
            return {
                "success": False,
                "error": f"Unsupported language '{language}'. Supported: {sorted(set(_ALIASES.values()))}",
            }
        try:
            timeout_s = min(max(1, int(timeout)), _MAX_TIMEOUT)
        except (TypeError, ValueError):
            timeout_s = _DEFAULT_TIMEOUT

        tmp_dir = tempfile.mkdtemp(prefix="alita_exec_")
        try:
            file_path = os.path.join(tmp_dir, "snippet" + _EXTENSIONS[lang])
            # Bash chokes on CRLF, so write LF-only. Windows PowerShell 5.1 reads
            # BOM-less files as ANSI, so .ps1 files get a UTF-8 BOM.
            encoding = "utf-8-sig" if lang == "powershell" else "utf-8"
            with open(file_path, "w", encoding=encoding, newline="\n") as fh:
                fh.write(code)

            built = _build_command(lang, file_path)
            if "error" in built:
                return {"success": False, "error": built["error"]}

            env = os.environ.copy()
            env.setdefault("PYTHONIOENCODING", "utf-8")
            result = await asyncio.to_thread(
                run_process,
                built["cmd"],
                cwd=tmp_dir,
                timeout=timeout_s,
                env=env,
                max_output=_MAX_OUTPUT,
            )
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            out: Dict[str, Any] = {
                "success": bool(result.get("success")),
                "return_code": result.get("exit_code", -1),
                "stdout": stdout,
                "stderr": stderr,
                "output": stdout if stdout else stderr,
            }
            if result.get("timed_out") or result.get("error") == "timeout":
                out["timed_out"] = True
                out["error"] = result.get("error") or f"Timed out after {timeout_s}s."
            elif result.get("error"):
                out["error"] = result["error"]
            return out
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
