"""
OpenCode executor.

Hands a task to the `opencode` CLI in headless mode and maps its NDJSON
output onto :class:`~alita.executors.base.ExecutorEvent`. The OpenCode
``sessionID`` becomes the executor handle, and a follow-up :meth:`send`
resumes that same session with ``-s <sessionID>``.

The CLI is spawned with an explicit argv list (never ``shell=True``). On
Windows an ``opencode`` install resolves to a ``.ps1`` (or ``.cmd``) shim
which ``create_subprocess_exec`` cannot execute directly, so the resolved
command is wrapped in PowerShell or ``cmd.exe`` as appropriate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from .base import ExecutorEvent

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from .mandate import Mandate

logger = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 2000
_SESSION_GRACE = 10.0
_KILL_GRACE = 5.0


def _powershell_exe() -> str:
    if os.name == "nt":
        return shutil.which("powershell") or "powershell.exe"
    return shutil.which("pwsh") or "pwsh"


def _cmd_exe() -> str:
    return os.environ.get("COMSPEC") or "cmd.exe"


@dataclass
class _Run:
    """Bookkeeping for one OpenCode session (which may span many processes)."""

    handle: str
    session_id: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None
    reader_task: Optional[asyncio.Task] = None
    queue: "asyncio.Queue[Optional[ExecutorEvent]]" = field(default_factory=asyncio.Queue)
    session_ready: asyncio.Event = field(default_factory=asyncio.Event)
    workdir: Optional[Path] = None
    initial_pending: bool = True
    interrupted: bool = False
    timed_out: bool = False
    returncode: Optional[int] = None
    stderr_tail: str = ""
    cost: float = 0.0
    tokens: Dict[str, Any] = field(default_factory=dict)


class OpenCodeExecutor:
    """Run delegated work on the OpenCode CLI."""

    name = "opencode"

    def __init__(
        self,
        *,
        binary: str | None = None,
        default_model: str | None = None,
        variant: str | None = None,
        timeout: float = 1800.0,
        extra_args: List[str] | None = None,
        store: Any = None,
    ):
        self.binary = binary
        self.default_model = default_model
        self.variant = variant
        self.timeout = float(timeout)
        self.extra_args = list(extra_args or [])
        self.store = store
        self._command_prefix = self._resolve_command(binary)
        self._runs: Dict[str, _Run] = {}

    # ── binary resolution ────────────────────────────────────────────

    @staticmethod
    def _resolve_command(binary: str | None) -> List[str]:
        if binary:
            found = binary if os.path.isfile(binary) else shutil.which(binary)
            if not found:
                raise RuntimeError(
                    f"opencode executable not found: {binary!r}; install OpenCode "
                    "or pass binary=<path>"
                )
        else:
            found = shutil.which("opencode")
            if not found:
                raise RuntimeError(
                    "opencode executable not found on PATH; install OpenCode or "
                    "pass binary=<path>"
                )

        path = os.path.abspath(str(found))
        lowered = path.lower()
        if lowered.endswith(".ps1"):
            return [_powershell_exe(), "-NoProfile", "-NonInteractive", "-File", path]
        if lowered.endswith((".cmd", ".bat")):
            return [_cmd_exe(), "/c", path]
        if lowered.endswith((".py", ".pyw")):
            return [sys.executable, path]
        return [path]

    @property
    def command_prefix(self) -> List[str]:
        """The cached argv prefix used to invoke the CLI."""
        return list(self._command_prefix)

    # ── public protocol ──────────────────────────────────────────────

    async def start(
        self,
        task: str,
        *,
        mandate: "Mandate | None" = None,
        workdir: Path | None = None,
    ) -> str:
        """Spawn OpenCode on ``task`` and return its session handle."""
        if mandate is not None:
            logger.debug("OpenCodeExecutor ignores mandate for now")

        provisional = f"oc-{uuid.uuid4().hex[:12]}"
        run = _Run(handle=provisional, workdir=Path(workdir) if workdir is not None else None)
        self._runs[provisional] = run
        try:
            await self._spawn(run, task, session_id=None, workdir=run.workdir, queue=run.queue)
        except Exception:
            self._runs.pop(provisional, None)
            raise

        # The session id normally arrives on the very first event. Wait a
        # short while so ``start`` can return it, but never block for the
        # full task timeout just because a session id is missing.
        grace = min(self.timeout, _SESSION_GRACE) if self.timeout > 0 else 0.0
        try:
            await asyncio.wait_for(run.session_ready.wait(), timeout=grace)
        except asyncio.TimeoutError:
            logger.debug("OpenCode session id not seen within %.1fs", grace)

        if run.session_id:
            self._runs.setdefault(run.session_id, run)
            self._persist(run)
            return run.session_id
        return provisional

    def rehydrate(
        self,
        handle: str,
        *,
        workdir: Path | str | None = None,
        session_id: str | None = None,
        cost: float = 0.0,
    ) -> _Run:
        """Rebuild an in-memory run from a persisted handle (after a restart).

        ``session_id`` defaults to ``handle`` because OpenCode session ids are
        themselves the handle. The caller is responsible for validating
        ``workdir`` before passing it here.
        """
        sid = str(session_id or handle)
        run = self._runs.get(handle) or self._runs.get(sid)
        if run is None:
            run = _Run(
                handle=handle,
                session_id=sid,
                workdir=Path(workdir) if workdir is not None else None,
            )
            self._runs[handle] = run
        else:
            run.session_id = sid
            if workdir is not None:
                run.workdir = Path(workdir)
        run.initial_pending = False
        run.cost = float(cost or 0.0)
        run.session_ready.set()
        self._runs.setdefault(handle, run)
        self._runs.setdefault(sid, run)
        return run

    def _persist(self, run: _Run) -> None:
        """Best-effort save of the resume record for ``run``."""
        if self.store is None:
            return
        handle = run.session_id or run.handle
        if not handle:
            return
        try:
            self.store.save_external_handle(
                handle,
                self.name,
                str(run.workdir) if run.workdir is not None else "",
                cost=run.cost,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not persist opencode handle %s", handle, exc_info=True
            )

    async def send(self, handle: str, message: str) -> AsyncIterator[ExecutorEvent]:
        """Stream the initial run, then resume the same session per call."""
        run = self._runs.get(handle)
        if run is None:
            yield ExecutorEvent(kind="error", text=f"Unknown opencode handle: {handle}")
            return

        if run.initial_pending:
            run.initial_pending = False
            async for event in self._consume(run, run.queue):
                yield event
            return

        if not run.session_id:
            yield ExecutorEvent(
                kind="error",
                text="OpenCode session ID was never captured; cannot resume",
            )
            return

        queue: "asyncio.Queue[Optional[ExecutorEvent]]" = asyncio.Queue()
        try:
            await self._spawn(
                run,
                message,
                session_id=run.session_id,
                workdir=run.workdir,
                queue=queue,
            )
        except Exception as exc:  # noqa: BLE001
            yield ExecutorEvent(kind="error", text=str(exc))
            return

        async for event in self._consume(run, queue):
            yield event

    async def interrupt(self, handle: str) -> None:
        """Terminate this handle's running process (and its child tree)."""
        run = self._runs.get(handle)
        if run is None:
            return
        run.interrupted = True
        await self._stop(run)

    async def close(self, handle: str) -> None:
        """Stop the run and forget every alias that points at it."""
        run = self._runs.get(handle)
        if run is None:
            return
        await self._stop(run)
        for key in [key for key, value in self._runs.items() if value is run]:
            self._runs.pop(key, None)

    def has_handle(self, handle: str) -> bool:
        return handle in self._runs

    def handle_cost(self, handle: str) -> float:
        """Total OpenCode cost accumulated for a handle, in USD."""
        run = self._runs.get(handle)
        return run.cost if run is not None else 0.0

    # ── process plumbing ─────────────────────────────────────────────

    def _argv(
        self,
        *,
        workdir: Path | None,
        session_id: str | None,
        prompt: str,
    ) -> List[str]:
        work = Path(workdir) if workdir is not None else Path.cwd()
        argv = [*self._command_prefix, "run", "--dir", str(work), "--format", "json"]
        if self.default_model:
            argv += ["-m", self.default_model]
        if self.variant:
            argv += ["--variant", self.variant]
        if self.extra_args:
            argv += self.extra_args
        if session_id:
            argv += ["-s", session_id]
        argv.append(prompt)
        return argv

    async def _spawn(
        self,
        run: _Run,
        prompt: str,
        *,
        session_id: str | None,
        workdir: Path | None,
        queue: "asyncio.Queue[Optional[ExecutorEvent]]",
    ) -> None:
        argv = self._argv(workdir=workdir, session_id=session_id, prompt=prompt)
        logger.debug("Starting opencode: %s", argv)

        run.process = None
        run.returncode = None
        run.stderr_tail = ""
        run.interrupted = False
        run.timed_out = False
        run.queue = queue

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir) if workdir is not None else None,
            )
        except Exception as exc:  # noqa: BLE001
            run.returncode = -1
            run.stderr_tail = str(exc)
            run.session_ready.set()
            raise RuntimeError(f"failed to start opencode: {exc}") from exc

        run.process = proc
        run.reader_task = asyncio.create_task(
            self._read_stream(run, proc, queue),
            name=f"opencode-reader-{run.handle}",
        )

    async def _read_stream(
        self,
        run: _Run,
        proc: asyncio.subprocess.Process,
        queue: "asyncio.Queue[Optional[ExecutorEvent]]",
    ) -> None:
        stderr_task = asyncio.create_task(self._drain_stderr(run, proc))
        cancelled = False
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                event = self._map_line(raw, run)
                if event is not None:
                    await queue.put(event)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("OpenCode stdout reader failed: %s", exc)
        finally:
            if proc.returncode is None:
                try:
                    run.returncode = await asyncio.wait_for(proc.wait(), _KILL_GRACE)
                except asyncio.CancelledError:
                    cancelled = True
                    run.returncode = proc.returncode
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    run.returncode = proc.returncode
            else:
                run.returncode = proc.returncode

            try:
                await stderr_task
            except asyncio.CancelledError:
                stderr_task.cancel()
                cancelled = True
            except Exception:  # noqa: BLE001
                logger.debug("OpenCode stderr reader failed", exc_info=True)

            run.session_ready.set()
            queue.put_nowait(None)

        if cancelled:
            raise asyncio.CancelledError

    async def _drain_stderr(
        self,
        run: _Run,
        proc: asyncio.subprocess.Process,
    ) -> None:
        try:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                run.stderr_tail = (run.stderr_tail + text)[-_STDERR_TAIL_CHARS:]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("OpenCode stderr drain failed", exc_info=True)

    def _map_line(self, raw: str, run: _Run) -> Optional[ExecutorEvent]:
        try:
            payload = json.loads(raw)
        except ValueError:
            logger.debug("Ignoring non-JSON opencode line: %s", raw[:200])
            return None
        if not isinstance(payload, dict):
            logger.debug("Ignoring non-object opencode line: %s", raw[:200])
            return None

        session_id = payload.get("sessionID") or payload.get("session_id")
        if session_id:
            session_id = str(session_id)
            run.session_id = session_id
            self._runs.setdefault(session_id, run)
            run.handle = session_id
            run.session_ready.set()
            self._persist(run)

        event_type = str(payload.get("type") or "").lower()
        part = payload.get("part")
        if not isinstance(part, dict):
            part = {}

        if event_type == "error":
            error = payload.get("error")
            if isinstance(error, dict):
                error_name = str(error.get("name") or "").strip()
                error_data = error.get("data")
                if isinstance(error_data, dict):
                    message = str(
                        error_data.get("message")
                        or error_data.get("error")
                        or ""
                    ).strip()
                else:
                    message = str(error_data or "").strip()
                if not message:
                    message = str(error.get("message") or "").strip()
                data = {"error": error}
            else:
                error_name = ""
                message = str(error or payload.get("message") or "").strip()
                data = {"error": str(error or "")}
            if error_name and message:
                text = f"{error_name}: {message}"
            else:
                text = error_name or message or "opencode reported an error"
            return ExecutorEvent(kind="error", text=text, data=data)

        if event_type == "step_start":
            return ExecutorEvent(kind="status", text="working")

        if event_type == "text":
            text = part.get("text", payload.get("text", ""))
            return ExecutorEvent(kind="text", text=str(text or ""))

        if event_type in ("tool", "tool_use"):
            return ExecutorEvent(kind="tool", data=part)

        if event_type == "step_finish":
            tokens = part.get("tokens") or payload.get("tokens") or {}
            if not isinstance(tokens, dict):
                tokens = {}
            cost = part.get("cost", payload.get("cost", 0.0)) or 0.0
            try:
                cost_value = float(cost)
            except (TypeError, ValueError):
                cost_value = 0.0

            run.cost += cost_value
            self._merge_tokens(run.tokens, tokens)
            self._persist(run)
            return ExecutorEvent(
                kind="done",
                data={
                    "reason": part.get("reason"),
                    "tokens": tokens,
                    "cost": cost_value,
                },
                cost=cost_value,
                tokens=dict(tokens),
            )

        logger.debug("Ignoring unknown opencode event type %r", event_type)
        return None

    @staticmethod
    def _merge_tokens(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
        for key, value in incoming.items():
            if isinstance(value, dict):
                nested = target.get(key)
                if not isinstance(nested, dict):
                    nested = {}
                    target[key] = nested
                OpenCodeExecutor._merge_tokens(nested, value)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                previous = target.get(key, 0)
                target[key] = (previous if isinstance(previous, (int, float)) else 0) + value
            else:
                target[key] = value

    # ── event consumption / teardown ─────────────────────────────────

    async def _consume(
        self,
        run: _Run,
        queue: "asyncio.Queue[Optional[ExecutorEvent]]",
    ) -> AsyncIterator[ExecutorEvent]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        timed_out = False
        saw_text = False
        saw_done = False
        saw_error = False

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break
            if item is None:
                break
            if item.kind == "text" and item.text and item.text.strip():
                saw_text = True
            elif item.kind == "done":
                saw_done = True
            elif item.kind == "error":
                saw_error = True
            yield item

        if timed_out:
            run.timed_out = True
            await self._stop(run)
            yield ExecutorEvent(
                kind="error",
                text=f"timed out after {self.timeout:g}s",
            )
            yield ExecutorEvent(
                kind="done",
                data={
                    "reason": "timeout",
                    "tokens": dict(run.tokens),
                    "cost": run.cost,
                },
                tokens=dict(run.tokens),
            )
            return

        if run.returncode not in (0, None) and not run.interrupted:
            tail = run.stderr_tail.strip()
            if tail:
                logger.warning("OpenCode stderr (handle %s): %s", run.handle, tail)
            message = f"opencode exited with code {run.returncode}"
            if tail:
                message += f":\n{tail}"
            yield ExecutorEvent(
                kind="error",
                text=message,
                data={"returncode": run.returncode},
            )
            return

        # OpenCode can exit 0 after an unreported provider failure. Treat a
        # run that emitted no text and no done event as a failure rather than
        # reporting an empty success to the caller.
        if not saw_text and not saw_done and not saw_error and not run.interrupted:
            yield ExecutorEvent(
                kind="error",
                text="opencode produced no output",
                data={"returncode": run.returncode},
            )

    async def _stop(self, run: _Run) -> None:
        await self._terminate_process(run.process)
        task = run.reader_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), _KILL_GRACE)
            except asyncio.TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                task.cancel()
            except Exception:  # noqa: BLE001
                logger.debug("OpenCode reader task ended with an error", exc_info=True)

    async def _terminate_process(
        self,
        proc: Optional[asyncio.subprocess.Process],
    ) -> None:
        if proc is None or proc.returncode is not None:
            return

        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(killer.wait(), _KILL_GRACE)
                except asyncio.TimeoutError:
                    killer.kill()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("taskkill failed for pid %s", proc.pid, exc_info=True)

        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

        try:
            await asyncio.wait_for(proc.wait(), _KILL_GRACE)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), _KILL_GRACE)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
