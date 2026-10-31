"""
Tool Guardrails — Detect and stop non-progressing tool-call loops.

When the agent calls the same tool with the same arguments multiple times
without making progress, the guardrail fires and tells the agent to change
strategy rather than repeat indefinitely.

Adapted from Hermes Agent's agent/tool_guardrails.py.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# How many identical (name, args) calls trigger warn vs halt
_WARN_THRESHOLD = 2
_HALT_THRESHOLD = 3


@dataclass
class ToolGuardrailDecision:
    """Decision returned by the guardrail after each tool call."""

    action: str          # "allow" | "warn" | "halt"
    tool_name: str
    count: int           # how many times this exact call has been seen
    code: str = ""       # short machine-readable code
    message: str = ""    # human-readable message appended to tool result

    @property
    def should_halt(self) -> bool:
        return self.action == "halt"

    @property
    def should_warn(self) -> bool:
        return self.action in ("warn", "halt")


class ToolGuardrails:
    """
    Per-turn guardrail tracker.

    Call ``after_call()`` after every tool execution. On a halt decision,
    the agent loop should stop dispatching further tools for this iteration
    and let the final-response path surface the guardrail message.

    Reset with ``reset_turn()`` at the start of each new user turn.
    """

    def __init__(
        self,
        warn_threshold: int = _WARN_THRESHOLD,
        halt_threshold: int = _HALT_THRESHOLD,
    ):
        self.warn_threshold = warn_threshold
        self.halt_threshold = halt_threshold

        # (tool_name, args_hash) -> count
        self._call_counts: Dict[Tuple[str, str], int] = {}
        # First halt decision this turn (first-wins)
        self._halt_decision: Optional[ToolGuardrailDecision] = None

    # ── Public API ────────────────────────────────────────────────

    def after_call(
        self,
        tool_name: str,
        args: Dict,
        result: object,
        *,
        failed: bool = False,
    ) -> ToolGuardrailDecision:
        """
        Record a tool call and decide whether to warn or halt.

        Should be called once per tool execution *after* the result is
        available. Pass ``failed=True`` when the tool returned an error —
        this makes the threshold stricter (any failure + repeat = halt).

        Returns a ``ToolGuardrailDecision`` the caller can act on.
        """
        key = (tool_name, _hash_args(args))
        count = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = count

        # Determine action
        effective_halt = self.halt_threshold - (1 if failed else 0)
        if count >= effective_halt:
            decision = ToolGuardrailDecision(
                action="halt",
                tool_name=tool_name,
                count=count,
                code="repeated_non_progressing",
                message=(
                    f"\n\n⚠️ **Guardrail:** `{tool_name}` has been called with "
                    f"identical arguments {count} time(s) without making progress. "
                    "Changing strategy instead of retrying the same call."
                ),
            )
            if self._halt_decision is None:
                self._halt_decision = decision
                logger.warning(
                    "Guardrail HALT: %s called %d times (args hash: %s)",
                    tool_name,
                    count,
                    key[1][:8],
                )
            return decision

        if count >= self.warn_threshold:
            decision = ToolGuardrailDecision(
                action="warn",
                tool_name=tool_name,
                count=count,
                code="repeated_call",
                message=(
                    f"\n\n⚠️ **Note:** `{tool_name}` has been called with the same "
                    f"arguments {count} time(s). Consider whether a different approach is needed."
                ),
            )
            logger.debug(
                "Guardrail WARN: %s called %d times", tool_name, count
            )
            return decision

        return ToolGuardrailDecision(
            action="allow", tool_name=tool_name, count=count
        )

    @property
    def halt_decision(self) -> Optional[ToolGuardrailDecision]:
        """The first halt decision fired this turn, or None."""
        return self._halt_decision

    @property
    def is_halted(self) -> bool:
        return self._halt_decision is not None

    def reset_turn(self) -> None:
        """Reset all counters for a new user turn."""
        self._call_counts.clear()
        self._halt_decision = None


def append_guardrail_guidance(tool_result_content: str, decision: ToolGuardrailDecision) -> str:
    """Append the guardrail warning/halt message to a tool result string."""
    return str(tool_result_content) + decision.message


def guardrail_synthetic_result(decision: ToolGuardrailDecision) -> str:
    """Build a synthetic tool result for a call blocked before execution."""
    return json.dumps({
        "success": False,
        "error": (
            f"Tool call blocked by guardrail ({decision.code}). "
            f"`{decision.tool_name}` was called {decision.count} time(s) "
            "with identical arguments. Change your approach."
        ),
    })


# ── Helpers ───────────────────────────────────────────────────────


def _hash_args(args: Dict) -> str:
    """Produce a stable short hash of a tool-call argument dict."""
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=True, default=str)
    except Exception:
        canonical = str(args)
    return hashlib.sha1(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]
