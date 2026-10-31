"""
Shared pytest fixtures for the Alita backend.

* ``make_app`` / ``public_client`` — minimal apps for the auth middleware tests.
* ``FakeLLM`` + ``agent`` — a fully constructed AlitaAgent in an isolated
  temporary home (SQLite, memory, skills) with a scripted LLM, so the real
  agent loop can be exercised without network access.
"""

import asyncio
import copy
import json
import uuid
from typing import Any, Callable, Dict, List, Optional, Union

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alita.api.security import AuthMiddleware
from alita.llm.provider import LLMProvider


@pytest.fixture
def make_app():
    """Return a factory that builds a minimal app protected by AuthMiddleware."""

    def _build(token):
        app = FastAPI()

        @app.get("/")
        def root():
            return {"ok": True}

        @app.get("/health")
        def health():
            return {"ok": True}

        @app.get("/tools")
        def tools():
            return {"ok": True}

        app.add_middleware(AuthMiddleware, token=token)
        return app

    return _build


@pytest.fixture
def public_client(make_app):
    """TestClient with auth disabled (no token configured)."""
    return TestClient(make_app(token=None))


# ─── Fake LLM ─────────────────────────────────────────────────────────

Step = Union[Dict[str, Any], Callable[[List[Dict[str, Any]]], Dict[str, Any]]]


def tool_call(name: str, args: Dict[str, Any], call_id: Optional[str] = None) -> Dict[str, Any]:
    return {"id": call_id or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM(LLMProvider):
    """Scripted provider: each streamed call pops the next step from ``script``."""

    provider_id = "fake"

    def __init__(self, script: Optional[List[Step]] = None, window: int = 128_000):
        self.script: List[Step] = list(script or [])
        self.model = "fake-model"
        self.base_url = ""
        self._window = window
        self.requests: List[List[Dict[str, Any]]] = []
        self.chat_prompts: List[str] = []
        self.extraction: Dict[str, Any] = {"memories": [], "profile_add": []}
        self.delay = 0.0

    @property
    def context_window(self) -> int:
        return self._window

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        self.requests.append(copy.deepcopy(messages))
        step = self.script.pop(0) if self.script else {"content": "(script exhausted)"}
        if callable(step):
            step = step(messages)
        text = step.get("content", "")
        for i in range(0, len(text), 7):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {"content": text[i:i + 7]}
        if step.get("reasoning"):
            yield {"reasoning": step["reasoning"]}
        if step.get("tool_calls"):
            yield {"tool_calls": step["tool_calls"]}
        yield {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 0}}

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
        prompt = messages[-1]["content"] if messages else ""
        self.chat_prompts.append(prompt)
        if "short title" in prompt:
            return {"role": "assistant", "content": "Fake Title"}
        if "long-term memory of an AI" in prompt:
            return {"role": "assistant", "content": json.dumps(self.extraction)}
        if "compacting an AI assistant" in prompt:
            return {"role": "assistant", "content": "## User goals and requests\nSUMMARY OF EARLIER TURNS"}
        if "reusable SKILL" in prompt:
            return {"role": "assistant", "content": '{"action": "none"}'}
        return {"role": "assistant", "content": "OK"}


@pytest.fixture
def alita_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("ALITA_HOME", str(home))
    monkeypatch.setenv("ALITA_WORKSPACE", str(ws))
    monkeypatch.setenv("ALITA_VECTOR_ENABLED", "false")
    monkeypatch.setenv("ALITA_AUTO_TITLE", "false")
    monkeypatch.setenv("ALITA_MEMORY_AUTO_EXTRACT", "false")
    monkeypatch.setenv("ALITA_SKILLS_AUTO_CREATE", "false")
    monkeypatch.setenv("ALITA_APPROVAL_MODE", "auto")
    monkeypatch.setenv("ALITA_ALLOW_HOST_EXEC", "false")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    return home


@pytest.fixture
def agent(alita_home):
    from alita.core.agent import AlitaAgent
    from alita.memory.vector_store import VectorStore

    a = AlitaAgent(vector_store=VectorStore(enabled=False))
    a.llm = FakeLLM()
    yield a
    a.sqlite.close()


async def drain(agent) -> None:
    """Wait for the agent's background tasks (memory extraction, titles ...)."""
    for _ in range(20):
        pending = [t for t in agent._background if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
