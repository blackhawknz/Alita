"""
Vector store — semantic index for long-term memories and skills.

Thin wrapper over ChromaDB with local ONNX embeddings (all-MiniLM-L6-v2).
Everything is optional and failure-tolerant: if ChromaDB is missing, the
embedding model cannot be loaded, or ``ALITA_VECTOR_ENABLED=false``, the
store reports itself unavailable and callers fall back to keyword search.

Embedding is CPU-bound and synchronous, so async callers should use the
``a*`` coroutine variants which run in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    import chromadb
    from chromadb.config import Settings

    HAS_CHROMADB = True
except Exception:  # noqa: BLE001
    HAS_CHROMADB = False

MEMORY_COLLECTION = "alita_longterm_v2"
SKILL_COLLECTION = "alita_skills_v2"


class VectorStore:
    """Lazily-initialised ChromaDB client with cosine-space collections."""

    def __init__(self, persist_dir: Optional[str] = None, enabled: Optional[bool] = None):
        from ..core.config import alita_home, env_bool

        self.persist_dir = Path(persist_dir) if persist_dir else alita_home() / "vector_db"
        self.enabled = env_bool("ALITA_VECTOR_ENABLED", True) if enabled is None else enabled
        self._client = None
        self._collections: Dict[str, Any] = {}
        self._failed = False
        self._lock = threading.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────

    def _ensure(self) -> bool:
        if self._client is not None:
            return True
        if self._failed or not self.enabled or not HAS_CHROMADB:
            return False
        with self._lock:
            if self._client is not None:
                return True
            try:
                self.persist_dir.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(
                    path=str(self.persist_dir),
                    settings=Settings(anonymized_telemetry=False),
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vector store unavailable: %s", exc)
                self._failed = True
                return False

    def _collection(self, name: str):
        if name in self._collections:
            return self._collections[name]
        if not self._ensure():
            return None
        try:
            col = self._client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
            self._collections[name] = col
            return col
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector collection %s unavailable: %s", name, exc)
            self._failed = True
            return None

    def is_available(self) -> bool:
        return self.enabled and HAS_CHROMADB and not self._failed

    def warm_up(self) -> bool:
        """Initialise the client and embedding model (call from a thread)."""
        col = self._collection(MEMORY_COLLECTION)
        if col is None:
            return False
        try:
            col.query(query_texts=["warm up"], n_results=1)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding model failed to load: %s", exc)
            self._failed = True
            return False

    # ─── Generic ops ──────────────────────────────────────────────

    def upsert(self, collection: str, doc_id: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        col = self._collection(collection)
        if col is None:
            return False
        try:
            meta = {k: v for k, v in (metadata or {}).items() if isinstance(v, (str, int, float, bool))}
            col.upsert(ids=[doc_id], documents=[text], metadatas=[meta or {"_": 1}])
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector upsert failed: %s", exc)
            return False

    def delete(self, collection: str, doc_id: str) -> None:
        col = self._collection(collection)
        if col is None:
            return
        try:
            col.delete(ids=[doc_id])
        except Exception:  # noqa: BLE001
            pass

    def search(self, collection: str, query: str, k: int = 10) -> List[Dict[str, Any]]:
        """Return [{id, similarity, metadata}] (similarity in [0, 1])."""
        col = self._collection(collection)
        if col is None or not query.strip():
            return []
        try:
            count = col.count()
            if count == 0:
                return []
            res = col.query(query_texts=[query], n_results=min(k, count))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector search failed: %s", exc)
            return []
        out = []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        for i, doc_id in enumerate(ids):
            dist = dists[i] if i < len(dists) and dists[i] is not None else 1.0
            out.append({
                "id": doc_id,
                "similarity": max(0.0, 1.0 - float(dist)),
                "metadata": metas[i] if i < len(metas) else {},
            })
        return out

    # ─── Async wrappers ───────────────────────────────────────────

    async def aupsert(self, collection: str, doc_id: str, text: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_available():
            return False
        return await asyncio.to_thread(self.upsert, collection, doc_id, text, metadata)

    async def adelete(self, collection: str, doc_id: str) -> None:
        if not self.is_available():
            return
        await asyncio.to_thread(self.delete, collection, doc_id)

    async def asearch(self, collection: str, query: str, k: int = 10) -> List[Dict[str, Any]]:
        if not self.is_available():
            return []
        return await asyncio.to_thread(self.search, collection, query, k)
