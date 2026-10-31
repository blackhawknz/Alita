"""
Tests for SandboxManager: work_dir validation, isolation classification, and
that work_dir is threaded as a real cwd (never interpolated into the shell).
"""

import sys

import pytest

from alita.tools.sandbox import SandboxManager, run_process


# ─── _validate_work_dir ───────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("src", "src"),
        ("src/foo", "src/foo"),
        ("src\\foo", "src/foo"),     # backslash normalized
        ("src/foo/", "src/foo"),     # trailing slash stripped
        (" a/b ", "a/b"),            # whitespace trimmed
    ],
)
def test_validate_work_dir_accepts_clean(raw, expected):
    assert SandboxManager._validate_work_dir(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../etc",          # parent traversal
        "foo/../bar",      # mid-path traversal
        "/etc",            # absolute (posix-style)
        "\\etc",           # absolute (windows-style, leading backslash)
        "C:\\Users",       # windows drive
        "foo; rm -rf /",   # shell metachar (command injection attempt)
        "foo$(whoami)",    # command substitution
        "foo`id`",         # backtick substitution
        "foo|cat",         # pipe
    ],
)
def test_validate_work_dir_rejects_unsafe(raw):
    with pytest.raises(ValueError):
        SandboxManager._validate_work_dir(raw)


# ─── is_isolated ──────────────────────────────────────────────────


def test_is_isolated_tracks_docker_for_terminal():
    mgr = SandboxManager()
    mgr._docker_available = True
    assert mgr.is_isolated("terminal") is True
    mgr._docker_available = False
    assert mgr.is_isolated("terminal") is False


def test_is_isolated_execute_code_never_isolated():
    mgr = SandboxManager()
    mgr._docker_available = True  # even with Docker available
    assert mgr.is_isolated("execute_code") is False


def test_is_isolated_other_tools_safe_by_default():
    mgr = SandboxManager()
    mgr._docker_available = False
    assert mgr.is_isolated("file_ops") is True
    assert mgr.is_isolated("memory") is True


# ─── timeout partial output ───────────────────────────────────────


def test_run_process_returns_unflushed_stdout_on_timeout():
    script = "print('a'); import time; time.sleep(30); print('b')"
    result = run_process([sys.executable, "-c", script], timeout=2, max_output=10_000)
    assert result["timed_out"] is True
    assert result["success"] is False
    assert "a" in result["stdout"]
    assert "timed out" in result["error"].lower()


def test_run_process_returns_partial_stderr_on_timeout():
    script = "import sys, time; sys.stderr.write('ERR-LINE\\n'); time.sleep(30)"
    result = run_process([sys.executable, "-c", script], timeout=2, max_output=10_000)
    assert result["timed_out"] is True
    assert "ERR-LINE" in result["stderr"]


# ─── execute threads work_dir, rejects traversal ──────────────────


def _force_subprocess(mgr, capture):
    """Replace _execute_subprocess with a recorder so no real command runs."""
    def fake(workdir, command, timeout, env=None, work_dir=None):
        capture["workdir"] = workdir
        capture["work_dir"] = work_dir
        capture["command"] = command
        return {"success": True, "stdout": "", "stderr": "", "exit_code": 0}

    mgr._execute_subprocess = fake


def test_execute_passes_valid_work_dir(tmp_path):
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    sb = mgr.create_sandbox()

    captured = {}
    _force_subprocess(mgr, captured)

    mgr.execute(sb["sandbox_id"], "ls -la", work_dir="sub/dir")
    assert captured["work_dir"] == "sub/dir"
    # command is passed through verbatim — no `cd ... &&` interpolation
    assert captured["command"] == "ls -la"


def test_execute_rejects_traversal_work_dir(tmp_path):
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    sb = mgr.create_sandbox()

    captured = {}
    _force_subprocess(mgr, captured)

    with pytest.raises(ValueError):
        mgr.execute(sb["sandbox_id"], "ls", work_dir="../../etc")
    assert captured == {}  # nothing executed


def test_execute_rejects_injection_work_dir(tmp_path):
    mgr = SandboxManager(workspace_dir=str(tmp_path))
    mgr._docker_available = False
    sb = mgr.create_sandbox()

    captured = {}
    _force_subprocess(mgr, captured)

    with pytest.raises(ValueError):
        # Classic injection payload — must be rejected, never threaded as cwd
        mgr.execute(sb["sandbox_id"], "echo hi", work_dir="x; rm -rf /")
    assert captured == {}
