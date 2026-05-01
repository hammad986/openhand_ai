"""
long_term_memory.py — Phase 34 Long-Term Memory System
=======================================================
Unified semantic + structured memory layer:
  • VectorStore (ChromaDB) for semantic retrieval across 5 collections
  • SQLite index for fast keyed lookups
  • "Have we solved this before?" deduplication logic
  • Tool-learning persistence: solved tasks → reusable tool recipes
  • Pattern injection: top-k memory hits injected into planner prompts
  • Fully thread-safe, silent on failure
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Chroma import (optional) ──────────────────────────────────────────────────
try:
    import chromadb
    from chromadb.config import Settings as _CSettings
    _CHROMA = True
except Exception:
    chromadb = None        # type: ignore
    _CSettings = None      # type: ignore
    _CHROMA = False


# ─────────────────────────────────────────────────────────────────────────────
# SQLite schema for structured indexing
# ─────────────────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    task_hash   TEXT,
    task        TEXT,
    content     TEXT NOT NULL,
    meta        TEXT DEFAULT '{}',
    success     INTEGER DEFAULT 0,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memories_kind     ON memories(kind);
CREATE INDEX IF NOT EXISTS ix_memories_task_hash ON memories(task_hash);
CREATE INDEX IF NOT EXISTS ix_memories_ts       ON memories(ts DESC);

CREATE TABLE IF NOT EXISTS tools_learned (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    trigger_pattern TEXT,
    code_template TEXT,
    usage_count INTEGER DEFAULT 1,
    last_used   REAL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tools_ts ON tools_learned(ts DESC);

CREATE TABLE IF NOT EXISTS task_patterns (
    pattern     TEXT PRIMARY KEY,
    description TEXT,
    example     TEXT,
    hit_count   INTEGER DEFAULT 1,
    last_seen   REAL,
    ts          REAL NOT NULL
);
"""

_COLLECTIONS = ("tasks", "solutions", "errors", "reflections",
                "tool_outcomes", "tools_learned", "patterns")


class LongTermMemory:
    """Phase 34 unified memory: SQLite index + ChromaDB semantic store.

    Public API (all thread-safe, never raise):
      search(text, k) → list[dict]          — semantic + keyword combined
      remember(kind, task, content, meta, success) → str  — store a memory
      have_we_solved(task) → Optional[dict] — dedup / known-solution check
      inject_context(task, prompt) → str    — prepend relevant memories
      learn_tool(name, desc, pattern, code) — persist a solved-task tool
      find_tool(task) → Optional[dict]      — retrieve matching tool
      match_pattern(task) → Optional[str]   — fastest pattern match
      store_pattern(pattern, desc, example) — upsert pattern count
      recall_patterns(task, k) → list[dict] — top-k patterns for task
    """

    def __init__(self,
                 db_path: str = "./data/long_term_memory.db",
                 vector_path: str = "./memory_vectors/ltm") -> None:
        self._lock = threading.Lock()
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

        # ChromaDB — optional
        self._chroma_client = None
        self._collections: Dict[str, Any] = {}
        self._chroma_ok = False
        self._init_chroma(vector_path)

    # ── DB init ───────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        try:
            with self._conn() as c:
                c.executescript(_DDL)
        except Exception as e:
            logger.warning("[LTM] DB init failed: %s", e)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── ChromaDB init ─────────────────────────────────────────────────────────

    def _init_chroma(self, path: str) -> None:
        if not _CHROMA:
            return
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            settings = _CSettings(anonymized_telemetry=False)  # type: ignore
            self._chroma_client = chromadb.PersistentClient(
                path=path, settings=settings)
            for name in _COLLECTIONS:
                self._collections[name] = (
                    self._chroma_client.get_or_create_collection(
                        name=name,
                        metadata={"hnsw:space": "cosine"}))
            self._chroma_ok = True
        except Exception as e:
            logger.info("[LTM] ChromaDB unavailable: %s — semantic recall disabled", e)

    # ── Core: store ───────────────────────────────────────────────────────────

    def remember(self, kind: str, task: str, content: str,
                 meta: Optional[dict] = None,
                 success: bool = False) -> str:
        """Store a memory. Returns the generated ID."""
        mid   = uuid.uuid4().hex[:16]
        th    = self._hash(task)
        ts    = time.time()
        meta  = meta or {}
        msafe = json.dumps(meta, default=str)[:4000]
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO memories "
                    "(id,kind,task_hash,task,content,meta,success,ts) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (mid, kind, th, task[:500], content[:2000],
                     msafe, int(success), ts))
        except Exception as e:
            logger.debug("[LTM] remember write failed: %s", e)

        # Also push to ChromaDB if available
        if self._chroma_ok:
            coll_name = kind if kind in _COLLECTIONS else "tasks"
            coll = self._collections.get(coll_name)
            if coll:
                try:
                    safe_meta = self._safe_meta(
                        {**meta, "kind": kind, "success": success,
                         "ts": ts, "task": task[:200]})
                    coll.upsert(ids=[mid],
                                documents=[content[:3000]],
                                metadatas=[safe_meta])
                except Exception:
                    pass
        return mid

    # ── Core: search ──────────────────────────────────────────────────────────

    def search(self, text: str, k: int = 5) -> list[dict]:
        """Semantic search across all ChromaDB collections,
        falling back to SQLite keyword search."""
        results: list[dict] = []

        if self._chroma_ok and text:
            for name, coll in self._collections.items():
                try:
                    res = coll.query(query_texts=[text],
                                     n_results=min(k, 5))
                    ids   = (res.get("ids")       or [[]])[0]
                    docs  = (res.get("documents") or [[]])[0]
                    metas = (res.get("metadatas") or [[]])[0]
                    dists = (res.get("distances") or [[]])[0]
                    for i, doc_id in enumerate(ids):
                        results.append({
                            "id":         doc_id,
                            "collection": name,
                            "document":   docs[i]  if i < len(docs)  else "",
                            "metadata":   metas[i] if i < len(metas) else {},
                            "distance":   float(dists[i]) if i < len(dists) else 1.0,
                        })
                except Exception:
                    pass
            results.sort(key=lambda x: x["distance"])
            return results[:k]

        # SQLite keyword fallback
        try:
            words = re.findall(r"\w{4,}", text)[:6]
            if not words:
                return []
            like = " OR ".join([f"content LIKE ?" for _ in words])
            params = [f"%{w}%" for w in words]
            with self._conn() as c:
                rows = c.execute(
                    f"SELECT id,kind,task,content,meta,success,ts "
                    f"FROM memories WHERE {like} "
                    f"ORDER BY ts DESC LIMIT ?",
                    params + [k]).fetchall()
            return [{"id": r["id"], "collection": r["kind"],
                     "document": r["content"],
                     "metadata": json.loads(r["meta"] or "{}"),
                     "distance": 0.5}
                    for r in rows]
        except Exception:
            return []

    # ── Deduplication: "Have we solved this before?" ──────────────────────────

    def have_we_solved(self, task: str) -> Optional[dict]:
        """Return the best past solution for this task (or None)."""
        th = self._hash(task)
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT id,content,meta,ts FROM memories "
                    "WHERE task_hash=? AND success=1 "
                    "ORDER BY ts DESC LIMIT 1",
                    (th,)).fetchone()
            if row:
                return {"id": row["id"], "content": row["content"],
                        "meta": json.loads(row["meta"] or "{}"),
                        "ts": row["ts"]}
        except Exception:
            pass
        # Try semantic recall as fallback
        hits = self.search(task, k=3)
        for h in hits:
            if (h.get("metadata") or {}).get("success"):
                return h
        return None

    # ── Prompt injection ──────────────────────────────────────────────────────

    def inject_context(self, task: str, prompt: str,
                       k: int = 3, max_chars: int = 800) -> str:
        """Prepend relevant memory hits to a planner prompt."""
        hits = self.search(task, k=k)
        if not hits:
            return prompt
        snippets = []
        for h in hits[:k]:
            doc = (h.get("document") or "")[:200]
            snippets.append(f"• {doc}")
        memory_block = "\n".join(snippets)
        prefix = (
            f"[LONG-TERM MEMORY — {len(snippets)} relevant past experiences]\n"
            f"{memory_block}\n\n"
        )
        return prefix + prompt

    # ── Tool learning ─────────────────────────────────────────────────────────

    def learn_tool(self, name: str, description: str,
                   trigger_pattern: str = "", code_template: str = "") -> bool:
        """Persist a new learned tool (or increment usage count)."""
        ts = time.time()
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO tools_learned "
                    "(name,description,trigger_pattern,code_template,"
                    " usage_count,last_used,ts) "
                    "VALUES(?,?,?,?,1,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "usage_count=usage_count+1, last_used=excluded.last_used",
                    (name, description[:500],
                     trigger_pattern[:300], code_template[:5000],
                     ts, ts))
            return True
        except Exception as e:
            logger.debug("[LTM] learn_tool failed: %s", e)
            return False

    def find_tool(self, task: str, threshold: float = 0.4) -> Optional[dict]:
        """Find a stored tool whose trigger_pattern matches this task."""
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT name,description,trigger_pattern,code_template,"
                    "usage_count FROM tools_learned ORDER BY usage_count DESC"
                ).fetchall()
            for row in rows:
                pat = row["trigger_pattern"] or ""
                if pat and re.search(pat, task, re.I):
                    return dict(row)
            # semantic fallback
            hits = self.search(task + " tool", k=3)
            for h in hits:
                if h["distance"] < threshold:
                    m = h.get("metadata") or {}
                    if m.get("kind") == "tool":
                        return m
        except Exception:
            pass
        return None

    # ── Pattern storage ───────────────────────────────────────────────────────

    def store_pattern(self, pattern: str, description: str,
                      example: str = "") -> None:
        ts = time.time()
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO task_patterns "
                    "(pattern,description,example,hit_count,last_seen,ts) "
                    "VALUES(?,?,?,1,?,?) "
                    "ON CONFLICT(pattern) DO UPDATE SET "
                    "hit_count=hit_count+1, last_seen=excluded.last_seen",
                    (pattern, description[:300], example[:200], ts, ts))
        except Exception:
            pass

    def match_pattern(self, task: str) -> Optional[str]:
        """Return the name of the most-used matching pattern for task."""
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT pattern,description FROM task_patterns "
                    "ORDER BY hit_count DESC LIMIT 30"
                ).fetchall()
            for row in rows:
                if re.search(row["pattern"].replace("_", r"\w*"), task, re.I):
                    return row["pattern"]
        except Exception:
            pass
        return None

    def recall_patterns(self, task: str, k: int = 3) -> list[dict]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT pattern,description,example,hit_count,last_seen "
                    "FROM task_patterns ORDER BY hit_count DESC LIMIT ?",
                    (k * 4,)).fetchall()
            return [{"pattern": r["pattern"], "description": r["description"],
                     "example": r["example"], "hits": r["hit_count"]}
                    for r in rows[:k]]
        except Exception:
            return []

    # ── Internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()[:1000]).hexdigest()[:32]

    @staticmethod
    def _safe_meta(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, (bool, int, float, str)):
                out[str(k)[:80]] = v if isinstance(v, str) else v
            else:
                try:
                    out[str(k)[:80]] = str(v)[:300]
                except Exception:
                    pass
        return out

    # ── Stats ──────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        try:
            with self._conn() as c:
                mem_count    = c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                tool_count   = c.execute("SELECT COUNT(*) FROM tools_learned").fetchone()[0]
                pat_count    = c.execute("SELECT COUNT(*) FROM task_patterns").fetchone()[0]
                solved_count = c.execute(
                    "SELECT COUNT(*) FROM memories WHERE success=1").fetchone()[0]
            return {
                "memories":  mem_count,
                "solved":    solved_count,
                "tools":     tool_count,
                "patterns":  pat_count,
                "chroma":    self._chroma_ok,
            }
        except Exception:
            return {}


# ── Module-level singleton ────────────────────────────────────────────────────
_ltm_instance: Optional[LongTermMemory] = None
_ltm_lock = threading.Lock()


def get_ltm(db_path: str = "./data/long_term_memory.db",
            vector_path: str = "./memory_vectors/ltm") -> LongTermMemory:
    global _ltm_instance
    with _ltm_lock:
        if _ltm_instance is None:
            _ltm_instance = LongTermMemory(db_path, vector_path)
    return _ltm_instance
