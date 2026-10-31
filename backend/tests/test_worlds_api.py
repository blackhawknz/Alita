"""World endpoints (Fortitude): turns, scoped fiction memory, isolation from home memory."""

import json

import pytest
from fastapi.testclient import TestClient

from alita.api import server
from tests.conftest import FakeLLM

SCOPE = "fortitude:w1"


class WorldLLM(FakeLLM):
    """Records world calls and returns a canned JSON turn."""

    def __init__(self):
        super().__init__()
        self.world_calls = []

    async def chat(self, messages, tools=None, temperature=None, max_tokens=None):
        if messages and messages[0]["role"] == "system" and "FICTION" in messages[0]["content"]:
            self.world_calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
            return {"role": "assistant", "content": json.dumps({"intents": [], "mood": "calm"}),
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5}}
        return await super().chat(messages, tools, temperature, max_tokens)


@pytest.fixture
def client(agent, monkeypatch):
    agent.llm = WorldLLM()
    monkeypatch.setattr(server, "_agent", agent)
    return TestClient(server.app)


def turn_body(**over):
    body = {"scope": SCOPE, "system": "Reply with JSON.", "user": "Day 3, 09:00. TRIGGER: the alarm.", "recall_query": "alarm seal"}
    body.update(over)
    return body


def test_turn_is_a_single_tools_off_completion_with_persona_and_scoped_memories(client, agent):
    client.post("/agent/memory", json={"scope": SCOPE, "content": "Sera came to me about the seal alarm.", "external_ref": f"{SCOPE}:1:alita:0"})
    client.post("/agent/memory", json={"scope": "fortitude:other", "content": "A memory from another world.", "external_ref": "fortitude:other:1"})
    r = client.post("/agent/turn", json=turn_body())
    assert r.status_code == 200, r.text
    assert json.loads(r.json()["content"]) == {"intents": [], "mood": "calm"}
    call = agent.llm.world_calls[-1]
    assert call["tools"] is None
    system = call["messages"][0]["content"]
    assert "Sera came to me about the seal alarm." in system
    assert "another world" not in system  # scopes don't leak into each other
    assert "FICTION" in system and "Reply with JSON." in system
    assert call["messages"][1]["content"].startswith("Day 3")
    # No chat session was created → no auto-extraction on world traffic.
    assert agent.sqlite.query_one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0


def test_reflect_uses_the_same_path(client, agent):
    r = client.post("/agent/reflect", json=turn_body(max_tokens=2000))
    assert r.status_code == 200
    assert agent.llm.world_calls[-1]["max_tokens"] == 2000


def test_memory_write_is_idempotent_and_scoped(client):
    body = {"scope": SCOPE, "content": "Tomas would not meet my eye.", "external_ref": f"{SCOPE}:14:alita:0", "importance": 4}
    a = client.post("/agent/memory", json=body).json()
    b = client.post("/agent/memory", json=body).json()
    assert a["status"] == "added" and b == {"status": "duplicate", "id": a["id"]}
    rows = client.get("/agent/memory", params={"scope": SCOPE}).json()["memories"]
    assert [m["content"] for m in rows] == ["Tomas would not meet my eye."]
    assert client.get("/agent/memory", params={"scope": "fortitude:w2"}).json()["memories"] == []


def test_external_ref_must_belong_to_scope_and_scope_is_validated(client):
    assert client.post("/agent/memory", json={"scope": SCOPE, "content": "x", "external_ref": "home:1"}).status_code == 400
    assert client.post("/agent/memory", json={"scope": "no-colon", "content": "x"}).status_code == 422
    assert client.get("/agent/memory", params={"scope": "bad scope"}).status_code == 422


def test_retract_removes_a_world_and_frees_its_refs(client):
    ref = f"{SCOPE}:1:alita:0"
    client.post("/agent/memory", json={"scope": SCOPE, "content": "The crack is wider.", "external_ref": ref})
    client.post("/agent/memory", json={"scope": SCOPE, "content": "Sera knows now.", "external_ref": f"{SCOPE}:2:alita:0"})
    assert client.post("/agent/memory/retract", json={"scope": SCOPE}).json()["retracted"] == 2
    assert client.get("/agent/memory", params={"scope": SCOPE}).json()["memories"] == []
    again = client.post("/agent/memory", json={"scope": SCOPE, "content": "A new world, same ref.", "external_ref": ref}).json()
    assert again["status"] == "added"


async def test_fiction_never_merges_with_real_memory(agent):
    lt = agent.longterm
    real = await lt.add("The user lives in New Zealand.", "fact")
    fic = await lt.add_scoped("The user lives in New Zealand.", SCOPE, f"{SCOPE}:9")
    assert fic["status"] == "added" and fic["id"] != real["id"]
    # And a home write identical to a fiction memory doesn't merge into it.
    await lt.add_scoped("The Queen keeps the board figure high.", SCOPE, f"{SCOPE}:10")
    home = await lt.add("The Queen keeps the board figure high.", "fact")
    assert home["status"] == "added"
    assert lt.get(home["id"])["fiction"] == 0


async def test_home_recall_labels_fiction(agent):
    await agent.longterm.add_scoped("The crack in the Spine is wider than the board says.", SCOPE, f"{SCOPE}:11")
    hits = await agent.memory.recall("crack spine board", k=6)
    text = agent.memory.format_recall(hits)
    assert "FICTION · Fortitude" in text
    assert "never real-world facts" in text


async def test_extraction_cannot_touch_fiction(agent):
    fic = await agent.longterm.add_scoped("Sera held the lamp at the seal.", SCOPE, f"{SCOPE}:12")
    agent.llm.extraction = {"memories": [{"op": "delete", "id": fic["id"]}, {"op": "update", "id": fic["id"], "content": "changed"}]}
    res = await agent.memory.extract_from_exchange("Tell me about Sera and the lamp at the seal again", "She held it.")
    assert res["applied"] == []
    row = agent.longterm.get(fic["id"])
    assert row["active"] == 1 and row["content"] == "Sera held the lamp at the seal."
