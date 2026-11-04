"""
Structured worker reports.

Sub-agents return prose today. A :class:`Report` gives the parent a small,
machine-checkable status without making the protocol brittle: any message that
does not contain a report is treated as ``ready`` with the whole message as its
summary.

Parsing order (first match wins):

1. a fenced ``report`` block whose body is JSON
2. the whole message as JSON with a ``kind`` key
3. a first line beginning ``REPORT: <kind>``
4. fallback: ``kind="ready"`` with the whole message as the summary
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REPORT_KINDS = ("progress", "waiting", "ready", "blocked", "failed", "dropped")

_FENCE_RE = re.compile(r"```report[ \t]*\r?\n?(.*?)```", re.IGNORECASE | re.DOTALL)
_REPORT_LINE_RE = re.compile(r"^\s*REPORT:\s*([A-Za-z_]+)[ \t]*(?:\r?\n)?", re.IGNORECASE)


def _try_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class Report:
    """A worker's structured status update."""

    kind: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    question: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "question": self.question,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Report":
        """Build a report from a dict, tolerating missing/odd fields."""
        if not isinstance(raw, dict):
            return cls(kind="ready", summary=str(raw or ""))

        kind = str(raw.get("kind") or "ready").strip().lower()
        if kind not in REPORT_KINDS:
            kind = "ready"

        summary = raw.get("summary")
        if summary is None:
            summary = raw.get("text") or raw.get("message") or ""
        summary = str(summary)

        evidence = raw.get("evidence")
        if evidence is None:
            evidence = []
        elif isinstance(evidence, str):
            evidence = [evidence]
        elif not isinstance(evidence, list):
            evidence = [evidence]
        evidence = [str(item) for item in evidence]

        question = raw.get("question")
        if question is not None:
            question = str(question)
            if not question.strip():
                question = None

        return cls(kind=kind, summary=summary, evidence=evidence, question=question)

    @classmethod
    def parse(cls, raw: str) -> "Report":
        """Parse a worker's final message; never raises."""
        text = raw if isinstance(raw, str) else str(raw or "")

        match = _FENCE_RE.search(text)
        if match:
            parsed = _try_json(match.group(1).strip())
            if isinstance(parsed, dict):
                return cls.from_dict(parsed)

        parsed = _try_json(text.strip())
        if isinstance(parsed, dict) and "kind" in parsed:
            return cls.from_dict(parsed)

        match = _REPORT_LINE_RE.match(text)
        if match:
            kind = match.group(1).lower()
            if kind in REPORT_KINDS:
                return cls(kind=kind, summary=text[match.end():].strip())

        return cls(kind="ready", summary=text.strip())
