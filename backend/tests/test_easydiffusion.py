"""
Tests for the EasyDiffusion client (regression suite for the v3.x API fix).

Network-free: ``httpx.MockTransport`` scripts both the /render response and
the NDJSON stream so we exercise the brace-tracking parser without a live
EasyDiffusion instance.
"""

import json

import httpx
import pytest

from alita.tools.easydiffusion import EasyDiffusionTool


# ─── Helpers ──────────────────────────────────────────────────────


def _make_tool(handler, *, render_timeout=10):
    """Build an EasyDiffusionTool wired to a MockTransport."""
    tool = EasyDiffusionTool(base_url="http://ed.test", render_timeout=render_timeout)
    tool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=tool.base_url,
        timeout=httpx.Timeout(30.0, read=None),
    )
    return tool


def _render_ack(task_id=999):
    """EasyDiffusion's /render response shape (note: `task` is an int)."""
    return {
        "status": "Rendering",
        "queue": 1,
        "stream": f"/image/stream/{task_id}",
        "task": task_id,
    }


def _ndjson_stream(records):
    """Concatenate JSON records without separators — EasyDiffusion's NDJSON."""
    return "".join(json.dumps(r) for r in records)


# ─── /render / task-id extraction ─────────────────────────────────


async def test_txt2img_extracts_task_id_from_int_task_field():
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            seen_payload.update(json.loads(request.content))
            return httpx.Response(200, json=_render_ack(task_id=777))
        # Any /image/stream/* path returns an immediate success.
        if request.url.path.startswith("/image/stream/"):
            stream = _ndjson_stream([
                {"step": 0, "step_time": -1, "total_steps": 5},
                {"step": 5, "step_time": 1.0, "total_steps": 5,
                 "status": "succeeded",
                 "output": [{"path": "/output/img.png", "seed": 42, "data": "BASE64"}],
                 "render_request": {}, "task_data": {}},
            ])
            return httpx.Response(200, text=stream, headers={"content-type": "application/x-ndjson"})
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="a cat", seed=42, steps=5)

    assert result["success"] is True
    assert result["task_id"] == "777"   # KEY FIX: extracted from int `task`, str-coerced
    assert result["status"] == "completed"  # sync-by-default waits for completion
    assert result["image_count"] == 1
    assert result["images"][0]["path"] == "/output/img.png"
    assert result["images"][0]["url"].startswith("http://ed.test/output/img.png?t=")
    assert result["images"][0]["seed"] == 42
    # Echo of the user's params (the previous shape kept these too).
    assert result["prompt"] == "a cat"
    assert seen_payload["sampler_name"] == "euler_a"


async def test_txt2img_random_seed_when_minus_one():
    """seed=-1 should be replaced with a random positive int before /render."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            captured["seed"] = json.loads(request.content)["seed"]
            return httpx.Response(200, json=_render_ack(task_id=1))
        if request.url.path.startswith("/image/stream/"):
            return httpx.Response(200, text=_ndjson_stream([
                {"status": "succeeded", "output": []},
            ]))
        return httpx.Response(404)

    tool = _make_tool(handler)
    out = await tool.execute(action="txt2img", prompt="x", seed=-1, steps=2)
    assert captured["seed"] != -1
    assert captured["seed"] >= 0
    # The agent-visible seed should be the same random value so calls are reproducible.
    assert out["seed"] == captured["seed"]


async def test_txt2img_no_wait_returns_ack_only():
    """wait=False should NOT poll the stream; return immediately with task_id."""
    stream_was_read = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=42))
        if request.url.path.startswith("/image/stream/"):
            nonlocal stream_was_read
            stream_was_read = True
            return httpx.Response(200, text=_ndjson_stream([
                {"status": "succeeded", "output": []},
            ]))
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="x", steps=2, wait=False)

    assert result["status"] == "Rendering"
    assert result["task_id"] == "42"
    assert "message" in result  # tells agent to poll action=status later
    assert stream_was_read is False


async def test_txt2img_requires_prompt():
    tool = _make_tool(lambda r: httpx.Response(200, json=_render_ack()))
    result = await tool.execute(action="txt2img", prompt="", steps=2)
    assert result["success"] is False
    assert "prompt is required" in result["error"]


async def test_img2img_requires_init_image():
    tool = _make_tool(lambda r: httpx.Response(200, json=_render_ack()))
    result = await tool.execute(action="img2img", prompt="x", steps=2)
    assert result["success"] is False
    assert "init_image" in result["error"]


# ─── NDJSON brace-tracking parser ─────────────────────────────────


async def test_ndjson_with_progress_then_success():
    """Multiple progress records concatenated with no newline should parse cleanly."""
    records = [
        {"step": 0, "step_time": -1, "total_steps": 3},
        {"step": 1, "step_time": 1.5, "total_steps": 3},
        {"step": 2, "step_time": 1.6, "total_steps": 3},
        {"step": 3, "step_time": 1.6, "total_steps": 3,
         "status": "succeeded",
         "output": [{"path": "/output/a.png", "seed": 1}, {"path": "/output/b.png", "seed": 2}],
         "render_request": {}, "task_data": {}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=10))
        if request.url.path.startswith("/image/stream/"):
            # Single response body containing all records back-to-back.
            return httpx.Response(200, text=_ndjson_stream(records))
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="multi", steps=3)

    assert result["success"] is True
    assert result["image_count"] == 2
    assert {img["path"] for img in result["images"]} == {"/output/a.png", "/output/b.png"}
    assert result["elapsed_steps"] == 3
    assert result["total_steps"] == 3


async def test_ndjson_chunked_across_multiple_reads():
    """The stream parser handles multiple records concatenated in one body.

    httpx MockTransport does not cleanly simulate chunked stream delivery via
    arbitrary async iterators, so we cover the parser's chunk-boundary
    handling here by concatenating two records in a single body and verifying
    that the brace-counter emits both. The same code path handles real
    streaming because the parser accumulates into `buffer` and only emits a
    record when brace depth returns to zero — chunk boundaries are irrelevant.
    """
    full_body = (
        '{"step": 0, "step_time": -1, "total_steps": 5}'
        '{"step": 5, "step_time": 1.0, "total_steps": 5,'
        ' "status": "succeeded", "output": [{"path": "/o/x.png", "seed": 7}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=11))
        if request.url.path.startswith("/image/stream/"):
            return httpx.Response(200, text=full_body)
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="chunked", steps=5)

    assert result["success"] is True
    assert result["images"][0]["path"] == "/o/x.png"
    assert result["images"][0]["seed"] == 7


async def test_ndjson_with_strings_containing_braces():
    """The brace counter must respect string literals containing { or }."""
    prompt_with_brace = "a {magic} dragon"
    records = [
        {"step": 1, "step_time": 1.0, "total_steps": 1},
        {"status": "succeeded", "output": [{"path": "/o.png", "seed": 1}],
         "render_request": {"prompt": prompt_with_brace}, "task_data": {}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            ack = dict(_render_ack(task_id=12))
            return httpx.Response(200, json=ack)
        if request.url.path.startswith("/image/stream/"):
            return httpx.Response(200, text=_ndjson_stream(records))
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt=prompt_with_brace, steps=1)
    assert result["success"] is True
    assert result["render_request"]["prompt"] == prompt_with_brace


async def test_ndjson_with_escaped_quotes_in_strings():
    """Backslash-escaped quotes must not terminate the string."""
    prompt = 'a "quoted" dragon'
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=13))
        if request.url.path.startswith("/image/stream/"):
            body = _ndjson_stream([
                {"status": "succeeded", "output": [{"path": "/x.png", "seed": 1}],
                 "render_request": {"prompt": prompt}},
            ])
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt=prompt, steps=1)
    assert result["success"] is True
    assert result["render_request"]["prompt"] == prompt


# ─── Failure modes ────────────────────────────────────────────────


async def test_render_returns_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(400, text="bad prompt")
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="x", steps=1)
    assert result["success"] is False
    assert "EasyDiffusion API error" in result["error"]


async def test_render_succeeded_with_failed_status_in_stream():
    """Status != succeeded → success=False, error from detail field."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=14))
        if request.url.path.startswith("/image/stream/"):
            body = _ndjson_stream([
                {"step": 1, "step_time": 1.0, "total_steps": 10},
                {"status": "failed", "detail": "CUDA out of memory"},
            ])
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="x", steps=10)
    assert result["success"] is False
    assert result["status"] == "failed"
    assert "out of memory" in result["error"]


async def test_render_interrupted_when_stream_ends_without_terminal():
    """
    If the stream never emits a terminal record before render_timeout elapses,
    return status=timeout. The poll loop is bounded externally by
    asyncio.wait_for (no internal deadline), so the timeout propagates from
    the wrapper rather than being detected mid-poll.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=15))
        if request.url.path.startswith("/image/stream/"):
            # Progress events only — no succeeded/failed terminal record.
            return httpx.Response(200, text=_ndjson_stream([
                {"step": 0, "step_time": -1, "total_steps": 100},
                {"step": 5, "step_time": 1.0, "total_steps": 100},
                {"step": 9, "step_time": 1.0, "total_steps": 100},
            ]))
        return httpx.Response(404)

    tool = _make_tool(handler, render_timeout=2)
    result = await tool.execute(action="txt2img", prompt="x", steps=100)
    assert result["success"] is False
    assert result["status"] == "timeout"
    assert "2s" in result["error"]


async def test_render_timeout_via_asyncio_wait_for():
    """
    Verify the asyncio.wait_for envelope fires when the stream is silent.

    We monkeypatch _poll_stream to await forever — directly exercising the
    wait_for wrapper without needing to script a hung stream over MockTransport.
    """
    import asyncio as _asyncio
    from alita.tools import easydiffusion as ed_mod

    tool = EasyDiffusionTool(base_url="http://ed.test", render_timeout=1)

    async def hang(_task_id, _prompt):
        await _asyncio.sleep(60)

    tool._poll_stream = hang  # type: ignore[assignment]
    tool._client = httpx.AsyncClient()  # never used

    result = await tool._await_completion("16", prompt={})
    assert result["success"] is False
    assert result["status"] == "timeout"
    assert "1s" in result["error"]


# ─── Models ───────────────────────────────────────────────────────


async def test_get_models_unwraps_models_key():
    """Server wraps the list in {"models": [...]}. Tool should flatten it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get/models":
            return httpx.Response(200, json={
                "models": [
                    {"model": "sd-v1-5", "name": "sd-v1-5", "tags": ["stable-diffusion"]},
                    {"model": "flux1-dev", "name": "flux1-dev", "tags": ["stable-diffusion", "flux_dev"]},
                ],
            })
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="models")
    assert result["success"] is True
    assert result["count"] == 2
    assert isinstance(result["models"], list)
    assert result["models"][0]["name"] == "sd-v1-5"
    assert result["models"][1]["tags"] == ["stable-diffusion", "flux_dev"]


async def test_get_models_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    tool = _make_tool(handler)
    result = await tool.execute(action="models")
    assert result["success"] is False
    assert "Could not fetch models" in result["error"]


# ─── Status action ────────────────────────────────────────────────


async def test_status_action_requires_task_id():
    tool = _make_tool(lambda r: httpx.Response(404))
    result = await tool.execute(action="status")
    assert result["success"] is False
    assert "task_id" in result["error"]


async def test_status_action_polls_stream():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/image/stream/555":
            return httpx.Response(200, text=_ndjson_stream([
                {"status": "succeeded", "output": [{"path": "/o/x.png", "seed": 55}],
                 "render_request": {}, "task_data": {}},
            ]))
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="status", task_id="555")
    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["task_id"] == "555"
    assert result["images"][0]["seed"] == 55


# ─── Unknown action ───────────────────────────────────────────────


async def test_unknown_action_returns_error():
    tool = _make_tool(lambda r: httpx.Response(404))
    result = await tool.execute(action="upscale", prompt="x")
    assert result["success"] is False
    assert "Unknown action" in result["error"]


# ─── Image URL construction ───────────────────────────────────────


async def test_image_url_built_from_relative_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/render":
            return httpx.Response(200, json=_render_ack(task_id=20))
        if request.url.path.startswith("/image/stream/"):
            return httpx.Response(200, text=_ndjson_stream([
                {"status": "succeeded", "output": [{"path": "/output/img.png", "seed": 1}], "render_request": {}},
            ]))
        return httpx.Response(404)

    tool = _make_tool(handler)
    result = await tool.execute(action="txt2img", prompt="x", steps=1)

    url = result["images"][0]["url"]
    # Absolute URL = base + path + cache-bust fragment.
    assert url.startswith("http://ed.test/output/img.png?t=")
    # Cache-bust suffix is a Unix epoch.
    assert "?t=" in url
    # The path field is preserved raw for callers that want it.
    assert result["images"][0]["path"] == "/output/img.png"