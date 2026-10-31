"""
Context window management.

* :func:`estimate_tokens` — fast heuristic (~3.6 chars/token, counting tool
  call arguments and tool schemas).
* :func:`build_payload` — turns a session's stored history into the message
  list sent to the model: strips internal keys, trims bulky tool results from
  older steps ("context editing"), and attaches the per-turn ephemeral
  context block to the latest user message.
* :func:`plan_compaction` / :func:`summarise` — when the conversation outgrows
  the window, older turns are summarised into a structured digest that
  replaces them in context (the full transcript stays in SQLite).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..memory.sqlite_store import content_to_text

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.6


def _msg_chars(m: Dict[str, Any]) -> int:
    content = m.get("content")
    if isinstance(content, list):
        n = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                n += 3000  # ~850 tokens per image
            else:
                n += len(content_to_text([part]))
    else:
        n = len(content or "")
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        n += len(fn.get("name", "")) + len(fn.get("arguments", "") or "") + 20
    return n + 16


def estimate_tokens(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> int:
    chars = sum(_msg_chars(m) for m in messages)
    if tools:
        chars += len(json.dumps(tools))
    return int(chars / CHARS_PER_TOKEN)


def _trim_text(text: str, keep: int) -> str:
    if len(text) <= keep:
        return text
    head = int(keep * 0.75)
    tail = keep - head
    return f"{text[:head]}\n…[{len(text) - keep} chars trimmed from this older tool result]…\n{text[-tail:]}"


def build_payload(
    system_prompt: str,
    history: List[Dict[str, Any]],
    ephemeral: str = "",
    keep_recent_steps: int = 6,
    old_tool_chars: int = 2500,
) -> List[Dict[str, Any]]:
    """Assemble the request messages from stored history."""
    msgs: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    # Index of the step boundary before which tool results are trimmed.
    tool_idx = [i for i, m in enumerate(history) if m.get("role") == "tool"]
    cutoff = tool_idx[-keep_recent_steps] if len(tool_idx) > keep_recent_steps else -1
    last_user = max((i for i, m in enumerate(history) if m.get("role") == "user"), default=-1)
    for i, m in enumerate(history):
        msg = {k: v for k, v in m.items() if not k.startswith("_")}
        if msg.get("role") == "tool" and i < cutoff and isinstance(msg.get("content"), str):
            msg["content"] = _trim_text(msg["content"], old_tool_chars)
        if i == last_user and ephemeral:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = list(content) + [{"type": "text", "text": ephemeral}]
            else:
                msg["content"] = f"{content or ''}\n\n{ephemeral}"
        msgs.append(msg)
    return msgs


def hard_trim(payload: List[Dict[str, Any]], max_chars: int = 600) -> None:
    """Last-resort in-place trim of every tool result except the final batch."""
    last_assistant = max((i for i, m in enumerate(payload) if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(payload):
        if m.get("role") == "tool" and i < last_assistant and isinstance(m.get("content"), str):
            m["content"] = _trim_text(m["content"], max_chars)


def plan_compaction(history: List[Dict[str, Any]], budget_tokens: int) -> Optional[int]:
    """
    Choose how many leading messages to summarise.

    Returns the index of the first message to KEEP (always a user message so
    the kept tail starts a clean turn), or None when compaction isn't
    possible (fewer than two user turns).
    """
    user_idx = [i for i, m in enumerate(history) if m.get("role") == "user"]
    if len(user_idx) < 2:
        return None
    # Walk back from the most recent user message, keeping turns while they fit.
    keep_from = user_idx[-1]
    for idx in reversed(user_idx[:-1]):
        if estimate_tokens(history[idx:]) > budget_tokens:
            break
        keep_from = idx
    if keep_from == user_idx[0]:
        keep_from = user_idx[1]  # must summarise at least the first turn
    return keep_from


SUMMARY_PROMPT = """You are compacting an AI assistant's conversation so it can continue with a smaller context.
Write a dense, faithful summary of the conversation below{prev_note}. Use these sections (omit empty ones):

## User goals and requests
## Key facts, preferences and decisions
## Work completed (files changed, commands run, results — keep exact paths, names, numbers, URLs, errors)
## Current state
## Open questions / next steps

Be specific; this summary fully replaces the original messages.
{prev}
<conversation>
{transcript}
</conversation>"""


def render_transcript(messages: List[Dict[str, Any]], max_chars: int = 120_000) -> str:
    lines: List[str] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            text = content_to_text(m.get("content"))
            lines.append(f"[tool result {m.get('name', '')}]: {_trim_text(text, 1500)}")
            continue
        text = content_to_text(m.get("content"))
        if role == "user":
            # Never feed ephemeral context blocks into the summary.
            text = text.split("\n\n<context>")[0]
        if text.strip():
            lines.append(f"[{role}]: {_trim_text(text, 6000)}")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            lines.append(f"[assistant called {fn.get('name')}]: {_trim_text(fn.get('arguments') or '', 600)}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars // 3] + "\n…[middle omitted]…\n" + text[-(2 * max_chars) // 3:]
    return text


async def summarise(llm: Any, messages: List[Dict[str, Any]], previous_summary: str = "") -> str:
    prev = f"\n<previous_summary>\n{previous_summary}\n</previous_summary>\n" if previous_summary else ""
    prompt = SUMMARY_PROMPT.format(
        prev_note=" (merge in the previous summary, which covers even earlier messages)" if previous_summary else "",
        prev=prev,
        transcript=render_transcript(messages),
    )
    resp = await llm.chat(messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2500)
    return content_to_text(resp.get("content", "")).strip()


def split_for_compaction(history: List[Dict[str, Any]], keep_from: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return history[:keep_from], history[keep_from:]
