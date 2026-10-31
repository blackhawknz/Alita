"""
Tests for the security policy: token config, bind guard, auth middleware,
and the host-exec gate helper.
"""

import pytest
from fastapi.testclient import TestClient

from alita.api.security import (
    AuthMiddleware,
    HOST_EXEC_DISABLED_MSG,
    assert_safe_bind,
    configured_token,
    is_loopback,
    should_block_host_exec,
)


# ─── configured_token / is_loopback ───────────────────────────────


def test_configured_token_reads_env(monkeypatch):
    monkeypatch.setenv("ALITA_API_TOKEN", "  secret  ")
    assert configured_token() == "secret"


def test_configured_token_none_when_unset(monkeypatch):
    monkeypatch.delenv("ALITA_API_TOKEN", raising=False)
    assert configured_token() is None


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


# ─── assert_safe_bind ─────────────────────────────────────────────


def test_safe_bind_loopback_always_allowed():
    # No token, no insecure flag — loopback is fine.
    assert_safe_bind("127.0.0.1", token=None, insecure=False)


def test_safe_bind_non_loopback_without_token_exits(capsys):
    with pytest.raises(SystemExit):
        assert_safe_bind("0.0.0.0", token=None, insecure=False)
    err = capsys.readouterr().err
    assert "Refusing to start" in err


def test_safe_bind_non_loopback_with_token_passes():
    assert_safe_bind("0.0.0.0", token="abc", insecure=False)


def test_safe_bind_non_loopback_insecure_warns_but_passes(capsys):
    assert_safe_bind("0.0.0.0", token=None, insecure=True)
    err = capsys.readouterr().err
    assert "WARNING" in err


# ─── AuthMiddleware via TestClient ────────────────────────────────


def test_no_token_allows_anonymous(public_client):
    r = public_client.get("/tools")
    assert r.status_code == 200


def test_token_blocks_unauthenticated(make_app):
    client = TestClient(make_app(token="secret"))
    assert client.get("/tools").status_code == 401


def test_token_blocks_wrong_token(make_app):
    client = TestClient(make_app(token="secret"))
    r = client.get("/tools", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_token_allows_correct_bearer(make_app):
    client = TestClient(make_app(token="secret"))
    r = client.get("/tools", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_public_paths_remain_open_with_token(make_app):
    client = TestClient(make_app(token="secret"))
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    # ...but a protected path still requires the token
    assert client.get("/tools").status_code == 401


def test_auth_error_payload_shape(make_app):
    client = TestClient(make_app(token="secret"))
    r = client.get("/tools")
    assert r.status_code == 401
    assert "detail" in r.json()


# ─── should_block_host_exec ───────────────────────────────────────


def test_block_when_host_exec_not_isolated():
    msg = should_block_host_exec(
        tool_tags=["host_exec"], isolated=False, allow_host_exec=False
    )
    assert msg == HOST_EXEC_DISABLED_MSG


def test_allow_when_isolated():
    assert should_block_host_exec(
        tool_tags=["host_exec"], isolated=True, allow_host_exec=False
    ) is None


def test_allow_when_host_exec_explicitly_permitted():
    assert should_block_host_exec(
        tool_tags=["host_exec"], isolated=False, allow_host_exec=True
    ) is None


def test_non_host_exec_tools_never_blocked():
    assert should_block_host_exec(
        tool_tags=["coding"], isolated=False, allow_host_exec=False
    ) is None
    # Even with no tags at all
    assert should_block_host_exec(
        tool_tags=[], isolated=False, allow_host_exec=False
    ) is None
