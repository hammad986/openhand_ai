"""
Phase 13C — Semantic memory layer.

A thin, pluggable wrapper around ChromaDB's persistent client that the
rest of the system uses for *long-term* recall across sessions:

  * `tasks`        – every task we've attempted, with its outcome.
  * `solutions`    – successful runs (the "what fixed it" recipe).
  * `errors`       – failed runs grouped by error category.
  * `reflections`  – post-run summaries written by the orchestrator.

Design rules
------------
1. **Plug-and-play** — nothing else in the codebase depends on Chroma's
   API directly. If you swap the backend later, only this file changes.
2. **Silent fallback** — every public method catches all exceptions and
   returns an empty-but-valid value (or `False`). The orchestrator
   never has to wrap calls in try/except. If chromadb is missing, every
   call is a no-op and `enabled` stays False.
3. **Persistent** — the on-disk client survives across sessions. The
   default location lives next to the SQLite memory at
   `./memory_vectors/chroma`, but `path=` can override it.
4. **Non-blocking** — Chroma's persistent client is local & fast (no
   network). All calls are synchronous but bounded; we cap query k
   so a runaway recall can't stall the planner.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Optional dep — never fatal. The whole VectorStore degrades to a no-op
# when chromadb is not importable. This mirrors how memory.py already
# handles the same import.
try:
    import chromadb
    from chromadb.config import Settings as _ChromaSettings
    CHROMA_AVAILABLE = True
except Exception:                       # pragma: no cover
    chromadb = None
    _ChromaSettings = None
    CHROMA_AVAILABLE = False


# Public collection names — keep these stable; renames break old data.
# Phase 15 adds `tool_outcomes`: every controlled-tool execution we
# observed (success / failure / usefulness score) so future tool
# decisions can bias toward what worked on similar sub-tasks.
COLLECTIONS = ("tasks", "solutions", "errors", "reflections",
               "tool_outcomes")

# Hard cap on `k` so a bad caller can't ask Chroma for thousands of
# rows. Chroma's HNSW is fast but not free; recall is meant to feed a
# planner prompt, not page through the whole corpus.
_MAX_K = 25


class VectorStore:
    """Persistent semantic memory backed by Chroma.

    Public surface (all silent on failure):
      * `add(collection, doc_id, text, metadata)`         → bool
      * `query(collection, text, k)`                      → list[dict]
      * `semantic_recall(text, k)`                        → dict[str, list[dict]]
      * `count(collection)`                               → int
      * `enabled`                                         → bool

    The instance is safe to construct even when chromadb is missing —
    in that case `enabled` is False and every method is a no-op.
    """

    def __init__(self, path: str = "./memory_vectors/chroma") -> None:
        self.path = Path(path)
        self._client: Any = None
        self._collections: dict[str, Any] = {}
        self.enabled: bool = False
        self._init()

    # ── lifecycle ──────────────────────────────────────────────────
    def _init(self) -> None:
        if not CHROMA_AVAILABLE:
            logger.info("[VectorStore] chromadb not installed — semantic recall disabled.")
            return
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            # `anonymized_telemetry=False` keeps Chroma from making
            # outbound calls in offline / restricted environments.
            settings = _ChromaSettings(anonymized_telemetry=False)  # type: ignore[arg-type]
            self._client = chromadb.PersistentClient(
                path=str(self.path), settings=settings)
            for name in COLLECTIONS:
                # cosine space matches the default sentence-transformer
                # behaviour and is what most semantic-recall heuristics
                # expect (smaller distance = more similar).
                self._collections[name] = (
                    self._client.get_or_create_collection(
                        name=name, metadata={"hnsw:space": "cosine"}))
            self.enabled = True
            logger.info(f"[VectorStore] ready at {self.path} "
                        f"(collections: {', '.join(COLLECTIONS)})")
        except Exception as e:
            logger.warning(f"[VectorStore] init failed: {e!r} — "
                           f"falling back silently.")
            self.enabled = False
            self._client = None
            self._collections = {}

    # ── writes ─────────────────────────────────────────────────────
    def add(self, collection: str, doc_id: str, text: str,
            metadata: dict | None = None) -> bool:
        """Upsert (id, text, metadata) into `collection`. Returns True
        on success, False on no-op / failure. Never raises."""
        if not self.enabled:
            return False
        coll = self._collections.get(collection)
        if coll is None or not text or not doc_id:
            return False
        try:
            # Chroma rejects non-scalar metadata values, so we coerce
            # everything to str/int/float/bool. Keys are also stringified.
            safe_meta = self._normalize_metadata(metadata or {})
            # Add a `ts` we can sort by later if needed.
            safe_meta.setdefault("ts", time.time())
            coll.upsert(
                ids=[str(doc_id)],
                documents=[text[:4000]],   # bound document size
                metadatas=[safe_meta],
            )
            return True
        except Exception as e:
            logger.debug(f"[VectorStore] add failed ({collection}): {e!r}")
            return False

    # ── reads ──────────────────────────────────────────────────────
    def query(self, collection: str, text: str,
              k: int = 5) -> list[dict]:
        """Return up to `k` semantically-similar entries from
        `collection`. Each entry is a dict of
        {id, document, metadata, distance}. Never raises."""
        if not self.enabled or not text:
            return []
        coll = self._collections.get(collection)
        if coll is None:
            return []
        k = max(1, min(int(k or 5), _MAX_K))
        try:
            res = coll.query(query_texts=[text], n_results=k)
        except Exception as e:
            logger.debug(f"[VectorStore] query failed ({collection}): {e!r}")
            return []
        # Chroma returns parallel lists wrapped in an outer list (one
        # per query). We only ever pass one query, so unwrap [0].
        ids       = (res.get("ids")        or [[]])[0]
        docs      = (res.get("documents")  or [[]])[0]
        metas     = (res.get("metadatas")  or [[]])[0]
        dists     = (res.get("distances")  or [[]])[0]
        out: list[dict] = []
        for i, doc_id in enumerate(ids):
            out.append({
                "id":       doc_id,
                "document": docs[i]  if i < len(docs)  else "",
                "metadata": metas[i] if i < len(metas) else {},
                "distance": float(dists[i]) if i < len(dists) else None,
            })
        return out

    def semantic_recall(self, text: str, k: int = 3) -> dict[str, list[dict]]:
        """Convenience: query *all* four collections at once. Returns a
        dict keyed by collection name. Empty collections are omitted
        so the caller's loop stays tight."""
        out: dict[str, list[dict]] = {}
        if not self.enabled or not text:
            return out
        for name in COLLECTIONS:
            hits = self.query(name, text, k=k)
            if hits:
                out[name] = hits
        return out

    def count(self, collection: str) -> int:
        """Number of items currently stored in a collection. Returns 0
        when disabled or unknown collection. Never raises."""
        if not self.enabled:
            return 0
        coll = self._collections.get(collection)
        if coll is None:
            return 0
        try:
            return int(coll.count())
        except Exception:
            return 0

    # ── internals ──────────────────────────────────────────────────
    @staticmethod
    def _normalize_metadata(d: dict) -> dict:
        """Chroma metadata must be flat scalars (str / int / float /
        bool). Coerce everything; drop None and empty containers."""
        out: dict[str, Any] = {}
        for k, v in d.items():
            key = str(k)[:120]
            if v is None:
                continue
            if isinstance(v, bool) or isinstance(v, (int, float)):
                out[key] = v
            elif isinstance(v, str):
                out[key] = v[:500]   # bound size
            else:
                # lists / dicts — stringify so we don't lose them
                try:
                    out[key] = str(v)[:500]
                except Exception:
                    continue
        return out
