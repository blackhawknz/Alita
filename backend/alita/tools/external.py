"""
External executor tools.

These tools hand work to a CLI agent (OpenCode today, Claude Code later) and
wrap the executor protocol with the guardrails that make spending real money
safe:

* the working directory must resolve inside the agent workspace or an
  explicitly allowed root (``ALITA_EXECUTOR_ALLOWED_ROOTS``);
* a per-call cost cap (``ALITA_EXECUTOR_MAX_COST``) and a per-day aggregate
  cap (``ALITA_EXECUTOR_DAILY_COST``) are enforced while events stream in;
* the CLI's permission bypass is only passed by the executor when
  ``ALITA_EXECUTOR_ALLOW_SKIP_PERMISSIONS=1`` (registered in ``agent.py``).

Returned values stay small and stable: ``success``, ``executor``,
``session_id``, ``cost``, ``report`` and ``response`` (plus ``error`` when a
guardrail trips).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core import config
from ..core.away import AwayState
from ..executors.base import Executor
from ..executors.mandate import Mandate
from ..executors.report import Report
from .registry import Tool

logger = logging.getLogger(__name__)

EXECUTOR_NAMES = ("native", "opencode", "claude_code")
EXTERNAL_EXECUTOR_NAMES = ("opencode", "claude_code")

_COST_EPSILON = 1e-9


class WorkdirError(ValueError):
    """Raised when a requested workdir is outside the allowed roots."""


class ExternalToolError(RuntimeError):
    """Raised when an external executor is unavailable or misconfigured."""


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:  # different drives on Windows
        return False


_MANDATE_TOOL_KEYS = {
    "read_only",
    "allow_bash",
    "allow_webfetch",
    "write",
    "note",
}


def _coerce_mandate(value: Any, workdir: Optional[Path]) -> Mandate:
    """Turn a tool/persistence value into a validated ``Mandate``.

    ``None`` fails closed to :meth:`Mandate.read_only`.  Unknown keys are
    rejected so a typo cannot silently drop a restriction.
    """
    if value is None:
        mandate = Mandate.read_only()
    elif isinstance(value, Mandate):
        mandate = value
    elif isinstance(value, Mapping):
        unknown = set(value) - _MANDATE_TOOL_KEYS
        if unknown:
            raise ValueError(
                "unknown mandate key(s): "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        read_only = value.get("read_only", True)
        if not isinstance(read_only, bool):
            raise ValueError("mandate.read_only must be a boolean")
        allow_bash = value.get("allow_bash", False)
        allow_webfetch = value.get("allow_webfetch", False)
        if not isinstance(allow_bash, bool) or not isinstance(allow_webfetch, bool):
            raise ValueError("mandate.allow_bash/allow_webfetch must be booleans")
        write = value.get("write") or []
        if isinstance(write, str) or not isinstance(write, (list, tuple)):
            raise ValueError("mandate.write must be a list of glob strings")
        note = str(value.get("note") or "")
        if read_only:
            if write or allow_bash or allow_webfetch:
                raise ValueError(
                    "read_only=True cannot be combined with write, allow_bash "
                    "or allow_webfetch; set read_only=false explicitly"
                )
            mandate = Mandate.read_only(note=note)
        else:
            mandate = Mandate.from_dict(
                {
                    "read": ["*"],
                    "write": list(write),
                    "allow_bash": allow_bash,
                    "allow_webfetch": allow_webfetch,
                    "allow_subagents": False,
                    "source": "user",
                    "note": note,
                }
            )
    else:
        raise ValueError("mandate must be an object, not free text")

    mandate.validate()
    if workdir is not None:
        mandate.validate_globs(workdir)
    return mandate


def _mandate_from_record(record: Optional[Dict[str, Any]]) -> Optional[Mandate]:
    if not record:
        return None
    raw = record.get("mandate_json")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return Mandate.from_dict(data)
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid stored mandate for %s", record.get("handle"))
        return None


def parse_allowed_roots(raw: Optional[str]) -> List[Path]:
    """Split an ``os.pathsep``-separated allow-list into resolved paths."""
    roots: List[Path] = []
    for part in str(raw or "").split(os.pathsep):
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        try:
            roots.append(Path(os.path.expanduser(part)).resolve())
        except OSError:
            logger.warning("Ignoring unusable executor allowed root: %s", part)
    return roots


def resolve_workdir(
    workdir: Optional[str | Path],
    *,
    workspace: Path | str,
    allowed_roots: Optional[Iterable[Path | str]] = None,
    allowed_roots_raw: Optional[str] = None,
) -> Path:
    """Resolve ``workdir`` and require it to be inside the workspace or allow-list.

    ``Path.resolve()`` follows symlinks and collapses ``..`` before the
    containment check, so absolute escapes, traversal and symlink escapes are
    all rejected. Relative paths are interpreted relative to the workspace.
    """
    if allowed_roots_raw is None:
        allowed_roots_raw = os.getenv("ALITA_EXECUTOR_ALLOWED_ROOTS", "")

    workspace_path = Path(workspace).resolve()
    roots: List[Path] = [workspace_path]
    if allowed_roots:
        for root in allowed_roots:
            try:
                roots.append(Path(root).expanduser().resolve())
            except OSError:
                logger.warning("Ignoring unusable executor allowed root: %s", root)
    roots.extend(parse_allowed_roots(allowed_roots_raw))

    if workdir is None or str(workdir).strip() == "":
        candidate = workspace_path
    else:
        raw = str(workdir).strip().strip('"').strip("'")
        candidate_path = Path(os.path.expanduser(raw))
        if not candidate_path.is_absolute():
            candidate_path = workspace_path / candidate_path
        candidate = candidate_path.resolve()

    for root in roots:
        if _within(candidate, root):
            return candidate

    allowed = ", ".join(str(root) for root in roots)
    raise WorkdirError(
        f"workdir {str(workdir)!r} resolves to {candidate}, which is outside the "
        f"allowed roots: {allowed}. Pass a path inside the workspace or add it "
        "to ALITA_EXECUTOR_ALLOWED_ROOTS."
    )


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ExternalToolManager:
    """Bridge between the tools and the registered external executors."""

    def __init__(
        self,
        agent: Any = None,
        *,
        settings: Any = None,
        workspace: Path | str | None = None,
        sqlite: Any = None,
        executors: Optional[Dict[str, Executor]] = None,
    ):
        self.agent = agent
        self.settings = settings if settings is not None else getattr(agent, "settings", None)
        if self.settings is None:
            self.settings = config.AgentSettings()
        self.workspace = Path(
            workspace
            if workspace is not None
            else getattr(agent, "workspace_dir", None) or config.workspace_dir()
        )
        self.sqlite = sqlite if sqlite is not None else getattr(agent, "sqlite", None)
        self._executors = executors
        if self._executors is None:
            agent_execs = getattr(agent, "executors", None)
            self._executors = agent_execs if isinstance(agent_execs, dict) else {}

    # ── executor lookup ─────────────────────────────────────────────

    def _registry(self) -> Dict[str, Executor]:
        """Return the stable executor registry (never a throwaway dict)."""
        if self._executors is None:
            agent_execs = getattr(self.agent, "executors", None)
            self._executors = agent_execs if isinstance(agent_execs, dict) else {}
        return self._executors

    def _get_executor(self, name: str) -> Executor:
        executor = self._registry().get(name)
        if executor is not None:
            if (
                name == "opencode"
                and hasattr(executor, "default_model")
                and not getattr(executor, "default_model", None)
            ):
                raise ExternalToolError(
                    "executor 'opencode' has no model configured; set "
                    "ALITA_EXECUTOR_OPENCODE_MODEL (e.g. deepseek/deepseek-flash)"
                )
            return executor
        if name == "opencode":
            model = self._opencode_model()
            if not model:
                raise ExternalToolError(
                    "executor 'opencode' has no model configured; set "
                    "ALITA_EXECUTOR_OPENCODE_MODEL (e.g. deepseek/deepseek-flash)"
                )
            # Lazy registration keeps an app on a machine without OpenCode
            # usable, and picks the CLI up if it appears after startup.
            try:
                from ..executors.opencode import OpenCodeExecutor

                executor = OpenCodeExecutor(
                    default_model=model,
                    variant=self._opencode_variant() or None,
                    extra_args=self._opencode_extra_args(),
                    store=self.sqlite,
                )
            except ExternalToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ExternalToolError(
                    f"executor 'opencode' is unavailable: {exc}"
                ) from exc
            self._registry()[name] = executor
            return executor
        raise ExternalToolError(
            f"executor {name!r} is not registered; available executors: "
            f"{', '.join(sorted(self._registry())) or 'none'}"
        )

    def _opencode_model(self) -> str:
        return str(getattr(self.settings, "executor_opencode_model", "") or "").strip()

    def _opencode_variant(self) -> str:
        return str(
            getattr(self.settings, "executor_opencode_variant", "") or ""
        ).strip()

    def _opencode_extra_args(self) -> List[str]:
        if getattr(self.settings, "executor_allow_skip_permissions", False):
            return ["--dangerously-skip-permissions"]
        return []

    def _allowed_roots_raw(self) -> str:
        raw = getattr(self.settings, "executor_allowed_roots", "") or ""
        return raw or os.getenv("ALITA_EXECUTOR_ALLOWED_ROOTS", "")

    def _away(self) -> AwayState:
        state = getattr(self.agent, "away_state", None) if self.agent is not None else None
        if state is not None:
            return state
        return AwayState(self.sqlite, self.settings)

    def _away_block(self) -> Optional[str]:
        try:
            if self._away().is_away() is True:
                return "away mode is on; external delegation is paused"
        except Exception:  # noqa: BLE001
            logger.debug("away-mode check failed for external delegation", exc_info=True)
        return None

    def _find_handle(
        self, handle: str
    ) -> Tuple[
        Optional[str],
        Optional[Executor],
        Optional[str],
        Optional[Mandate],
        Optional[Path],
    ]:
        """Locate a live handle or rehydrate one persisted before a restart.

        Returns ``(name, executor, error, mandate, workdir)``.  ``error`` is
        only set when a stored record exists but cannot safely be resumed (for
        example a workdir outside the allowed roots).  ``mandate`` is the
        stored/enforced envelope and is never widened here.
        """
        stored_record: Optional[Dict[str, Any]] = None
        if self.sqlite is not None:
            try:
                stored_record = self.sqlite.load_external_handle(handle)
            except Exception:  # noqa: BLE001
                logger.warning("Could not load external handle %s", handle, exc_info=True)

        for name, executor in list(self._registry().items()):
            check = getattr(executor, "has_handle", None)
            if callable(check):
                try:
                    if check(handle):
                        mandate = _mandate_from_record(stored_record)
                        if mandate is None:
                            get_mandate = getattr(executor, "handle_mandate", None)
                            if callable(get_mandate):
                                try:
                                    candidate = get_mandate(handle)
                                    if isinstance(candidate, Mandate):
                                        mandate = candidate
                                except Exception:  # noqa: BLE001
                                    logger.debug(
                                        "handle_mandate failed for %s", name, exc_info=True
                                    )
                        workdir: Optional[Path] = None
                        if stored_record and stored_record.get("workdir"):
                            try:
                                workdir = resolve_workdir(
                                    stored_record.get("workdir"),
                                    workspace=self.workspace,
                                    allowed_roots_raw=self._allowed_roots_raw(),
                                )
                            except WorkdirError:
                                workdir = None
                        if mandate is not None and workdir is not None:
                            try:
                                mandate.validate()
                                mandate.validate_globs(workdir)
                            except ValueError as exc:
                                logger.warning(
                                    "Refusing to resume handle %s: %s", handle, exc
                                )
                                return (
                                    None,
                                    None,
                                    f"stored mandate is not safe to resume: {exc}",
                                    None,
                                    workdir,
                                )
                        return name, executor, None, mandate, workdir
                except Exception:  # noqa: BLE001
                    logger.debug("has_handle failed for %s", name, exc_info=True)

        if stored_record is None:
            return None, None, None, None, None

        name = str(stored_record.get("executor") or "").strip()
        try:
            executor = self._get_executor(name)
        except ExternalToolError as exc:
            return None, None, str(exc), None, None

        try:
            resolved = resolve_workdir(
                stored_record.get("workdir") or None,
                workspace=self.workspace,
                allowed_roots_raw=self._allowed_roots_raw(),
            )
        except WorkdirError as exc:
            logger.warning("Refusing to resume stored handle %s: %s", handle, exc)
            return None, None, str(exc), None, None

        mandate = _mandate_from_record(stored_record) or Mandate.read_only()
        try:
            mandate.validate()
            mandate.validate_globs(resolved)
        except ValueError as exc:
            logger.warning("Refusing to resume handle %s: %s", handle, exc)
            return None, None, f"stored mandate is not safe to resume: {exc}", None, resolved

        rehydrate = getattr(executor, "rehydrate", None)
        if not callable(rehydrate):
            return None, None, None, mandate, resolved

        try:
            rehydrate(
                handle,
                workdir=resolved,
                session_id=handle,
                cost=_float(stored_record.get("cost", 0.0), 0.0),
                mandate=mandate,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not rehydrate external handle %s", handle, exc_info=True)
            return None, None, f"could not resume {handle!r}: {exc}", None, resolved
        return name, executor, None, mandate, resolved

    def _save_handle_mandate(
        self, handle: str, name: str, workdir: Optional[Path], mandate: Mandate
    ) -> None:
        if self.sqlite is None:
            return
        if workdir is None:
            try:
                if self.sqlite.load_external_handle(handle) is None:
                    return
            except Exception:  # noqa: BLE001
                return
        try:
            self.sqlite.save_external_handle(
                handle,
                name,
                str(workdir) if workdir is not None else "",
                mandate=mandate.to_dict(),
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not persist mandate for %s", handle, exc_info=True)


    # ── spend tracking ──────────────────────────────────────────────

    def _spent(self, executor: str) -> float:
        if self.sqlite is None:
            return 0.0
        try:
            return float(self.sqlite.get_executor_spend(executor))
        except Exception:  # noqa: BLE001
            logger.warning("could not read executor spend for %s", executor, exc_info=True)
            return 0.0

    def _record_spend(self, executor: str, cost: float) -> None:
        if self.sqlite is None or not cost:
            return
        try:
            self.sqlite.record_executor_spend(executor, cost)
        except Exception:  # noqa: BLE001
            logger.warning("could not record executor spend for %s", executor, exc_info=True)

    # ── public operations ───────────────────────────────────────────

    async def delegate(
        self,
        task: str,
        *,
        executor: str = "opencode",
        workdir: Optional[str | Path] = None,
        mandate: Any = None,
        max_minutes: Any = 30,
        max_iterations: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Start an external run and return a compact result dict."""
        name = str(executor or "opencode").strip().lower()
        task = str(task or "").strip()
        if not task:
            return _failure(name, "task is required")
        away_error = self._away_block()
        if away_error:
            return _failure(name, away_error)

        try:
            resolved = resolve_workdir(
                workdir,
                workspace=self.workspace,
                allowed_roots_raw=self._allowed_roots_raw(),
            )
        except WorkdirError as exc:
            return _failure(name, str(exc))

        try:
            mandate_obj = _coerce_mandate(mandate, resolved)
        except ValueError as exc:
            return _failure(name, str(exc))

        try:
            ex = self._get_executor(name)
        except ExternalToolError as exc:
            return _failure(name, str(exc))

        daily_cap = _float(getattr(self.settings, "executor_daily_cost", 0.0), 0.0)
        spent = self._spent(name)
        if daily_cap > 0 and spent >= daily_cap - _COST_EPSILON:
            return _failure(
                name,
                f"daily cost cap reached for {name}: ${spent:.4f} of "
                f"ALITA_EXECUTOR_DAILY_COST=${daily_cap:g}",
            )

        try:
            handle = await ex.start(task, mandate=mandate_obj, workdir=resolved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to start external executor %s: %s", name, exc)
            return _failure(name, f"failed to start {name}: {exc}")

        self._save_handle_mandate(handle, name, resolved, mandate_obj)

        iterator = ex.send(handle, "")
        return await self._collect(
            name,
            ex,
            handle,
            iterator,
            base_cost=_handle_cost(ex, handle),
            max_minutes=max_minutes,
            daily_spent=spent,
        )

    async def answer(
        self,
        session_id: str,
        message: str,
        *,
        mandate: Any = None,
        max_minutes: Any = 30,
    ) -> Dict[str, Any]:
        """Resume an existing external session with a new message.

        The stored mandate is the ceiling: a caller may narrow it but a wider
        request is intersected back down instead of escalating the session.
        """
        handle = str(session_id or "").strip()
        message = str(message or "")
        if not handle:
            return _failure("", "session_id is required")
        if not message:
            return _failure("", "message is required")
        away_error = self._away_block()
        if away_error:
            return _failure("", away_error, session_id=handle)

        name, ex, resume_error, stored_mandate, stored_workdir = self._find_handle(handle)
        if ex is None or name is None:
            return _failure(
                "",
                resume_error
                or (
                    f"unknown external session: {handle!r}. Only sessions started "
                    "by this process can be resumed."
                ),
                session_id=handle,
            )

        original = stored_mandate or Mandate.read_only()
        try:
            original.validate()
            if mandate is None:
                effective = original
            else:
                requested = _coerce_mandate(mandate, stored_workdir)
                effective = original.intersect(requested)
            effective.validate()
        except ValueError as exc:
            return _failure(name, str(exc), session_id=handle)

        set_mandate = getattr(ex, "set_mandate", None)
        if callable(set_mandate):
            try:
                set_mandate(handle, effective)
            except Exception as exc:  # noqa: BLE001
                return _failure(
                    name, f"could not apply mandate to {handle!r}: {exc}", session_id=handle
                )
        self._save_handle_mandate(handle, name, stored_workdir, effective)

        daily_cap = _float(getattr(self.settings, "executor_daily_cost", 0.0), 0.0)
        spent = self._spent(name)
        if daily_cap > 0 and spent >= daily_cap - _COST_EPSILON:
            return _failure(
                name,
                f"daily cost cap reached for {name}: ${spent:.4f} of "
                f"ALITA_EXECUTOR_DAILY_COST=${daily_cap:g}",
                session_id=handle,
            )

        iterator = ex.send(handle, message)
        return await self._collect(
            name,
            ex,
            handle,
            iterator,
            base_cost=_handle_cost(ex, handle),
            max_minutes=max_minutes,
            daily_spent=spent,
        )

    async def _collect(
        self,
        name: str,
        ex: Executor,
        handle: str,
        iterator: Any,
        *,
        base_cost: float,
        max_minutes: Any,
        daily_spent: float,
    ) -> Dict[str, Any]:
        text_parts: List[str] = []
        errors: List[str] = []
        call_cost = 0.0
        saw_done = False
        max_cost = _float(getattr(self.settings, "executor_max_cost", 0.0), 0.0)
        daily_cap = _float(getattr(self.settings, "executor_daily_cost", 0.0), 0.0)
        timeout_minutes = _float(max_minutes, 30.0)
        timeout_seconds = timeout_minutes * 60 if timeout_minutes > 0 else None

        async def consume() -> None:
            nonlocal call_cost
            async for event in iterator:
                cost = _float(getattr(event, "cost", 0.0), 0.0)
                if cost:
                    call_cost += cost
                    projected = base_cost + call_cost
                    cap_error = None
                    if max_cost > 0 and projected > max_cost + _COST_EPSILON:
                        cap_error = (
                            f"cost cap exceeded for {name}: ${projected:.4f} > "
                            f"ALITA_EXECUTOR_MAX_COST=${max_cost:g}"
                        )
                    elif daily_cap > 0 and daily_spent + call_cost > daily_cap + _COST_EPSILON:
                        cap_error = (
                            f"daily cost cap exceeded for {name}: "
                            f"${daily_spent + call_cost:.4f} > "
                            f"ALITA_EXECUTOR_DAILY_COST=${daily_cap:g}"
                        )
                    if cap_error:
                        errors.append(cap_error)
                        await _safe_await(ex.interrupt(handle))
                        break
                if event.kind == "text" and event.text:
                    text_parts.append(event.text)
                elif event.kind == "done":
                    saw_done = True
                elif event.kind == "error" and event.text:
                    errors.append(event.text)

        try:
            if timeout_seconds is None:
                await consume()
            else:
                async with asyncio.timeout(timeout_seconds):
                    await consume()
        except asyncio.TimeoutError:
            await _safe_await(ex.interrupt(handle))
            errors.append(f"timed out after {timeout_minutes:g} minutes")
        except asyncio.CancelledError:
            await _safe_await(ex.interrupt(handle))
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("External executor %s failed: %s", name, exc)
            errors.append(str(exc))
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

        if call_cost:
            self._record_spend(name, call_cost)

        response = "".join(text_parts).strip()
        if not errors and not response and not saw_done:
            errors.append(f"{name} produced no output")
        if errors and not response:
            report = Report(kind="failed", summary="; ".join(errors)).to_dict()
        else:
            report = Report.parse(response).to_dict()

        result: Dict[str, Any] = {
            "success": not errors,
            "executor": name,
            "session_id": handle,
            "cost": call_cost,
            "report": report,
            "response": response,
        }
        if errors:
            result["error"] = "; ".join(errors)
        return result


def _handle_cost(executor: Executor, handle: str) -> float:
    fn = getattr(executor, "handle_cost", None)
    if callable(fn):
        try:
            return float(fn(handle) or 0.0)
        except Exception:  # noqa: BLE001
            pass
    return 0.0


async def _safe_await(awaitable: Any) -> None:
    try:
        await awaitable
    except Exception:  # noqa: BLE001
        logger.debug("external executor teardown failed", exc_info=True)


def _failure(
    executor: str,
    error: str,
    *,
    session_id: str = "",
) -> Dict[str, Any]:
    return {
        "success": False,
        "executor": executor,
        "session_id": session_id,
        "cost": 0.0,
        "report": Report(kind="failed", summary=error).to_dict(),
        "response": "",
        "error": error,
    }


# ─── Tools ──────────────────────────────────────────────────────────

MANDATE_TOOL_SCHEMA = {
    "type": "object",
    "description": (
        "Permission envelope for the external worker. Defaults to read-only. "
        "`bash` is a full-write capability: allow_bash=true is only accepted "
        "with write=['*'] because a shell can write anywhere the OS user can."
    ),
    "properties": {
        "read_only": {
            "type": "boolean",
            "default": True,
            "description": "When true (default) the worker may only read the workdir.",
        },
        "allow_bash": {
            "type": "boolean",
            "default": False,
            "description": "Allow shell commands. Requires write=['*'].",
        },
        "allow_webfetch": {
            "type": "boolean",
            "default": False,
            "description": "Allow fetching URLs from the web.",
        },
        "write": {
            "type": "array",
            "items": {"type": "string"},
            "default": [],
            "description": "Write globs relative to the workdir; empty means no writes.",
        },
        "note": {
            "type": "string",
            "description": "Human-readable note. NOT enforced; permission comes from the fields above.",
        },
    },
    "additionalProperties": False,
}


DELEGATE_EXTERNAL_TOOL_SCHEMA = {
    "name": "delegate_external",
    "description": (
        "Hand a coding task to an external CLI agent (OpenCode by default). This "
        "spends real money and runs outside Alita's tools, so it is capped per "
        "call and per day. `workdir` must stay inside the workspace (or an "
        "allowed root). `mandate` is an enforced permission envelope and "
        "defaults to read-only. The returned `session_id` can be resumed with "
        "`answer_external`; a resumed session keeps its original mandate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Complete task description for the external agent."},
            "executor": {
                "type": "string",
                "enum": list(EXTERNAL_EXECUTOR_NAMES),
                "default": "opencode",
                "description": "Which external agent to run.",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory, inside the workspace or an allowed root.",
            },
            "mandate": MANDATE_TOOL_SCHEMA,
            "max_minutes": {
                "type": "number",
                "description": "Wall-clock limit in minutes (default 30).",
                "default": 30,
            },
        },
        "required": ["task"],
    },
}


ANSWER_EXTERNAL_TOOL_SCHEMA = {
    "name": "answer_external",
    "description": (
        "Resume an external agent session started by `delegate_external`, "
        "sending it a follow-up message. The same cost caps apply. An optional "
        "`mandate` may narrow the session's original permissions but can never "
        "widen them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The external session to resume."},
            "message": {"type": "string", "description": "The follow-up message or answer."},
            "mandate": MANDATE_TOOL_SCHEMA,
        },
        "required": ["session_id", "message"],
    },
}


def make_delegate_external_tool(manager: ExternalToolManager) -> Tool:
    async def _handler(**kwargs: Any) -> Dict[str, Any]:
        name = kwargs.get("executor") or "opencode"
        return await manager.delegate(
            kwargs.get("task") or "",
            executor=str(name),
            workdir=kwargs.get("workdir"),
            mandate=kwargs.get("mandate"),
            max_minutes=kwargs.get("max_minutes", 30),
        )

    return Tool(
        name="delegate_external",
        description="Delegate a task to an external CLI agent (OpenCode).",
        definition=DELEGATE_EXTERNAL_TOOL_SCHEMA,
        handler=_handler,
        tags=["planning", "subagent", "delegation", "external"],
    )


def make_answer_external_tool(manager: ExternalToolManager) -> Tool:
    async def _handler(**kwargs: Any) -> Dict[str, Any]:
        return await manager.answer(
            str(kwargs.get("session_id") or ""),
            str(kwargs.get("message") or ""),
            mandate=kwargs.get("mandate"),
        )

    return Tool(
        name="answer_external",
        description="Answer and resume an external agent session.",
        definition=ANSWER_EXTERNAL_TOOL_SCHEMA,
        handler=_handler,
        tags=["planning", "subagent", "delegation", "external"],
    )
