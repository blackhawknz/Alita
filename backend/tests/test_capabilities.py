"""
Tests for capability-based execute_code approval.

Part G replaces the blocklist-only check with static capability analysis.
The analyser itself is tested directly; the decision table is tested through
ApprovalPolicy.classify (the public surface the agent uses).
"""

from pathlib import Path

import pytest

from alita.core import capabilities
from alita.core.approval import ApprovalPolicy
from alita.core.capabilities import (
    CAP_ENV,
    CAP_EVAL,
    CAP_FS_DELETE,
    CAP_FS_READ,
    CAP_FS_WRITE,
    CAP_NETWORK,
    CAP_PROCESS,
    MAX_CAPABILITIES,
    analyse,
)


# ─── Analyser: Python capabilities ─────────────────────────────────


def test_plain_computation_has_no_capabilities():
    result = analyse("print(sum(range(10)))", "python")
    assert result.capabilities == set()
    assert result.failed is False


@pytest.mark.parametrize("code", [
    'open("x.txt")',
    'open("x.txt", "r")',
    'open("x.txt", "rb")',
])
def test_open_read_modes_are_read(code):
    result = analyse(code, "python")
    assert CAP_FS_READ in result.capabilities
    assert CAP_FS_WRITE not in result.capabilities


@pytest.mark.parametrize("code", [
    'open("x.txt", "w")',
    'open("x.txt", "a")',
    'open("x.txt", "x")',
    'open("x.txt", "r+")',
    'open("x.txt", mode="wb")',
])
def test_open_write_modes_are_write(code):
    result = analyse(code, "python")
    assert CAP_FS_WRITE in result.capabilities


def test_open_non_literal_mode_fails_closed_to_write():
    result = analyse('mode = "w"; open("x.txt", mode)', "python")
    assert CAP_FS_WRITE in result.capabilities


@pytest.mark.parametrize("code", [
    'from pathlib import Path; Path("x").write_text("a")',
    'from pathlib import Path; Path("x").write_bytes(b"a")',
    'from pathlib import Path; Path("x").mkdir()',
    'from pathlib import Path; Path("x").rename("y")',
])
def test_pathlib_write_methods(code):
    result = analyse(code, "python")
    assert CAP_FS_WRITE in result.capabilities


@pytest.mark.parametrize("code", [
    'from pathlib import Path; Path("x").unlink()',
    'from pathlib import Path; Path("x").rmdir()',
])
def test_pathlib_delete_methods(code):
    result = analyse(code, "python")
    assert CAP_FS_DELETE in result.capabilities
    assert CAP_FS_WRITE not in result.capabilities


def test_pathlib_write_on_local_variable_is_write_with_unknown_path():
    result = analyse('from pathlib import Path; p = Path("x"); p.write_text("a")', "python")
    assert CAP_FS_WRITE in result.capabilities
    assert result.unknown_write_path is True


def test_shutil_rmtree_is_delete():
    result = analyse('import shutil; shutil.rmtree("build")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert CAP_FS_WRITE not in result.capabilities
    assert "build" in result.delete_paths


def test_os_read_operations():
    for code in ('import os; os.listdir(".")',
                 'import os; os.walk(".")',
                 'import glob; glob.glob("*.py")'):
        result = analyse(code, "python")
        assert CAP_FS_READ in result.capabilities, code


def test_aliased_import_attribute():
    result = analyse('import os as o; o.remove("x")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert "x" in result.delete_paths


def test_aliased_from_import():
    result = analyse('from os import remove as rm; rm("x")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert "x" in result.delete_paths


def test_bare_from_import_is_detected():
    result = analyse('from os import unlink; unlink("x")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert "x" in result.delete_paths


def test_rebound_module_alias_is_detected():
    result = analyse('import os; m = os; m.remove("x")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert "x" in result.delete_paths


@pytest.mark.parametrize("code,path", [
    ('import os\nos.remove("x")', "x"),
    ('import os as o\no.remove("x")', "x"),
    ('import os as o\no.unlink("x")', "x"),
    ('import shutil as sh\nsh.rmtree("x")', "x"),
    ('from shutil import rmtree\nrmtree("x")', "x"),
    ('from os import remove as rm\nrm("x")', "x"),
    ('from os import remove\nremove("x")', "x"),
    ('from os import unlink as u\nu("x")', "x"),
    ('import os\nos.rmdir("x")', "x"),
    ('import os\nos.removedirs("x")', "x"),
    ('import pathlib\npathlib.Path("x").unlink()', "x"),
    ('from pathlib import Path\nPath("x").rmdir()', "x"),
])
def test_fs_delete_detected_for_all_forms(code, path):
    result = analyse(code, "python")
    assert CAP_FS_DELETE in result.capabilities
    assert CAP_FS_WRITE not in result.capabilities
    assert path in result.delete_paths


def test_unknown_receiver_delete_is_delete_with_unknown_path():
    result = analyse('import os; p = "x"; os.remove(p)', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert result.unknown_delete_path is True


def test_shutil_move_across_paths_is_delete_and_write():
    result = analyse('import shutil; shutil.move("a.txt", "b.txt")', "python")
    assert CAP_FS_DELETE in result.capabilities
    assert CAP_FS_WRITE in result.capabilities
    assert "a.txt" in result.delete_paths
    assert "b.txt" in result.write_paths


def test_shutil_move_same_literal_path_is_not_delete():
    result = analyse('import shutil; shutil.move("a.txt", "a.txt")', "python")
    assert CAP_FS_DELETE not in result.capabilities
    assert CAP_FS_WRITE in result.capabilities


def test_shutil_move_unknown_destination_fails_closed_to_delete():
    result = analyse(
        'import shutil; src = "a.txt"; shutil.move(src, "b.txt")', "python"
    )
    assert CAP_FS_DELETE in result.capabilities
    assert CAP_FS_WRITE in result.capabilities


@pytest.mark.parametrize("code", [
    'import subprocess; subprocess.run(["cmd"])',
    'import os; os.system("echo hi")',
    'import ctypes; ctypes.windll.x()',
    'import os; os.execv("/bin/sh", ["sh"])',
])
def test_process_capability(code):
    assert CAP_PROCESS in analyse(code, "python").capabilities


@pytest.mark.parametrize("code", [
    'import socket; socket.socket()',
    'import requests; requests.post("http://example.com")',
    'import urllib.request; urllib.request.urlopen("http://example.com")',
    'import http.client; http.client.HTTPConnection("example.com")',
])
def test_network_capability(code):
    assert CAP_NETWORK in analyse(code, "python").capabilities


@pytest.mark.parametrize("code", [
    'eval("1+1")',
    'exec("x = 1")',
    'compile("x = 1", "<s>", "exec")',
    '__import__("os")',
    'import importlib; importlib.import_module("os")',
])
def test_eval_capability(code):
    assert CAP_EVAL in analyse(code, "python").capabilities


@pytest.mark.parametrize("code", [
    'import os; print(os.environ["PATH"])',
    'import os; os.getenv("PATH")',
    'import getpass; getpass.getpass()',
])
def test_env_capability(code):
    assert CAP_ENV in analyse(code, "python").capabilities


# ─── Analyser: path extraction ─────────────────────────────────────


def test_literal_write_path_is_extracted():
    result = analyse(r'open(r"C:\Users\x\Documents\important.docx", "w")', "python")
    assert result.write_paths == {r"C:\Users\x\Documents\important.docx"}
    assert result.unknown_write_path is False


def test_os_path_join_of_literals_is_extracted():
    result = analyse('import os; open(os.path.join("a", "b.txt"), "w")', "python")
    assert result.write_paths == {"a\\b.txt"}


def test_variable_write_path_is_unknown():
    result = analyse('p = "x.txt"; open(p, "w")', "python")
    assert CAP_FS_WRITE in result.capabilities
    assert result.unknown_write_path is True


# ─── Analyser: fail closed ─────────────────────────────────────────


def test_unparseable_python_is_maximally_capable():
    result = analyse("def broken(:\n", "python")
    assert result.failed is True
    assert set(result.capabilities) == set(MAX_CAPABILITIES)
    assert result.unknown_write_path is True


def test_unknown_language_is_maximally_capable():
    result = analyse("puts 1", "ruby")
    assert result.failed is True
    assert set(result.capabilities) == set(MAX_CAPABILITIES)


def test_internal_analyser_exception_is_maximally_capable(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("analyser exploded")

    monkeypatch.setattr(capabilities.ast, "parse", boom)
    result = analyse("print(1)", "python")
    assert result.failed is True
    assert set(result.capabilities) == set(MAX_CAPABILITIES)


# ─── Analyser: other languages ─────────────────────────────────────


def test_javascript_write_and_process_patterns():
    assert CAP_FS_WRITE in analyse(
        'const fs = require("fs"); fs.writeFileSync("x", "y")', "javascript"
    ).capabilities
    assert CAP_PROCESS in analyse(
        'require("child_process").exec("whoami")', "javascript"
    ).capabilities


def test_bash_is_always_process():
    assert CAP_PROCESS in analyse("echo hi", "bash").capabilities


def test_powershell_patterns():
    caps = analyse("Invoke-WebRequest https://example.com", "powershell").capabilities
    assert CAP_NETWORK in caps
    assert CAP_PROCESS in caps


# ─── Decision table ────────────────────────────────────────────────


def _policy(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ApprovalPolicy(mode="auto", workspace_dir=str(workspace))


def test_decision_none_requires_no_approval(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": "print(sum(range(10)))"}
    )
    assert decision.needs_approval is False


def test_decision_fs_read_only_requires_no_approval(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": 'import os; print(os.listdir("."))'}
    )
    assert decision.needs_approval is False


def test_decision_fs_write_inside_workspace_requires_no_approval(tmp_path):
    target = tmp_path / "workspace" / "safe.txt"
    code = f'open(r"{target}", "w").write("hi")'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is False


def test_decision_relative_write_requires_no_approval(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": 'open("safe.txt", "w").write("hi")'}
    )
    assert decision.needs_approval is False


def test_decision_fs_write_outside_requires_danger_approval(tmp_path):
    outside = Path(tmp_path.anchor) / "alita_outside_test" / "important.docx"
    code = f'open(r"{outside}", "w").write("")'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert str(outside) in decision.reason
    assert "outside the workspace" in decision.reason


def test_decision_unknown_write_destination_requires_danger_approval(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": 'p = "x"; open(p, "w")'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert "unknown destination" in decision.reason


@pytest.mark.parametrize("code,severity", [
    ('import subprocess; subprocess.run(["cmd"])', "danger"),
    ('eval("1")', "danger"),
    ('import socket; socket.socket()', "warning"),
    ('import os; os.getenv("X")', "warning"),
])
def test_decision_capability_rows(tmp_path, code, severity):
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is True
    assert decision.severity == severity


def test_decision_unparseable_and_unknown_language_are_danger(tmp_path):
    policy = _policy(tmp_path)
    for params in (
        {"code": "def broken(:\n"},
        {"code": "puts 1", "language": "ruby"},
    ):
        decision = policy.classify("execute_code", params)
        assert decision.needs_approval is True
        assert decision.severity == "danger"


# ─── Deletion and overwrite decisions (Part G1) ────────────────────


def test_delete_alias_table_every_row_prompts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "keep.txt"
    target.write_text("keep me")
    tree = workspace / "build"
    tree.mkdir()
    policy = _policy(tmp_path)

    codes = [
        f'import os\nos.remove({str(target)!r})',
        f'import os as o\no.remove({str(target)!r})',
        f'import os as o\no.unlink({str(target)!r})',
        f'import shutil as sh\nsh.rmtree({str(tree)!r})',
        f'from shutil import rmtree\nrmtree({str(tree)!r})',
        f'import os\nos.open({str(target)!r}, os.O_WRONLY | os.O_TRUNC)',
        f'from pathlib import Path\nPath({str(target)!r}).write_text("")',
    ]
    for code in codes:
        decision = policy.classify("execute_code", {"code": code})
        assert decision.needs_approval is True, code
        assert decision.severity == "danger", code


def test_delete_inside_workspace_prompts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "keep.txt"
    target.write_text("keep me")
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": f'import os as o\no.remove({str(target)!r})'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert str(target) in decision.reason


def test_shutil_move_across_workspace_paths_prompts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "a.txt"
    source.write_text("a")
    destination = workspace / "b.txt"
    code = f'import shutil\nshutil.move({str(source)!r}, {str(destination)!r})'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert str(source) in decision.reason


def test_new_file_inside_workspace_still_frictionless(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "brand-new.txt"
    assert not target.exists()
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": f'open({str(target)!r}, "w").write("hi")'}
    )
    assert decision.needs_approval is False


def test_new_file_via_os_open_inside_workspace_still_frictionless(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "brand-new-os.txt"
    assert not target.exists()
    code = f'import os\nos.open({str(target)!r}, os.O_WRONLY | os.O_CREAT)'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is False


def test_overwriting_existing_file_inside_workspace_prompts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "existing.txt"
    target.write_text("original")
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": f'open({str(target)!r}, "w").write("new")'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert "overwrite existing files" in decision.reason
    assert str(target) in decision.reason


def test_overwriting_existing_file_via_path_write_text_prompts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "existing.txt"
    target.write_text("original")
    code = f'from pathlib import Path\nPath({str(target)!r}).write_text("new")'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is True
    assert decision.severity == "danger"


def test_delete_outside_workspace_still_prompts(tmp_path):
    outside = Path(tmp_path.anchor) / "alita_outside_delete_test" / "x.txt"
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": f'import os\nos.remove({str(outside)!r})'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"


# ─── Regression: the original hole is closed ───────────────────────


def test_regression_original_hole_requires_approval(tmp_path):
    code = r'open(r"C:\Users\x\Documents\important.docx", "w").write("")'
    decision = _policy(tmp_path).classify("execute_code", {"code": code})
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert r"C:\Users\x\Documents\important.docx" in decision.reason


def test_regression_workspace_write_still_frictionless(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": 'open("safe.txt", "w").write("hi")'}
    )
    assert decision.needs_approval is False


def test_regression_plain_computation_still_frictionless(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": "print(sum(range(10)))"}
    )
    assert decision.needs_approval is False


def test_destructive_blocklist_still_an_extra_trigger(tmp_path):
    decision = _policy(tmp_path).classify(
        "execute_code", {"code": 'import shutil; shutil.rmtree("build")'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"
    assert "destructive pattern" in decision.reason


# ─── Settings ──────────────────────────────────────────────────────


def test_capability_gating_off_restores_blocklist_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_EXECUTE_CODE_CAPABILITIES", "false")
    outside = Path(tmp_path.anchor) / "alita_outside_test" / "important.docx"
    policy = ApprovalPolicy(mode="auto", workspace_dir=str(tmp_path))
    decision = policy.classify(
        "execute_code", {"code": f'open(r"{outside}", "w").write("")'}
    )
    assert decision.needs_approval is False

    # The old destructive blocklist still fires when capability gating is off.
    decision = policy.classify(
        "execute_code", {"code": 'import shutil; shutil.rmtree("build")'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "danger"


def test_strict_mode_gates_read_only_access(tmp_path, monkeypatch):
    monkeypatch.setenv("ALITA_APPROVAL_EXECUTE_CODE_STRICT", "true")
    policy = ApprovalPolicy(mode="auto", workspace_dir=str(tmp_path))
    decision = policy.classify(
        "execute_code", {"code": 'open("x.txt")'}
    )
    assert decision.needs_approval is True
    assert decision.severity == "warning"

    # Strict still leaves pure computation frictionless.
    decision = policy.classify(
        "execute_code", {"code": "print(1 + 1)"}
    )
    assert decision.needs_approval is False
