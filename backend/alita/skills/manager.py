"""
Skills — reusable procedural knowledge in the Agent Skills format.

A skill is a directory containing ``SKILL.md`` with YAML front-matter::

    ---
    name: pdf-report
    description: Build a PDF report from CSV data. Use when the user asks for a printable report.
    tags: [reports, pdf]
    ---
    # Instructions ...

plus any supporting files (scripts, templates, references).

Skills use *progressive disclosure*: only ``name`` + ``description`` of each
skill is placed in the system prompt; the agent loads the full instructions
with the ``skill_view`` tool when a skill is relevant. The agent can author
and refine its own skills with ``skill_manage``, and after complex successful
tasks a background reflection pass may distil the trajectory into a new
skill (or improve an existing one).

Two roots are scanned: built-in skills shipped with the backend (read-only)
and user skills in ``~/.alita/skills`` (writable). A user skill with the same
name overrides a built-in one.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from ..memory.sqlite_store import SQLiteStore, content_to_text
from ..memory.vector_store import SKILL_COLLECTION, VectorStore

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
MAX_SKILL_CHARS = 40000


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64] or "skill"


def parse_skill_md(text: str) -> tuple:
    """Return (frontmatter dict, body)."""
    m = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.DOTALL)
    if not m:
        return {}, text
    meta: Dict[str, Any] = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:  # noqa: BLE001
            meta = {}
    if not meta:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    return meta, m.group(2)


def render_skill_md(name: str, description: str, body: str, tags: Optional[List[str]] = None) -> str:
    meta: Dict[str, Any] = {"name": name, "description": description.strip()}
    if tags:
        meta["tags"] = [str(t) for t in tags]
    if yaml is not None:
        head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000).strip()
    else:  # pragma: no cover
        head = "\n".join(f"{k}: {json.dumps(v)}" for k, v in meta.items())
    return f"---\n{head}\n---\n\n{body.strip()}\n"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    source: str  # "builtin" | "user"
    tags: List[str] = field(default_factory=list)
    mtime: float = 0.0

    @property
    def dir(self) -> Path:
        return self.path.parent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "source": self.source,
            "path": str(self.path),
            "updated_at": datetime.fromtimestamp(self.mtime).isoformat() if self.mtime else None,
            "editable": self.source == "user",
        }


class SkillManager:
    """Discovers, serves and edits skills across built-in and user roots."""

    def __init__(
        self,
        user_dir: Path,
        builtin_dir: Optional[Path] = None,
        db: Optional[SQLiteStore] = None,
        vectors: Optional[VectorStore] = None,
    ):
        self.user_dir = Path(user_dir)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.builtin_dir = Path(builtin_dir) if builtin_dir else None
        self.db = db
        self.vectors = vectors
        self._lock = threading.RLock()
        self._cache: Dict[str, Skill] = {}
        self._cache_sig: Optional[tuple] = None
        self._indexed: Dict[str, float] = {}

    # ─── Discovery ────────────────────────────────────────────────

    def _scan_root(self, root: Path, source: str) -> Dict[str, Skill]:
        found: Dict[str, Skill] = {}
        if not root or not root.exists():
            return found
        for skill_md in sorted(root.glob("*/SKILL.md")):
            sk = self._load(skill_md, source)
            if sk:
                found[sk.name] = sk
        # Legacy flat markdown skills (v0.1 auto-generated files).
        for md in sorted(root.glob("*.md")):
            if md.name.upper() in ("README.MD",):
                continue
            sk = self._load(md, source, legacy=True)
            if sk and sk.name not in found:
                found[sk.name] = sk
        return found

    def _load(self, path: Path, source: str, legacy: bool = False) -> Optional[Skill]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        meta, body = parse_skill_md(text)
        name = str(meta.get("name") or (path.stem if legacy else path.parent.name))
        name = slugify(name)
        desc = str(meta.get("description") or "").strip()
        if not desc:
            # Fall back to first meaningful paragraph line.
            for line in body.splitlines():
                s = line.strip()
                if s and not s.startswith(("#", ">", "---")):
                    desc = s
                    break
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return Skill(name=name, description=desc[:1024], path=path, source=source,
                     tags=[str(t) for t in tags][:12], mtime=path.stat().st_mtime)

    def _signature(self) -> tuple:
        sig = []
        for root in (self.builtin_dir, self.user_dir):
            if root and root.exists():
                for p in list(root.glob("*/SKILL.md")) + list(root.glob("*.md")):
                    try:
                        sig.append((str(p), p.stat().st_mtime))
                    except OSError:
                        pass
        return tuple(sorted(sig))

    def _skills(self) -> Dict[str, Skill]:
        with self._lock:
            sig = self._signature()
            if sig != self._cache_sig:
                merged: Dict[str, Skill] = {}
                if self.builtin_dir:
                    merged.update(self._scan_root(self.builtin_dir, "builtin"))
                merged.update(self._scan_root(self.user_dir, "user"))
                self._cache = merged
                self._cache_sig = sig
            return self._cache

    def list(self) -> List[Dict[str, Any]]:
        stats = self._usage_stats()
        out = []
        for sk in sorted(self._skills().values(), key=lambda s: s.name):
            d = sk.to_dict()
            d.update(stats.get(sk.name, {"usage_count": 0, "last_used": None}))
            out.append(d)
        return out

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        sk = self._skills().get(slugify(name)) or self._skills().get(name)
        if not sk:
            return None
        text = sk.path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_skill_md(text)
        files = []
        if sk.path.name == "SKILL.md":
            for p in sorted(sk.dir.rglob("*")):
                if p.is_file() and p.name != "SKILL.md":
                    files.append(str(p.relative_to(sk.dir)).replace("\\", "/"))
        d = sk.to_dict()
        d.update({"content": body.strip(), "raw": text, "files": files[:100]})
        d.update(self._usage_stats().get(sk.name, {"usage_count": 0, "last_used": None}))
        return d

    def read_file(self, name: str, relpath: str) -> Optional[str]:
        sk = self._skills().get(slugify(name))
        if not sk or sk.path.name != "SKILL.md":
            return None
        target = (sk.dir / relpath).resolve()
        try:
            target.relative_to(sk.dir.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL_CHARS]

    # ─── Editing (user skills) ────────────────────────────────────

    def create(self, name: str, description: str, content: str, tags: Optional[List[str]] = None,
               overwrite: bool = False) -> Dict[str, Any]:
        slug = slugify(name)
        if not NAME_RE.match(slug):
            raise ValueError("Skill name must be 2-64 chars of lowercase letters, digits and hyphens.")
        if not description or len(description.strip()) < 10:
            raise ValueError("description is required: say what the skill does AND when to use it.")
        if not content or len(content.strip()) < 20:
            raise ValueError("content (the skill instructions in markdown) is required.")
        if len(content) > MAX_SKILL_CHARS:
            raise ValueError(f"content too long ({len(content)} chars, max {MAX_SKILL_CHARS}).")
        target_dir = self.user_dir / slug
        target = target_dir / "SKILL.md"
        if target.exists() and not overwrite:
            raise ValueError(f"Skill '{slug}' already exists; use update or patch.")
        _, body = parse_skill_md(content)  # tolerate content that already has front-matter
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(render_skill_md(slug, description, body, tags), encoding="utf-8")
        self._invalidate()
        return self.get(slug) or {}

    def update(self, name: str, description: Optional[str] = None, content: Optional[str] = None,
               tags: Optional[List[str]] = None) -> Dict[str, Any]:
        existing = self.get(name)
        if not existing:
            raise ValueError(f"Skill '{name}' not found.")
        new_desc = description if description else existing["description"]
        new_body = parse_skill_md(content)[1] if content else existing["content"]
        new_tags = tags if tags is not None else existing.get("tags")
        if existing["source"] != "user":
            # Editing a built-in creates a user override.
            return self.create(existing["name"], new_desc, new_body, new_tags, overwrite=True)
        Path(existing["path"]).write_text(render_skill_md(existing["name"], new_desc, new_body, new_tags),
                                          encoding="utf-8")
        self._invalidate()
        return self.get(existing["name"]) or {}

    def patch(self, name: str, old_text: str, new_text: str) -> Dict[str, Any]:
        existing = self.get(name)
        if not existing:
            raise ValueError(f"Skill '{name}' not found.")
        body = existing["content"]
        count = body.count(old_text) if old_text else 0
        if count != 1:
            raise ValueError(f"old_text must match exactly once in the skill body (matched {count}).")
        return self.update(name, content=body.replace(old_text, new_text or "", 1))

    def write_file(self, name: str, relpath: str, content: str) -> str:
        existing = self.get(name)
        if not existing or existing["source"] != "user":
            raise ValueError("Supporting files can only be added to user skills.")
        sk_dir = Path(existing["path"]).parent
        target = (sk_dir / relpath).resolve()
        target.relative_to(sk_dir.resolve())  # raises on traversal
        if target.name == "SKILL.md":
            raise ValueError("Use update to change SKILL.md.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._invalidate()
        return str(target.relative_to(sk_dir)).replace("\\", "/")

    def delete(self, name: str) -> bool:
        sk = self._skills().get(slugify(name))
        if not sk or sk.source != "user":
            return False
        if sk.path.name == "SKILL.md":
            shutil.rmtree(sk.dir, ignore_errors=True)
        else:
            sk.path.unlink(missing_ok=True)
        self._invalidate()
        if self.vectors:
            self.vectors.delete(SKILL_COLLECTION, sk.name)
        return True

    def _invalidate(self) -> None:
        with self._lock:
            self._cache_sig = None

    # ─── Usage stats (sqlite ``skills`` table) ────────────────────

    def _usage_stats(self) -> Dict[str, Dict[str, Any]]:
        if not self.db:
            return {}
        try:
            rows = self.db.query("SELECT name, usage_count, last_used FROM skills")
        except Exception:  # noqa: BLE001
            return {}
        return {r["name"]: {"usage_count": r["usage_count"] or 0, "last_used": r["last_used"]} for r in rows}

    def record_use(self, name: str) -> None:
        if not self.db:
            return
        sk = self._skills().get(slugify(name))
        if not sk:
            return
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO skills (name, description, file_path, tags) VALUES (?, ?, ?, ?)",
                    (sk.name, sk.description, str(sk.path), json.dumps(sk.tags)),
                )
                conn.execute(
                    "UPDATE skills SET usage_count = usage_count + 1, last_used = CURRENT_TIMESTAMP WHERE name = ?",
                    (sk.name,),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("skill usage update failed: %s", exc)

    # ─── Prompt + relevance ───────────────────────────────────────

    def render_index(self, limit: int = 60) -> str:
        skills = sorted(self._skills().values(), key=lambda s: s.name)[:limit]
        if not skills:
            return ""
        lines = [f"- {s.name}: {s.description}" for s in skills]
        return "<available_skills>\n" + "\n".join(lines) + "\n</available_skills>"

    async def sync_index(self) -> None:
        """Embed skills whose files changed since the last sync."""
        if not self.vectors or not self.vectors.is_available():
            return
        for sk in list(self._skills().values()):
            if self._indexed.get(sk.name) == sk.mtime:
                continue
            doc = f"{sk.name.replace('-', ' ')}: {sk.description} {' '.join(sk.tags)}"
            if await self.vectors.aupsert(SKILL_COLLECTION, sk.name, doc, {"source": sk.source}):
                self._indexed[sk.name] = sk.mtime

    async def find_relevant(self, query: str, k: int = 3, min_similarity: float = 0.38) -> List[Dict[str, Any]]:
        skills = self._skills()
        if not skills or not query.strip():
            return []
        scored: Dict[str, float] = {}
        if self.vectors and self.vectors.is_available():
            await self.sync_index()
            for hit in await self.vectors.asearch(SKILL_COLLECTION, query, k * 2):
                if hit["id"] in skills and hit["similarity"] >= min_similarity:
                    scored[hit["id"]] = hit["similarity"]
        else:
            words = {w for w in re.findall(r"[a-z0-9]{4,}", query.lower())}
            for sk in skills.values():
                hay = set(re.findall(r"[a-z0-9]{4,}", f"{sk.name} {sk.description} {' '.join(sk.tags)}".lower()))
                overlap = len(words & hay)
                if overlap >= 2:
                    scored[sk.name] = overlap / (len(words) + 1)
        best = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [{"name": n, "description": skills[n].description, "score": round(s, 3)} for n, s in best]

    # ─── Agent tools ──────────────────────────────────────────────

    async def tool_view(self, name: str = "", file: str = "", **_: Any) -> Dict[str, Any]:
        if not name:
            return {"success": True, "skills": [{"name": s["name"], "description": s["description"]} for s in self.list()]}
        if file:
            text = self.read_file(name, file)
            if text is None:
                return {"success": False, "error": f"File '{file}' not found in skill '{name}'."}
            return {"success": True, "name": name, "file": file, "content": text}
        sk = self.get(name)
        if not sk:
            return {"success": False, "error": f"Skill '{name}' not found. Available: "
                                               + ", ".join(s["name"] for s in self.list()[:40])}
        self.record_use(sk["name"])
        return {"success": True, "name": sk["name"], "description": sk["description"],
                "content": sk["content"], "files": sk["files"], "directory": str(Path(sk["path"]).parent)}

    async def tool_manage(self, action: str = "", name: str = "", **params: Any) -> Dict[str, Any]:
        try:
            if action == "create":
                sk = self.create(name, params.get("description", ""), params.get("content", ""), params.get("tags"))
            elif action == "update":
                sk = self.update(name, params.get("description"), params.get("content"), params.get("tags"))
            elif action == "patch":
                sk = self.patch(name, params.get("old_text", ""), params.get("new_text", ""))
            elif action == "write_file":
                rel = self.write_file(name, params.get("file", ""), params.get("content", ""))
                return {"success": True, "name": name, "file": rel}
            elif action == "delete":
                ok = self.delete(name)
                return {"success": ok, **({} if ok else {"error": "Only existing user skills can be deleted."})}
            else:
                return {"success": False, "error": "action must be create, update, patch, write_file or delete"}
            return {"success": True, "name": sk.get("name"), "path": sk.get("path")}
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    # ─── Reflection: distil a trajectory into a skill ─────────────

    async def reflect(self, llm: Any, task: str, trajectory: str, final_response: str) -> Optional[Dict[str, Any]]:
        """Ask the LLM whether this successful task should become (or improve) a skill."""
        similar = await self.find_relevant(task, k=3, min_similarity=0.3)
        similar_txt = "\n".join(f"- {s['name']}: {s['description']}" for s in similar) or "(none)"
        prompt = SKILL_REFLECTION_PROMPT.format(
            task=task[:3000], trajectory=trajectory[:8000], final=final_response[:2000], similar=similar_txt,
        )
        try:
            resp = await llm.chat(messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2500)
        except Exception as exc:  # noqa: BLE001
            logger.info("skill reflection failed: %s", exc)
            return None
        text = content_to_text(resp.get("content", ""))
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        action = data.get("action")
        try:
            if action == "create" and data.get("name"):
                if self.get(data["name"]):
                    action = "update"
                else:
                    sk = self.create(data["name"], data.get("description", ""), data.get("content", ""), data.get("tags"))
                    logger.info("auto-created skill %s", sk.get("name"))
                    return {"action": "created", "name": sk.get("name")}
            if action == "update" and data.get("name") and self.get(data["name"]):
                sk = self.update(data["name"], data.get("description"), data.get("content"), data.get("tags"))
                logger.info("auto-updated skill %s", sk.get("name"))
                return {"action": "updated", "name": sk.get("name")}
        except ValueError as exc:
            logger.info("skill reflection produced invalid skill: %s", exc)
        return None


SKILL_REFLECTION_PROMPT = """You are reviewing a task an AI agent just completed successfully, to decide
whether it should be saved as a reusable SKILL (procedural knowledge for future similar tasks).

Create a skill ONLY if the task was a non-trivial, multi-step procedure that is likely to recur and
where written guidance would make next time faster or more reliable (e.g. a deployment workflow, a
data-processing recipe, a debugging procedure for a specific stack, an API integration pattern).
Do NOT create skills for one-off questions, chit-chat, simple lookups, or highly specific one-time tasks.
If an existing similar skill covers it, prefer "update" with improved instructions (keep what was good).

Skill content guidelines: markdown; generalise (no one-off values like specific file names unless they
are conventions); include When to use, Steps, Pitfalls/gotchas discovered, and Verification. Be concise
(under ~60 lines).

## Task
{task}

## Trajectory (tool calls and results, abbreviated)
{trajectory}

## Final answer
{final}

## Existing similar skills
{similar}

Respond with ONLY JSON:
{{"action": "none"}}
or
{{"action": "create" | "update", "name": "kebab-case-name", "description": "What it does. Use when ...",
  "tags": ["..."], "content": "# Title\\n\\n## When to use\\n..."}}"""


SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": (
        "Load a skill's full instructions (or one of its supporting files). Skills listed in "
        "<available_skills> contain proven procedures — load the matching skill BEFORE starting a task it covers. "
        "Call with no name to list all skills."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill name."},
            "file": {"type": "string", "description": "Optional relative path of a supporting file in the skill."},
        },
    },
}

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Create or improve reusable skills (your procedural memory). After solving a non-trivial, repeatable "
        "task — or when the user asks — save the procedure as a skill. When a skill proved wrong or incomplete, "
        "patch it. The description must state what the skill does AND when to use it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "patch", "write_file", "delete"]},
            "name": {"type": "string", "description": "kebab-case skill name."},
            "description": {"type": "string"},
            "content": {"type": "string", "description": "Markdown instructions (create/update) or file content (write_file)."},
            "tags": {"type": "array", "items": {"type": "string"}},
            "old_text": {"type": "string", "description": "Exact text to replace (patch)."},
            "new_text": {"type": "string", "description": "Replacement text (patch)."},
            "file": {"type": "string", "description": "Relative path for write_file (e.g. scripts/run.py)."},
        },
        "required": ["action", "name"],
    },
}
