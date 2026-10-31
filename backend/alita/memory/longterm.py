"""
Long-term memory — an unbounded store of atomic, durable memories.

Each memory is a short self-contained statement ("User prefers TypeScript
over JavaScript", "Project Alita's backend runs on port 8080") with a
category and importance. Retrieval is hybrid:

* **Keyword** — SQLite FTS5 / BM25
* **Semantic** — local embeddings via ChromaDB (when available)

Candidates from both are fused with Reciprocal Rank Fusion, then weighted by
importance and recency. Writes de-duplicate: an exact or near-identical
memory updates the existing row instead of adding a new one.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .sqlite_store import SQLiteStore, fts_query
from .vector_store import MEMORY_COLLECTION, VectorStore

logger = logging.getLogger(__name__)

CATEGORIES = ("fact", "preference", "person", "project", "event", "procedure", "other")

# Similarity at or above which a new memory is treated as the same memory.
_MERGE_SIMILARITY = 0.9
# Minimum semantic similarity for a vector-only hit to count as relevant.
_MIN_SEMANTIC = 0.3


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class LongTermMemory:
    """Hybrid keyword + semantic memory store backed by SQLite (+ Chroma)."""

    def __init__(self, db: SQLiteStore, vectors: Optional[VectorStore] = None):
        self.db = db
        self.vectors = vectors

    # ─── CRUD ─────────────────────────────────────────────────────

    def get(self, memory_id: int) -> Optional[Dict[str, Any]]:
        return self.db.query_one("SELECT * FROM memories WHERE id = ?", (int(memory_id),))

    def list(
        self,
        limit: int = 100,
        offset: int = 0,
        category: Optional[str] = None,
        query: str = "",
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        where, params = [], []
        if not include_inactive:
            where.append("active = 1")
        if category:
            where.append("category = ?")
            params.append(category)
        if query:
            where.append("content LIKE ?")
            params.append(f"%{query}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        return self.db.query(
            f"SELECT * FROM memories {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM memories WHERE active = 1")
        return int(row["n"]) if row else 0

    def stats(self) -> Dict[str, Any]:
        rows = self.db.query("SELECT category, COUNT(*) AS n FROM memories WHERE active = 1 GROUP BY category")
        return {
            "total": sum(r["n"] for r in rows),
            "by_category": {r["category"]: r["n"] for r in rows},
            "semantic_search": bool(self.vectors and self.vectors.is_available()),
        }

    async def add(
        self,
        content: str,
        category: str = "fact",
        importance: int = 3,
        source_session: Optional[str] = None,
        dedupe: bool = True,
    ) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"status": "error", "error": "content is empty"}
        if len(content) > 1200:
            content = content[:1200].rstrip() + "…"
        category = category if category in CATEGORIES else "other"
        importance = max(1, min(5, int(importance or 3)))

        if dedupe:
            existing = await self._find_duplicate(content)
            if existing is not None:
                mem_id, exact = existing
                if exact:
                    self.db.execute(
                        "UPDATE memories SET importance = MAX(importance, ?), "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (importance, mem_id),
                    )
                    return {"status": "duplicate", "id": mem_id}
                await self.update(mem_id, content=content, importance=importance)
                return {"status": "merged", "id": mem_id}

        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO memories (content, category, importance, source_session) VALUES (?, ?, ?, ?)",
                (content, category, importance, source_session),
            )
            mem_id = cur.lastrowid
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (mem_id, content))
        if self.vectors:
            await self.vectors.aupsert(MEMORY_COLLECTION, str(mem_id), content, {"category": category})
        return {"status": "added", "id": mem_id}

    async def update(
        self,
        memory_id: int,
        content: Optional[str] = None,
        category: Optional[str] = None,
        importance: Optional[int] = None,
    ) -> bool:
        row = self.get(memory_id)
        if not row or not row["active"]:
            return False
        new_content = (content or row["content"]).strip()
        new_category = category if category in CATEGORIES else row["category"]
        new_importance = max(1, min(5, int(importance))) if importance else row["importance"]
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE memories SET content = ?, category = ?, importance = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_content, new_category, new_importance, int(memory_id)),
            )
            if new_content != row["content"]:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(memory_id),))
                conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (int(memory_id), new_content))
        if self.vectors and new_content != row["content"]:
            await self.vectors.aupsert(MEMORY_COLLECTION, str(memory_id), new_content, {"category": new_category})
        return True

    async def delete(self, memory_id: int) -> bool:
        with self.db.tx() as conn:
            cur = conn.execute("UPDATE memories SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND active = 1",
                               (int(memory_id),))
            conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (int(memory_id),))
            ok = cur.rowcount > 0
        if ok and self.vectors:
            await self.vectors.adelete(MEMORY_COLLECTION, str(memory_id))
        return ok

    # ─── Retrieval ────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        k: int = 8,
        category: Optional[str] = None,
        touch: bool = True,
    ) -> List[Dict[str, Any]]:
        """Hybrid search. Each result carries ``relevance`` (0..1-ish) and ``semantic`` similarity."""
        query = (query or "").strip()
        if not query:
            return []
        pool = max(k * 3, 20)

        keyword_ids: List[int] = []
        match = fts_query(query)
        if match:
            try:
                rows = self.db.query(
                    "SELECT rowid AS id FROM memories_fts WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, pool),
                )
                keyword_ids = [int(r["id"]) for r in rows]
            except Exception as exc:  # noqa: BLE001
                logger.debug("memory FTS failed: %s", exc)

        semantic: Dict[int, float] = {}
        if self.vectors and self.vectors.is_available():
            for hit in await self.vectors.asearch(MEMORY_COLLECTION, query, pool):
                try:
                    semantic[int(hit["id"])] = hit["similarity"]
                except (TypeError, ValueError):
                    continue
        semantic_ids = [i for i, _ in sorted(semantic.items(), key=lambda kv: kv[1], reverse=True)]

        fused: Dict[int, float] = {}
        for rank, mem_id in enumerate(keyword_ids):
            fused[mem_id] = fused.get(mem_id, 0.0) + 1.0 / (60 + rank)
        for rank, mem_id in enumerate(semantic_ids):
            if semantic[mem_id] < _MIN_SEMANTIC and mem_id not in keyword_ids:
                continue
            fused[mem_id] = fused.get(mem_id, 0.0) + 1.0 / (60 + rank)
        if not fused:
            return []

        ids = list(fused)
        marks = ",".join("?" for _ in ids)
        params: List[Any] = list(ids)
        cat_clause = ""
        if category:
            cat_clause = " AND category = ?"
            params.append(category)
        rows = {r["id"]: r for r in self.db.query(
            f"SELECT * FROM memories WHERE active = 1 AND id IN ({marks}){cat_clause}", params
        )}

        now = datetime.now(timezone.utc)
        results = []
        max_rrf = 2.0 / 60
        for mem_id, rrf in fused.items():
            row = rows.get(mem_id)
            if not row:
                continue
            sim = semantic.get(mem_id, 0.0)
            ts = _parse_ts(row.get("updated_at")) or now
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
            recency = 0.85 + 0.15 * math.exp(-age_days / 90)
            importance = 0.8 + 0.1 * (int(row["importance"]) - 1)  # 0.8 .. 1.2
            # Blend normalised RRF with raw semantic similarity so the score
            # stays meaningful when only one retriever produced hits.
            base = 0.5 * (rrf / max_rrf) + 0.5 * (sim if semantic else min(1.0, rrf / max_rrf * 1.4))
            item = dict(row)
            item["semantic"] = round(sim, 3)
            item["keyword_hit"] = mem_id in keyword_ids
            item["relevance"] = round(base * importance * recency, 4)
            results.append(item)
        results.sort(key=lambda r: r["relevance"], reverse=True)
        results = results[:k]
        if touch and results:
            hit_ids = [r["id"] for r in results]
            self.db.execute(
                f"UPDATE memories SET access_count = access_count + 1, last_accessed = CURRENT_TIMESTAMP "
                f"WHERE id IN ({','.join('?' for _ in hit_ids)})",
                hit_ids,
            )
        return results

    async def _find_duplicate(self, content: str) -> Optional[tuple]:
        # Real-world memories never merge into fiction (and vice versa): only
        # non-fiction rows are candidates here. Scoped writes use add_scoped().
        norm = _norm(content)
        match = fts_query(content)
        if match:
            try:
                rows = self.db.query(
                    "SELECT m.id, m.content FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.active = 1 AND m.fiction = 0 ORDER BY bm25(memories_fts) LIMIT 10",
                    (match,),
                )
                for r in rows:
                    if _norm(r["content"]) == norm:
                        return int(r["id"]), True
            except Exception:  # noqa: BLE001
                pass
        if self.vectors and self.vectors.is_available():
            hits = await self.vectors.asearch(MEMORY_COLLECTION, content, 3)
            for hit in hits:
                if hit["similarity"] >= _MERGE_SIMILARITY:
                    try:
                        mem_id = int(hit["id"])
                    except (TypeError, ValueError):
                        continue
                    row = self.get(mem_id)
                    if row and row["active"] and not row.get("fiction"):
                        return mem_id, False
        return None

    # ─── Scoped (fiction) memories ────────────────────────────────
    #
    # Used by story worlds such as Fortitude. Every scoped memory is marked
    # ``fiction=1``: it can be recalled at home (labelled as fiction), but it
    # is never merged with, updated by, or mistaken for a real-world memory.

    async def add_scoped(
        self,
        content: str,
        scope: str,
        external_ref: Optional[str] = None,
        importance: int = 3,
        category: str = "event",
    ) -> Dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"status": "error", "error": "content is empty"}
        if not scope:
            return {"status": "error", "error": "scope is required"}
        if len(content) > 1200:
            content = content[:1200].rstrip() + "…"
        category = category if category in CATEGORIES else "event"
        importance = max(1, min(5, int(importance or 3)))
        if external_ref:
            existing = self.db.query_one("SELECT id, active FROM memories WHERE external_ref = ?", (external_ref,))
            if existing:
                return {"status": "duplicate", "id": int(existing["id"])}
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO memories (content, category, importance, scope, external_ref, fiction) VALUES (?, ?, ?, ?, ?, 1)",
                (content, category, importance, scope, external_ref),
            )
            mem_id = cur.lastrowid
            conn.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (mem_id, content))
        if self.vectors:
            await self.vectors.aupsert(MEMORY_COLLECTION, str(mem_id), content, {"category": category, "fiction": 1, "scope": scope})
        return {"status": "added", "id": mem_id}

    def list_scope(self, scope: str, limit: int = 24) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM memories WHERE active = 1 AND scope = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (scope, int(limit)),
        )

    def search_scope(self, scope: str, query: str, k: int = 8) -> List[Dict[str, Any]]:
        """Keyword search confined to one scope (falls back to most recent)."""
        match = fts_query(query or "")
        rows: List[Dict[str, Any]] = []
        if match:
            try:
                rows = self.db.query(
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    "WHERE memories_fts MATCH ? AND m.active = 1 AND m.scope = ? "
                    "ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, scope, int(k)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("scoped FTS failed: %s", exc)
        return rows

    async def retract_scope(self, scope: str) -> int:
        """Soft-delete every memory in a scope (world reset / deletion)."""
        if not scope:
            return 0
        ids = [int(r["id"]) for r in self.db.query("SELECT id FROM memories WHERE active = 1 AND scope = ?", (scope,))]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self.db.tx() as conn:
            # external_ref is cleared so a reset world can reuse the same refs.
            conn.execute(f"UPDATE memories SET active = 0, external_ref = NULL, updated_at = CURRENT_TIMESTAMP WHERE id IN ({marks})", ids)
            conn.execute(f"DELETE FROM memories_fts WHERE rowid IN ({marks})", ids)
        if self.vectors:
            for mem_id in ids:
                await self.vectors.adelete(MEMORY_COLLECTION, str(mem_id))
        return len(ids)

    # ─── Maintenance ──────────────────────────────────────────────

    async def reindex(self) -> int:
        """Embed every active memory (idempotent upsert). Returns count."""
        if not self.vectors or not self.vectors.is_available():
            return 0
        rows = self.db.query("SELECT id, content, category FROM memories WHERE active = 1")
        done = 0
        for r in rows:
            if await self.vectors.aupsert(MEMORY_COLLECTION, str(r["id"]), r["content"], {"category": r["category"]}):
                done += 1
            await asyncio.sleep(0)
        return done
