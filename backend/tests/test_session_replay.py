"""HTTP API: sessions, streaming chat, replay, memory and skills endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

import alita.api.server as server
from tests.conftest import tool_call


@pytest.fixture
def client(agent, monkeypatch):
    monkeypatch.setattr(server, "_agent", agent)
    return TestClient(server.app)  # no context manager => lifespan services not started


def sse_events(resp):
    out = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def test_stream_creates_session_and_replays(client, agent):
    agent.llm.script = [
        {"content": "", "tool_calls": [tool_call("todo", {"todos": [{"title": "step", "status": "completed"}]}, "c1")]},
        {"content": "Finished the plan."},
    ]
    with client.stream("POST", "/chat/stream", json={"message": "plan something"}) as resp:
        assert resp.status_code == 200
        events = sse_events(resp)
    assert events[0]["type"] == "session"
    sid = events[0]["session_id"]
    types = [e["type"] for e in events]
    assert "tool_call" in types and "tool_result" in types and types[-1] == "stream_end"
    done = next(e for e in events if e["type"] == "done")
    assert done["final_response"] == "Finished the plan."

    data = client.get(f"/sessions/{sid}/messages").json()
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert data["messages"][1]["tool_calls"][0]["name"] == "todo"
    assert data["messages"][2]["tool_call_id"] == "c1" and data["messages"][2]["success"] is True
    assert data["todos"][0]["title"] == "step"

    listing = client.get("/sessions").json()["sessions"]
    assert listing[0]["id"] == sid and listing[0]["title"] == "plan something"
    assert listing[0]["message_count"] == 2


def test_continue_existing_session(client, agent):
    agent.llm.script = [{"content": "first"}, {"content": "second"}]
    with client.stream("POST", "/chat/stream", json={"message": "one"}) as r:
        sid = sse_events(r)[0]["session_id"]
    with client.stream("POST", "/chat/stream", json={"message": "two", "session_id": sid}) as r:
        sse_events(r)
    second_request = agent.llm.requests[1]
    assert [m["role"] for m in second_request] == ["system", "user", "assistant", "user"]


def test_unknown_session_404(client):
    assert client.post("/chat/stream", json={"message": "x", "session_id": "nope"}).status_code == 404
    assert client.get("/sessions/nope/messages").status_code == 404


def test_session_crud(client):
    sid = client.post("/sessions", json={"title": "My chat"}).json()["session_id"]
    assert client.patch(f"/sessions/{sid}", json={"title": "Renamed", "pinned": True}).json()["success"]
    s = client.get(f"/sessions/{sid}").json()
    assert s["title"] == "Renamed" and s["pinned"] is True
    assert client.delete(f"/sessions/{sid}").json()["success"]
    assert client.get(f"/sessions/{sid}").status_code == 404


def test_slash_command_not_persisted(client, agent):
    with client.stream("POST", "/chat/stream", json={"message": "/help"}) as r:
        events = sse_events(r)
    assert events[0]["type"] == "command" and "/remember" in events[0]["message"]
    assert agent.llm.requests == []


def test_persona_slash_command(client, agent):
    def run(msg):
        with client.stream("POST", "/chat/stream", json={"message": msg}) as r:
            return sse_events(r)[0]["message"]

    assert "(default)" in run("/persona")
    assert "updated" in run("/persona You are Pirate Alita. Speak like a pirate.")
    assert client.get("/personality").json()["persona"] == "You are Pirate Alita. Speak like a pirate."
    assert "(custom)" in run("/persona")
    assert "reset" in run("/persona reset")
    assert client.get("/personality").json()["is_default"] is True
    assert agent.llm.requests == []


def test_unknown_slash_is_normal_message(client, agent):
    agent.llm.script = [{"content": "that's a path"}]
    with client.stream("POST", "/chat/stream", json={"message": "/usr/bin/python is broken"}) as r:
        events = sse_events(r)
    assert any(e["type"] == "done" for e in events)


def test_resume_stream_of_finished_turn(client, agent):
    agent.llm.script = [{"content": "hello"}]
    with client.stream("POST", "/chat/stream", json={"message": "hi"}) as r:
        sid = sse_events(r)[0]["session_id"]
    with client.stream("GET", f"/sessions/{sid}/stream?after=0") as r:
        events = sse_events(r)
    assert events[0]["resumed"] is True and any(e["type"] == "done" for e in events)


def test_memory_endpoints(client):
    r = client.post("/memory/entries", json={"content": "User owns a 3D printer", "category": "fact"}).json()
    assert r["status"] == "added"
    found = client.get("/memory/entries", params={"q": "3D printer"}).json()["memories"]
    assert found[0]["content"] == "User owns a 3D printer"
    assert client.patch(f"/memory/entries/{r['id']}", json={"importance": 5}).json()["success"]
    assert client.put("/memory/blocks/user", json={"content": "- Name: Kim"}).json()["success"]
    mem = client.get("/memory").json()
    assert next(b for b in mem["blocks"] if b["name"] == "user")["content"] == "- Name: Kim"
    assert client.put("/memory/blocks/user", json={"content": "x" * 9999}).status_code == 400
    assert client.delete(f"/memory/entries/{r['id']}").json()["success"]


def test_skill_endpoints(client):
    body = {"name": "api-skill", "description": "Made via the API. Use in tests.", "content": "# Steps\n1. do it well"}
    assert client.post("/skills", json=body).json()["name"] == "api-skill"
    assert client.get("/skills/api-skill").json()["content"].startswith("# Steps")
    assert client.put("/skills/api-skill", json={"description": "Updated description for tests."}).status_code == 200
    assert client.delete("/skills/api-skill").json()["success"]
    assert client.get("/skills/api-skill").status_code == 404


def test_personality_endpoints(client):
    r = client.put("/personality", json={"persona": "You are Testy.", "instructions": "Be brief."}).json()
    assert r["persona"] == "You are Testy." and r["instructions"] == "Be brief."
    preview = client.get("/personality/preview").json()
    assert preview["prompt"].startswith("You are Testy.")
    assert "# User's custom instructions\nBe brief." in preview["prompt"]
    assert preview["approx_tokens"] > 0
    r = client.post("/personality/reset").json()
    assert r["is_default"] is True
    assert "You are Testy." not in client.get("/personality/preview").json()["prompt"]


def test_upload_and_attach(client, agent):
    import base64
    data = base64.b64encode(b"name,score\nann,3\n").decode()
    up = client.post("/uploads", json={"name": "scores.csv", "data": data}).json()
    assert up["kind"] == "text"
    agent.llm.script = [{"content": "got it"}]
    with client.stream("POST", "/chat/stream", json={"message": "read this", "attachments": [up["id"]]}) as r:
        sse_events(r)
    sent_user = [m for m in agent.llm.requests[0] if m["role"] == "user"][-1]
    text = json.dumps(sent_user["content"])
    assert "ann,3" in text
    assert client.get(f"/uploads/{up['id']}").status_code == 200


def test_settings_roundtrip(client, agent, tmp_path, monkeypatch):
    monkeypatch.setattr("alita.core.config.ENV_PATH", tmp_path / ".env")
    # update_env_file also sets os.environ; register the keys so they are restored.
    monkeypatch.setenv("ALITA_MAX_ITERATIONS", "50")
    monkeypatch.setenv("ALITA_ALLOW_HOST_EXEC", "false")
    original = agent.llm
    r = client.put("/settings", json={"agent": {"max_iterations": 12, "allow_host_exec": True}})
    assert r.status_code == 200, r.text
    assert agent.settings.max_iterations == 12 and agent.allow_host_exec is True
    assert "ALITA_MAX_ITERATIONS=12" in (tmp_path / ".env").read_text()
    agent.llm = original
