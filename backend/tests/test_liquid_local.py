"""Liquid AI provider registry, local model paths and offline fallback."""

import asyncio

from alita.llm.liquid_local import liquid_status, resolve_file, resolve_repo
from alita.llm.provider import FallbackProvider, LLMError, LLMProvider, create_provider


class FailingProvider(LLMProvider):
    provider_id = "online"

    def __init__(self):
        self.model = "online-model"
        self.base_url = "https://example.invalid/v1"

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        raise LLMError("online: no API key configured. Add it in Settings.")
        yield {}


class OfflineProvider(LLMProvider):
    provider_id = "liquid_local"

    def __init__(self):
        self.model = "LiquidAI/LFM2.5-1.2B-Instruct-GGUF"
        self.base_url = ""

    async def chat_stream(self, messages, tools=None, temperature=None, max_tokens=None):
        yield {"content": "offline answer"}
        yield {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "cached_tokens": 0}}


def test_liquid_registry():
    from alita.llm.provider import PROVIDER_REGISTRY, list_providers

    assert PROVIDER_REGISTRY["liquid"]["needs_key"] is True
    assert PROVIDER_REGISTRY["liquid"]["default_base_url"] == "https://api.liquid.ai/openai/v1"
    local = next(p for p in list_providers() if p["id"] == "liquid_local")
    assert local["needs_key"] is False and local["local"] is True


def test_local_model_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("ALITA_HOME", str(tmp_path))
    monkeypatch.delenv("LIQUID_LOCAL_PATH", raising=False)
    monkeypatch.delenv("LIQUID_LOCAL_FILE", raising=False)
    assert resolve_repo("") == "LiquidAI/LFM2.5-1.2B-Instruct-GGUF"
    assert resolve_file("LiquidAI/LFM2.5-350M-GGUF") == "LFM2.5-350M-Q4_K_M.gguf"
    status = liquid_status("LiquidAI/LFM2.5-1.2B-Instruct-GGUF")
    assert status["downloaded"] is False
    assert status["path"].endswith("LFM2.5-1.2B-Instruct-Q4_K_M.gguf")
    assert status["state"] in ("idle", "error")


def test_prepare_uses_existing_file(monkeypatch, tmp_path):
    from alita.llm.liquid_local import prepare

    monkeypatch.setenv("ALITA_HOME", str(tmp_path))
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    monkeypatch.setenv("LIQUID_LOCAL_PATH", str(model))
    status = prepare()
    assert status["downloaded"] is True
    assert status["path"] == str(model)
    assert status["state"] == "ready"


def test_fallback_on_missing_key():
    provider = FallbackProvider(FailingProvider(), OfflineProvider)
    events = []

    async def run():
        async for event in provider.chat_stream([{"role": "user", "content": "hi"}]):
            events.append(event)

    asyncio.run(run())
    assert events[0]["content"] == "offline answer"
    assert events[-1]["usage"]["completion_tokens"] == 2


def test_fallback_not_used_for_bad_request():
    exc = LLMError("online error 400: invalid request", 400, False)
    assert FallbackProvider.should_fallback(exc) is False
    assert FallbackProvider.should_fallback(LLMError("no API key configured")) is True


def test_create_provider_wraps_online_provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    provider = create_provider("deepseek")
    assert isinstance(provider, FallbackProvider)
    assert provider.model == "deepseek-chat"
