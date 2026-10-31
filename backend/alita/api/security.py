"""
Security policy for the Alita API.

Three concerns live here so they can be tested in isolation:

1. **Opt-in bearer-token auth** — when ``ALITA_API_TOKEN`` is set, every non-public
   endpoint requires ``Authorization: Bearer <token>``. When it is unset, no auth
   is enforced (safe because the server binds to loopback by default).
2. **Fail-closed bind guard** — refuse to start on a non-loopback interface without
   a token unless ``ALITA_ALLOW_INSECURE_BIND=true``.
3. **Host-execution gate** — pure helper deciding whether a ``host_exec`` tool may
   run given sandbox isolation and the ``ALITA_ALLOW_HOST_EXEC`` flag.

Auth is enforced as middleware so it covers REST endpoints and SSE streams alike.
"""

import hmac
import os
import sys
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Paths reachable without a token (service discovery + health monitoring).
_PUBLIC_PATHS = {"/", "/health"}

#: Message returned (and shown to the model) when a host-exec tool is blocked.
HOST_EXEC_DISABLED_MSG = (
    "Host execution disabled: this tool would run on the host without an "
    "isolated Docker sandbox. Start Docker, or set "
    "ALITA_ALLOW_HOST_EXEC=true to permit host execution."
)


# ─── Token + bind policy ──────────────────────────────────────────

def configured_token() -> Optional[str]:
    """Return the configured API token, or ``None`` when auth is disabled."""
    token = os.getenv("ALITA_API_TOKEN", "").strip()
    return token or None


def is_loopback(host: str) -> bool:
    """True if ``host`` binds only to the local machine."""
    return host in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def allow_insecure_bind() -> bool:
    """True if the operator explicitly accepted an unauthenticated network bind."""
    return os.getenv("ALITA_ALLOW_INSECURE_BIND", "false").lower() == "true"


def assert_safe_bind(
    host: str,
    token: Optional[str] = None,
    insecure: Optional[bool] = None,
) -> None:
    """
    Refuse to start on a non-loopback interface without authentication.

    ``/tools/execute`` runs arbitrary shell on the host, so exposing it
    unauthenticated on the network is unsafe. Either bind loopback, set a token,
    or explicitly opt in via ``ALITA_ALLOW_INSECURE_BIND=true``.

    Exits the process (non-zero) when the bind is unsafe.
    """
    if is_loopback(host):
        return
    if token:
        return
    if insecure is None:
        insecure = allow_insecure_bind()
    if insecure:
        sys.stderr.write(
            f"\n⚠️  WARNING: Alita is binding to a non-loopback interface ({host}) "
            "without authentication. POST /tools/execute will be reachable from "
            "the network. Set ALITA_API_TOKEN for safety.\n\n"
        )
        return

    sys.stderr.write(
        f"\n🛑 Refusing to start: ALITA_HOST={host} is non-loopback but no "
        "ALITA_API_TOKEN is configured. The API can execute arbitrary shell on "
        "your host, so exposing it unauthenticated is unsafe.\n"
        "Options:\n"
        "  • bind loopback instead (ALITA_HOST=127.0.0.1), or\n"
        "  • set a token (ALITA_API_TOKEN=...), or\n"
        "  • explicitly accept the risk (ALITA_ALLOW_INSECURE_BIND=true).\n\n"
    )
    sys.exit(1)


# ─── Host-execution gate ──────────────────────────────────────────

def should_block_host_exec(
    *,
    tool_tags,
    isolated: bool,
    allow_host_exec: bool,
) -> Optional[str]:
    """
    Return an error message if this tool call must be blocked, else ``None``.

    A tool tagged ``host_exec`` is allowed only when it runs inside an isolated
    Docker sandbox (``isolated=True``) or the operator opted into host execution
    (``allow_host_exec=True``). Keeps the gate logic unit-testable without an
    ``AlitaAgent`` instance.
    """
    if "host_exec" not in (tool_tags or ()):
        return None
    if allow_host_exec:
        return None
    if isolated:
        return None
    return HOST_EXEC_DISABLED_MSG


# ─── Auth middleware ──────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce bearer-token auth on every non-public path when a token is set.

    No-op when no token is configured. The token is resolved at registration
    time from the environment, but may be passed explicitly for tests.
    """

    def __init__(self, app, token: Optional[str] = None):
        super().__init__(app)
        # When ``token`` is None we re-read the env each request so that tests
        # which monkeypatch the env after app construction behave as expected.
        self._explicit_token = token

    async def dispatch(self, request: Request, call_next):
        token = self._explicit_token
        if token is None:
            token = configured_token()
        if not token:
            return await call_next(request)
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(value.strip(), token):
            return await call_next(request)
        # <img>/<a> requests can't set headers; allow ?token= for GETs.
        query_token = request.query_params.get("token", "")
        if request.method == "GET" and query_token and hmac.compare_digest(query_token, token):
            return await call_next(request)

        return JSONResponse(
            {"detail": "Unauthorized: missing or invalid bearer token."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
