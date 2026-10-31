"""Tests for FileOps: path confinement, nested checkpoints, patch uniqueness, list/search, paging."""

import os

import pytest

from alita.tools.file_ops import FileOps, is_within, resolve_within


@pytest.fixture
def fops(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return FileOps(str(ws))


# ─── Traversal ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["../secret.txt", "a/../../secret.txt", "..\\secret.txt"])
async def test_traversal_denied(fops, bad):
    (fops.workspace.parent / "secret.txt").write_text("top secret", encoding="utf-8")
    res = await fops.execute(action="read", path=bad)
    assert res["success"] is False
    assert "traversal" in res["error"].lower()


async def test_sibling_prefix_dir_is_not_inside_workspace(fops):
    # "ws_evil" shares the "ws" prefix; str.startswith would wrongly allow it.
    evil = fops.workspace.parent / (fops.workspace.name + "_evil")
    evil.mkdir()
    (evil / "x.txt").write_text("nope", encoding="utf-8")
    res = await fops.execute(action="read", path=str(evil / "x.txt"))
    assert res["success"] is False
    assert "traversal" in res["error"].lower()


async def test_absolute_path_inside_workspace_allowed(fops):
    target = fops.workspace / "in.txt"
    target.write_text("ok", encoding="utf-8")
    res = await fops.execute(action="read", path=str(target))
    assert res["success"] is True
    assert res["content"] == "ok"


def test_is_within_case_insensitive_on_windows(tmp_path):
    base = tmp_path.resolve()
    child = base / "Sub" / "f.txt"
    assert is_within(child, base)
    if os.name == "nt":
        assert is_within(type(child)(str(child).upper()), base)
    with pytest.raises(ValueError):
        resolve_within(base, "../outside")


async def test_unknown_kwargs_ignored(fops):
    res = await fops.execute(action="list", bogus_param=1)
    assert res["success"] is True


# ─── Checkpoint / rollback with nested paths ──────────────────────


async def test_write_rollback_restores_nested_file_in_place(fops):
    nested = fops.workspace / "src" / "pkg" / "mod.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("original\n", encoding="utf-8")

    res = await fops.execute(action="write", path="src/pkg/mod.py", content="changed\n")
    assert res["success"] is True
    cid = res["checkpoint_id"]
    assert nested.read_text(encoding="utf-8") == "changed\n"

    rb = await fops.execute(action="rollback", checkpoint_id=cid)
    assert rb["success"] is True
    assert nested.read_text(encoding="utf-8") == "original\n"
    # Nothing was restored to the workspace root by basename.
    assert not (fops.workspace / "mod.py").exists()


async def test_rollback_removes_file_created_after_checkpoint(fops):
    res = await fops.execute(action="write", path="new/dir/file.txt", content="x")
    assert res["success"] is True and res["created"] is True
    rb = await fops.execute(action="rollback", checkpoint_id=res["checkpoint_id"])
    assert rb["success"] is True
    assert not (fops.workspace / "new" / "dir" / "file.txt").exists()


async def test_directory_checkpoint_preserves_structure_and_skips_ignored(fops):
    (fops.workspace / "a" / "b").mkdir(parents=True)
    (fops.workspace / "a" / "b" / "one.txt").write_text("1", encoding="utf-8")
    (fops.workspace / "a" / "two.txt").write_text("2", encoding="utf-8")
    (fops.workspace / "a" / "node_modules").mkdir()
    (fops.workspace / "a" / "node_modules" / "big.js").write_text("junk", encoding="utf-8")

    res = await fops.execute(action="checkpoint", path="a")
    assert res["success"] is True
    cid = res["checkpoint_id"]
    files = fops.checkpointer.describe(cid)
    assert sorted(files) == ["a/b/one.txt", "a/two.txt"]

    (fops.workspace / "a" / "b" / "one.txt").write_text("changed", encoding="utf-8")
    (fops.workspace / "a" / "two.txt").unlink()
    await fops.execute(action="rollback", checkpoint_id=cid)
    assert (fops.workspace / "a" / "b" / "one.txt").read_text(encoding="utf-8") == "1"
    assert (fops.workspace / "a" / "two.txt").read_text(encoding="utf-8") == "2"


async def test_workspace_checkpoint_never_copies_ignored_dirs(fops):
    (fops.workspace / "keep.txt").write_text("k", encoding="utf-8")
    for d in (".git", "venv", "node_modules", ".sandboxes"):
        (fops.workspace / d).mkdir()
        (fops.workspace / d / "f").write_text("x", encoding="utf-8")
    res = await fops.execute(action="checkpoint", path="")
    assert res["success"] is True
    assert fops.checkpointer.describe(res["checkpoint_id"]) == ["keep.txt"]


async def test_rollback_rejects_bad_checkpoint_id(fops):
    res = await fops.execute(action="rollback", checkpoint_id="../../etc")
    assert res["success"] is False


async def test_delete_is_checkpointed_and_files_only(fops):
    (fops.workspace / "d").mkdir()
    (fops.workspace / "d" / "f.txt").write_text("data", encoding="utf-8")
    res = await fops.execute(action="delete", path="d")
    assert res["success"] is False
    res = await fops.execute(action="delete", path="d/f.txt")
    assert res["success"] is True
    assert not (fops.workspace / "d" / "f.txt").exists()
    await fops.execute(action="rollback", checkpoint_id=res["checkpoint_id"])
    assert (fops.workspace / "d" / "f.txt").read_text(encoding="utf-8") == "data"


# ─── Patch uniqueness ─────────────────────────────────────────────


async def test_patch_requires_unique_match(fops):
    (fops.workspace / "p.txt").write_text("foo\nfoo\nbar\n", encoding="utf-8")
    res = await fops.execute(action="patch", path="p.txt", old_string="foo", new_string="baz")
    assert res["success"] is False
    assert "2 times" in res["error"]
    assert res["matches"] == 2

    res = await fops.execute(action="patch", path="p.txt", old_string="nope", new_string="x")
    assert res["success"] is False
    assert res["matches"] == 0


async def test_patch_unique_and_replace_all(fops):
    f = fops.workspace / "p.txt"
    f.write_text("foo\nfoo\nbar\n", encoding="utf-8")
    res = await fops.execute(action="patch", path="p.txt", old_string="bar", new_string="qux")
    assert res["success"] is True
    assert f.read_text(encoding="utf-8") == "foo\nfoo\nqux\n"

    res = await fops.execute(action="patch", path="p.txt", old_string="foo", new_string="z", replace_all=True)
    assert res["success"] is True
    assert res["replacements"] == 2
    assert f.read_text(encoding="utf-8") == "z\nz\nqux\n"


async def test_patch_preserves_lf_line_endings(fops):
    f = fops.workspace / "lf.txt"
    f.write_bytes(b"a\nb\nc\n")
    await fops.execute(action="patch", path="lf.txt", old_string="b\nc", new_string="B\nC")
    assert f.read_bytes() == b"a\nB\nC\n"


async def test_patch_lf_old_string_matches_crlf_file(fops):
    f = fops.workspace / "crlf.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = await fops.execute(action="patch", path="crlf.txt", old_string="a\nb", new_string="x\ny")
    assert res["success"] is True
    assert f.read_bytes() == b"x\r\ny\r\nc\r\n"


async def test_patch_fuzzy_whitespace_match(fops):
    f = fops.workspace / "fuzzy.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    res = await fops.execute(
        action="patch",
        path="fuzzy.py",
        old_string="def foo():\n  return 1",
        new_string="def foo():\n    return 2",
    )
    assert res["success"] is True and res["fuzzy"] is True
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 2\n"


async def test_patch_fuzzy_refuses_ambiguous_match(fops):
    f = fops.workspace / "ambiguous.txt"
    f.write_text("foo    bar\nfoo\tbar\n", encoding="utf-8")
    res = await fops.execute(action="patch", path="ambiguous.txt", old_string="foo bar", new_string="baz")
    assert res["success"] is False
    assert res["matches"] == 2
    assert f.read_text(encoding="utf-8") == "foo    bar\nfoo\tbar\n"


async def test_patch_reports_closest_region(fops):
    f = fops.workspace / "close.txt"
    f.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    res = await fops.execute(action="patch", path="close.txt", old_string="betta", new_string="x")
    assert res["success"] is False
    assert res["matches"] == 0
    assert res["closest"]["line"] == 2
    assert "beta" in res["closest"]["text"]
    assert "hint" in res


async def test_patch_crlf_new_string_is_not_double_converted(fops):
    f = fops.workspace / "no-double.txt"
    f.write_bytes(b"a\r\nb\r\nc\r\n")
    res = await fops.execute(action="patch", path="no-double.txt", old_string="a\nb", new_string="x\r\ny")
    assert res["success"] is True
    assert f.read_bytes() == b"x\r\ny\r\nc\r\n"


# ─── Read paging ──────────────────────────────────────────────────


async def test_read_paging_with_offset_and_limit(fops):
    (fops.workspace / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 101)), encoding="utf-8")
    res = await fops.execute(action="read", path="big.txt", offset=10, limit=5)
    assert res["success"] is True
    assert res["content"] == "".join(f"line {i}\n" for i in range(10, 15))
    assert res["lines"] == 100
    assert res["truncated"] is True
    assert res["next_offset"] == 15


async def test_read_size_cap(fops):
    (fops.workspace / "huge.txt").write_text(("x" * 999 + "\n") * 400, encoding="utf-8")
    res = await fops.execute(action="read", path="huge.txt")
    assert res["success"] is True
    assert len(res["content"]) <= 200_000
    assert res["truncated"] is True
    assert res["next_offset"] > 1


async def test_read_rejects_binary(fops):
    (fops.workspace / "b.bin").write_bytes(b"\x00\x01\x02binary")
    res = await fops.execute(action="read", path="b.bin")
    assert res["success"] is False
    assert "binary" in res["error"]


# ─── List / search / mkdir ────────────────────────────────────────


async def test_list_with_pattern(fops):
    (fops.workspace / "a.py").write_text("", encoding="utf-8")
    (fops.workspace / "b.txt").write_text("", encoding="utf-8")
    (fops.workspace / "sub").mkdir()
    res = await fops.execute(action="list", path="")
    names = [i["name"] for i in res["items"]]
    assert names[0] == "sub"  # directories first
    assert set(names) == {"sub", "a.py", "b.txt"}
    assert ".alita_checkpoints" not in names

    res = await fops.execute(action="list", pattern="*.py")
    assert [i["name"] for i in res["items"]] == ["a.py"]


async def test_search_regex_glob_and_skips(fops):
    (fops.workspace / "src").mkdir()
    (fops.workspace / "src" / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (fops.workspace / "src" / "notes.txt").write_text("hello there\n", encoding="utf-8")
    (fops.workspace / "node_modules").mkdir()
    (fops.workspace / "node_modules" / "lib.py").write_text("def hello(): pass\n", encoding="utf-8")
    (fops.workspace / "bin.dat").write_bytes(b"\x00hello")

    res = await fops.execute(action="search", pattern=r"def\s+hello")
    assert res["success"] is True
    assert [(m["path"], m["line"]) for m in res["matches"]] == [("src/main.py", 1)]

    res = await fops.execute(action="search", pattern="hello", glob="*.txt")
    assert [m["path"] for m in res["matches"]] == ["src/notes.txt"]

    res = await fops.execute(action="search", pattern="HELLO", ignore_case=True)
    assert {m["path"] for m in res["matches"]} == {"src/main.py", "src/notes.txt"}


async def test_search_invalid_regex_falls_back_to_literal(fops):
    (fops.workspace / "f.txt").write_text("call foo(bar\n", encoding="utf-8")
    res = await fops.execute(action="search", pattern="foo(bar")
    assert res["success"] is True
    assert res["count"] == 1
    assert "literal" in res.get("note", "")


async def test_mkdir(fops):
    res = await fops.execute(action="mkdir", path="x/y/z")
    assert res["success"] is True
    assert (fops.workspace / "x" / "y" / "z").is_dir()
    res = await fops.execute(action="mkdir", path="../outside")
    assert res["success"] is False
