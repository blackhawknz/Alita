"""Tests for core memory blocks, the long-term memory store and memory tools."""

import sqlite3

import pytest

from alita.memory.core_memory import CoreMemory, CoreMemoryError
from alita.memory.longterm import LongTermMemory
from alita.memory.sqlite_store import SQLiteStore, fts_query


@pytest.fixture
def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "m.db"))
    yield store
    store.close()


def test_core_memory_append_replace_limits(tmp_path):
    core = CoreMemory(tmp_path)
    core.append("user", "Name: Sam")
    core.append("user", "Name: Sam")  # duplicate ignored
    assert core.get("user") == "- Name: Sam"
    core.replace("user", "Sam", "Samantha")
    assert "Samantha" in core.get("user")
    with pytest.raises(CoreMemoryError):
        core.replace("user", "not there", "x")
    with pytest.raises(CoreMemoryError):
        core.set("notes", "x" * 5000)
    with pytest.raises(CoreMemoryError):
        core.get("bogus")
    rendered = core.render()
    assert '<memory_block name="user"' in rendered and "(empty)" in rendered


def test_core_memory_imports_legacy_files(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "USER.md").write_text("# User Profile\n\n## Preferences\n- [General] Likes tea\n", encoding="utf-8")
    (legacy / "MEMORY.md").write_text("# Mem\n- [Facts] Runs Windows 11\n", encoding="utf-8")
    core = CoreMemory(tmp_path / "core")
    core.import_legacy(legacy)
    assert core.get("user") == "- Likes tea"
    assert core.get("notes") == "- Runs Windows 11"


async def test_longterm_add_search_dedupe(db):
    lt = LongTermMemory(db, vectors=None)
    r1 = await lt.add("User prefers dark mode in every app", category="preference", importance=4)
    assert r1["status"] == "added"
    dup = await lt.add("user prefers dark mode in every app!", category="preference")
    assert dup == {"status": "duplicate", "id": r1["id"]}
    await lt.add("User's cat is called Miso", category="person")
    hits = await lt.search("what theme does the user like? dark mode")
    assert hits and hits[0]["id"] == r1["id"]
    assert hits[0]["keyword_hit"] is True
    assert lt.count() == 2
    assert lt.stats()["by_category"] == {"preference": 1, "person": 1}


async def test_longterm_update_and_delete(db):
    lt = LongTermMemory(db, vectors=None)
    mid = (await lt.add("User lives in Berlin", category="fact"))["id"]
    assert await lt.update(mid, content="User lives in Munich")
    assert (await lt.search("Munich"))[0]["id"] == mid
    assert await lt.search("Berlin") == []
    assert await lt.delete(mid)
    assert await lt.search("Munich") == []
    assert not await lt.delete(mid)


async def test_fts_tracks_soft_delete_and_reactivation(db):
    lt = LongTermMemory(db, vectors=None)
    mid = (await lt.add("User lives in Berlin", category="fact"))["id"]

    def fts_count() -> int:
        return db.query_one("SELECT COUNT(*) AS n FROM memories_fts")["n"]

    def active_count() -> int:
        return db.query_one("SELECT COUNT(*) AS n FROM memories WHERE active = 1")["n"]

    assert fts_count() == active_count() == 1

    assert await lt.delete(mid)
    assert await lt.search("Berlin") == []
    assert fts_count() == active_count() == 0

    db.execute("UPDATE memories SET active = 1 WHERE id = ?", (mid,))
    assert fts_count() == active_count() == 1
    assert (await lt.search("Berlin"))[0]["id"] == mid

    db.execute("UPDATE memories SET active = 0 WHERE id = ?", (mid,))
    assert fts_count() == active_count() == 0


async def test_fts_content_update_keeps_one_row(db):
    lt = LongTermMemory(db, vectors=None)
    mid = (await lt.add("User lives in Berlin", category="fact"))["id"]
    assert await lt.update(mid, content="User lives in Munich")

    rows = db.query("SELECT rowid FROM memories_fts")
    assert [int(r["rowid"]) for r in rows] == [mid]
    assert (await lt.search("Munich"))[0]["id"] == mid
    assert await lt.search("Berlin") == []


async def test_startup_repair_rebuilds_active_only_index(tmp_path):
    path = tmp_path / "repair.db"
    store = SQLiteStore(str(path))
    lt = LongTermMemory(store, vectors=None)
    keep = (await lt.add("User keeps this memory", category="fact"))["id"]
    drop = (await lt.add("User dropped this memory", category="fact"))["id"]
    store.close()

    raw = sqlite3.connect(str(path))
    try:
        # Simulate a pre-trigger database: soft-delete without touching FTS,
        # remove an active row from FTS, and leave a phantom row behind.
        raw.execute("DROP TRIGGER IF EXISTS memories_active_au")
        raw.execute("UPDATE memories SET active = 0 WHERE id = ?", (drop,))
        raw.execute("DELETE FROM memories_fts WHERE rowid = ?", (keep,))
        raw.execute("INSERT INTO memories_fts(rowid, content) VALUES (9999, 'ghost')")
        raw.commit()
    finally:
        raw.close()

    repaired = SQLiteStore(str(path))
    try:
        fts = repaired.query("SELECT rowid FROM memories_fts ORDER BY rowid")
        active = repaired.query("SELECT id FROM memories WHERE active = 1 ORDER BY id")
        assert [int(r["rowid"]) for r in fts] == [int(r["id"]) for r in active] == [keep]

        lt2 = LongTermMemory(repaired, vectors=None)
        assert (await lt2.search("keeps"))[0]["id"] == keep
        assert await lt2.search("dropped") == []
    finally:
        repaired.close()


def test_fts_query_is_safe():
    # FTS operators and punctuation are neutralised into quoted terms.
    assert fts_query('what is "AND OR" NEAR(x)?') == '"near"'
    assert fts_query("deploy* the server:prod") == '"deploy" OR "server" OR "prod"'
    assert fts_query("") == ""
    assert fts_query("the a an") == ""


async def test_memory_tool_actions(agent):
    res = await agent.memory.tool_memory("save", content="User drives a blue Volvo", category="fact")
    assert res["success"] and res["status"] == "added"
    found = await agent.memory.tool_memory("search", query="car Volvo")
    assert found["results"][0]["content"] == "User drives a blue Volvo"
    upd = await agent.memory.tool_memory("update", id=res["id"], content="User drives a red Volvo")
    assert upd["success"]
    bad = await agent.memory.tool_memory("explode")
    assert not bad["success"]
    core = await agent.memory.tool_core_memory("append", block="notes", text="Workspace is a monorepo")
    assert core["success"]
    over = await agent.memory.tool_core_memory("rewrite", block="notes", text="y" * 9000)
    assert not over["success"] and "limit" in over["error"]


async def test_recall_threshold_filters_noise(agent):
    await agent.longterm.add("User is allergic to peanuts", category="fact", importance=5)
    assert await agent.memory.recall("hello") == []
    hits = await agent.memory.recall("Can you suggest a snack? I have a peanut allergy question")
    assert hits and "peanuts" in hits[0]["content"]
