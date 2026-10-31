"""ElevenLabs voice: tool (Alita chooses her voice) and speech endpoints. No network."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from alita.tools.voice import VoiceService, speakable_text, truncate_speech

ACCOUNT = {
    "voices": [
        {"voice_id": "voiceA", "name": "Lily - Velvety Actress", "category": "premade",
         "labels": {"gender": "female", "age": "middle_aged", "accent": "british"}, "description": "Velvety.",
         "preview_url": "https://example.test/lily.mp3"},
        {"voice_id": "voiceB", "name": "Roger", "category": "premade", "labels": {"gender": "male"}},
    ],
    "has_more": False,
}
LIBRARY = {"voices": [{"voice_id": "libV", "public_owner_id": "owner1", "name": "Adrina", "gender": "female",
                       "age": "young", "accent": "american", "descriptive": "calm", "use_case": "conversational",
                       "preview_url": "https://example.test/adrina.mp3", "is_added_by_user": False}],
           "has_more": False, "total_count": 1}


class FakeEleven:
    def __init__(self):
        self.calls = []
        self.added = False
        self.tts_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "test-key"
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v2/voices":
            return httpx.Response(200, json=ACCOUNT)
        if path == "/v1/shared-voices":
            return httpx.Response(200, json=LIBRARY)
        if path == "/v1/user/subscription":
            return httpx.Response(200, json={"character_count": 10, "character_limit": 1000, "tier": "payg",
                                             "voice_slots_used": 0, "voice_limit": 3})
        if path.startswith("/v1/voices/add/"):
            self.added = True
            return httpx.Response(200, json={"voice_id": "libCopy"})
        if path.startswith("/v1/voices/"):
            vid = path.rsplit("/", 1)[1]
            known = {v["voice_id"]: v for v in ACCOUNT["voices"]}
            if self.added:
                known["libCopy"] = {"voice_id": "libCopy", "name": "Adrina", "category": "professional", "labels": {}}
            if vid in known:
                return httpx.Response(200, json=known[vid])
            return httpx.Response(400, json={"detail": {"message": f"A voice with ID '{vid}' was not found."}})
        if path.endswith("/stream"):
            if self.tts_status != 200:
                return httpx.Response(self.tts_status, json={"detail": {"message": "quota_exceeded"}})
            body = json.loads(request.content)
            assert body["model_id"] and body["voice_settings"]["speed"] <= 1.2
            return httpx.Response(200, content=b"ID3" + body["text"].encode())
        return httpx.Response(404)


@pytest.fixture
def eleven(alita_home, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.delenv("ELEVENLABS_MODEL", raising=False)
    monkeypatch.delenv("ELEVENLABS_BASE_URL", raising=False)
    # These tests exercise the ElevenLabs provider specifically (the default
    # provider for a fresh install is the free Edge one).
    (alita_home / "voice.json").write_text(
        json.dumps({"provider": "elevenlabs", "provider_voices": {}, "settings": {}}), encoding="utf-8")
    fake = FakeEleven()
    return fake, VoiceService(transport=httpx.MockTransport(fake))


def test_speakable_text_strips_markdown():
    text = "# Title\nI **really** like *this*.\n```py\nprint(1)\n```\n- a [link](http://x.y)\n![img](u) see https://x.y `code`"
    out = speakable_text(text)
    assert "print" not in out and "http" not in out and "*" not in out and "#" not in out
    assert "I really like this." in out and "a link" in out and "code" in out


def test_truncate_prefers_sentence_boundary():
    text = "One sentence here. " * 20
    out, cut = truncate_speech(text, 100)
    assert cut and out.endswith("…") and len(out) <= 102 and "here. …" in out


async def test_choose_voice_flow(eleven):
    fake, voice = eleven
    current = await voice.execute("current")
    assert current["success"] and current["voice"] is None and "No usable voice" in current["note"]

    listed = await voice.execute("list", gender="female")
    assert [v["name"] for v in listed["voices"]][0].startswith("Lily")

    preview = await voice.execute("preview", voice_id="voiceA")
    assert preview["audio_url"] == "https://example.test/lily.mp3" and preview["kind"] == "stock_sample"

    chosen = await voice.execute("set", voice_id="voiceA", reason="Warm but dry.", speed=9, stability=0.3)
    assert chosen["success"] and chosen["voice"]["settings"]["speed"] == 1.2  # clamped to ElevenLabs' limit
    assert voice.current_voice()["reason"] == "Warm but dry."

    tuned = await voice.execute("set", style=0.4)
    assert tuned["message"].startswith("Tuned")
    saved = voice.current_voice()
    assert saved["settings"]["style"] == 0.4 and saved["settings"]["stability"] == 0.3 and saved["reason"] == "Warm but dry."

    current = await voice.execute("current")
    assert current["voice"]["voice_id"] == "voiceA"


async def test_library_voice_is_added_then_set(eleven):
    fake, voice = eleven
    listed = await voice.execute("list", source="library", search="calm")
    assert listed["voices"][0]["public_owner_id"] == "owner1" and listed["voice_slots"] == {"used": 0, "limit": 3}
    assert (await voice.execute("preview", voice_id="libV", public_owner_id="owner1"))["audio_url"].endswith("adrina.mp3")
    chosen = await voice.execute("set", voice_id="libV", public_owner_id="owner1")
    assert chosen["success"] and chosen["added_from_library"] and voice.current_voice()["voice_id"] == "libCopy"


async def test_unknown_voice_and_missing_key(eleven, monkeypatch):
    fake, voice = eleven
    bad = await voice.execute("set", voice_id="nope")
    assert not bad["success"] and "not found" in bad["error"]
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    res = await voice.execute("list")
    assert not res["success"] and "Settings" in res["error"]


async def test_speech_streams_once_and_caches(eleven):
    fake, voice = eleven
    with pytest.raises(Exception, match="hasn't chosen"):
        voice.prepare("hello")
    await voice.execute("set", voice_id="voiceA")
    u = voice.prepare("**Hello** there. ```x = 1```")
    assert u["url"] == f"/api/voice/speech/{u['id']}.mp3" and u["chars"] == len("Hello there.")
    first, second = await asyncio.gather(voice.stream(u["id"]), voice.stream(u["id"]))
    a = b"".join([c async for c in first])
    b = b"".join([c async for c in second])
    assert a == b == b"ID3Hello there."
    assert sum(1 for m, p in fake.calls if p.endswith("/stream")) == 1
    assert voice.cached_path(u["id"]) and voice.speech_state(u["id"]) == {"state": "ready"}
    assert voice.prepare("**Hello** there. ```x = 1```")["id"] == u["id"]


async def test_speech_error_is_reported(eleven):
    fake, voice = eleven
    await voice.execute("set", voice_id="voiceA")
    fake.tts_status = 401
    u = voice.prepare("hello")
    with pytest.raises(Exception, match="quota_exceeded"):
        await voice.stream(u["id"])
    state = voice.speech_state(u["id"])
    assert state["state"] == "error" and "401" in state["error"]


def test_voice_tool_registered_and_hidden_from_subagents(agent):
    from alita.core.session import Session
    names = lambda sess: {d["function"]["name"] for d in agent.tool_definitions(sess)}  # noqa: E731
    assert "voice" in names(Session("c", kind="chat"))
    assert "voice" not in names(Session("s", kind="subagent"))
    assert "voice" not in names(Session("j", kind="job"))


def test_speech_endpoints(agent, eleven, monkeypatch):
    from alita.api import server
    fake, voice = eleven
    agent.voice = voice
    monkeypatch.setattr(server, "_agent", agent)
    client = TestClient(server.app)
    assert client.post("/voice/speech", json={"text": "hi"}).status_code == 409
    asyncio.run(voice.execute("set", voice_id="voiceA"))
    status = client.get("/voice/status").json()
    assert status["configured"] and status["voice"]["voice_id"] == "voiceA"
    prepared = client.post("/voice/speech", json={"text": "Hello *you*."}).json()
    audio = client.get(f"/voice/speech/{prepared['id']}.mp3")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg" and audio.content == b"ID3Hello you."
    again = client.get(f"/voice/speech/{prepared['id']}.mp3")
    assert again.content == audio.content
    assert client.get("/voice/speech/" + "0" * 40 + ".mp3").status_code == 404
    assert client.get("/settings").json()["voice"]["voice"]["name"].startswith("Lily")


def test_settings_save_elevenlabs(agent, eleven, monkeypatch, tmp_path):
    from alita.api import server
    from alita.core import config
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_PATH", env_file)
    monkeypatch.setattr(server, "_agent", agent)
    client = TestClient(server.app)
    res = client.put("/settings", json={"elevenlabs_api_key": "new-key", "elevenlabs_model": "eleven_flash_v2_5"})
    assert res.status_code == 200 and res.json()["voice"]["model"] == "eleven_flash_v2_5"
    text = env_file.read_text()
    assert "ELEVENLABS_API_KEY=new-key" in text and "ELEVENLABS_MODEL=eleven_flash_v2_5" in text
    assert client.put("/settings", json={"elevenlabs_model": "bad model!"}).status_code == 400

# ─── Free / multi-provider behaviour ─────────────────────────────────

import sys
import types

import alita.voice.providers as voice_providers

EDGE_VOICES = [
    {"ShortName": "en-GB-SoniaNeural", "Gender": "Female", "Locale": "en-GB",
     "FriendlyName": "Microsoft Sonia Online (Natural) - English (United Kingdom)"},
    {"ShortName": "en-US-AvaMultilingualNeural", "Gender": "Female", "Locale": "en-US",
     "FriendlyName": "Microsoft Ava Online (Natural) - English (United States)"},
    {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female", "Locale": "fr-FR",
     "FriendlyName": "Microsoft Denise Online (Natural) - French (France)"},
]


@pytest.fixture
def edge(alita_home, monkeypatch):
    """Edge TTS with the network stubbed out; no ElevenLabs key, so Edge is the default."""
    calls = []

    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", **_kw):
            self.text, self.voice, self.rate = text, voice, rate
            calls.append({"text": text, "voice": voice, "rate": rate})

        async def stream(self):
            yield {"type": "audio", "data": b"ID3|" + self.voice.encode() + b"|" + self.text.encode()}

    module = types.ModuleType("edge_tts")

    async def list_voices():
        return EDGE_VOICES

    module.list_voices = list_voices
    module.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    monkeypatch.setattr(voice_providers, "_edge_available", lambda: True)
    monkeypatch.setattr(voice_providers.EdgeTTSProvider, "_cache", None)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    (alita_home / "voice.json").unlink(missing_ok=True)
    return calls, VoiceService()


async def test_edge_is_the_free_default(edge):
    calls, voice = edge
    status = voice.status()
    assert status["provider"] == "edge" and status["free"] and status["configured"]
    assert status["voice"]["voice_id"] == "en-GB-SoniaNeural" and status["voice"]["default"]

    listed = await voice.execute("list", search="sonia")
    assert listed["voices"][0]["voice_id"] == "en-GB-SoniaNeural"
    assert listed["voices"][0]["locale"] == "en-GB" and listed["voices"][0]["name"] == "Sonia"

    prepared = voice.prepare("Hi **there**.")
    assert prepared["voice"] == "Sonia" and prepared["chars"] == len("Hi there.")
    audio = b"".join([c async for c in await voice.stream(prepared["id"])])
    assert audio == b"ID3|en-GB-SoniaNeural|Hi there."
    assert calls[-1]["rate"] == "+0%"


async def test_edge_speed_and_voice_choice(edge):
    calls, voice = edge
    chosen = await voice.execute("set", voice_id="en-US-AvaMultilingualNeural", speed=1.2)
    assert chosen["success"] and chosen["voice"]["name"] == "Ava"
    prepared = voice.prepare("Faster now")
    b"".join([c async for c in await voice.stream(prepared["id"])])
    assert calls[-1]["voice"] == "en-US-AvaMultilingualNeural" and calls[-1]["rate"] == "+20%"

    unknown = await voice.execute("set", voice_id="xx-YY-NobodyNeural")
    assert not unknown["success"] and "Unknown Edge voice" in unknown["error"]


async def test_provider_switch_remembers_each_voice(alita_home, monkeypatch):
    class FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", **_kw):
            self.text = text

        async def stream(self):
            yield {"type": "audio", "data": b"ID3" + self.text.encode()}

    module = types.ModuleType("edge_tts")

    async def list_voices():
        return EDGE_VOICES

    module.list_voices = list_voices
    module.Communicate = FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    monkeypatch.setattr(voice_providers, "_edge_available", lambda: True)
    monkeypatch.setattr(voice_providers.EdgeTTSProvider, "_cache", None)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    voice = VoiceService(transport=httpx.MockTransport(FakeEleven()))

    await voice.set_voice(provider="edge", voice_id="en-US-AvaMultilingualNeural")
    await voice.set_voice(provider="elevenlabs", voice_id="voiceA")
    assert voice.current_voice()["voice_id"] == "voiceA"
    await voice.set_voice(provider="edge")
    restored = voice.current_voice()
    assert restored["voice_id"] == "en-US-AvaMultilingualNeural" and restored["name"] == "Ava"


def test_v1_voice_file_is_migrated(alita_home):
    (alita_home / "voice.json").write_text(json.dumps(
        {"voice_id": "abc123", "name": "Old pick", "settings": {"speed": 1.1}, "reason": "warm"}), encoding="utf-8")
    voice = VoiceService()
    current = voice.current_voice()
    assert current["provider"] == "elevenlabs" and current["voice_id"] == "abc123"
    assert current["settings"]["speed"] == 1.1 and current["reason"] == "warm"


async def test_openai_tts(alita_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, content=b"ID3OPENAI")

    voice = VoiceService(transport=httpx.MockTransport(handler))
    chosen = await voice.set_voice(provider="openai", voice_id="nova", instructions="Warm and unhurried.", speed=1.1)
    assert chosen["success"] and chosen["voice"]["provider"] == "openai"
    prepared = voice.prepare("Hello there.")
    audio = b"".join([c async for c in await voice.stream(prepared["id"])])
    assert audio == b"ID3OPENAI"
    body = seen[-1]["body"]
    assert seen[-1]["path"].endswith("/audio/speech")
    assert body["voice"] == "nova" and body["input"] == "Hello there." and body["speed"] == 1.1
    assert body["instructions"] == "Warm and unhurried." and body["model"] == "gpt-4o-mini-tts"


async def test_browser_provider_is_ui_only(alita_home):
    voice = VoiceService()
    chosen = await voice.set_voice(provider="browser", voice_id="Microsoft Zira")
    assert chosen["success"]
    with pytest.raises(Exception, match="locally"):
        voice.prepare("hello")


async def test_voice_provider_endpoints(agent, edge, monkeypatch):
    from alita.api import server

    calls, voice = edge
    agent.voice = voice
    monkeypatch.setattr(server, "_agent", agent)
    client = TestClient(server.app)

    providers = client.get("/voice/providers").json()
    assert providers["active"] == "edge"
    assert {"edge", "browser", "elevenlabs", "openai"} <= {p["id"] for p in providers["providers"]}

    listed = client.get("/voice/voices", params={"provider": "edge", "search": "sonia"}).json()
    assert listed["voices"][0]["voice_id"] == "en-GB-SoniaNeural"

    updated = client.put("/voice", json={"provider": "edge", "voice_id": "en-US-AvaMultilingualNeural", "speed": 1.1}).json()
    assert updated["success"] and updated["status"]["voice"]["voice_id"] == "en-US-AvaMultilingualNeural"

    preview = client.post("/voice/preview", json={"provider": "edge"}).json()
    audio = client.get(preview["audio_url"].removeprefix("/api"))
    assert audio.status_code == 200 and audio.content.startswith(b"ID3")

    assert client.put("/voice", json={"provider": "nope"}).status_code == 400
    assert client.get("/voice/voices", params={"provider": "edge", "locale": "xx"}).json()["count"] == 0
