"""
LLM provider abstraction.

All providers speak one internal dialect — OpenAI chat-completions messages
and tool definitions — and translate at the edge:

* ``OpenAICompatibleProvider`` — OpenAI, DeepSeek, Gemini, Grok, Qwen, Z.AI,
  MiniMax, OpenRouter and any custom OpenAI-compatible endpoint
  (LM Studio, vLLM, llama.cpp server, ...).
* ``AnthropicProvider`` — native Messages API (tool_use / tool_result blocks,
  prompt caching).
* ``OllamaProvider`` — native ``/api/chat`` (so ``num_ctx`` can be set).

Interface::

    await provider.chat(messages, tools=None, temperature=None, max_tokens=None)
        -> {"role": "assistant", "content": str, "tool_calls": [...]?,
            "reasoning_content": str?, "usage": {...}?}

    async for ev in provider.chat_stream(...):
        {"content": str} | {"reasoning": str} | {"tool_calls": [...]} | {"usage": {...}}

Retries with exponential backoff on 429 / 5xx / connection errors happen
before any streamed output is produced.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from .credential_pool import CredentialPool, pool_from_value

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_RETRIES = 3


class LLMError(RuntimeError):
    """Provider error with a user-presentable message."""

    def __init__(self, message: str, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# ─── Helpers ──────────────────────────────────────────────────────────

def clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop internal keys (``_id``, ``_meta`` ...) and normalise content."""
    out = []
    for m in messages:
        msg = {k: v for k, v in m.items() if not k.startswith("_")}
        out.append(msg)
    return out


def _retry_delay(attempt: int, response: Optional[httpx.Response] = None) -> float:
    if response is not None:
        ra = response.headers.get("retry-after")
        if ra:
            try:
                return min(30.0, float(ra))
            except ValueError:
                pass
    return min(20.0, (2 ** attempt) + random.random())


def _error_text(response: httpx.Response, body: Optional[bytes] = None) -> str:
    raw = body if body is not None else response.content
    try:
        data = json.loads(raw)
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:600]
        return str(err)[:600]
    except Exception:  # noqa: BLE001
        return (raw or b"").decode("utf-8", errors="replace")[:600]


def _parse_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (json.JSONDecodeError, TypeError):
        return {}


def _data_url_parts(url: str) -> Optional[tuple]:
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", url or "", flags=re.DOTALL)
    return (m.group(1), m.group(2)) if m else None


_CONTEXT_WINDOWS = [
    (r"^claude", 200_000),
    (r"gpt-4\.1", 1_000_000),
    (r"^gpt-5", 400_000),
    (r"^(o1|o3|o4)", 200_000),
    (r"gpt-4o", 128_000),
    (r"gpt-4-turbo", 128_000),
    (r"gemini", 1_000_000),
    (r"grok-4", 256_000),
    (r"grok", 131_072),
    (r"deepseek", 128_000),
    (r"qwen", 131_072),
    (r"glm-4\.[56]", 128_000),
    (r"glm", 128_000),
    (r"minimax", 200_000),
    (r"kimi|moonshot", 128_000),
    (r"lfm3", 256_000),
    (r"lfm2\.5-24b|lfm2\.5-2\.6b", 131_072),
    (r"lfm2\.5-8b", 128_000),
    (r"lfm2\.5|lfm2", 32_000),
    (r"llama-?3\.[123]", 128_000),
]


def guess_context_window(model: str, default: int = 128_000) -> int:
    name = (model or "").lower().split("/")[-1]
    for pattern, size in _CONTEXT_WINDOWS:
        if re.search(pattern, name):
            return size
    return default


# ─── Base ─────────────────────────────────────────────────────────────

class LLMProvider:
    provider_id: str = "base"
    api_mode: str = "openai"
    model: str = ""
    base_url: str = ""

    @property
    def context_window(self) -> int:
        override = int(os.getenv("ALITA_CONTEXT_WINDOW", "0") or 0)
        return override or guess_context_window(self.model)

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None) -> Dict[str, Any]:
        """Default non-streaming implementation built on ``chat_stream``."""
        content, reasoning, tool_calls, usage = [], [], [], {}
        async for ev in self.chat_stream(messages, tools=tools, temperature=temperature, max_tokens=max_tokens):
            if "content" in ev:
                content.append(ev["content"])
            elif "reasoning" in ev:
                reasoning.append(ev["reasoning"])
            elif "tool_calls" in ev:
                tool_calls = ev["tool_calls"]
            elif "usage" in ev:
                usage = ev["usage"]
        out: Dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            out["tool_calls"] = tool_calls
        if reasoning:
            out["reasoning_content"] = "".join(reasoning)
        if usage:
            out["usage"] = usage
        return out

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None) -> AsyncGenerator[Dict[str, Any], None]:
        raise NotImplementedError
        yield {}  # pragma: no cover

    async def aclose(self) -> None:
        pass


class FallbackProvider(LLMProvider):
    """Use the primary provider, falling back to an offline model when it can't be reached."""

    provider_id = "fallback"

    def __init__(self, primary: LLMProvider, fallback_factory, fallback_id: str = "liquid_local"):
        self.primary = primary
        self.fallback_factory = fallback_factory
        self.fallback_id = fallback_id
        self._fallback: Optional[LLMProvider] = None
        self.provider_id = primary.provider_id
        self.api_mode = primary.api_mode
        self.model = primary.model
        self.base_url = primary.base_url

    @property
    def context_window(self) -> int:
        return self.primary.context_window

    @staticmethod
    def should_fallback(exc: LLMError) -> bool:
        if exc.retryable or exc.status in RETRYABLE_STATUS or exc.status in (401, 403, 404):
            return True
        text = str(exc).lower()
        return any(marker in text for marker in (
            "no api key", "all api keys", "cannot reach", "connection error",
            "connect", "timed out", "timeout", "network", "unreachable",
        ))

    def _get_fallback(self) -> LLMProvider:
        if self._fallback is None:
            self._fallback = self.fallback_factory()
        return self._fallback

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        emitted = False
        try:
            async for event in self.primary.chat_stream(
                messages, tools=tools, temperature=temperature, max_tokens=max_tokens
            ):
                if event.get("content") or event.get("reasoning") or event.get("tool_calls"):
                    emitted = True
                yield event
            return
        except LLMError as exc:
            if emitted or not self.should_fallback(exc):
                raise
            logger.warning(
                "%s is unavailable (%s); using the offline Liquid model for this turn.",
                self.primary.provider_id, exc,
            )
        fallback = self._get_fallback()
        async for event in fallback.chat_stream(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        ):
            yield event

    async def aclose(self) -> None:
        await self.primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()


# ─── OpenAI-compatible ────────────────────────────────────────────────

class OpenAICompatibleProvider(LLMProvider):
    """Any endpoint implementing ``POST /chat/completions`` with SSE streaming."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        provider_id: str = "openai_compatible",
        stream_usage: bool = True,
        reasoning_echo: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
        use_max_completion_tokens: bool = False,
    ):
        self.provider_id = provider_id
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._pool: CredentialPool = pool_from_value(api_key) if api_key else CredentialPool([])
        self.api_key = self._pool.get_available() or ""
        self.stream_usage = stream_usage
        self.reasoning_echo = reasoning_echo
        self.extra_headers = extra_headers or {}
        self.use_max_completion_tokens = use_max_completion_tokens

    def _headers(self, key: str) -> Dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _is_reasoning_model(self) -> bool:
        name = self.model.lower().split("/")[-1]
        return bool(re.match(r"^(o1|o3|o4|gpt-5)", name))

    def _prepare_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        msgs = clean_messages(messages)
        last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        out = []
        for i, m in enumerate(msgs):
            m = dict(m)
            if m.get("role") == "tool":
                m.pop("name", None)
                if not isinstance(m.get("content"), str):
                    m["content"] = json.dumps(m.get("content"))
            if m.get("role") == "assistant":
                if m.get("tool_calls") and not m.get("content"):
                    m["content"] = None
                # Reasoning is only echoed (DeepSeek thinking + tools) within the current turn.
                if not (self.reasoning_echo and i > last_user and m.get("tool_calls")):
                    m.pop("reasoning_content", None)
            out.append(m)
        return out

    def _payload(self, messages, tools, temperature, max_tokens, stream: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": self.model, "messages": self._prepare_messages(messages)}
        if max_tokens:
            payload["max_completion_tokens" if self.use_max_completion_tokens else "max_tokens"] = max_tokens
        if temperature is not None and not self._is_reasoning_model():
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            payload["stream"] = True
            if self.stream_usage:
                payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        if not self.base_url:
            raise LLMError(f"{self.provider_id}: no base URL configured")
        if not self._pool.total and self.provider_id not in ("custom", "lmstudio"):
            raise LLMError(f"{self.provider_id}: no API key configured. Add it in Settings.")
        payload = self._payload(messages, tools, temperature, max_tokens, stream=True)
        attempt = 0
        while True:
            key = self._pool.get_available() or ""
            if self._pool.total and not key:
                raise LLMError(f"{self.provider_id}: all API keys are rate-limited; try again shortly.", 429, True)
            emitted = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as client:
                    async with client.stream("POST", f"{self.base_url}/chat/completions",
                                             headers=self._headers(key), json=payload) as resp:
                        if resp.status_code >= 400:
                            body = await resp.aread()
                            msg = _error_text(resp, body)
                            if resp.status_code == 400 and "stream_options" in msg and "stream_options" in payload:
                                payload.pop("stream_options", None)
                                self.stream_usage = False
                                continue
                            if resp.status_code in (401, 403, 429) and self._pool.total > 1:
                                self._pool.mark_exhausted(key)
                                if self._pool.available_count:
                                    continue
                            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                                await asyncio.sleep(_retry_delay(attempt, resp))
                                attempt += 1
                                continue
                            raise LLMError(f"{self.provider_id} error {resp.status_code}: {msg}", resp.status_code,
                                           resp.status_code in RETRYABLE_STATUS)
                        partial: Dict[int, Dict[str, Any]] = {}
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            if chunk.get("error"):
                                raise LLMError(f"{self.provider_id}: {chunk['error'].get('message', chunk['error'])}")
                            if chunk.get("usage"):
                                u = chunk["usage"]
                                yield {"usage": {
                                    "prompt_tokens": u.get("prompt_tokens", 0),
                                    "completion_tokens": u.get("completion_tokens", 0),
                                    "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens",
                                                                                                u.get("prompt_cache_hit_tokens", 0)),
                                }}
                            for choice in chunk.get("choices") or []:
                                delta = choice.get("delta") or {}
                                rc = delta.get("reasoning_content") or delta.get("reasoning")
                                if rc:
                                    emitted = True
                                    yield {"reasoning": rc}
                                if delta.get("content"):
                                    emitted = True
                                    yield {"content": delta["content"]}
                                for tc in delta.get("tool_calls") or []:
                                    idx = tc.get("index", 0)
                                    slot = partial.setdefault(idx, {"id": "", "type": "function",
                                                                    "function": {"name": "", "arguments": ""}})
                                    if tc.get("id"):
                                        slot["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        slot["function"]["name"] += fn["name"]
                                    if fn.get("arguments"):
                                        args = fn["arguments"]
                                        slot["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
                                    emitted = True
                        if partial:
                            calls = []
                            for i in sorted(partial):
                                c = partial[i]
                                if not c["function"]["name"]:
                                    continue
                                if not c["id"]:
                                    c["id"] = f"call_{uuid.uuid4().hex[:12]}"
                                calls.append(c)
                            if calls:
                                yield {"tool_calls": calls}
                return
            except httpx.HTTPError as exc:
                if emitted or attempt >= MAX_RETRIES:
                    raise LLMError(f"{self.provider_id}: connection error: {exc}", retryable=True) from exc
                await asyncio.sleep(_retry_delay(attempt))
                attempt += 1


# ─── Anthropic ────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    provider_id = "anthropic"
    api_mode = "anthropic"

    def __init__(self, api_key: str = "", model: str = "", base_url: str = ""):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.base_url = (base_url or os.getenv("ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com/v1").rstrip("/")

    @staticmethod
    def convert_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        out = []
        for t in tools or []:
            fn = t.get("function", t)
            out.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        if out:
            out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    @staticmethod
    def _convert_content(content: Any) -> List[Dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        blocks = []
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                parsed = _data_url_parts(url)
                if parsed:
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": parsed[0], "data": parsed[1]}})
                elif url:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        return blocks

    @classmethod
    def convert_messages(cls, messages: List[Dict[str, Any]]) -> tuple:
        system_parts: List[str] = []
        out: List[Dict[str, Any]] = []

        def push(role: str, blocks: List[Dict[str, Any]]) -> None:
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in clean_messages(messages):
            role = m.get("role")
            if role == "system":
                system_parts.append(m.get("content") or "")
            elif role == "user":
                push("user", cls._convert_content(m.get("content")))
            elif role == "assistant":
                blocks = cls._convert_content(m.get("content"))
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    blocks.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:12]}",
                                   "name": fn.get("name", ""), "input": _parse_args(fn.get("arguments"))})
                push("assistant", blocks)
            elif role == "tool":
                content = m.get("content")
                push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                               "content": content if isinstance(content, str) else json.dumps(content)}])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
        system = "\n\n".join(p for p in system_parts if p)
        return system, out

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        if not self.api_key:
            raise LLMError("anthropic: no API key configured. Add it in Settings.")
        system, msgs = self.convert_messages(messages)
        payload: Dict[str, Any] = {"model": self.model, "messages": msgs, "max_tokens": max_tokens or 8192, "stream": True}
        if system:
            payload["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = self.convert_tools(tools)
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        attempt = 0
        while True:
            emitted = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0)) as client:
                    async with client.stream("POST", f"{self.base_url}/messages", headers=headers, json=payload) as resp:
                        if resp.status_code >= 400:
                            body = await resp.aread()
                            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                                await asyncio.sleep(_retry_delay(attempt, resp))
                                attempt += 1
                                continue
                            raise LLMError(f"anthropic error {resp.status_code}: {_error_text(resp, body)}",
                                           resp.status_code, resp.status_code in RETRYABLE_STATUS)
                        blocks: Dict[int, Dict[str, Any]] = {}
                        usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            et = ev.get("type")
                            if et == "message_start":
                                u = (ev.get("message") or {}).get("usage") or {}
                                usage["prompt_tokens"] = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                                          + u.get("cache_creation_input_tokens", 0))
                                usage["cached_tokens"] = u.get("cache_read_input_tokens", 0)
                            elif et == "content_block_start":
                                blk = ev.get("content_block") or {}
                                blocks[ev.get("index", 0)] = {"type": blk.get("type"), "id": blk.get("id"),
                                                              "name": blk.get("name"), "json": ""}
                            elif et == "content_block_delta":
                                d = ev.get("delta") or {}
                                if d.get("type") == "text_delta" and d.get("text"):
                                    emitted = True
                                    yield {"content": d["text"]}
                                elif d.get("type") == "thinking_delta" and d.get("thinking"):
                                    emitted = True
                                    yield {"reasoning": d["thinking"]}
                                elif d.get("type") == "input_json_delta":
                                    emitted = True
                                    blocks.setdefault(ev.get("index", 0), {"type": "tool_use", "json": ""})["json"] += d.get("partial_json", "")
                            elif et == "message_delta":
                                u = ev.get("usage") or {}
                                if u.get("output_tokens"):
                                    usage["completion_tokens"] = u["output_tokens"]
                            elif et == "error":
                                err = ev.get("error") or {}
                                raise LLMError(f"anthropic: {err.get('message', err)}", retryable=err.get("type") == "overloaded_error")
                        calls = []
                        for idx in sorted(blocks):
                            b = blocks[idx]
                            if b.get("type") == "tool_use" and b.get("name"):
                                calls.append({"id": b.get("id") or f"toolu_{uuid.uuid4().hex[:12]}", "type": "function",
                                              "function": {"name": b["name"], "arguments": b["json"] or "{}"}})
                        if calls:
                            yield {"tool_calls": calls}
                        yield {"usage": usage}
                return
            except httpx.HTTPError as exc:
                if emitted or attempt >= MAX_RETRIES:
                    raise LLMError(f"anthropic: connection error: {exc}", retryable=True) from exc
                await asyncio.sleep(_retry_delay(attempt))
                attempt += 1


# ─── Ollama ───────────────────────────────────────────────────────────

class OllamaProvider(LLMProvider):
    provider_id = "ollama"

    def __init__(self, base_url: str = "", model: str = ""):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "") or "http://localhost:11434").rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.1")
        self.num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "16384") or 16384)

    @property
    def context_window(self) -> int:
        override = int(os.getenv("ALITA_CONTEXT_WINDOW", "0") or 0)
        return override or self.num_ctx

    @staticmethod
    def convert_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for m in clean_messages(messages):
            role = m.get("role")
            msg: Dict[str, Any] = {"role": role}
            content = m.get("content")
            if isinstance(content, list):
                texts, images = [], []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        parsed = _data_url_parts((part.get("image_url") or {}).get("url", ""))
                        if parsed:
                            images.append(parsed[1])
                msg["content"] = "\n".join(texts)
                if images:
                    msg["images"] = images
            else:
                msg["content"] = content or ""
            if role == "assistant" and m.get("tool_calls"):
                msg["tool_calls"] = [{"function": {"name": tc["function"]["name"],
                                                   "arguments": _parse_args(tc["function"].get("arguments"))}}
                                     for tc in m["tool_calls"]]
            if role == "tool" and m.get("name"):
                msg["tool_name"] = m["name"]
            out.append(msg)
        return out

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        options: Dict[str, Any] = {"num_ctx": self.num_ctx}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: Dict[str, Any] = {"model": self.model, "messages": self.convert_messages(messages),
                                   "stream": True, "options": options}
        if tools:
            payload["tools"] = tools
        attempt = 0
        while True:
            emitted = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0)) as client:
                    async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                        if resp.status_code >= 400:
                            body = await resp.aread()
                            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                                await asyncio.sleep(_retry_delay(attempt, resp))
                                attempt += 1
                                continue
                            raise LLMError(f"ollama error {resp.status_code}: {_error_text(resp, body)}", resp.status_code)
                        calls: List[Dict[str, Any]] = []
                        async for line in resp.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if chunk.get("error"):
                                raise LLMError(f"ollama: {chunk['error']}")
                            msg = chunk.get("message") or {}
                            if msg.get("thinking"):
                                emitted = True
                                yield {"reasoning": msg["thinking"]}
                            if msg.get("content"):
                                emitted = True
                                yield {"content": msg["content"]}
                            for tc in msg.get("tool_calls") or []:
                                fn = tc.get("function") or {}
                                calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                                              "function": {"name": fn.get("name", ""),
                                                           "arguments": json.dumps(fn.get("arguments") or {})}})
                            if chunk.get("done"):
                                if calls:
                                    yield {"tool_calls": calls}
                                yield {"usage": {"prompt_tokens": chunk.get("prompt_eval_count", 0),
                                                 "completion_tokens": chunk.get("eval_count", 0), "cached_tokens": 0}}
                                break
                return
            except httpx.ConnectError as exc:
                raise LLMError(f"Cannot reach Ollama at {self.base_url} — is it running? ({exc})", retryable=True) from exc
            except httpx.HTTPError as exc:
                if emitted or attempt >= MAX_RETRIES:
                    raise LLMError(f"ollama: connection error: {exc}", retryable=True) from exc
                await asyncio.sleep(_retry_delay(attempt))
                attempt += 1


# ─── Registry ─────────────────────────────────────────────────────────

PROVIDER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "name": "Ollama", "description": "Local models via Ollama", "needs_key": False,
        "default_model": "llama3.1", "env_model": "OLLAMA_MODEL", "env_base_url": "OLLAMA_BASE_URL",
        "default_base_url": "http://localhost:11434",
        "models": ["llama3.1", "qwen3", "qwen2.5-coder", "mistral-small", "gpt-oss:20b"],
    },
    "openai": {
        "name": "OpenAI", "description": "GPT-5, GPT-4.1, o-series", "needs_key": True,
        "default_model": "gpt-4.1", "env_key": "OPENAI_API_KEY", "env_model": "OPENAI_MODEL",
        "env_base_url": "OPENAI_BASE_URL", "default_base_url": "https://api.openai.com/v1",
        "models": ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "o4-mini"],
    },
    "anthropic": {
        "name": "Anthropic", "description": "Claude Sonnet / Opus / Haiku", "needs_key": True,
        "default_model": "claude-sonnet-4-5", "env_key": "ANTHROPIC_API_KEY", "env_model": "ANTHROPIC_MODEL",
        "env_base_url": "ANTHROPIC_BASE_URL", "default_base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
    },
    "deepseek": {
        "name": "DeepSeek", "description": "DeepSeek chat & reasoner", "needs_key": True,
        "default_model": "deepseek-chat", "env_key": "DEEPSEEK_API_KEY", "env_model": "DEEPSEEK_MODEL",
        "env_base_url": "DEEPSEEK_BASE_URL", "default_base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"], "reasoning_echo": True,
    },
    "gemini": {
        "name": "Google Gemini", "description": "Gemini 2.5 Pro / Flash", "needs_key": True,
        "default_model": "gemini-2.5-flash", "env_key": "GOOGLE_API_KEY", "alt_env_keys": ["GEMINI_API_KEY"],
        "env_model": "GEMINI_MODEL", "env_base_url": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    },
    "openrouter": {
        "name": "OpenRouter", "description": "Hundreds of models via one key", "needs_key": True,
        "default_model": "anthropic/claude-sonnet-4.5", "env_key": "OPENROUTER_API_KEY", "env_model": "OPENROUTER_MODEL",
        "env_base_url": "OPENROUTER_BASE_URL", "default_base_url": "https://openrouter.ai/api/v1",
        "models": ["anthropic/claude-sonnet-4.5", "openai/gpt-5", "google/gemini-2.5-pro", "deepseek/deepseek-chat",
                   "qwen/qwen3-coder"],
        "extra_headers": {"HTTP-Referer": "http://localhost:3001", "X-Title": "Alita"},
    },
    "grok": {
        "name": "xAI Grok", "description": "Grok 4 / Grok 3", "needs_key": True,
        "default_model": "grok-4", "env_key": "XAI_API_KEY", "env_model": "GROK_MODEL",
        "env_base_url": "GROK_BASE_URL", "default_base_url": "https://api.x.ai/v1",
        "models": ["grok-4", "grok-3", "grok-3-mini"],
    },
    "qwen": {
        "name": "Qwen (DashScope)", "description": "Qwen Plus / Max / Coder", "needs_key": True,
        "default_model": "qwen-plus", "env_key": "DASHSCOPE_API_KEY", "env_model": "QWEN_MODEL",
        "env_base_url": "QWEN_BASE_URL", "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen3-coder-plus", "qwen-turbo"],
    },
    "zai": {
        "name": "Z.AI (GLM)", "description": "GLM-4.6 / GLM-4.5", "needs_key": True,
        "default_model": "glm-4.6", "env_key": "ZAI_API_KEY", "alt_env_keys": ["GLM_API_KEY"],
        "env_model": "ZAI_MODEL", "env_base_url": "ZAI_BASE_URL",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4-plus"],
    },
    "minimax": {
        "name": "MiniMax", "description": "MiniMax M2", "needs_key": True,
        "default_model": "MiniMax-M2", "env_key": "MINIMAX_API_KEY", "env_model": "MINIMAX_MODEL",
        "env_base_url": "MINIMAX_BASE_URL", "default_base_url": "https://api.minimax.chat/v1",
        "models": ["MiniMax-M2", "abab7-chat"], "stream_usage": False,
    },
    "liquid": {
        "name": "Liquid AI", "description": "LFM models via Liquid's OpenAI-compatible API", "needs_key": True,
        "default_model": "lfm2.5-1.2b-instruct", "env_key": "LIQUID_API_KEY", "env_model": "LIQUID_MODEL",
        "env_base_url": "LIQUID_BASE_URL", "default_base_url": "https://api.liquid.ai/openai/v1",
        "models": ["lfm3-1.7b", "lfm2.5-24b-a2b", "lfm2.5-2.6b", "lfm2.5-1.2b-instruct",
                   "lfm2.5-1.2b-thinking", "lfm2.5-350m", "lfm2.5-230m"],
    },
    "liquid_local": {
        "name": "Liquid AI (offline)", "description": "Runs a Liquid LFM on your machine; no key or internet",
        "needs_key": False, "default_model": "LiquidAI/LFM2.5-1.2B-Instruct-GGUF",
        "env_model": "LIQUID_LOCAL_MODEL", "env_base_url": "LIQUID_LOCAL_BASE_URL",
        "default_base_url": "",
        "models": ["LiquidAI/LFM2.5-1.2B-Instruct-GGUF", "LiquidAI/LFM2.5-350M-GGUF",
                   "LiquidAI/LFM2.5-230M-GGUF", "LiquidAI/LFM2.5-1.2B-Thinking-GGUF",
                   "LiquidAI/LFM2.5-2.6B-GGUF"],
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)", "description": "LM Studio, vLLM, llama.cpp, LiteLLM ...",
        "needs_key": False, "default_model": "", "env_key": "CUSTOM_API_KEY", "env_model": "CUSTOM_MODEL",
        "env_base_url": "CUSTOM_BASE_URL", "default_base_url": "http://localhost:1234/v1", "models": [],
    },
}


def resolve_key(provider_id: str) -> str:
    info = PROVIDER_REGISTRY.get(provider_id, {})
    for env in [info.get("env_key", "")] + list(info.get("alt_env_keys", [])):
        if env and os.getenv(env):
            return os.getenv(env, "")
    return ""


def resolve_model(provider_id: str) -> str:
    info = PROVIDER_REGISTRY.get(provider_id, {})
    return os.getenv(info.get("env_model", ""), "") or info.get("default_model", "")


def resolve_base_url(provider_id: str) -> str:
    info = PROVIDER_REGISTRY.get(provider_id, {})
    return os.getenv(info.get("env_base_url", ""), "") or info.get("default_base_url", "")


def _local_fallback_enabled() -> bool:
    raw = os.getenv("ALITA_LIQUID_FALLBACK", "true")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def create_provider(provider_type: str = "ollama", model: Optional[str] = None, api_key: Optional[str] = None,
                    base_url: Optional[str] = None, **_: Any) -> LLMProvider:
    """Instantiate a provider from the registry (env supplies missing values)."""
    pid = (provider_type or "ollama").lower()
    info = PROVIDER_REGISTRY.get(pid)
    if not info:
        raise ValueError(f"Unknown provider: {provider_type}. Options: {list(PROVIDER_REGISTRY)}")
    model = model or resolve_model(pid)
    base = base_url or resolve_base_url(pid)
    if pid == "liquid_local":
        from .liquid_local import LiquidLocalProvider

        return LiquidLocalProvider(model=model)
    if pid == "ollama":
        primary: LLMProvider = OllamaProvider(base_url=base, model=model)
    else:
        key = api_key or resolve_key(pid)
        if pid == "anthropic":
            primary = AnthropicProvider(api_key=key, model=model, base_url=base)
        else:
            primary = OpenAICompatibleProvider(
                base_url=base, api_key=key, model=model, provider_id=pid,
                stream_usage=info.get("stream_usage", True),
                reasoning_echo=info.get("reasoning_echo", False),
                extra_headers=info.get("extra_headers"),
                use_max_completion_tokens=(pid == "openai"),
            )
    if _local_fallback_enabled():

        def make_local() -> LLMProvider:
            from .liquid_local import LiquidLocalProvider

            return LiquidLocalProvider(model=os.getenv("LIQUID_LOCAL_MODEL", "") or
                                       PROVIDER_REGISTRY["liquid_local"]["default_model"])

        return FallbackProvider(primary, make_local)
    return primary


def get_provider_info(provider_type: str) -> Optional[Dict[str, Any]]:
    return PROVIDER_REGISTRY.get(provider_type.lower())


def list_providers() -> List[Dict[str, Any]]:
    out = []
    for pid, info in PROVIDER_REGISTRY.items():
        out.append({
            "id": pid,
            "name": info["name"],
            "description": info["description"],
            "needs_key": info["needs_key"],
            "has_key": bool(resolve_key(pid)),
            "default_model": info["default_model"],
            "model": resolve_model(pid),
            "base_url": resolve_base_url(pid),
            "default_base_url": info.get("default_base_url", ""),
            "models": info.get("models", []),
            "env_key": info.get("env_key"),
            "env_model": info.get("env_model"),
            "env_base_url": info.get("env_base_url"),
            "local": pid == "liquid_local",
        })
    return out


def image_file_to_data_url(path: str, mime: str) -> str:
    with open(path, "rb") as fh:
        return f"data:{mime};base64,{base64.b64encode(fh.read()).decode('ascii')}"
