"""Tests for the Agent Skills manager."""

import pytest

from alita.skills.manager import SkillManager, parse_skill_md, render_skill_md


@pytest.fixture
def mgr(tmp_path):
    builtin = tmp_path / "builtin"
    (builtin / "hello-world").mkdir(parents=True)
    (builtin / "hello-world" / "SKILL.md").write_text(
        "---\nname: hello-world\ndescription: Say hello properly. Use when greeting.\ntags: [demo]\n---\n# Hello\nSay hi.\n",
        encoding="utf-8",
    )
    user = tmp_path / "user"
    user.mkdir()
    # Legacy flat v0.1 skill file
    (user / "old_style_skill.md").write_text("# Old Style\n\nDoes an old thing.\n", encoding="utf-8")
    return SkillManager(user, builtin)


def test_frontmatter_round_trip():
    text = render_skill_md("x-y", "Does X. Use when Y.", "# Body\nsteps", ["a"])
    meta, body = parse_skill_md(text)
    assert meta == {"name": "x-y", "description": "Does X. Use when Y.", "tags": ["a"]}
    assert body.strip() == "# Body\nsteps"


def test_discovery_and_index(mgr):
    names = [s["name"] for s in mgr.list()]
    assert names == ["hello-world", "old-style-skill"]
    idx = mgr.render_index()
    assert "- hello-world: Say hello properly. Use when greeting." in idx
    assert mgr.get("hello-world")["source"] == "builtin"
    assert mgr.get("old-style-skill")["description"] == "Does an old thing."


def test_create_update_patch_delete(mgr):
    sk = mgr.create("Deploy App", "Deploy the app to prod. Use when releasing.", "# Deploy\n1. build\n2. ship", ["ops"])
    assert sk["name"] == "deploy-app" and sk["editable"]
    with pytest.raises(ValueError):
        mgr.create("deploy-app", "Deploy the app to prod. Use when releasing.", "# again " * 5)
    mgr.patch("deploy-app", "2. ship", "2. ship\n3. verify")
    assert "3. verify" in mgr.get("deploy-app")["content"]
    with pytest.raises(ValueError):
        mgr.patch("deploy-app", "missing text", "x")
    rel = mgr.write_file("deploy-app", "scripts/run.sh", "echo hi")
    assert rel == "scripts/run.sh"
    assert mgr.get("deploy-app")["files"] == ["scripts/run.sh"]
    assert mgr.read_file("deploy-app", "scripts/run.sh") == "echo hi"
    assert mgr.read_file("deploy-app", "../../etc/passwd") is None
    assert mgr.delete("deploy-app")
    assert mgr.get("deploy-app") is None


def test_builtin_cannot_be_deleted_but_can_be_overridden(mgr):
    assert not mgr.delete("hello-world")
    mgr.update("hello-world", content="# Hello\nSay hi loudly.")
    sk = mgr.get("hello-world")
    assert sk["source"] == "user" and "loudly" in sk["content"]


def test_validation(mgr):
    with pytest.raises(ValueError):
        mgr.create("x", "short", "content long enough to pass the check")
    with pytest.raises(ValueError):
        mgr.create("good-name", "A good description of when to use it", "")


async def test_tools(mgr):
    listing = await mgr.tool_view()
    assert {s["name"] for s in listing["skills"]} == {"hello-world", "old-style-skill"}
    view = await mgr.tool_view(name="hello-world")
    assert view["success"] and "Say hi." in view["content"]
    missing = await mgr.tool_view(name="nope")
    assert not missing["success"] and "hello-world" in missing["error"]
    res = await mgr.tool_manage(action="create", name="new-skill", description="Does new things. Use when new.",
                                content="# New\nDo the new thing step by step.")
    assert res["success"]
    bad = await mgr.tool_manage(action="frobnicate", name="new-skill")
    assert not bad["success"]


async def test_keyword_relevance_without_vectors(mgr):
    mgr.create("csv-report", "Build summary reports from CSV spreadsheets. Use for CSV analysis.",
               "# CSV\nUse pandas to load the csv and summarise columns.")
    hits = await mgr.find_relevant("please analyse this CSV spreadsheet and build a report")
    assert hits and hits[0]["name"] == "csv-report"


async def test_skill_view_records_usage(agent):
    agent.skills.create("usage-test", "Track usage of skills. Use in tests.", "# Usage\nThis is a test skill body.")
    await agent.skills.tool_view(name="usage-test")
    await agent.skills.tool_view(name="usage-test")
    sk = next(s for s in agent.skills.list() if s["name"] == "usage-test")
    assert sk["usage_count"] == 2
    # Built-in skills shipped with the backend are discovered too.
    assert any(s["name"] == "code-change" for s in agent.skills.list())
