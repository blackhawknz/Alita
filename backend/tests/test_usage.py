"""Token usage persistence and read-only usage endpoints."""

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import alita.api.server as server
from alita.llm.provider import FallbackProvider, LLMError, LLMProvider
from alita.memory.sqlite_store import SQLiteStore
from tests.conftest import drain


class NoUsageLLM(LLMProvider):
    provider_id = "no-usage"

    def __init__(self):
        self.model = "no-usage-model"
        self.base_url = ""

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"content": "No usage here."}


class PartialUsageLLM(LLMProvider):
    provider_id = "partial-usage"

    def __init__(self):
        self.model = "partial-usage-model"
        self.base_url = ""

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"content": "Partial usage."}
        yield {"usage": {"completion_tokens": 7}}


class FailingPrimaryLLM(LLMProvider):
    provider_id = "primary"

    def __init__(self):
        self.model = "primary-model"
        self.base_url = ""

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        raise LLMError("primary: no API key configured")
        yield {}


class WorkingFallbackLLM(LLMProvider):
    provider_id = "fallback"

    def __init__(self):
        self.model = "fallback-model"
        self.base_url = ""

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"content": "Answered by the fallback."}
        yield {"usage": {"prompt_tokens": 12, "completion_tokens": 4, "cached_tokens": 0}}


async def run_turn(agent, text: str = "hello") -> str:
    events: List[Dict[str, Any]] = []

    async def emit(event: Dict[str, Any]) -> None:
        events.append(event)

    sess = agent.sessions.create(kind="chat")
    await agent._run_turn(sess, text, emit)
    await drain(agent)
    return sess.id


async def test_turn_persists_exactly_one_usage_event(agent):
    agent.llm.script = [{"content": "Hello there."}]
    session_id = await run_turn(agent)

    rows = agent.sqlite.query("SELECT * FROM usage_events")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == session_id
    assert row["source"] == "chat"
    assert row["model"] == "fake-model"
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 10
    assert row["cached_tokens"] == 0
    assert row["estimated"] == 0


async def test_usage_survives_store_restart(agent):
    agent.llm.script = [{"content": "Persist me."}]
    await run_turn(agent)
    path = agent.sqlite.db_path
    agent.sqlite.close()

    reopened = SQLiteStore(str(path))
    try:
        rows = reopened.query("SELECT prompt_tokens, completion_tokens FROM usage_events")
        assert rows == [{"prompt_tokens": 100, "completion_tokens": 10}]
    finally:
        reopened.close()


async def test_provider_without_usage_writes_no_row(agent):
    agent.llm = NoUsageLLM()
    await run_turn(agent)
    assert agent.sqlite.query("SELECT * FROM usage_events") == []


async def test_usage_records_the_model_that_actually_served(agent):
    agent.llm = FallbackProvider(FailingPrimaryLLM(), WorkingFallbackLLM)
    await run_turn(agent)
    row = agent.sqlite.query_one("SELECT model, prompt_tokens FROM usage_events")
    assert row == {"model": "fallback-model", "prompt_tokens": 12}


async def test_missing_prompt_tokens_is_marked_estimated(agent):
    agent.llm = PartialUsageLLM()
    await run_turn(agent)
    row = agent.sqlite.query_one("SELECT * FROM usage_events")
    assert row is not None
    assert row["prompt_tokens"] > 0
    assert row["completion_tokens"] == 7
    assert row["estimated"] == 1


@pytest.fixture
def client(agent, monkeypatch):
    monkeypatch.setattr(server, "_agent", agent)
    return TestClient(server.app)


async def test_usage_summary_and_session_endpoints(client, agent):
    await run_turn(agent)
    summary = client.get("/usage/summary")
    assert summary.status_code == 200
    data = summary.json()
    assert data["total"]["prompt_tokens"] == 100
    assert data["total"]["completion_tokens"] == 10
    assert data["total"]["runs"] == 1
    assert len(data["by_day"]) == 1
    assert data["by_model"][0]["model"] == "fake-model"

    session_id = agent.sqlite.query_one("SELECT session_id FROM usage_events")["session_id"]
    events = client.get(f"/usage/sessions/{session_id}")
    assert events.status_code == 200
    assert events.json()["count"] == 1


def test_usage_summary_rejects_bad_timestamp(client):
    response = client.get("/usage/summary", params={"since": "not-a-date"})
    assert response.status_code == 400
    assert "since" in response.json()["detail"]
