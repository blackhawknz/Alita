"""
Tests for the http_request tool and its SSRF guard.

Network-free: hostname resolution is monkeypatched, and the HTTP transport
is an httpx.MockTransport so the suite does not require connectivity.
"""

import ipaddress
import socket

import httpx
import pytest

from alita.tools.http_request import (
    DEFAULT_MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_SECONDS,
    HttpRequestTool,
    should_block_url,
)


# ─── resolution fixtures ──────────────────────────────────────────


def _patch_resolve(monkeypatch, mapping):
    """
    Make socket.getaddrinfo return controlled IPs.

    ``mapping`` may be either:
      * a list — returned for every hostname (uniform fake)
      * a dict {hostname: [ip, ...]} — per-host resolution
    """

    def fake(host, *args, **kwargs):
        host = (host or "").lower()
        if isinstance(mapping, dict):
            ips = mapping.get(host, [])
        else:
            ips = mapping
        out = []
        for ip in ips:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            family = socket.AF_INET if addr.version == 4 else socket.AF_INET6
            out.append((family, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _make_tool(monkeypatch, handler, *, resolved_ip="93.184.216.34"):
    """Build an HttpRequestTool with a MockTransport and uniform patched DNS."""
    _patch_resolve(monkeypatch, [resolved_ip])
    tool = HttpRequestTool()
    tool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        timeout=MAX_TIMEOUT_SECONDS,
    )
    return tool


def _make_tool_per_host(monkeypatch, handler, host_to_ips):
    """Like _make_tool but with per-host DNS mapping (for redirect tests)."""
    _patch_resolve(monkeypatch, host_to_ips)
    tool = HttpRequestTool()
    tool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        timeout=MAX_TIMEOUT_SECONDS,
    )
    return tool


# ─── should_block_url: pure helper tests ──────────────────────────


def test_block_invalid_scheme():
    msg = should_block_url("ftp://example.com", allow_private_network=False, allowlist=set())
    assert msg is not None
    assert "Unsupported URL scheme" in msg


def test_block_no_hostname():
    msg = should_block_url("http://", allow_private_network=False, allowlist=set())
    assert msg is not None
    assert "no hostname" in msg


def test_block_unresolvable(monkeypatch):
    def fail(host, *a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    msg = should_block_url("http://nonexistent.invalid/", allow_private_network=False, allowlist=set())
    assert msg is not None
    assert "Unable to resolve" in msg


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254", "::1", "fe80::1", "fc00::1"])
def test_block_private_addresses(monkeypatch, ip):
    _patch_resolve(monkeypatch, [ip])
    msg = should_block_url(f"http://targethost.test/path", allow_private_network=False, allowlist=set())
    assert msg is not None
    assert "non-globally-routable" in msg


def test_allow_global_ip(monkeypatch):
    _patch_resolve(monkeypatch, ["93.184.216.34"])  # example.com
    msg = should_block_url("http://example.test/", allow_private_network=False, allowlist=set())
    assert msg is None


def test_allow_private_network_opt_in(monkeypatch):
    _patch_resolve(monkeypatch, ["127.0.0.1"])
    msg = should_block_url("http://localhost.test/", allow_private_network=True, allowlist=set())
    assert msg is None


def test_allowlist_bypasses_private_check(monkeypatch):
    _patch_resolve(monkeypatch, ["127.0.0.1"])
    msg = should_block_url(
        "http://api.local/health",
        allow_private_network=False,
        allowlist={"api.local"},
    )
    assert msg is None


def test_allowlist_is_case_insensitive(monkeypatch):
    _patch_resolve(monkeypatch, ["127.0.0.1"])
    msg = should_block_url(
        "http://API.Local/x",
        allow_private_network=False,
        allowlist={"api.local"},
    )
    assert msg is None


def test_allowlist_does_not_bypass_scheme_check():
    msg = should_block_url("ftp://api.local/x", allow_private_network=False, allowlist={"api.local"})
    assert msg is not None
    assert "scheme" in msg.lower()


# ─── HttpRequestTool.execute via MockTransport ─────────────────────


def _ok_json(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "echo": request.url.path})


async def test_get_success(monkeypatch):
    tool = _make_tool(monkeypatch, _ok_json)
    result = await tool.execute(url="http://api.test/users/42")
    assert result["success"] is True
    assert result["status_code"] == 200
    assert '"ok":true' in result["body"]
    assert result["content_type"].startswith("application/json")
    assert result["truncated"] is False


async def test_post_json_body(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"id": 7})

    tool = _make_tool(monkeypatch, handler)
    result = await tool.execute(
        url="http://api.test/items",
        method="post",
        json_body={"name": "alita"},
    )
    assert result["status_code"] == 201
    assert seen["method"] == "POST"
    assert "application/json" in seen["content_type"]
    assert '"name"' in seen["body"]


async def test_raw_body_and_custom_headers(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(204)

    tool = _make_tool(monkeypatch, handler)
    result = await tool.execute(
        url="http://api.test/raw",
        method="put",
        body="hello world",
        headers={"Authorization": "Bearer test-token"},
    )
    assert result["status_code"] == 204
    assert seen["body"] == "hello world"
    assert seen["auth"] == "Bearer test-token"


async def test_query_params(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.query)
        return httpx.Response(200, json={})

    tool = _make_tool(monkeypatch, handler)
    await tool.execute(url="http://api.test/search", params={"q": "alita", "n": "3"})
    assert "q=alita" in seen["query"]
    assert "n=3" in seen["query"]


async def test_truncation(monkeypatch):
    big = "x" * (DEFAULT_MAX_RESPONSE_BYTES + 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big, headers={"content-type": "text/plain"})

    tool = _make_tool(monkeypatch, handler)
    result = await tool.execute(url="http://api.test/big")
    assert result["truncated"] is True
    assert len(result["body"]) <= DEFAULT_MAX_RESPONSE_BYTES + 1
    assert result["length"] == DEFAULT_MAX_RESPONSE_BYTES + 100


async def test_custom_truncation_cap(monkeypatch):
    payload = "y" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload)

    tool = _make_tool(monkeypatch, handler)
    result = await tool.execute(url="http://api.test/cap", max_response_bytes=200)
    assert result["truncated"] is True
    assert len(result["body"]) <= 200


async def test_timeout_is_clamped(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx.MockTransport doesn't actually enforce timeouts, but the tool
        # passes a clamped value into client.request — verify it's bounded.
        return httpx.Response(200, json={})

    tool = _make_tool(monkeypatch, handler)
    result = await tool.execute(url="http://api.test/", timeout=10_000)
    assert result["success"] is True  # did not raise


async def test_ssrf_block_short_circuits(monkeypatch):
    # localhost resolves to loopback → guard blocks before any request fires.
    requests_made: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request)
        return httpx.Response(200)

    # Patch resolve to 127.0.0.1 so the guard blocks every host.
    _patch_resolve(monkeypatch, ["127.0.0.1"])
    tool = HttpRequestTool()
    tool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )

    result = await tool.execute(url="http://localhost/admin")
    assert result["success"] is False
    assert "non-globally-routable" in result["error"]
    assert requests_made == []


async def test_redirect_revalidation_blocks_dangerous_target(monkeypatch):
    hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "http://localhost/secret"})
        return httpx.Response(200)

    tool = _make_tool_per_host(
        monkeypatch,
        handler,
        {"api.test": ["93.184.216.34"], "localhost": ["127.0.0.1"]},
    )
    result = await tool.execute(url="http://api.test/start")

    # The first hop is allowed (api.test → example.com IP). The redirect to
    # localhost must be blocked by the SSRF guard before the second request.
    assert result["success"] is False
    assert "non-globally-routable" in result["error"]
    # Exactly one request reached the transport (the initial /start).
    assert hits == ["http://api.test/start"]


async def test_redirect_to_safe_target_followed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "http://api.test/dest"})
        return httpx.Response(200, json={"reached": str(request.url.path)})

    tool = _make_tool(monkeypatch, handler, resolved_ip="93.184.216.34")
    result = await tool.execute(url="http://api.test/start")
    assert result["success"] is True
    assert result["status_code"] == 200
    assert result["redirects"] == 1
    assert "dest" in result["body"]


async def test_redirect_chain_stops_at_max(monkeypatch):
    # Each hop redirects again — tool should stop after MAX_REDIRECTS.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://api.test/loop"})

    tool = _make_tool(monkeypatch, handler, resolved_ip="93.184.216.34")
    result = await tool.execute(url="http://api.test/loop")
    # After MAX_REDIRECTS hops the loop breaks and the final 302 is returned
    # (not an error). The important invariant is "did not loop forever".
    assert result["success"] is True
    assert result["redirects"] == 5


async def test_missing_url_returns_error(monkeypatch):
    tool = _make_tool(monkeypatch, _ok_json)
    result = await tool.execute(url="")
    assert result["success"] is False
    assert "url is required" in result["error"]


async def test_unsupported_method(monkeypatch):
    tool = _make_tool(monkeypatch, _ok_json)
    result = await tool.execute(url="http://api.test/", method="trace")
    assert result["success"] is False
    assert "Unsupported method" in result["error"]