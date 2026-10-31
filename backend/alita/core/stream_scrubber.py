"""
Stream Think-Block Scrubber — Stateful suppression of <think>...</think> tags.

The previous regex-per-delta approach breaks when a tag straddles a chunk
boundary (e.g., delta 1 = "<thi", delta 2 = "nk>reasoning</think>").
This stateful scrubber buffers suspect partial-tag prefixes and only passes
through text that is definitely not inside a reasoning block.

Adapted from Hermes Agent's agent/stream_think_scrubber.py.
"""

import re
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# All tag variants we suppress (case-insensitive handled via lower() pre-check)
_OPEN_TAGS: List[str] = ["<think>", "<thinking>", "<reasoning>", "<reflection>"]
_CLOSE_TAGS: List[str] = ["</think>", "</thinking>", "</reasoning>", "</reflection>"]

# Longest open-tag string (for partial-buffer threshold)
_MAX_OPEN_TAG_LEN = max(len(t) for t in _OPEN_TAGS)

# Precompiled: detect a *complete* open or close tag anywhere in a string
_OPEN_RE = re.compile(r"<(think|thinking|reasoning|reflection)>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</(think|thinking|reasoning|reflection)>", re.IGNORECASE)


class ThinkBlockScrubber:
    """
    Stateful per-stream scrubber.  Create one instance per LLM response.

    Feed each text delta to ``feed(delta)``; it returns the text that is safe
    to pass to the UI (empty string while inside a think block or buffering a
    partial tag).  Call ``flush()`` at stream end to release any buffered
    benign partial-tag tail.
    """

    def __init__(self):
        self._inside_think = False   # currently inside a <think>…</think> block
        self._partial_buf = ""       # buffer for a partial open-tag prefix

    # ── Public API ────────────────────────────────────────────────

    def feed(self, delta: str) -> str:
        """
        Process one text delta.  Returns the portion safe to display.
        May return an empty string when buffering or suppressing content.
        """
        if not delta:
            return ""

        text = self._partial_buf + delta
        self._partial_buf = ""
        return self._process(text)

    def flush(self) -> str:
        """
        End of stream.  Returns any buffered partial-tag tail that turned out
        to be benign (i.e., the stream ended before a complete open tag was seen).
        Resets scrubber state.
        """
        tail = self._partial_buf
        self._partial_buf = ""
        self._inside_think = False
        # If we're flushing mid-think block, the reasoning was never closed —
        # don't pass it through.
        if self._inside_think:
            return ""
        return tail

    # ── Internal ──────────────────────────────────────────────────

    def _process(self, text: str) -> str:
        """Process ``text`` (which already has the partial buffer prepended)."""
        out_parts: List[str] = []
        pos = 0

        while pos < len(text):
            if self._inside_think:
                # Look for the closing tag
                m = _CLOSE_RE.search(text, pos)
                if m:
                    # Skip everything up to and including the close tag
                    pos = m.end()
                    self._inside_think = False
                else:
                    # Rest of delta is inside the think block — consume it all
                    pos = len(text)
            else:
                # Look for the next opening tag
                m = _OPEN_RE.search(text, pos)
                if m:
                    # Emit safe text before the tag
                    safe = text[pos:m.start()]
                    if safe:
                        out_parts.append(safe)
                    pos = m.end()
                    self._inside_think = True
                else:
                    # No complete open tag found — but there might be a *partial*
                    # open-tag prefix at the very end of the text that straddles
                    # the next delta boundary.
                    tail_start = _find_partial_open_tag_at_end(text, pos)
                    if tail_start is not None:
                        # Emit safe portion, buffer the suspicious tail
                        safe = text[pos:tail_start]
                        if safe:
                            out_parts.append(safe)
                        self._partial_buf = text[tail_start:]
                    else:
                        # All remaining text is safe
                        safe = text[pos:]
                        if safe:
                            out_parts.append(safe)
                    pos = len(text)

        return "".join(out_parts)


# ── Helpers ───────────────────────────────────────────────────────


def _find_partial_open_tag_at_end(text: str, from_pos: int) -> int | None:
    """
    Check whether ``text[from_pos:]`` ends with a prefix of any open tag.

    E.g., "hello <thi" → returns the index of '<' so it can be buffered.
    Returns None if no suspicious partial tag is at the tail.
    """
    sub = text[from_pos:]
    # Walk backwards: look for '<' and check if the suffix from there could
    # be the start of any of our open tags.
    lt_pos = sub.rfind("<")
    if lt_pos == -1:
        return None

    candidate = sub[lt_pos:].lower()
    for tag in _OPEN_TAGS:
        if tag.lower().startswith(candidate) and len(candidate) < len(tag):
            # It's a genuine partial prefix — buffer it
            return from_pos + lt_pos

    return None


def strip_think_blocks_simple(text: str) -> str:
    """
    Simple non-streaming stripping of complete think blocks from a full string.
    Used for non-streaming (batch) paths and for post-processing stored content.
    Safe to use when the full text is already available.
    """
    for open_tag, close_tag in zip(_OPEN_TAGS, _CLOSE_TAGS):
        open_re = re.compile(re.escape(open_tag) + r".*?" + re.escape(close_tag), re.DOTALL | re.IGNORECASE)
        text = open_re.sub("", text)
    # Clean up resulting double-blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
