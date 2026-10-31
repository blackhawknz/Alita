"""
Approval Gates — require explicit user approval before destructive tool calls.

Two cooperating components live here so they can be tested in isolation:

1. **ApprovalPolicy** — pure helper classifying a (tool_name, params) pair as
   ``safe`` (run immediately) or ``needs_approval`` (with a human-readable
   reason). The default rules cover the destructive operations surfaced by
   Alita's built-in tools: file overwrites/patches, terminal ``rm``/``del``
   /``format``/``shutdown``, ``git push`` (and ``push --force``), and SQL
   ``DROP``/``DELETE``/``UPDATE``/``TRUNCATE``.

   The policy is configurable via env vars:
     - ``ALITA_APPROVAL_MODE`` — ``auto`` (default; gate only destructive ops),
       ``always`` (gate every tool call), ``never`` (gate nothing), or
       ``headless`` (classified like ``auto``; callers auto-deny instead of
       waiting for a human).
     - ``ALITA_APPROVAL_GATE_MCP`` — ``true`` gates every ``mcp__*`` tool call.
     - ``ALITA_APPROVAL_TIMEOUT`` — seconds before a pending approval is
       auto-denied (default 300; set 0 to disable auto-deny).
     - ``ALITA_APPROVAL_ALLOWLIST`` — comma-separated tool names that skip the
       gate even in ``auto`` mode (e.g. ``file_ops,terminal``).

2. **ApprovalCoordinator** — async registry of pending approvals. Each gated
   tool call creates a future keyed by ``approval_id``; the agent loop awaits
   that future while a separate REST endpoint (``POST /approve``) or slash
   command (``/approve <id>``) resolves it.

The coordinator is per-agent-instance (not global) so multiple agents in the
same process don't cross-resolve each other's approvals.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Policy ────────────────────────────────────────────────────────


@dataclass
class ApprovalDecision:
    """Outcome of classifying a tool call."""

    needs_approval: bool
    reason: str = ""
    severity: str = "info"  # "info" | "warning" | "danger"


_I = re.IGNORECASE

#: Commands that are destructive in POSIX shells, cmd.exe and PowerShell.
#: Matching is case-insensitive (PowerShell and cmd are case-insensitive).
_DESTRUCTIVE_CMD_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"(?<![-\w.])rm\b", _I), "rm removes files"),
    (re.compile(r"(?<![-\w.])del\b", _I), "del removes files"),
    (re.compile(r"(?<![-\w.])erase\b", _I), "erase removes files"),
    (re.compile(r"(?<![-\w.])rmdir\b", _I), "rmdir removes directories"),
    (re.compile(r"(?<![-\w.])rd\s+/s\b", _I), "rd /s removes a directory tree"),
    (re.compile(r"\bRemove-Item", _I), "Remove-Item deletes files"),
    (re.compile(r"\bClear-Content\b", _I), "Clear-Content erases file contents"),
    (re.compile(r"(?<![-\w./=])format(?:\.com)?\s+[a-z]:", _I), "format wipes a volume"),
    (re.compile(r"\bFormat-Volume\b", _I), "Format-Volume wipes a volume"),
    (re.compile(r"\b(?:Clear-Disk|Initialize-Disk|Remove-Partition)\b", _I), "disk cmdlet wipes data"),
    (re.compile(r"\bdiskpart\b", _I), "diskpart modifies disks"),
    (re.compile(r"\bmkfs\b", _I), "mkfs formats a filesystem"),
    (re.compile(r"\bshutdown\b", _I), "shutdown powers off the machine"),
    (re.compile(r"\breboot\b", _I), "reboot restarts the machine"),
    (re.compile(r"\b(?:Stop-Computer|Restart-Computer)\b", _I), "powers off / restarts the machine"),
    (re.compile(r"\bSet-ExecutionPolicy\b", _I), "Set-ExecutionPolicy weakens script security"),
    (re.compile(r"\breg(?:\.exe)?\s+delete\b", _I), "reg delete removes registry keys"),
    (re.compile(r"\bsudo\b", _I), "sudo runs with elevated privileges"),
    (re.compile(r"\bchmod\b", _I), "chmod changes permissions"),
    (re.compile(r"\bchown\b", _I), "chown changes ownership"),
    (re.compile(r":\(\)\s*\{.*\}"), "fork bomb pattern"),
    (re.compile(r">\s*/dev/(?:sd|hd|nvme|disk|mmcblk)", _I), "writes directly to a block device"),
    (re.compile(r"\bdd\b.*\bof=", _I), "dd writes a raw device image"),
    (re.compile(r"\bgit\s+reset\b[^;&|\n]*--hard\b", _I), "git reset --hard discards local changes"),
    (re.compile(r"\bgit\s+clean\b[^;&|\n]*\s-\w*f", _I), "git clean -f deletes untracked files"),
    (re.compile(r"\bgit\s+checkout\b[^;&|\n]*\s--\s+\S", _I), "git checkout -- <path> discards local changes"),
    (re.compile(r"\bgit\s+checkout\s+\.(?:\s|$|;)", _I), "git checkout . discards local changes"),
    (re.compile(r"\bgit\s+restore\b(?![^;&|\n]*--staged)", _I), "git restore discards local changes"),
    (re.compile(r"\bgit\s+push\b[^;&|\n]*\s(?:--force(?:-with-lease)?\b|-\w*f\b|\+\S)", _I),
     "git push --force overwrites remote history"),
    (re.compile(r"\bgit\s+branch\b[^;&|\n]*\s-D\b"), "git branch -D deletes a branch"),
    (re.compile(r"\bgit\s+stash\s+(?:drop|clear)\b", _I), "git stash drop/clear discards stashed work"),
    (re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b", _I), "pipes a download into a shell"),
    (re.compile(r"\b(?:iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^|\n]*\|\s*(?:iex|Invoke-Expression)\b", _I),
     "pipes a download into Invoke-Expression"),
]

#: Extra patterns applied to execute_code snippets (language-level deletes).
_DESTRUCTIVE_CODE_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"\bshutil\.rmtree\b"), "shutil.rmtree deletes a directory tree"),
    (re.compile(r"\bos\.(?:remove|unlink|rmdir|removedirs)\b"), "os.remove/unlink deletes files"),
    (re.compile(r"\.unlink\(\s*(?:missing_ok\s*=\s*\w+\s*)?\)"), "Path.unlink deletes a file"),
    (re.compile(r"\bfs(?:\.promises)?\.(?:rm|rmSync|unlink|unlinkSync|rmdir|rmdirSync)\b"), "fs.rm/unlink deletes files"),
]

_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]")

#: SQL keywords that mutate data or schema wherever they appear in a statement.
_SQL_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "attach", "detach", "vacuum", "reindex", "rename",
}
#: Read-only PRAGMAs that may take an argument.
_SQL_READ_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "database_list", "compile_options",
    "function_list", "pragma_list", "collation_list", "module_list",
    "integrity_check", "quick_check",
}


def strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments."""
    return _SQL_LINE_COMMENT_RE.sub(" ", _SQL_BLOCK_COMMENT_RE.sub(" ", sql or ""))


def sql_write_keyword(sql: str) -> Optional[str]:
    """Return the first data/schema-mutating keyword in ``sql``, else ``None``.

    Comments and quoted literals/identifiers are ignored, every ``;``-separated
    statement is inspected (including ``WITH ... DELETE`` forms), statements
    starting with ``REPLACE`` count as writes, and PRAGMA assignments or
    non-read PRAGMA calls are treated as writes. ``load_extension`` is too.
    """
    cleaned = _SQL_STRING_RE.sub(" '' ", strip_sql_comments(sql)).lower()
    for stmt in cleaned.split(";"):
        words = re.findall(r"[a-z_]+", stmt)
        if not words:
            continue
        if words[0] == "replace":
            return "replace"
        if words[0] == "pragma":
            m = re.match(r"\s*pragma\s+(?:\w+\.)?(\w+)\s*(=|\()?", stmt)
            if m and (m.group(2) == "=" or (m.group(2) == "(" and m.group(1) not in _SQL_READ_PRAGMAS)):
                return "pragma"
            continue
        for w in words:
            if w in _SQL_WRITE_KEYWORDS:
                return w
        if "load_extension" in words:
            return "load_extension"
    return None


class ApprovalPolicy:
    """
    Decide whether a tool call requires explicit user approval.

    Mode resolution (env → constructor):
        - ``auto``     — gate only destructive operations (default)
        - ``always``   — gate *every* tool call
        - ``never``    — gate nothing
        - ``headless`` — classified like ``auto``; callers auto-deny gated
          calls instead of waiting for an interactive decision.

    ``workspace_dir`` (optional) lets the policy distinguish creating a new
    file (not gated) from overwriting an existing one (gated). Without it,
    every ``file_ops`` write is gated.
    """

    MODES = {"auto", "always", "never", "headless"}

    # file_ops actions that mutate the filesystem.
    _FILE_WRITE_ACTIONS = {"write", "patch", "create", "overwrite", "delete", "move", "rollback"}

    _DESTRUCTIVE_CMD_PATTERNS = _DESTRUCTIVE_CMD_PATTERNS

    # Git subcommands that are destructive.
    _DESTRUCTIVE_GIT_SUBCOMMANDS = {
        "push": "git push publishes commits to a remote",
        "reset": "git reset rewrites branch history",
        "force": "git push --force overwrites remote history",
        "clean": "git clean deletes untracked files",
        "rebase": "git rebase rewrites commit history",
    }

    # SQL statement keywords that mutate data/schema.
    _DESTRUCTIVE_SQL_KEYWORDS = _SQL_WRITE_KEYWORDS | {"replace", "pragma", "load_extension"}

    def __init__(
        self,
        mode: Optional[str] = None,
        allowlist: Optional[List[str]] = None,
        timeout: Optional[float] = None,
        workspace_dir: Optional[str] = None,
    ):
        env_mode = os.getenv("ALITA_APPROVAL_MODE", "auto").lower()
        self.mode = (mode or env_mode).lower()
        if self.mode not in self.MODES:
            self.mode = "auto"

        if allowlist is None:
            raw = os.getenv("ALITA_APPROVAL_ALLOWLIST", "")
            allowlist = [t.strip() for t in raw.split(",") if t.strip()]
        self.allowlist: set = set(allowlist)

        if timeout is None:
            try:
                timeout = float(os.getenv("ALITA_APPROVAL_TIMEOUT", "300"))
            except (TypeError, ValueError):
                timeout = 300.0
        self.timeout = max(0.0, float(timeout))

        self.workspace_dir = workspace_dir
        #: Gate every MCP tool call (their side effects cannot be introspected).
        self.gate_mcp = os.getenv("ALITA_APPROVAL_GATE_MCP", "false").strip().lower() == "true"

    # – Public API ─────────────────────────────────────────────────

    def classify(self, tool_name: str, params: Dict[str, Any]) -> ApprovalDecision:
        """Return an ApprovalDecision for this tool call."""
        if self.mode == "never":
            return ApprovalDecision(needs_approval=False)
        if tool_name in self.allowlist:
            return ApprovalDecision(needs_approval=False)
        if self.mode == "always":
            return ApprovalDecision(
                needs_approval=True,
                reason=f"Approval mode is 'always' — every tool call requires confirmation.",
                severity="info",
            )
        # auto mode — apply per-tool rules.
        return self._classify_auto(tool_name, params)

    # – Per-tool rules ─────────────────────────────────────────────

    def _classify_auto(self, tool_name: str, params: Dict[str, Any]) -> ApprovalDecision:
        """Default (auto) classification — gate only clearly destructive ops."""
        if tool_name == "file_ops":
            return self._check_file_ops(params)
        if tool_name == "terminal":
            return self._check_terminal(params)
        if tool_name == "git":
            return self._check_git(params)
        if tool_name == "database":
            return self._check_database(params)
        if tool_name == "execute_code":
            return self._check_execute_code(params)
        if tool_name and tool_name.startswith("mcp__") and self.gate_mcp:
            # MCP side effects cannot be introspected; operators opt in to
            # gating them with ALITA_APPROVAL_GATE_MCP=true.
            return ApprovalDecision(
                needs_approval=True,
                reason=f"external MCP tool {tool_name!r} (ALITA_APPROVAL_GATE_MCP=true)",
                severity="warning",
            )
        return ApprovalDecision(needs_approval=False)

    def _target_exists(self, path: str) -> Optional[bool]:
        """Whether ``path`` exists inside the workspace (None if unknown/invalid)."""
        if not self.workspace_dir or not path:
            return None
        try:
            base = Path(self.workspace_dir).resolve()
            target = Path(path)
            if not target.is_absolute():
                target = base / target
            target = target.resolve()
            norm_t, norm_b = os.path.normcase(str(target)), os.path.normcase(str(base))
            if not Path(norm_t).is_relative_to(Path(norm_b)):
                return None
            return target.exists()
        except (OSError, ValueError):
            return None

    def _check_file_ops(self, params: Dict[str, Any]) -> ApprovalDecision:
        action = (params.get("action") or "").lower()
        if action not in self._FILE_WRITE_ACTIONS:
            return ApprovalDecision(needs_approval=False)
        path = params.get("path") or params.get("file_path") or "?"
        if action == "rollback":
            cid = params.get("checkpoint_id") or "?"
            return ApprovalDecision(
                needs_approval=True,
                reason=f"file_ops 'rollback' to checkpoint {cid} overwrites files",
                severity="warning",
            )
        if action in {"write", "create"} and self._target_exists(str(path)) is False:
            # Creating a brand-new file inside the workspace is non-destructive.
            return ApprovalDecision(needs_approval=False)
        return ApprovalDecision(
            needs_approval=True,
            reason=f"file_ops {action!r} on {path}",
            severity="warning",
        )

    @staticmethod
    def _match_patterns(text: str, patterns: List[Tuple["re.Pattern[str]", str]]) -> Optional[str]:
        for pattern, label in patterns:
            if pattern.search(text):
                return label
        return None

    def _check_terminal(self, params: Dict[str, Any]) -> ApprovalDecision:
        command = params.get("command") or ""
        if not command:
            return ApprovalDecision(needs_approval=False)
        label = self._match_patterns(command, self._DESTRUCTIVE_CMD_PATTERNS)
        if label:
            # Trim the command for the reason so we don't dump a huge script.
            preview = command.strip().split("\n")[0][:120]
            return ApprovalDecision(
                needs_approval=True,
                reason=f"terminal command matches destructive pattern ({label}): {preview!r}",
                severity="danger",
            )
        return ApprovalDecision(needs_approval=False)

    def _check_git(self, params: Dict[str, Any]) -> ApprovalDecision:
        subcommand = (params.get("subcommand") or params.get("action") or "").lower()
        args = str(params.get("args") or "")
        if not subcommand:
            return ApprovalDecision(needs_approval=False)
        tokens = args.split()
        if subcommand == "push" and (
            "force" in args.lower()
            or any(t == "-f" or (t.startswith("-") and not t.startswith("--") and "f" in t) or t.startswith("+")
                   for t in tokens)
        ):
            return ApprovalDecision(
                needs_approval=True,
                reason="git push --force overwrites remote history",
                severity="danger",
            )
        if subcommand == "checkout" and any(t in {"--", ".", "-f", "--force"} for t in tokens):
            return ApprovalDecision(
                needs_approval=True,
                reason=f"git checkout {args.strip()!r} discards local changes",
                severity="warning",
            )
        if subcommand == "stash" and tokens and tokens[0].lower() in {"drop", "clear"}:
            return ApprovalDecision(
                needs_approval=True,
                reason=f"git stash {tokens[0]} discards stashed work",
                severity="warning",
            )
        if subcommand == "branch" and any(t in {"-D", "-d", "--delete"} for t in tokens):
            return ApprovalDecision(
                needs_approval=True,
                reason=f"git branch {args.strip()!r} deletes a branch",
                severity="warning",
            )
        label = self._DESTRUCTIVE_GIT_SUBCOMMANDS.get(subcommand)
        if label:
            return ApprovalDecision(
                needs_approval=True,
                reason=label,
                severity="warning" if subcommand in {"push", "reset", "rebase"} else "danger",
            )
        return ApprovalDecision(needs_approval=False)

    def _check_database(self, params: Dict[str, Any]) -> ApprovalDecision:
        action = (params.get("action") or "").lower()
        if action not in {"execute", "query"}:
            return ApprovalDecision(needs_approval=False)
        sql = (params.get("sql") or "").strip()
        if not sql:
            return ApprovalDecision(needs_approval=False)
        keyword = sql_write_keyword(sql)
        if keyword is None:
            return ApprovalDecision(needs_approval=False)
        preview = " ".join(strip_sql_comments(sql).split())[:120]
        return ApprovalDecision(
            needs_approval=True,
            reason=f"database {action!r} with {keyword.upper()}: {preview!r}",
            severity="warning" if keyword in {"insert", "update", "replace"} else "danger",
        )

    def _check_execute_code(self, params: Dict[str, Any]) -> ApprovalDecision:
        code = params.get("code") or ""
        if not code:
            return ApprovalDecision(needs_approval=False)
        label = self._match_patterns(code, self._DESTRUCTIVE_CMD_PATTERNS + _DESTRUCTIVE_CODE_PATTERNS)
        if label:
            preview = code.strip().split("\n")[0][:120]
            return ApprovalDecision(
                needs_approval=True,
                reason=(
                    "This runs code directly on your machine, outside the sandbox, with "
                    "your full user permissions. execute_code contains destructive "
                    f"pattern ({label}): {preview!r}"
                ),
                severity="danger",
            )
        return ApprovalDecision(needs_approval=False)


# ─── Coordinator ─────────────────────────────────────────────────


@dataclass
class PendingApproval:
    """An approval request awaiting a user decision."""

    approval_id: str
    tool_name: str
    params: Dict[str, Any]
    reason: str
    severity: str
    created_at: float
    future: "asyncio.Future[bool]"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "params": self.params,
            "reason": self.reason,
            "severity": self.severity,
            "created_at": self.created_at,
            "age_seconds": round(time.time() - self.created_at, 1),
        }


class ApprovalCoordinator:
    """
    Tracks pending approval requests and resolves them via /approve.

    Per-agent-instance. Methods are safe to call from any coroutine on the
    same event loop.
    """

    def __init__(self, timeout: float = 300.0):
        self.timeout = timeout
        self._pending: Dict[str, PendingApproval] = {}

    # – Agent side ─────────────────────────────────────────────────

    async def request(self, tool_name: str, params: Dict[str, Any],
                      reason: str, severity: str) -> Tuple[str, "asyncio.Future[bool]"]:
        """Create a pending approval and return (approval_id, future_to_await)."""
        approval_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[bool]" = loop.create_future()
        pending = PendingApproval(
            approval_id=approval_id,
            tool_name=tool_name,
            params=params,
            reason=reason,
            severity=severity,
            created_at=time.time(),
            future=future,
        )
        self._pending[approval_id] = pending
        return approval_id, future

    async def wait(self, approval_id: str, future: "asyncio.Future[bool]",
                   timeout: Optional[float] = None) -> bool:
        """
        Await a pending approval. Returns True if approved, False otherwise.

        Cleans up the pending entry on resolve, deny, timeout, or cancel.
        """
        if timeout is None:
            timeout = self.timeout
        try:
            if timeout and timeout > 0:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(approval_id, None)

    # – Responder side (REST endpoint / slash command) ─────────────

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """
        Resolve a pending approval. Returns True if a pending request was found.
        """
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(bool(approved))
        return True

    def list_pending(self) -> List[Dict[str, Any]]:
        """Snapshot of all pending approvals (for the /approvals/pending endpoint)."""
        return [p.to_dict() for p in self._pending.values()]

    def cancel(self, approval_id: str) -> bool:
        """Cancel a pending approval (treat as denied)."""
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            return False
        pending.future.cancel()
        self._pending.pop(approval_id, None)
        return True

    def clear(self) -> None:
        """Deny every pending approval. Used at session reset / shutdown.

        Futures are resolved with ``False`` (not cancelled) so in-flight turns
        see a normal denial instead of a ``CancelledError``.
        """
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(False)
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)