"""
Text preparation for speech: strip markdown and truncate at natural boundaries.
"""

from __future__ import annotations

import re
from typing import Tuple


def speakable_text(markdown: str) -> str:
    """Strip markdown and other things that shouldn't be read aloud."""
    t = markdown or ""
    t = re.sub(r"```.*?(?:```|$)", " ", t, flags=re.S)          # fenced code
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                 # images
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)              # links -> label
    t = re.sub(r"<[^>\n]+>", " ", t)                            # html tags
    t = re.sub(r"https?://\S+", " ", t)                         # bare urls
    t = re.sub(r"`([^`]*)`", r"\1", t)                          # inline code
    t = re.sub(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*$", " ", t, flags=re.M)  # table rules
    t = t.replace("|", ", ")
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)         # headings
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)                  # quotes
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)              # bullets
    t = re.sub(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", " ", t, flags=re.M)  # rules
    t = re.sub(r"\*\*|__|~~", "", t)                            # bold/strike
    t = re.sub(r"(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)", "", t)       # *italic*
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def truncate_speech(text: str, limit: int) -> Tuple[str, bool]:
    """Cut at a sentence (or word) boundary so audio doesn't stop mid-word."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), head.rfind("\n"))
    if cut < limit * 0.5:
        cut = head.rfind(" ")
    if cut <= 0:
        cut = limit
    return head[:cut + 1].rstrip() + " …", True


def rate_percent(speed: float) -> str:
    """Map a speed multiplier to the ``+N%`` rate string both edge-tts and Settings use."""
    pct = int(round((max(0.5, min(2.0, float(speed))) - 1.0) * 100))
    return f"{pct:+d}%"
