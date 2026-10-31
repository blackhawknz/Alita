"""
Sandbox Manager -- Docker container isolation with a host-subprocess fallback.

Commands run against the real workspace directory: inside Docker the workspace
is bind-mounted at ``/workspace``; without Docker (or when ``docker run``
fails) commands run on the host in the workspace directory using PowerShell on
Windows and bash (or sh) elsewhere.

All blocking process work has async wrappers (``aexecute``,
``acreate_sandbox``, ``adocker_available``) that run in a worker thread so the
event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Workspace path inside a Docker sandbox container (matches SandboxConfig.work_dir).
_CONTAINER_WORKDIR = "/workspace"

#: Characters that must never appear in a ``work_dir`` -- defense-in-depth against
#: shell injection even though work_dir is passed as a path, not interpolated.
_UNSAFE_WORKDIR_RE = re.compile(r"[;&|`$<>*?()\n\r\"'\\]")

#: Maximum characters kept from each of stdout / stderr.
MAX_OUTPUT_CHARS = 30_000

#: Seconds allowed for the one-off ``docker info`` probe.
_DOCKER_PROBE_TIMEOUT = 10

IS_WINDOWS = os.name == "nt"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ─── Process helpers (shared with code_execution / git_tools) ──────


def cap_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the last ``limit`` characters of ``text`` with a truncation note."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"[... truncated {dropped} earlier characters ...]\n" + text[-limit:]


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort kill of ``proc`` and all of its descendants."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - fall back to killing the direct child
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _oem_codec() -> Optional[str]:
    if not IS_WINDOWS:
        return None
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:  # noqa: BLE001
        return None


_OEM_CODEC = _oem_codec()


def _decode(data: Optional[bytes]) -> str:
    """Decode process output as UTF-8, falling back per line to the OEM code page."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if not _OEM_CODEC:
        return data.decode("utf-8", errors="replace")
    parts = []
    for line in data.splitlines(keepends=True):
        try:
            parts.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            parts.append(line.decode(_OEM_CODEC, errors="replace"))
    return "".join(parts)


def run_process(
    args: List[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = 120,
    env: Optional[Dict[str, str]] = None,
    max_output: int = MAX_OUTPUT_CHARS,
) -> Dict[str, Any]:
    """Run ``args`` (never via a shell) with a timeout and process-tree kill.

    Output is captured through temporary files rather than pipes: when a
    process is killed its pipes are closed before ``communicate()`` can drain
    them, which used to discard all partial output.

    ``PYTHONUNBUFFERED=1`` is set so Python children do not lose their stdout
    buffer when killed. Other runtimes (Node, Go, Rust) mostly write unbuffered
    to files already; this guarantee is Python-specific.

    Blocking -- call through ``asyncio.to_thread`` from async code.
    """
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "cwd": cwd,
        "env": child_env,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        with tempfile.TemporaryDirectory(
            prefix="alita-process-", ignore_cleanup_errors=True
        ) as tmp:
            out_path = Path(tmp) / "stdout"
            err_path = Path(tmp) / "stderr"
            with open(out_path, "wb") as out_file, open(err_path, "wb") as err_file:
                proc = subprocess.Popen(args, stdout=out_file, stderr=err_file, **popen_kwargs)
                timed_out = False
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    kill_process_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except Exception:  # noqa: BLE001 - the tree may still be dying
                        pass
            out = out_path.read_bytes()
            err = err_path.read_bytes()
    except FileNotFoundError as exc:
        return {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "success": False,
            "timed_out": False,
            "error": f"Executable not found: {args[0]!r}",
        }
    except OSError as exc:
        return {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "success": False,
            "timed_out": False,
            "error": str(exc),
        }

    stdout = cap_output(_decode(out), max_output)
    stderr = cap_output(_decode(err), max_output)
    if timed_out:
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": -1,
            "success": False,
            "timed_out": True,
            "error": f"Command timed out after {timeout:g}s; partial output below.",
        }
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc.returncode,
        "success": proc.returncode == 0,
        "timed_out": False,
    }


def host_shell_name() -> str:
    """Human-readable name of the host shell used by the subprocess fallback."""
    if IS_WINDOWS:
        return "Windows PowerShell"
    return "bash" if shutil.which("bash") else "sh"


#: Longest PowerShell script accepted on the command line (Windows limit is 32767).
MAX_WINDOWS_COMMAND_CHARS = 24_000


def build_host_shell_args(command: str) -> List[str]:
    """Build the argv that runs ``command`` in the host shell (never cmd.exe).

    On Windows the command runs via ``powershell -NoProfile -NonInteractive
    -Command`` with UTF-8 output and the exit code of a failing last native
    command propagated. (``-EncodedCommand`` is avoided because it makes
    PowerShell emit CLIXML on stderr.)
    """
    if IS_WINDOWS:
        script = (
            "$ProgressPreference = 'SilentlyContinue'\n"
            "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
            f"{command}\n"
            "$__alitaOk = $?\n"
            "if (-not $__alitaOk) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }\n"
        )
        return [
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", script,
        ]
    shell = shutil.which("bash") or "/bin/sh"
    return [shell, "-c", command]


# ─── Config ────────────────────────────────────────────────────────


@dataclass
class SandboxConfig:
    """Configuration for sandboxed execution environments."""

    image: str = "python:3.11-slim"
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    pid_limit: int = 100
    read_only_root: bool = True
    network_disabled: bool = False
    timeout_seconds: int = 300
    work_dir: str = "/workspace"
    volumes: Dict[str, str] = field(default_factory=dict)


# ─── Manager ───────────────────────────────────────────────────────


class SandboxManager:
    """
    Manages execution environments rooted at the workspace directory.

    Uses Docker when available (workspace bind-mounted at ``/workspace``),
    otherwise falls back to host subprocesses running in the workspace.
    """

    def __init__(self, workspace_dir: Optional[str] = None):
        self.workspace_dir = Path(workspace_dir or os.getcwd()).resolve()
        #: Explicit override / cached Docker availability (tests set this directly).
        self._docker_available: Optional[bool] = None
        self._docker_probe_result: Optional[bool] = None
        self._docker_probe_thread: Optional[threading.Thread] = None
        self._docker_probe_lock = threading.Lock()
        #: Set when ``docker run`` failed and we fell back to the host.
        self._docker_run_failed = False
        self._active_containers: Dict[str, str] = {}  # sandbox_id -> container_id
        self._sandboxes: Dict[str, Dict[str, Any]] = {}

    # ─── Docker detection ──────────────────────────────────────────

    def _probe_docker(self) -> None:
        result = False
        try:
            probe = run_process(["docker", "info"], timeout=_DOCKER_PROBE_TIMEOUT, max_output=2_000)
            result = probe.get("exit_code") == 0
        except Exception:  # noqa: BLE001
            result = False
        self._docker_probe_result = result

    def prefetch_docker_status(self) -> None:
        """Start the ``docker info`` probe in a background thread (idempotent)."""
        if self._docker_available is not None:
            return
        with self._docker_probe_lock:
            if self._docker_probe_thread is None:
                self._docker_probe_thread = threading.Thread(
                    target=self._probe_docker, name="alita-docker-probe", daemon=True
                )
                self._docker_probe_thread.start()

    @property
    def docker_available(self) -> bool:
        """Whether Docker is usable. Probed once in a thread and cached."""
        if self._docker_available is not None:
            return self._docker_available
        if self._docker_probe_result is not None:
            return self._docker_probe_result
        self.prefetch_docker_status()
        thread = self._docker_probe_thread
        if thread is not None:
            thread.join(timeout=_DOCKER_PROBE_TIMEOUT + 5)
        return bool(self._docker_probe_result)

    async def adocker_available(self) -> bool:
        """Async variant of :attr:`docker_available` that never blocks the loop."""
        if self._docker_available is not None:
            return self._docker_available
        if self._docker_probe_result is not None:
            return self._docker_probe_result
        return await asyncio.to_thread(lambda: self.docker_available)

    def is_isolated(self, tool_name: str) -> bool:
        """True if executing ``tool_name`` is contained by an isolated sandbox.

        ``terminal`` is isolated only when Docker is available and ``docker run``
        has not failed (a failed run falls back to a host subprocess).
        ``execute_code`` always runs on the host and is never isolated.
        """
        if tool_name == "execute_code":
            return False
        if tool_name == "terminal":
            return bool(self.docker_available) and not self._docker_run_failed
        return True

    # ─── Sandbox lifecycle ─────────────────────────────────────────

    def create_sandbox(self, config: Optional[SandboxConfig] = None) -> Dict[str, Any]:
        """Create a new execution environment (blocking; see ``acreate_sandbox``)."""
        if config is None:
            config = SandboxConfig()
        sandbox_id = uuid.uuid4().hex[:12]
        workdir = self.workspace_dir

        if self.docker_available and not self._docker_run_failed:
            sandbox = self._create_docker_sandbox(sandbox_id, config, workdir)
        else:
            sandbox = self._create_subprocess_sandbox(sandbox_id, config, workdir)
        self._sandboxes[sandbox_id] = sandbox
        return sandbox

    async def acreate_sandbox(self, config: Optional[SandboxConfig] = None) -> Dict[str, Any]:
        """Async wrapper around :meth:`create_sandbox`."""
        return await asyncio.to_thread(self.create_sandbox, config)

    def _create_docker_sandbox(
        self, sandbox_id: str, config: SandboxConfig, workdir: Path
    ) -> Dict[str, Any]:
        """Create a Docker sandbox with the workspace bind-mounted."""
        docker_args = [
            "docker", "run", "-d",
            "--name", f"alita-sandbox-{sandbox_id}",
            "--memory", config.memory_limit,
            "--cpus", config.cpu_limit,
            "--pids-limit", str(config.pid_limit),
            "-v", f"{workdir}:{config.work_dir}",
            "-w", config.work_dir,
        ]
        if config.read_only_root:
            docker_args.append("--read-only")
            docker_args.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"])
        if config.network_disabled:
            docker_args.append("--network=none")
        docker_args.extend([config.image, "tail", "-f", "/dev/null"])

        result = run_process(docker_args, timeout=120, max_output=4_000)
        container_id = (result.get("stdout") or "").strip()
        if not result.get("success") or not container_id:
            logger.warning(
                "docker run failed, falling back to host subprocess: %s",
                (result.get("stderr") or result.get("error") or "").strip()[:500],
            )
            self._docker_run_failed = True
            return self._create_subprocess_sandbox(sandbox_id, config, workdir)

        self._active_containers[sandbox_id] = container_id
        return {
            "sandbox_id": sandbox_id,
            "type": "docker",
            "container_id": container_id,
            "work_dir": str(workdir),
            "status": "running",
        }

    def _create_subprocess_sandbox(
        self, sandbox_id: str, config: SandboxConfig, workdir: Path
    ) -> Dict[str, Any]:
        """Host fallback: commands run directly in the workspace directory."""
        return {
            "sandbox_id": sandbox_id,
            "type": "subprocess",
            "container_id": None,
            "work_dir": str(workdir),
            "status": "running",
        }

    def _get_sandbox(self, sandbox_id: str) -> Optional[Dict[str, Any]]:
        return self._sandboxes.get(sandbox_id)

    # ─── Execution ─────────────────────────────────────────────────

    def execute(
        self,
        sandbox_id: str,
        command: str,
        timeout: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
        work_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a command inside the sandbox (blocking; see ``aexecute``).

        ``work_dir`` is a path relative to the workspace. It is validated
        against traversal and applied as the real working directory (``cwd=``
        on the host, ``-w`` for ``docker exec``) -- never interpolated into
        the shell command.
        """
        sandbox = self._get_sandbox(sandbox_id)
        if sandbox is None:
            raise ValueError(f"Sandbox {sandbox_id} not found")

        timeout = timeout or 300
        rel_work_dir = self._validate_work_dir(work_dir) if work_dir else None

        if sandbox.get("type") == "docker" and sandbox.get("container_id"):
            return self._execute_docker(
                sandbox["container_id"], command, timeout, env, rel_work_dir
            )
        return self._execute_subprocess(
            Path(sandbox["work_dir"]), command, timeout, env, rel_work_dir
        )

    async def aexecute(
        self,
        sandbox_id: str,
        command: str,
        timeout: Optional[int] = None,
        env: Optional[Dict[str, str]] = None,
        work_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Async wrapper around :meth:`execute` (runs in a worker thread)."""
        return await asyncio.to_thread(self.execute, sandbox_id, command, timeout, env, work_dir)

    @staticmethod
    def _validate_work_dir(work_dir: Optional[str]) -> str:
        """Validate a relative ``work_dir``, rejecting traversal and unsafe chars.

        Returns a cleaned POSIX-style relative path. Raises ``ValueError`` on
        absolute paths, ``..`` traversal, or shell metacharacters.
        """
        if work_dir is None:
            return ""
        if work_dir[:1] in ("/", "\\") or re.match(r"^[A-Za-z]:", work_dir):
            raise ValueError(f"work_dir must be relative: {work_dir!r}")
        cleaned = work_dir.replace("\\", "/").strip().strip("/")
        if not cleaned:
            return ""
        parts = cleaned.split("/")
        if any(p == ".." for p in parts):
            raise ValueError(f"work_dir traversal denied: {work_dir!r}")
        if _UNSAFE_WORKDIR_RE.search(cleaned):
            raise ValueError(f"work_dir contains unsafe characters: {work_dir!r}")
        return cleaned

    def _execute_docker(
        self,
        container_id: str,
        command: str,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
        work_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        docker_args = ["docker", "exec"]
        if work_dir:
            docker_args.extend(["-w", f"{_CONTAINER_WORKDIR}/{work_dir}"])
        if env:
            for k, v in env.items():
                docker_args.extend(["-e", f"{k}={v}"])
        # `timeout` inside the container kills the command itself; killing the
        # host-side `docker exec` client alone would leave it running.
        docker_args.extend([
            container_id, "timeout", "-s", "KILL", str(int(timeout)), "sh", "-c", command,
        ])
        result = run_process(docker_args, timeout=timeout + 10)
        stderr = result.get("stderr") or ""
        if result.get("exit_code") == 137 and not result.get("error"):
            result["error"] = f"Command timed out after {timeout}s; partial output below."
            result["timed_out"] = True
            result["stderr"] = (stderr + f"\nCommand timed out after {timeout}s and was killed.").strip()
        if "No such container" in stderr or "is not running" in stderr:
            result["sandbox_lost"] = True
        return result

    def _execute_subprocess(
        self,
        workdir: Path,
        command: str,
        timeout: int,
        env: Optional[Dict[str, str]] = None,
        work_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        cwd = workdir / work_dir if work_dir else workdir
        if not cwd.is_dir():
            return {
                "stdout": "",
                "stderr": f"work_dir does not exist: {work_dir or str(workdir)}",
                "exit_code": -1,
                "success": False,
                "error": "invalid work_dir",
            }
        if IS_WINDOWS and len(command) > MAX_WINDOWS_COMMAND_CHARS:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": -1,
                "success": False,
                "error": (
                    f"Command too long ({len(command)} chars). Write it to a .ps1 file "
                    "with file_ops and run that file instead."
                ),
            }
        full_env = os.environ.copy()
        full_env.setdefault("PYTHONIOENCODING", "utf-8")
        if env:
            full_env.update(env)
        return run_process(
            build_host_shell_args(command), cwd=str(cwd), timeout=timeout, env=full_env
        )

    # ─── Teardown ──────────────────────────────────────────────────

    def destroy_sandbox(self, sandbox_id: str) -> None:
        """Tear down a sandbox environment."""
        container_id = self._active_containers.pop(sandbox_id, None)
        if container_id:
            try:
                run_process(["docker", "rm", "-f", container_id], timeout=30, max_output=2_000)
            except Exception as exc:  # noqa: BLE001
                logger.warning("docker rm failed for %s: %s", container_id, exc)
        self._sandboxes.pop(sandbox_id, None)
        legacy = self.workspace_dir / ".sandboxes" / sandbox_id
        if legacy.exists():
            shutil.rmtree(legacy, ignore_errors=True)

    def destroy_all(self) -> None:
        """Clean up all sandboxes, containers and legacy sandbox directories."""
        for sandbox_id in set(self._active_containers) | set(self._sandboxes):
            self.destroy_sandbox(sandbox_id)
        legacy_root = self.workspace_dir / ".sandboxes"
        if legacy_root.is_dir():
            shutil.rmtree(legacy_root, ignore_errors=True)
