"""Message/tool conversion for each provider dialect (no network)."""

import json

from alita.llm.provider import (
    AnthropicProvider, OllamaProvider, OpenAICompatibleProvider, create_provider, guess_context_window,
    list_providers,
)

HISTORY = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": [{"type": "text", "text": "look"},
                                 {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]},
    {"role": "assistant", "content": "", "reasoning_content": "thinking...",
     "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "browser", "arguments": '{"q": 1}'}},
                    {"id": "t2", "type": "function", "function": {"name": "todo", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "t1", "name": "browser", "content": "R1", "_meta": {"x": 1}},
    {"role": "tool", "tool_call_id": "t2", "name": "todo", "content": "R2"},
    {"role": "assistant", "content": "done", "_id": 5},
]
TOOLS = [{"type": "function", "function": {"name": "browser", "description": "d",
                                           "parameters": {"type": "object", "properties": {}}}}]


def test_anthropic_conversion():
    system, msgs = AnthropicProvider.convert_messages(HISTORY)
    assert system == "sys"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    uses = [b for b in msgs[1]["content"] if b["type"] == "tool_use"]
    assert [u["id"] for u in uses] == ["t1", "t2"] and uses[0]["input"] == {"q": 1}
    results = msgs[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # merged into one user turn
    tools = AnthropicProvider.convert_tools(TOOLS)
    assert tools[0]["input_schema"]["type"] == "object" and "cache_control" in tools[-1]


def test_ollama_conversion():
    msgs = OllamaProvider.convert_messages(HISTORY)
    assert msgs[1]["images"] == ["QUJD"] and msgs[1]["content"] == "look"
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == {"q": 1}
    assert msgs[3]["tool_name"] == "browser"


def test_openai_prepare_strips_internal_keys_and_reasoning():
    p = OpenAICompatibleProvider("https://x", "k", "gpt-4.1", provider_id="openai")
    out = p._prepare_messages(HISTORY)
    assert all(not k.startswith("_") for m in out for k in m)
    assert all("reasoning_content" not in m for m in out)
    assert "name" not in out[3]
    assert out[2]["content"] is None
    ds = OpenAICompatibleProvider("https://x", "k", "deepseek-reasoner", provider_id="deepseek", reasoning_echo=True)
    # Reasoning is echoed only for tool-call messages after the last user message.
    assert ds._prepare_messages(HISTORY)[2]["reasoning_content"] == "thinking..."


def test_reasoning_models_omit_temperature():
    p = OpenAICompatibleProvider("https://x", "k", "o4-mini", provider_id="openai", use_max_completion_tokens=True)
    payload = p._payload(HISTORY, TOOLS, 0.7, 1000, stream=True)
    assert "temperature" not in payload and payload["max_completion_tokens"] == 1000
    assert payload["stream_options"] == {"include_usage": True}


def test_registry_and_windows(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("ALITA_CONTEXT_WINDOW", raising=False)
    prov = create_provider("deepseek")
    assert prov.model == "deepseek-chat" and prov.context_window == 128_000
    assert guess_context_window("claude-sonnet-4-5") == 200_000
    assert guess_context_window("unknown-model") == 128_000
    ids = {p["id"] for p in list_providers()}
    assert {"openrouter", "custom", "anthropic", "ollama"} <= ids
    assert next(p for p in list_providers() if p["id"] == "deepseek")["has_key"] is True
    json.dumps(list_providers())  # serialisable
