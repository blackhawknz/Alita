"""
HTTP Request Tool — stateless HTTP client with SSRF protection.

Provides a general-purpose ``http_request`` tool for the Alita agent that
supports GET/POST/PUT/PATCH/DELETE/HEAD with headers, JSON or raw bodies,
query params, timeouts, and bounded response sizes.

SSRF protection:
    A pure helper ``should_block_url`` resolves the hostname and rejects any
    URL that points at a loopback, private, link-local, or otherwise non-
    globally-routable address. This prevents prompt-injected requests from
    reaching internal services (e.g. cloud metadata endpoints at
    169.254.169.254, the local Alita API itself, internal admin panels).

    Operators can opt out via ``ALITA_ALLOW_PRIVATE_NETWORK=true`` (mirrors
    the ``ALITA_ALLOW_HOST_EXEC`` pattern in :mod:`alita.api.security`).

    Allowlist of permitted hosts can be supplied via the
    ``ALITA_HTTP_ALLOWLIST`` env var (comma-separated); allowlisted hosts
    bypass the private-network check (but still must be valid URLs).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

try:
    import httpx
    HAS_HTTPX = True
except ImportError:  # pragma: no cover - httpx is a declared dependency
    HAS_HTTPX = False


#: Message returned (and surfaced to the model) when an SSRF attempt is blocked.
SSRF_BLOCKED_MSG = (
    "Refused to fetch {url}: host resolves to a non-globally-routable "
    "address (loopback/private/link-local). Set "
    "ALITA_ALLOW_PRIVATE_NETWORK=true to permit, or add the host to "
    "ALITA_HTTP_ALLOWLIST."
)

#: Default cap on response bodies to keep agent context bounded.
DEFAULT_MAX_RESPONSE_BYTES = 50_000

#: Hard ceiling on per-request timeout regardless of caller input.
MAX_TIMEOUT_SECONDS = 60

#: Maximum redirect hops before giving up.
MAX_REDIRECTS = 5

#: Methods the tool will execute.
_ALLOWED_METHODS = {"get", "post", "put", "patch", "delete", "head"}


# ─── SSRF guard (pure helpers, unit-testable) ──────────────────────


def _allow_private_network() -> bool:
    """True if the operator opted into fetching private/loopback hosts."""
    return os.getenv("ALITA_ALLOW_PRIVATE_NETWORK", "false").lower() == "true"


def _allowlist() -> set:
    """Return the set of allowlisted hostnames (lowercased)."""
    raw = os.getenv("ALITA_HTTP_ALLOWLIST", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_global_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    True if ``ip`` is globally routable (i.e. NOT loopback, private,
    link-local, multicast, reserved, or unspecified).
    """
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return True


def _resolve_hosts(hostname: str) -> Tuple[bool, List[str]]:
    """
    Resolve ``hostname`` to a list of IP strings.

    Returns ``(ok, ips)``. When DNS resolution fails, returns
    ``(False, [])`` and the caller should treat the URL as blocked.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, socket.herror, UnicodeError):
        return False, []
    ips: List[str] = []
    for info in infos:
        sockaddr = info[4]
        try:
            ip = sockaddr[0]
        except (IndexError, TypeError):
            continue
        if ip not in ips:
            ips.append(ip)
    return bool(ips), ips


def should_block_url(
    url: str,
    *,
    allow_private_network: Optional[bool] = None,
    allowlist: Optional[set] = None,
) -> Optional[str]:
    """
    Return an error message if this URL must be blocked, else ``None``.

    Checks:
      * URL scheme must be ``http`` or ``https``
      * Hostname must be present and resolvable
      * Every resolved IP must be globally routable, unless the operator
        opted into private-network access or the host is allowlisted.
    """
    if allow_private_network is None:
        allow_private_network = _allow_private_network()
    if allowlist is None:
        allowlist = _allowlist()

    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return f"Invalid URL: {url!r}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return f"Unsupported URL scheme {scheme!r}; only http/https are allowed"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return f"URL has no hostname: {url!r}"

    # Allowlisted hosts skip the private-network check entirely.
    if hostname in allowlist:
        return None

    ok, ips = _resolve_hosts(hostname)
    if not ok:
        return f"Unable to resolve hostname {hostname!r}"

    for ip_str in ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return f"Unparseable resolved address {ip_str!r} for {hostname!r}"
        if not _is_global_ip(ip):
            if allow_private_network:
                continue
            return SSRF_BLOCKED_MSG.format(url=url)

    return None


# ─── Tool class ────────────────────────────────────────────────────


class HttpRequestTool:
    """
    Stateless HTTP client tool with SSRF protection.

    Each call validates the request URL (and every redirect target) against
    :func:`should_block_url` before performing the fetch. Responses are
    truncated to ``max_response_bytes`` to keep agent context bounded.
    """

    def __init__(self, allow_private_network: Optional[bool] = None):
        # Allow tests to inject the policy explicitly; otherwise it is
        # resolved from the environment on every call.
        self._allow_private_network = allow_private_network
        self._client: Optional["httpx.AsyncClient"] = None

    @property
    def definition(self) -> Dict[str, Any]:
        return {
            "name": "http_request",
            "description": (
                "Perform an HTTP request to a public URL. Supports "
                "GET/POST/PUT/PATCH/DELETE/HEAD with headers, JSON or raw "
                "bodies, query params, and bounded response sizes. "
                "Use this to call REST APIs, webhooks, and fetch structured "
                "data. Use the `browser` tool for HTML scraping."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": sorted(_ALLOWED_METHODS),
                        "description": "HTTP method (default: get).",
                        "default": "get",
                    },
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to request.",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Request headers (string→string).",
                        "default": {},
                    },
                    "body": {
                        "type": "string",
                        "description": "Raw request body (string).",
                    },
                    "json_body": {
                        "type": "object",
                        "description": "JSON body; sets Content-Type to application/json when present.",
                    },
                    "params": {
                        "type": "object",
                        "description": "URL query parameters (string→string).",
                        "default": {},
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds (default 30, max {MAX_TIMEOUT_SECONDS}).",
                        "default": 30,
                    },
                    "max_response_bytes": {
                        "type": "integer",
                        "description": f"Truncate response body to this many bytes (default {DEFAULT_MAX_RESPONSE_BYTES}).",
                        "default": DEFAULT_MAX_RESPONSE_BYTES,
                    },
                },
                "required": ["url"],
            },
        }

    def _get_client(self) -> "httpx.AsyncClient":
        if self._client is None and HAS_HTTPX:
            # follow_redirects=False: we control each hop so we can
            # re-validate the SSRF guard against every Location.
            self._client = httpx.AsyncClient(
                timeout=MAX_TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={
                    "User-Agent": "Alita-Agent/1.0 (+https://github.com/alita)",
                    "Accept": "*/*",
                },
            )
        return self._client

    async def execute(
        self,
        url: str = "",
        method: str = "get",
        headers: Optional[Dict[str, str]] = None,
        body: str = "",
        json_body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        **kwargs,
    ) -> Dict[str, Any]:
        if not HAS_HTTPX:
            return {"success": False, "error": "httpx not installed. Run: pip install httpx"}
        if not url:
            return {"success": False, "error": "url is required"}

        m = (method or "get").lower()
        if m not in _ALLOWED_METHODS:
            return {"success": False, "error": f"Unsupported method: {method!r}"}

        # Clamp inputs.
        try:
            timeout_s = max(1, min(int(timeout), MAX_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_s = 30
        try:
            cap = max(1, int(max_response_bytes))
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_RESPONSE_BYTES

        allow_private = self._allow_private_network
        if allow_private is None:
            allow_private = _allow_private_network()

        client = self._get_client()
        current_url = url
        hops = 0
        request_headers: Dict[str, str] = dict(headers or {})
        # Caller-supplied headers win over the client defaults.
        merged_headers: Dict[str, str] = {}
        merged_headers.update(request_headers)

        # Body resolution: json_body takes precedence when provided.
        json_payload: Optional[Dict[str, Any]] = json_body if json_body is not None else None
        content_payload: Optional[bytes] = None
        if json_payload is None and body:
            content_payload = body.encode("utf-8", errors="replace")

        while True:
            block = should_block_url(current_url, allow_private_network=allow_private)
            if block:
                return {"success": False, "error": block, "url": current_url}

            try:
                response = await client.request(
                    method=m,
                    url=current_url,
                    headers=merged_headers or None,
                    params=params or None,
                    json=json_payload,
                    content=content_payload,
                    timeout=timeout_s,
                )
            except httpx.TimeoutException:
                return {"success": False, "error": f"Request timed out after {timeout_s}s", "url": current_url}
            except httpx.RequestError as exc:
                return {"success": False, "error": f"Request failed: {exc}", "url": current_url}

            # Handle redirects manually so each Location is SSRF-checked.
            if response.is_redirect and hops < MAX_REDIRECTS:
                location = response.headers.get("location")
                if not location:
                    break
                # Resolve relative redirects against the current URL.
                current_url = urljoin(current_url, location)
                # Redirects may carry fresh query strings; clear the original
                # params so they aren't re-appended to the redirect target.
                params = None
                hops += 1
                continue

            break

        # Read body with truncation.
        raw = response.content or b""
        truncated = len(raw) > cap
        if truncated:
            raw = raw[:cap]
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        # Return a focused subset of headers to keep context tight.
        safe_headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            in {"content-type", "content-length", "date", "etag", "last-modified", "server", "location"}
        }

        return {
            "success": True,
            "url": str(response.url),
            "method": m.upper(),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "headers": safe_headers,
            "body": text,
            "length": len(response.content or b""),
            "truncated": truncated,
            "redirects": hops,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
