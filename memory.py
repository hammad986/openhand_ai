"""
memory.py - Deep Memory System v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Layers:
  1. Chat history       (JSON, fast, windowed)
  2. Task logs          (SQLite, queryable)
  3. Code snippets      (SQLite, reusable)
  4. Project context    (SQLite, per-project understanding)
  5. Long-term learnings (JSON, agent self-improvement)

Schema overview:
  tasks(id, task, status, result, api_used, tokens, created_at)
  snippets(name, lang, code, description, used_count, created_at)
  projects(name, description, stack, files_json, notes, updated_at)
  learnings(id, category, insight, created_at)
"""

import json, sqlite3, logging, re, hashlib, math
from datetime import datetime
from pathlib import Path
from config import Config

logger = logging.getLogger(__name__)

try:
    import chromadb
    CHROMA_AVAILABLE = True
except Exception:
    chromadb = None
    CHROMA_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer as _ST
    SMODEL_AVAILABLE = True
except Exception:
    _ST = None
    SMODEL_AVAILABLE = False

# Phase 13C — additional vector layer (Chroma) used for *long-term*
# semantic recall across sessions. The legacy `_init_vector_store`
# below remains for backward compatibility with `find_similar_task` /
# `get_relevant_learnings`. The new VectorStore is ADDITIVE: it owns
# the four collections required by Phase 13C
# (tasks / solutions / errors / reflections) and degrades silently
# when the chromadb dep is missing.
try:
    from vector_store import VectorStore as _VectorStore
except Exception:
    _VectorStore = None


class Memory:
    def __init__(self, config: Config = None):
        self.config    = config or Config()
        self.json_path = Path(self.config.MEMORY_FILE)
        self.db_path   = Path(self.config.MEMORY_FILE.replace(".json", ".db"))
        self.checkpoint_path = Path(self.config.CHECKPOINT_FILE)
        self.vector_dir = Path(self.config.VECTOR_DB_DIR)
        self._vector_enabled = False
        self._task_collection = None
        self._learning_collection = None
        self._init_db()
        self._init_vector_store()
        self._data     = self._load_json()

        # Phase 13C — semantic memory layer. Lives in its OWN sub-dir
        # (chroma/) under VECTOR_DB_DIR so it never collides with the
        # legacy `tasks` / `learnings` collections used elsewhere in
        # this file. Construction is silent on failure: when chromadb
        # is missing or the on-disk store can't be opened, `vs.enabled`
        # stays False and every hook below is a no-op — the existing
        # SQLite scoring path keeps working unchanged.
        if _VectorStore is not None:
            try:
                self.vs = _VectorStore(
                    path=str(self.vector_dir / "chroma"))
            except Exception as e:
                logger.warning(f"[Memory] VectorStore init failed: {e!r}")
                self.vs = None
        else:
            self.vs = None

    # ══════════════════════════════════════════════════
    # 1. CHAT HISTORY
    # ══════════════════════════════════════════════════
    def add_message(self, role: str, content: str):
        self._data.setdefault("messages", []).append(
            {"role": role, "content": content, "ts": self._ts()}
        )
        self._save_json()

    def get_messages(self, last_n: int = 20) -> list:
        """Return last N messages stripped of timestamps (for LLM)"""
        msgs = self._data.get("messages", [])[-last_n:]
        return [{"role": m["role"], "content": m["content"]} for m in msgs]

    def clear_messages(self):
        self._data["messages"] = []
        self._save_json()

    # ══════════════════════════════════════════════════
    # 2. TASK LOGS
    # ══════════════════════════════════════════════════
    def log_task(self, task: str, status: str, result: str,
                 api_used: str = "", tokens: int = 0):
        cur = self._conn().execute(
            "INSERT INTO tasks(task,status,result,api_used,tokens,created_at) VALUES(?,?,?,?,?,?)",
            (task, status, result[:3000], api_used, tokens, self._ts())
        )
        self._db.commit()
        self._index_task(cur.lastrowid, task, status, result[:3000])
        # Phase 13C — mirror to long-term semantic store. Best-effort.
        # The VectorStore method is itself silent on failure; we still
        # wrap to be paranoid because this is on the hot write path.
        if self.vs is not None:
            try:
                self.vs.add(
                    "tasks",
                    f"task:{cur.lastrowid}",
                    f"{task}\n→ {status}\n{result[:600]}",
                    {"task": task[:300], "status": status,
                     "api_used": api_used, "tokens": tokens})
            except Exception:
                pass

    def get_task_history(self, limit: int = 20) -> list:
        rows = self._conn().execute(
            "SELECT id,task,status,api_used,tokens,created_at FROM tasks ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"id":r[0],"task":r[1],"status":r[2],"api":r[3],"tokens":r[4],"time":r[5]} for r in rows]

    def find_similar_task(self, task: str, limit: int = 3) -> list:
        """Semantic search over prior tasks with keyword fallback."""
        if self._vector_enabled:
            try:
                query = self._task_collection.query(
                    query_embeddings=[self._embed(task)],
                    n_results=limit,
                    include=["metadatas", "documents", "distances"],
                )
                out = []
                metadatas = query.get("metadatas", [[]])[0]
                docs = query.get("documents", [[]])[0]
                for i, meta in enumerate(metadatas):
                    out.append({
                        "task": meta.get("task", docs[i] if i < len(docs) else ""),
                        "status": meta.get("status", "unknown"),
                        "result": meta.get("result", "")[:200],
                    })
                if out:
                    return out
            except Exception as e:
                logger.warning(f"Semantic task search failed, falling back: {e}")

        words = re.findall(r"[a-zA-Z0-9_]+", task.lower())[:8]
        results = []
        for row in self._conn().execute("SELECT task,status,result FROM tasks ORDER BY id DESC LIMIT 150"):
            if any(w in row[0].lower() for w in words):
                results.append({"task": row[0], "status": row[1], "result": row[2][:200]})
            if len(results) >= limit:
                break
        return results

    # ══════════════════════════════════════════════════
    # 3. CODE SNIPPETS
    # ══════════════════════════════════════════════════
    def save_snippet(self, name: str, code: str, lang: str = "python",
                     description: str = "") -> None:
        self._conn().execute("""
            INSERT OR REPLACE INTO snippets(name,lang,code,description,used_count,created_at)
            VALUES(?,?,?,?,COALESCE((SELECT used_count FROM snippets WHERE name=?),0),?)
        """, (name, lang, code, description, name, self._ts()))
        self._db.commit()

    def get_snippet(self, name: str) -> str | None:
        self._conn().execute(
            "UPDATE snippets SET used_count=used_count+1 WHERE name=?", (name,)
        )
        self._db.commit()
        row = self._conn().execute("SELECT code FROM snippets WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def list_snippets(self) -> list:
        rows = self._conn().execute(
            "SELECT name,lang,description,used_count FROM snippets ORDER BY used_count DESC"
        ).fetchall()
        return [{"name":r[0],"lang":r[1],"desc":r[2],"uses":r[3]} for r in rows]

    # ══════════════════════════════════════════════════
    # 4. PROJECT CONTEXT
    # ══════════════════════════════════════════════════
    def set_project(self, name: str, description: str = "", stack: str = "",
                    files: list = None, notes: str = ""):
        """Store project understanding so agent remembers context across sessions"""
        self._conn().execute("""
            INSERT OR REPLACE INTO projects(name,description,stack,files_json,notes,updated_at)
            VALUES(?,?,?,?,?,?)
        """, (name, description, stack, json.dumps(files or []), notes, self._ts()))
        self._db.commit()
        self.set("active_project", name)

    def get_project(self, name: str = None) -> dict | None:
        name = name or self.get("active_project")
        if not name: return None
        row = self._conn().execute(
            "SELECT name,description,stack,files_json,notes,updated_at FROM projects WHERE name=?",
            (name,)
        ).fetchone()
        if not row: return None
        return {"name":row[0],"description":row[1],"stack":row[2],
                "files":json.loads(row[3]),"notes":row[4],"updated":row[5]}

    def update_project_files(self, files: list, name: str = None):
        name = name or self.get("active_project")
        if name:
            self._conn().execute(
                "UPDATE projects SET files_json=?,updated_at=? WHERE name=?",
                (json.dumps(files), self._ts(), name)
            )
            self._db.commit()

    def project_context_prompt(self) -> str:
        """Returns a context string the agent can prepend to prompts"""
        proj = self.get_project()
        if not proj: return ""
        return (
            f"\n[PROJECT CONTEXT]\n"
            f"Name: {proj['name']}\n"
            f"Stack: {proj['stack']}\n"
            f"Files: {', '.join(proj['files'][:15])}\n"
            f"Notes: {proj['notes']}\n"
        )

    # ══════════════════════════════════════════════════
    # 5. LONG-TERM LEARNINGS
    # ══════════════════════════════════════════════════
    def add_learning(self, category: str, insight: str):
        """
        Agent records things it learned (e.g. "Flask needs DEBUG=False for prod").
        Categories: error_fix, best_practice, user_preference, api_behavior
        """
        cur = self._conn().execute(
            "INSERT INTO learnings(category,insight,created_at) VALUES(?,?,?)",
            (category, insight, self._ts())
        )
        self._db.commit()
        self._index_learning(cur.lastrowid, category, insight)
        # Phase 13C — failure_pattern / improvement / success_pattern
        # entries are written by `Orchestrator.reflect()` and represent
        # post-run analysis — exactly the long-term recall signal we
        # want to surface in the next run's pre-plan hook. Index them
        # in the `reflections` collection. Other categories
        # (best_practice / user_preference / api_behavior / error_fix)
        # stay in the legacy `learnings` collection only.
        if self.vs is not None and category in (
                "failure_pattern", "improvement", "success_pattern",
                "strategy_score", "reflection"):
            try:
                self.vs.add(
                    "reflections",
                    f"learning:{cur.lastrowid}",
                    f"[{category}] {insight}",
                    {"category": category, "insight": insight[:400]})
            except Exception:
                pass

    def get_learnings(self, category: str = None, limit: int = 20) -> list:
        if category:
            rows = self._conn().execute(
                "SELECT category,insight,created_at FROM learnings WHERE category=? ORDER BY id DESC LIMIT ?",
                (category, limit)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT category,insight,created_at FROM learnings ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [{"category":r[0],"insight":r[1],"time":r[2]} for r in rows]

    def get_relevant_learnings(self, query: str, limit: int = 8) -> list:
        if self._vector_enabled:
            try:
                result = self._learning_collection.query(
                    query_embeddings=[self._embed(query)],
                    n_results=limit,
                    include=["metadatas", "documents"],
                )
                out = []
                metadatas = result.get("metadatas", [[]])[0]
                docs = result.get("documents", [[]])[0]
                for i, meta in enumerate(metadatas):
                    out.append({
                        "category": meta.get("category", "general"),
                        "insight": meta.get("insight", docs[i] if i < len(docs) else ""),
                        "time": meta.get("created_at", ""),
                    })
                if out:
                    return out
            except Exception as e:
                logger.warning(f"Semantic learning search failed, falling back: {e}")

        words = re.findall(r"[a-zA-Z0-9_]+", query.lower())[:8]
        out = []
        for row in self._conn().execute("SELECT category,insight,created_at FROM learnings ORDER BY id DESC LIMIT 100"):
            if not words or any(w in row[1].lower() for w in words):
                out.append({"category": row[0], "insight": row[1], "time": row[2]})
            if len(out) >= limit:
                break
        return out

    def learnings_prompt(self, query: str = "") -> str:
        learnings = self.get_relevant_learnings(query, limit=10) if query else self.get_learnings(limit=10)
        if not learnings: return ""
        items = "\n".join(f"- [{l['category']}] {l['insight']}" for l in learnings)
        return f"\n[PAST LEARNINGS]\n{items}\n"

    # ══════════════════════════════════════════════════
    # 5b. STRUCTURED EXECUTION RECORDS  (Phase 9 — learning loop)
    # ══════════════════════════════════════════════════
    def record_execution(self, task: str, subtask: str, strategy: str,
                         success: bool, error_type: str = "",
                         fix_applied: str = "",
                         elapsed_seconds: float = 0.0) -> int:
        """Persist one orchestrator attempt. Returns the new row id.

        Every (subtask, strategy) attempt — successful or not — is logged
        here. The decision engine reads this table to score strategies
        and to map error categories back to fixes that have worked
        before. Length-cap text fields so a runaway error doesn't blow
        out the SQLite row size.
        """
        cur = self._conn().execute(
            "INSERT INTO executions(task, subtask, strategy, success, "
            "error_type, fix_applied, elapsed_seconds, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task[:500], subtask[:500], strategy[:32],
             1 if success else 0, (error_type or "")[:64],
             (fix_applied or "")[:500], float(elapsed_seconds or 0.0),
             self._ts())
        )
        self._db.commit()
        new_id = cur.lastrowid
        # Cheap retention guard: only inspect/prune every Nth write so
        # we don't pay the COUNT cost on every single insert.
        if new_id and (new_id % 50) == 0:
            self._prune_executions()

        # Phase 13C — long-term semantic recall. Successful attempts
        # carrying a non-empty `fix_applied` go into `solutions`
        # (these are the actually-actionable recipes the next planner
        # should see). Failed attempts go into `errors`, keyed by
        # error category so we can surface "we hit this before, here's
        # what was tried" the next time a similar task shows up.
        if self.vs is not None and new_id:
            try:
                if success and (fix_applied or "").strip():
                    self.vs.add(
                        "solutions",
                        f"sol:{new_id}",
                        f"{subtask}\nstrategy={strategy}\nfix={fix_applied}",
                        {"task":     task[:300],
                         "subtask":  subtask[:300],
                         "strategy": strategy,
                         "elapsed":  float(elapsed_seconds or 0.0),
                         "fix":      fix_applied[:300]})
                elif not success:
                    self.vs.add(
                        "errors",
                        f"err:{new_id}",
                        f"{subtask}\nstrategy={strategy}\n"
                        f"error_type={error_type}",
                        {"task":       task[:300],
                         "subtask":    subtask[:300],
                         "strategy":   strategy,
                         "error_type": (error_type or "generic")[:64]})
            except Exception:
                pass
        return new_id

    # ── Phase 13C — public semantic recall surface ─────────────────
    # Two thin wrappers so callers (orchestrator) never reach into
    # `self.vs` directly. Both are ALWAYS safe to call: if the vector
    # store is disabled or chromadb is missing, they return empty
    # values without raising.
    def semantic_recall(self, text: str, k: int = 3) -> dict[str, list[dict]]:
        """Return up to `k` semantically-similar entries from each of
        the four long-term collections (tasks / solutions / errors /
        reflections). Empty collections are omitted from the result."""
        if self.vs is None:
            return {}
        try:
            return self.vs.semantic_recall(text, k=k)
        except Exception:
            return {}

    def add_reflection_summary(self, task: str, summary: dict) -> None:
        """Persist a reflect() summary verbatim into the `reflections`
        collection. Best-effort, silent on failure."""
        if self.vs is None or not summary:
            return
        try:
            doc = (f"{task}\n"
                   f"subtasks_total={summary.get('subtasks_total', 0)} "
                   f"ok={summary.get('subtasks_ok', 0)} "
                   f"failed={summary.get('subtasks_failed', 0)}\n"
                   f"strategies={summary.get('strategies_used', [])}")
            # A stable id derived from task+ts so re-reflections of
            # the same task replace (upsert) the previous summary
            # rather than ballooning the collection.
            rid = f"refl:{abs(hash(task)) & 0xFFFFFFFF}:{int(self._now())}"
            self.vs.add(
                "reflections", rid, doc,
                {"task":            task[:300],
                 "subtasks_total":  int(summary.get("subtasks_total", 0)),
                 "subtasks_ok":     int(summary.get("subtasks_ok", 0)),
                 "subtasks_failed": int(summary.get("subtasks_failed", 0))})
        except Exception:
            pass

    # ── Phase 15 — tool-outcome learning ───────────────────────────
    # Two thin methods that mirror the design of `record_execution` /
    # `semantic_recall` but are scoped to the controlled-tool layer
    # (Phase 14). Both are SAFE to call when the vector store is
    # disabled — `record_tool_outcome` still writes to the legacy
    # `learnings` table, and `recall_tool_stats` returns {}.
    def record_tool_outcome(self, task: str, subtask: str,
                            tool: str, args: dict,
                            success: bool, usefulness: float,
                            reason: str = "") -> None:
        """Persist a (subtask → tool → outcome) row.

        `success` is the boolean from `Tools.execute`; `usefulness`
        is a 0..1 score the *orchestrator* assigns AFTER it knows
        whether the surrounding sub-task ultimately passed
        verification (so the same tool call can be `success=True`
        + `usefulness=0.2` if the prefetch ran but the subtask
        still failed). `reason` is a short human-readable string
        for the observability event.

        Always silent on failure — never raises into the orchestrator.
        """
        try:
            arg_repr = json.dumps(args or {}, sort_keys=True,
                                  default=str)[:300]
        except Exception:
            arg_repr = ""
        # Legacy text learning so `get_learnings('tool_outcome')`
        # works even without Chroma. Bounded so a runaway insight
        # can't blow the row size.
        try:
            insight = (f"{tool}({arg_repr}) on \"{subtask[:80]}\" → "
                       f"{'ok' if success else 'fail'} "
                       f"useful={usefulness:.2f}"
                       f"{' (' + reason[:80] + ')' if reason else ''}")
            self.add_learning("tool_outcome", insight[:400])
        except Exception:
            pass
        # Vector store mirror — keyed by a stable tool+ts id so a
        # repeated identical (tool, args, subtask) tuple replaces
        # the older row (newest outcome wins) instead of bloating.
        if self.vs is None:
            return
        try:
            # Stable, cross-process digest. Python's built-in hash()
            # is randomized per interpreter, so the same tuple would
            # get a fresh id on every restart and the "newest wins
            # upsert" contract above would silently degrade into
            # "duplicate every restart". sha1 keeps the id identical
            # across runs so identical (tool, args, subtask) tuples
            # always upsert into the same Chroma row.
            _digest_input = f"{tool}|{arg_repr}|{subtask[:80]}".encode()
            doc_id = "to:" + hashlib.sha1(_digest_input).hexdigest()[:16]
            doc = (f"{subtask}\n"
                   f"tool={tool} args={arg_repr}\n"
                   f"outcome={'ok' if success else 'fail'} "
                   f"useful={usefulness:.2f} reason={reason[:120]}")
            self.vs.add(
                "tool_outcomes", doc_id, doc,
                {"task":       task[:300],
                 "subtask":    subtask[:300],
                 "tool":       tool,
                 "args":       arg_repr,
                 "success":    bool(success),
                 "usefulness": float(max(0.0, min(1.0, usefulness))),
                 "reason":     reason[:200]})
        except Exception:
            pass

    def recall_tool_stats(self, text: str,
                          k: int = 8) -> dict[str, dict]:
        """Aggregate per-tool stats from the K nearest tool outcomes.

        Returns `{tool_name: {n, ok, fail, useful_avg}}`. Empty when
        the vector store is disabled or no neighbors exist. The
        caller (tool decision layer) uses this to bias the LLM
        prompt toward tools that worked on similar sub-tasks.

        `useful_avg` is the mean `usefulness` over all matched rows
        (clamped to [0,1] when written), so callers can both rank
        tools and *avoid* ones whose history is uniformly unhelpful.
        """
        if self.vs is None:
            return {}
        try:
            rows = self.vs.query("tool_outcomes", text, k=k)
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for r in rows or []:
            meta = r.get("metadata") or {}
            tool = meta.get("tool")
            if not tool:
                continue
            slot = out.setdefault(tool, {"n": 0, "ok": 0,
                                         "fail": 0, "useful_sum": 0.0})
            slot["n"]   += 1
            slot["ok"]  += 1 if meta.get("success") else 0
            slot["fail"] += 0 if meta.get("success") else 1
            try:
                slot["useful_sum"] += float(meta.get("usefulness") or 0.0)
            except (TypeError, ValueError):
                pass
        # Finalize the average so callers don't have to.
        for tool, slot in out.items():
            n = max(1, slot["n"])
            slot["useful_avg"] = round(slot.pop("useful_sum") / n, 3)
        return out

    EXECUTIONS_MAX_ROWS = 5000

    def _prune_executions(self,
                          max_rows: int | None = None) -> int:
        """Keep at most `max_rows` execution rows. Deletes oldest by id.
        Returns the number of rows deleted (0 when under cap)."""
        cap = max_rows if max_rows is not None else self.EXECUTIONS_MAX_ROWS
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        if total <= cap:
            return 0
        # Find the id-cutoff for the newest `cap` rows, then delete
        # everything older. Single round-trip + indexed comparison.
        cutoff = c.execute(
            "SELECT id FROM executions ORDER BY id DESC LIMIT 1 OFFSET ?",
            (cap - 1,)
        ).fetchone()
        if not cutoff:
            return 0
        deleted = c.execute(
            "DELETE FROM executions WHERE id < ?", (cutoff[0],)
        ).rowcount
        self._db.commit()
        return deleted

    def query_executions(self, task: str = "", limit: int = 50) -> list:
        """Return recent execution rows, optionally biased toward
        keyword-matching `task`. No semantic search needed — strategy
        scoring is a simple aggregation, so cheap keyword overlap is
        plenty for picking which past attempts are 'similar'."""
        if task:
            words = re.findall(r"[a-zA-Z0-9_]+", task.lower())[:6]
        else:
            words = []
        rows = self._conn().execute(
            "SELECT id, task, subtask, strategy, success, error_type, "
            "fix_applied, elapsed_seconds, created_at "
            "FROM executions ORDER BY id DESC LIMIT ?", (max(limit * 4, 200),)
        ).fetchall()
        out = []
        for r in rows:
            row = {
                "id": r[0], "task": r[1], "subtask": r[2],
                "strategy": r[3], "success": bool(r[4]),
                "error_type": r[5] or "", "fix_applied": r[6] or "",
                "elapsed_seconds": r[7] or 0.0, "created_at": r[8],
            }
            if not words:
                out.append(row)
            else:
                hay = (row["task"] + " " + row["subtask"]).lower()
                if any(w in hay for w in words):
                    out.append(row)
            if len(out) >= limit:
                break
        return out

    def strategy_stats(self, task: str = "") -> dict:
        """Return per-strategy aggregates for picking the best strategy.

        Output: {strategy_name: {attempts, successes, success_rate,
        avg_elapsed, score}}.

        `score` combines success rate and speed:
            score = success_rate * (1 / (1 + avg_elapsed/30))
        — caps the speed term so a 0-second crash doesn't dominate.
        """
        rows = self.query_executions(task=task, limit=200)
        agg: dict = {}
        for r in rows:
            s = agg.setdefault(r["strategy"],
                               {"attempts": 0, "successes": 0,
                                "elapsed_total": 0.0})
            s["attempts"]      += 1
            s["successes"]     += 1 if r["success"] else 0
            s["elapsed_total"] += r["elapsed_seconds"] or 0.0
        out = {}
        for name, s in agg.items():
            n   = max(s["attempts"], 1)
            sr  = s["successes"] / n
            avg = s["elapsed_total"] / n
            speed = 1.0 / (1.0 + (avg / 30.0))
            out[name] = {
                "attempts":     s["attempts"],
                "successes":    s["successes"],
                "success_rate": round(sr, 3),
                "avg_elapsed":  round(avg, 2),
                "score":        round(sr * speed, 4),
            }
        return out

    def find_fix_for_error(self, error_type: str) -> str | None:
        """Return the most-frequently-recorded `fix_applied` string for
        successful runs that hit the same `error_type`. None if memory
        has nothing useful yet."""
        if not error_type:
            return None
        rows = self._conn().execute(
            "SELECT fix_applied FROM executions "
            "WHERE error_type=? AND success=1 AND fix_applied != '' "
            "ORDER BY id DESC LIMIT 50",
            (error_type[:64],)
        ).fetchall()
        if not rows:
            return None
        counts: dict = {}
        for (fix,) in rows:
            counts[fix] = counts.get(fix, 0) + 1
        # Return the most common fix string (ties broken by recency
        # because we walked DESC).
        return max(counts.items(), key=lambda kv: kv[1])[0]

    # ══════════════════════════════════════════════════
    # 5c. SKILLS  (Phase 10 — reusable recipe library)
    # ══════════════════════════════════════════════════
    # A skill is a frozen-but-evolving recipe: a list of steps + the
    # strategy that historically beat them, scored by success rate.
    # Lookup is intentionally simple keyword overlap (same approach as
    # query_executions) so the agent has zero infra dependencies.

    SKILL_MIN_KEYWORDS  = 2     # need >=2 overlapping keywords to call it a hit
    SKILL_PATTERN_WORDS = 8     # cap pattern size so long tasks don't blow it up

    # Phase 11 — concept abstraction. Maps surface synonyms to canonical
    # concept tokens so "login system" and "authentication system"
    # match the same skill. Hand-curated; small but covers the most
    # common dev verbs/nouns. Anything not in the map passes through
    # unchanged so unknown words still match literally.
    CONCEPT_MAP = {
        # auth / identity
        "login": "auth", "signin": "auth", "sign_in": "auth",
        "signon": "auth", "logon": "auth", "authenticate": "auth",
        "authentication": "auth", "auth": "auth", "register": "auth",
        "signup": "auth", "sign_up": "auth", "logout": "auth",
        "session": "auth", "credential": "auth", "credentials": "auth",
        "password": "auth", "user": "auth",
        # build / create verbs
        "build": "build", "create": "build", "make": "build",
        "generate": "build", "develop": "build", "implement": "build",
        "write": "build", "scaffold": "build", "setup": "build",
        "set_up": "build", "add": "build", "new": "build",
        # api
        "api": "api", "endpoint": "api", "rest": "api", "route": "api",
        "router": "api", "graphql": "api", "rpc": "api",
        # fix / debug
        "fix": "fix", "debug": "fix", "repair": "fix", "patch": "fix",
        "resolve": "fix", "solve": "fix", "correct": "fix",
        # test
        "test": "test", "tests": "test", "testing": "test",
        "spec": "test", "specs": "test", "unittest": "test",
        "pytest": "test", "verify": "test",
        # database
        "database": "db", "db": "db", "sql": "db", "table": "db",
        "schema": "db", "sqlite": "db", "postgres": "db",
        "postgresql": "db", "mysql": "db", "query": "db",
        # deploy
        "deploy": "deploy", "publish": "deploy", "release": "deploy",
        "ship": "deploy", "rollout": "deploy",
        # web framework synonyms
        "flask": "web", "django": "web", "fastapi": "web",
        "express": "web", "next": "web", "nextjs": "web",
        # files / io
        "file": "file", "files": "file", "directory": "file",
        "folder": "file", "path": "file",
        # ui / frontend
        "ui": "ui", "frontend": "ui", "component": "ui", "page": "ui",
        "view": "ui", "form": "ui",
    }

    @staticmethod
    def _keywords(text: str) -> list:
        """Lowercase + tokenize + de-stop. Used for both pattern
        derivation and lookup matching, so they stay in sync."""
        stop = {"a", "an", "and", "the", "to", "of", "for", "in", "on",
                "with", "that", "this", "is", "are", "be", "it",
                "then", "after", "simple", "system", "app", "application"}
        toks = re.findall(r"[a-zA-Z0-9_]+", (text or "").lower())
        return [t for t in toks if t and t not in stop and len(t) > 1]

    @classmethod
    def _concept_keywords(cls, text: str) -> list:
        """Same as `_keywords` but each token is replaced by its canonical
        concept (per CONCEPT_MAP) so synonyms collapse. Phase 11
        abstraction layer — used by `find_skill` for fuzzy concept-level
        matching and by `merge_similar_skills` for de-duplication."""
        out, seen = [], set()
        for kw in cls._keywords(text):
            c = cls.CONCEPT_MAP.get(kw, kw)
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def save_skill(self, name: str, task_pattern: str,
                   steps: list, best_strategy: str = "",
                   elapsed_seconds: float = 0.0) -> int:
        """Insert OR upgrade a skill. If the same `task_pattern`
        already exists, bump its success_count + recompute success_rate
        + refresh `last_used`; otherwise insert a fresh row.
        Returns the skill id."""
        # Normalize the pattern so future lookups are deterministic.
        kws = self._keywords(task_pattern)[:self.SKILL_PATTERN_WORDS]
        norm_pattern = " ".join(sorted(set(kws)))
        if not norm_pattern:
            return 0
        # Phase 11 — record the concept pattern alongside the literal
        # one so synonym lookups can hit even when the wording diverges.
        concept_kws  = self._concept_keywords(task_pattern)[:self.SKILL_PATTERN_WORDS]
        concept_pat  = " ".join(sorted(set(concept_kws)))
        try:
            steps_list = [str(s)[:300] for s in steps][:8]
            steps_json = json.dumps(steps_list)
        except Exception:
            steps_list = []
            steps_json = "[]"
        step_count = len(steps_list)
        c = self._conn()
        # Race-safe upsert: a UNIQUE INDEX on task_pattern (created in
        # _ensure_schema) guarantees ON CONFLICT can fire. The counter
        # math runs in SQL so two concurrent writers can't lose updates.
        # Note: do NOT overwrite steps_json/step_count on conflict —
        # `record_skill_use` is the only path that refines stored steps,
        # so a re-save mustn't blow away an already-optimized recipe.
        c.execute(
            "INSERT INTO skills(name, task_pattern, concept_pattern, "
            "steps_json, step_count, best_strategy, success_count, "
            "failure_count, success_rate, avg_elapsed, last_used, "
            "created_at) "
            "VALUES(?,?,?,?,?,?,1,0,1.0,?,?,?) "
            "ON CONFLICT(task_pattern) DO UPDATE SET "
            "  name             = excluded.name, "
            "  concept_pattern  = excluded.concept_pattern, "
            "  best_strategy    = excluded.best_strategy, "
            "  success_count    = skills.success_count + 1, "
            "  success_rate     = ROUND( "
            "      (skills.success_count + 1.0) / "
            "      (skills.success_count + skills.failure_count + 1.0), "
            "      4), "
            "  avg_elapsed      = ROUND( "
            "      (skills.avg_elapsed * skills.success_count + ?) / "
            "      (skills.success_count + 1), 3), "
            "  last_used        = excluded.last_used",
            (name[:120], norm_pattern, concept_pat, steps_json,
             step_count, (best_strategy or "")[:32],
             round(float(elapsed_seconds), 3), self._ts(), self._ts(),
             float(elapsed_seconds))
        )
        self._db.commit()
        # Resolve the actual row id (may be existing or new).
        row = c.execute(
            "SELECT id FROM skills WHERE task_pattern=? LIMIT 1",
            (norm_pattern,)
        ).fetchone()
        return row[0] if row else 0

    def find_skill(self, task: str,
                   min_success_rate: float = 0.5) -> dict | None:
        """Return the best-matching skill above the success-rate floor,
        or None.

        Phase 11 — matching is concept-aware:
          1. We score each candidate twice: once on raw keywords
             (literal overlap) and once on concept tokens (CONCEPT_MAP-
             collapsed). The higher score wins, scaled by 1.0 for
             literal hits and 0.85 for concept-only hits so a literal
             match still outranks a synonym match of equal coverage.
          2. Tie-break order: (effective_score, success_rate,
             success_count). Recency is implicit via the SQL ORDER BY.
        """
        raw_task_kws     = set(self._keywords(task))
        concept_task_kws = set(self._concept_keywords(task))
        # Need at least N tokens on the input side, regardless of which
        # representation matches — otherwise short noise like "fix it"
        # would match too eagerly.
        if max(len(raw_task_kws), len(concept_task_kws)) \
                < self.SKILL_MIN_KEYWORDS:
            return None
        rows = self._conn().execute(
            "SELECT id, name, task_pattern, steps_json, best_strategy, "
            "success_count, failure_count, success_rate, avg_elapsed, "
            "last_used, concept_pattern, step_count, strategy_stats "
            "FROM skills WHERE success_rate >= ? "
            "ORDER BY id DESC",
            (float(min_success_rate),)
        ).fetchall()
        best          = None
        best_tie      = None
        best_via      = ""
        best_raw_score = 0.0
        for r in rows:
            literal_kws = set((r[2] or "").split())
            concept_kws = set((r[10] or "").split())

            literal_overlap = raw_task_kws & literal_kws
            concept_overlap = concept_task_kws & concept_kws

            literal_score = (len(literal_overlap) / max(len(raw_task_kws), 1)) \
                            if len(literal_overlap) >= self.SKILL_MIN_KEYWORDS \
                            else 0.0
            concept_score = (len(concept_overlap) / max(len(concept_task_kws), 1)) \
                            if len(concept_overlap) >= self.SKILL_MIN_KEYWORDS \
                            else 0.0

            # Concept matches are slightly discounted vs literal so an
            # exact-wording skill still wins when both are equally good.
            eff_literal = literal_score * 1.0
            eff_concept = concept_score * 0.85

            if eff_literal == 0.0 and eff_concept == 0.0:
                continue

            if eff_literal >= eff_concept:
                score, via, raw = eff_literal, "literal", literal_score
            else:
                score, via, raw = eff_concept, "concept", concept_score

            tie = (score, r[7], r[5])
            if best is None or tie > best_tie:
                best_tie       = tie
                best           = r
                best_via       = via
                best_raw_score = raw
        if not best:
            return None
        try:
            steps = json.loads(best[3])
        except Exception:
            steps = []
        try:
            strategy_stats = json.loads(best[12] or "{}")
        except Exception:
            strategy_stats = {}
        return {
            "id":             best[0],
            "name":           best[1],
            "task_pattern":   best[2],
            "steps":          steps,
            "best_strategy":  best[4] or "",
            "success_count":  best[5],
            "failure_count":  best[6],
            "success_rate":   best[7],
            "avg_elapsed":    best[8],
            "last_used":      best[9],
            "concept_pattern": best[10] or "",
            "step_count":     best[11] or len(steps),
            "strategy_stats": strategy_stats,
            "match_score":    round(best_raw_score, 3),
            "match_via":      best_via,
        }

    def record_skill_use(self, skill_id: int, success: bool,
                         elapsed_seconds: float = 0.0,
                         strategy: str | None = None,
                         steps_used: list | None = None) -> dict:
        """Update a skill's score after the orchestrator used it.

        Phase 11 enhancements:
          * `strategy` — if provided, increments the per-strategy
            wins/losses tally in `strategy_stats` and promotes a new
            `best_strategy` whenever a different strategy now has both
            ≥3 wins AND a strictly higher win-rate than the incumbent.
          * `steps_used` — on a successful run that was both faster
            (elapsed < 0.7 * recorded avg_elapsed) AND used strictly
            fewer steps than the stored recipe, replace `steps_json`
            with the leaner version. This is the self-optimization
            loop: the agent literally writes a shorter procedure for
            itself when it discovers one.

        Returns a small dict describing what changed (best-effort —
        swallows on missing id).
        """
        report: dict = {"updated": False}
        if not skill_id:
            return report
        c = self._conn()
        row = c.execute(
            "SELECT success_count, failure_count, avg_elapsed, "
            "best_strategy, strategy_stats, step_count, steps_json "
            "FROM skills WHERE id=?", (skill_id,)
        ).fetchone()
        if not row:
            return report
        sc, fc, avg_e, cur_best, stats_json, cur_step_count, cur_steps_json = row
        if success:
            sc += 1
        else:
            fc += 1
        total = sc + fc
        sr = sc / total if total else 0.0
        new_avg = (avg_e * (total - 1) + float(elapsed_seconds)) / total \
            if total else 0.0
        report["updated"] = True

        # ── strategy_stats accounting ───────────────────────────────
        try:
            stats = json.loads(stats_json or "{}")
            if not isinstance(stats, dict):
                stats = {}
        except Exception:
            stats = {}
        new_best = cur_best
        if strategy:
            entry = stats.setdefault(str(strategy), {"wins": 0, "losses": 0,
                                                     "avg_elapsed": 0.0})
            n_prior = entry["wins"] + entry["losses"]
            if success:
                entry["wins"] += 1
            else:
                entry["losses"] += 1
            entry["avg_elapsed"] = round(
                (entry["avg_elapsed"] * n_prior + float(elapsed_seconds))
                / max(n_prior + 1, 1), 3)
            # Best-strategy promotion: another strategy must have ≥3
            # wins AND a strictly higher win-rate than the incumbent.
            cur_wr = -1.0
            cur_entry = stats.get(cur_best, {}) if cur_best else {}
            if cur_entry:
                cur_total = cur_entry["wins"] + cur_entry["losses"]
                cur_wr = cur_entry["wins"] / cur_total if cur_total else 0.0
            for s, e in stats.items():
                t = e["wins"] + e["losses"]
                if e["wins"] >= 3 and t > 0:
                    wr = e["wins"] / t
                    if wr > cur_wr + 0.05:  # 5% margin to avoid churn
                        new_best = s
                        cur_wr   = wr
                        report["promoted_strategy"] = s
        # Hard cap on the number of strategies we'll remember per
        # skill, so a runaway agent that invents strategy names
        # on the fly can't bloat this column. Keep the most-used.
        _MAX_STRAT_KEYS = 16
        if len(stats) > _MAX_STRAT_KEYS:
            keep = sorted(
                stats.items(),
                key=lambda kv: (kv[1].get("wins", 0) +
                                kv[1].get("losses", 0)),
                reverse=True,
            )[:_MAX_STRAT_KEYS]
            stats = dict(keep)
            # If the incumbent best got evicted, fall back to "" so
            # _meta_decide doesn't keep referencing a stale name.
            if new_best and new_best not in stats:
                new_best = ""
        stats_blob = json.dumps(stats)

        # ── step refinement (self-optimization) ─────────────────────
        new_steps_json = cur_steps_json
        new_step_count = cur_step_count
        if (success and steps_used and avg_e > 0
                and float(elapsed_seconds) < avg_e * 0.7
                and len(steps_used) < (cur_step_count or 99)):
            try:
                new_steps_json = json.dumps(
                    [str(s)[:300] for s in steps_used][:8])
                new_step_count = min(len(steps_used), 8)
                report["refined_steps"] = new_step_count
            except Exception:
                pass

        c.execute(
            "UPDATE skills SET success_count=?, failure_count=?, "
            "success_rate=?, avg_elapsed=?, last_used=?, "
            "best_strategy=?, strategy_stats=?, "
            "steps_json=?, step_count=? WHERE id=?",
            (sc, fc, round(sr, 4), round(new_avg, 3), self._ts(),
             (new_best or "")[:32], stats_blob,
             new_steps_json, new_step_count, skill_id)
        )
        self._db.commit()
        return report

    def merge_similar_skills(self,
                             jaccard_threshold: float = 0.7,
                             max_pairs: int = 50) -> list:
        """Phase 11 — collapse near-duplicate skills.

        Walks the (small) skills table; for every pair whose
        concept-keyword Jaccard similarity is ≥ `jaccard_threshold`,
        merges them into the row with the higher experience score:
          * sums success/failure counts
          * recomputes success_rate and weighted avg_elapsed
          * keeps the steps_json from the row that was the winner
          * deletes the loser

        Returns a list of `{kept, dropped, sim}` event dicts (one per
        merge), capped at `max_pairs`. Safe to call repeatedly — a
        no-op if nothing merges.
        """
        c = self._conn()
        # Wrap the whole select-then-mutate sequence in a single
        # transaction so a concurrent decay/save can't delete a row
        # under us between the SELECT and the UPDATE/DELETE below.
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            "SELECT id, name, task_pattern, concept_pattern, steps_json, "
            "step_count, best_strategy, success_count, failure_count, "
            "success_rate, avg_elapsed, last_used, strategy_stats "
            "FROM skills"
        ).fetchall()
        # id → mutable dict so we can fold counts as we merge.
        skills = {r[0]: {
            "id":             r[0],
            "name":           r[1],
            "task_pattern":   r[2],
            "concept_kws":    set((r[3] or "").split()),
            "steps_json":     r[4],
            "step_count":     r[5] or 0,
            "best_strategy":  r[6] or "",
            "sc":             r[7] or 0,
            "fc":             r[8] or 0,
            "sr":             r[9] or 0.0,
            "avg":            r[10] or 0.0,
            "last_used":      r[11],
            "stats_json":     r[12] or "{}",
        } for r in rows}

        events: list = []
        ids = list(skills.keys())
        dropped: set = set()
        for i in range(len(ids)):
            if ids[i] in dropped:
                continue
            for j in range(i + 1, len(ids)):
                if ids[j] in dropped:
                    continue
                a = skills[ids[i]]
                b = skills[ids[j]]
                ka, kb = a["concept_kws"], b["concept_kws"]
                if not ka or not kb:
                    continue
                inter = len(ka & kb)
                union = len(ka | kb)
                if union == 0:
                    continue
                sim = inter / union
                if sim < jaccard_threshold:
                    continue
                # Decide winner by experience score.
                ea = self.experience_score(a["sr"], a["sc"] + a["fc"],
                                           a["last_used"])
                eb = self.experience_score(b["sr"], b["sc"] + b["fc"],
                                           b["last_used"])
                winner, loser = (a, b) if ea >= eb else (b, a)
                # Sum counts; recompute rate + weighted average.
                tw = winner["sc"] + winner["fc"]
                tl = loser["sc"]  + loser["fc"]
                merged_sc = winner["sc"] + loser["sc"]
                merged_fc = winner["fc"] + loser["fc"]
                merged_total = merged_sc + merged_fc
                merged_sr  = merged_sc / merged_total if merged_total else 0.0
                merged_avg = ((winner["avg"] * tw + loser["avg"] * tl)
                              / merged_total) if merged_total else 0.0
                # Merge per-strategy stats by summing wins/losses.
                try:
                    sa = json.loads(winner["stats_json"] or "{}")
                    sb = json.loads(loser["stats_json"]  or "{}")
                except Exception:
                    sa, sb = {}, {}
                merged_stats = dict(sa)
                for s, e in (sb or {}).items():
                    cur = merged_stats.setdefault(
                        s, {"wins": 0, "losses": 0, "avg_elapsed": 0.0})
                    n_old = cur["wins"] + cur["losses"]
                    n_new = e.get("wins", 0) + e.get("losses", 0)
                    cur["wins"]   += e.get("wins", 0)
                    cur["losses"] += e.get("losses", 0)
                    if n_old + n_new:
                        cur["avg_elapsed"] = round(
                            (cur["avg_elapsed"] * n_old
                             + e.get("avg_elapsed", 0.0) * n_new)
                            / (n_old + n_new), 3)
                # Persist winner update + drop loser.
                c.execute(
                    "UPDATE skills SET success_count=?, failure_count=?, "
                    "success_rate=?, avg_elapsed=?, strategy_stats=?, "
                    "last_used=? WHERE id=?",
                    (merged_sc, merged_fc, round(merged_sr, 4),
                     round(merged_avg, 3), json.dumps(merged_stats),
                     self._ts(), winner["id"])
                )
                c.execute("DELETE FROM skills WHERE id=?", (loser["id"],))
                # Reflect in our in-memory mirror so subsequent pairs
                # see the merged numbers.
                winner["sc"], winner["fc"]   = merged_sc, merged_fc
                winner["sr"], winner["avg"]  = merged_sr, merged_avg
                winner["stats_json"]         = json.dumps(merged_stats)
                dropped.add(loser["id"])
                events.append({
                    "kept":    winner["id"],
                    "dropped": loser["id"],
                    "kept_name":    winner["name"],
                    "dropped_name": loser["name"],
                    "similarity":   round(sim, 3),
                })
                if len(events) >= max_pairs:
                    break
            if len(events) >= max_pairs:
                break
        # Always close the BEGIN IMMEDIATE — commit on success even if
        # no events fired (frees the write lock); rollback if anything
        # in the loop above crashed.
        try:
            self._db.commit()
        except sqlite3.OperationalError:
            self._db.rollback()
        return events

    def decay_unused_skills(self,
                            days_unused: int = 30,
                            decay_factor: float = 0.95,
                            drop_below_sr: float = 0.1,
                            min_uses: int = 3) -> dict:
        """Phase 11 — periodically penalize stale skills.

        For every skill not used in `days_unused` days:
          * multiply `success_rate` by `decay_factor` (default 0.95)
          * if the resulting sr < `drop_below_sr` AND the skill has
            been used at least `min_uses` times (so we don't drop a
            brand-new skill that just hasn't seen action yet),
            DELETE the row.

        Returns counts: {decayed, dropped}. Safe to call frequently —
        rows touched today are skipped entirely.
        """
        cutoff = self._ts(self._now() - days_unused * 86400)
        c = self._conn()
        # Same atomicity story as merge_similar_skills — without an
        # explicit transaction, two parallel agents could both see a
        # row pre-deletion and try to UPDATE/DELETE it twice.
        c.execute("BEGIN IMMEDIATE")
        try:
            rows = c.execute(
                "SELECT id, success_rate, success_count, failure_count "
                "FROM skills WHERE last_used IS NULL OR last_used < ?",
                (cutoff,)
            ).fetchall()
            decayed = dropped = 0
            for sid, sr, sc, fc in rows:
                new_sr = round(float(sr or 0.0) * float(decay_factor), 4)
                uses   = (sc or 0) + (fc or 0)
                if new_sr < drop_below_sr and uses >= min_uses:
                    c.execute("DELETE FROM skills WHERE id=?", (sid,))
                    dropped += 1
                else:
                    c.execute(
                        "UPDATE skills SET success_rate=? WHERE id=?",
                        (new_sr, sid)
                    )
                    decayed += 1
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return {"decayed": decayed, "dropped": dropped}

    @staticmethod
    def experience_score(success_rate: float,
                         uses: int,
                         last_used: str | None) -> float:
        """Phase 11 — composite ranking signal:
            sr * log(1+uses) / (1 + age_days/30)
        Higher = more trustworthy. Used to break ties in `find_skill`,
        order `list_skills`, and pick the winner during merging."""
        import math
        age_penalty = 1.0
        if last_used:
            try:
                age_days = max(
                    (Memory._now_static()
                     - Memory._parse_ts_static(last_used)) / 86400.0, 0.0)
                age_penalty = 1.0 + (age_days / 30.0)
            except Exception:
                pass
        return round(
            (max(float(success_rate or 0.0), 0.0)
             * math.log(1.0 + max(int(uses or 0), 0))
             / age_penalty), 4)

    def list_skills(self, limit: int = 20) -> list:
        rows = self._conn().execute(
            "SELECT id, name, task_pattern, concept_pattern, "
            "best_strategy, success_count, failure_count, success_rate, "
            "avg_elapsed, last_used, step_count, strategy_stats "
            "FROM skills"
        ).fetchall()
        out = []
        for r in rows:
            uses = (r[5] or 0) + (r[6] or 0)
            try:
                stats = json.loads(r[11] or "{}")
            except Exception:
                stats = {}
            out.append({
                "id":              r[0],
                "name":            r[1],
                "task_pattern":    r[2],
                "concept_pattern": r[3] or "",
                "best_strategy":   r[4] or "",
                "success_count":   r[5],
                "failure_count":   r[6],
                "success_rate":    r[7],
                "avg_elapsed":     r[8],
                "last_used":       r[9],
                "step_count":      r[10] or 0,
                "strategy_stats":  stats,
                "experience":      self.experience_score(
                                       r[7] or 0.0, uses, r[9]),
            })
        # Phase 11 — rank by experience score, NOT raw success_rate.
        out.sort(key=lambda s: s["experience"], reverse=True)
        return out[:limit]

    # ══════════════════════════════════════════════════
    # CHECKPOINTS
    # ══════════════════════════════════════════════════
    def save_checkpoint(self, state: dict):
        payload = {"saved_at": self._ts(), **state}
        self.checkpoint_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_checkpoint(self, task: str = "") -> dict | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            data = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            if task and data.get("task") and data.get("task") != task:
                return None
            return data
        except Exception as e:
            logger.warning(f"Failed to load checkpoint: {e}")
            return None

    def clear_checkpoint(self):
        if self.checkpoint_path.exists():
            self.checkpoint_path.unlink()

    # ══════════════════════════════════════════════════
    # KV STORE
    # ══════════════════════════════════════════════════
    def set(self, key: str, value):
        self._data.setdefault("kv", {})[key] = value
        self._save_json()

    def get(self, key: str, default=None):
        return self._data.get("kv", {}).get(key, default)

    # ══════════════════════════════════════════════════
    # DISPLAY
    # ══════════════════════════════════════════════════
    def show_history(self):
        tasks = self.get_task_history()
        if not tasks:
            print("No task history."); return
        print(f"\n{'─'*70}")
        print(f"{'ID':<4} {'Status':<10} {'API':<12} {'Tok':<6} {'Time':<20} Task")
        print("─"*70)
        for t in tasks:
            task_s = (t["task"][:35] + "...") if len(t["task"]) > 35 else t["task"]
            print(f"{t['id']:<4} {t['status']:<10} {t['api']:<12} {t['tokens']:<6} {t['time']:<20} {task_s}")
        print("─"*70)

    def show_learnings(self):
        ls = self.get_learnings()
        print("\n📚 Long-term Learnings")
        for l in ls:
            print(f"  [{l['category']}] {l['insight']}")

    def clear(self):
        self._data = {"messages": [], "kv": {}}
        self._save_json()
        for table in ("tasks", "snippets", "projects", "learnings",
                      "executions", "skills"):
            self._conn().execute(f"DELETE FROM {table}")
        self._db.commit()
        if self._vector_enabled:
            try:
                self._task_collection.delete(where={})
            except Exception:
                pass
            try:
                self._learning_collection.delete(where={})
            except Exception:
                pass
        self.clear_checkpoint()
        logger.info("Memory cleared.")

    # ══════════════════════════════════════════════════
    # INTERNALS
    # ══════════════════════════════════════════════════
    def _init_db(self):
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT, status TEXT, result TEXT,
                api_used TEXT, tokens INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS snippets(
                name TEXT PRIMARY KEY, lang TEXT, code TEXT,
                description TEXT, used_count INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS projects(
                name TEXT PRIMARY KEY, description TEXT, stack TEXT,
                files_json TEXT, notes TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS learnings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT, insight TEXT, created_at TEXT
            );
            -- Phase 9 — structured execution records for the learning loop.
            -- Every orchestrator attempt (per strategy) writes one row here.
            -- The decision engine queries this table to pick strategies and
            -- map error categories to known fixes.
            CREATE TABLE IF NOT EXISTS executions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT,
                subtask TEXT,
                strategy TEXT,
                success INTEGER DEFAULT 0,
                error_type TEXT DEFAULT '',
                fix_applied TEXT DEFAULT '',
                elapsed_seconds REAL DEFAULT 0,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_executions_strategy
                ON executions(strategy, success);
            CREATE INDEX IF NOT EXISTS idx_executions_error_type
                ON executions(error_type, success);
            -- Phase 10 — reusable skills. A "skill" is a frozen recipe
            -- (ordered steps + winning strategy) that worked end-to-end
            -- for a class of similar tasks. The orchestrator looks up a
            -- matching skill BEFORE planning so it can skip re-deciding
            -- a problem it has already solved.
            CREATE TABLE IF NOT EXISTS skills(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                task_pattern TEXT NOT NULL,
                concept_pattern TEXT DEFAULT '',
                steps_json TEXT NOT NULL,
                step_count INTEGER DEFAULT 0,
                best_strategy TEXT DEFAULT '',
                strategy_stats TEXT DEFAULT '{}',
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                success_rate REAL DEFAULT 0.0,
                avg_elapsed REAL DEFAULT 0.0,
                last_used TEXT,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_skills_pattern
                ON skills(task_pattern);
            CREATE INDEX IF NOT EXISTS idx_skills_score
                ON skills(success_rate DESC, success_count DESC);
            -- Phase 17 — Autonomous task chains. A "chain" is a long-
            -- running goal split into ordered "tasks" (one row each in
            -- chain_tasks). Lives in the same SQLite DB as the rest of
            -- Memory so it inherits the same connection + persistence
            -- guarantees, and so a chain naturally survives an
            -- orchestrator process restart.
            CREATE TABLE IF NOT EXISTS chains(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chain_tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id INTEGER NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                parent_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chain_id) REFERENCES chains(id)
            );
            CREATE INDEX IF NOT EXISTS idx_chain_tasks_chain
                ON chain_tasks(chain_id, status);
            CREATE INDEX IF NOT EXISTS idx_chain_tasks_status
                ON chain_tasks(status, id);
            -- Phase 19 — Self-driven goals: persistent event log for
            -- goal_generated / goal_started / goal_completed. Kept
            -- separate from per-session `logs` because goals are
            -- chain-scoped and outlive any single session.
            CREATE TABLE IF NOT EXISTS goal_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                chain_id INTEGER,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_goal_events_id
                ON goal_events(id);
            CREATE INDEX IF NOT EXISTS idx_goal_events_chain
                ON goal_events(chain_id);
            -- Phase 20.2 — Code Learning Memory: persistent
            -- error→fix knowledge base. A row starts life on a
            -- failed code-execution attempt (success=0, fix_applied
            -- empty) and is updated when a fix succeeds — or a new
            -- success row is inserted directly when caller already
            -- has both error and fix on hand. Lightweight by design:
            -- no embeddings, no vector store; similarity scoring
            -- happens in `code_learning.py` via difflib.
            CREATE TABLE IF NOT EXISTS code_lessons(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL,
                file TEXT NOT NULL DEFAULT '',
                fix_applied TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 0,
                context TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_code_lessons_type
                ON code_lessons(error_type);
            CREATE INDEX IF NOT EXISTS idx_code_lessons_success
                ON code_lessons(success, id DESC);
            -- Composite index for the dominant retrieval pattern:
            -- "successful fixes for this error_type, newest first"
            -- (used by code_learning.find_similar pre-filter).
            CREATE INDEX IF NOT EXISTS idx_code_lessons_type_success
                ON code_lessons(error_type, success, id DESC);
        """)
        # Phase 11 — additive ALTER TABLE migration. The CREATE above
        # is a no-op on existing DBs (already has the table), so for
        # those we need to add the new columns explicitly. SQLite is
        # cooperative here: adding a NULLable / defaulted column is
        # cheap and non-blocking.
        for col, ddl in (
            ("concept_pattern",  "ALTER TABLE skills ADD COLUMN concept_pattern TEXT DEFAULT ''"),
            ("step_count",       "ALTER TABLE skills ADD COLUMN step_count INTEGER DEFAULT 0"),
            ("strategy_stats",   "ALTER TABLE skills ADD COLUMN strategy_stats TEXT DEFAULT '{}'"),
            # Phase 18 — Goal Intelligence. Each chain task gets a
            # priority score (computed lazily, higher first), a 1-10
            # importance hint, an optional comma-separated list of
            # task IDs it depends on, and a skip reason for adaptive
            # execution. All defaulted so Phase 17 chains keep working.
            ("priority",        "ALTER TABLE chain_tasks ADD COLUMN priority REAL DEFAULT 0.0"),
            ("importance",      "ALTER TABLE chain_tasks ADD COLUMN importance INTEGER DEFAULT 5"),
            ("depends_on",      "ALTER TABLE chain_tasks ADD COLUMN depends_on TEXT DEFAULT ''"),
            ("skipped_reason",  "ALTER TABLE chain_tasks ADD COLUMN skipped_reason TEXT DEFAULT ''"),
            ("replanned_at",    "ALTER TABLE chain_tasks ADD COLUMN replanned_at TEXT DEFAULT ''"),
            # Phase 18 — chain-level replan budget. Capped in
            # task_chains.py so a chain can never replan forever.
            ("replan_count",    "ALTER TABLE chains ADD COLUMN replan_count INTEGER DEFAULT 0"),
            # Phase 19 — Self-driven goals. `system_generated` flags a
            # chain that the goal engine produced (vs. a user task).
            # `confidence` is the engine's 0..1 score for the
            # candidate. `auto_source` records the signal that drove
            # generation (e.g. "repeated_failure:network_timeout").
            ("system_generated", "ALTER TABLE chains ADD COLUMN system_generated INTEGER DEFAULT 0"),
            ("confidence",       "ALTER TABLE chains ADD COLUMN confidence REAL DEFAULT 1.0"),
            ("auto_source",      "ALTER TABLE chains ADD COLUMN auto_source TEXT DEFAULT ''"),
        ):
            try:
                self._db.execute(ddl)
            except sqlite3.OperationalError:
                # Column already exists — ignore.
                pass
        # Index on concept_pattern must come AFTER the ALTER TABLE so
        # the column exists on legacy databases.
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_skills_concept "
            "ON skills(concept_pattern)"
        )
        # Best-effort migration: dedupe any pre-existing rows that
        # share the same task_pattern (before UNIQUE was added),
        # keeping the row with the highest success_count, then enforce
        # UNIQUE so save_skill's ON CONFLICT upsert is race-safe.
        try:
            self._db.execute("""
                DELETE FROM skills WHERE id NOT IN (
                  SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                             PARTITION BY task_pattern
                             ORDER BY success_count DESC, id DESC
                           ) AS rn
                    FROM skills
                  ) WHERE rn = 1
                )
            """)
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_skills_pattern ON skills(task_pattern)"
            )
        except sqlite3.OperationalError as e:
            # Window functions need sqlite >= 3.25; fall back to a
            # simpler GROUP BY dedupe if missing.
            logger.debug("skills dedupe via window failed: %s", e)
            self._db.execute("""
                DELETE FROM skills WHERE id NOT IN (
                  SELECT MAX(id) FROM skills GROUP BY task_pattern
                )
            """)
            self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_skills_pattern ON skills(task_pattern)"
            )
        self._db.commit()

    def _init_vector_store(self):
        if not CHROMA_AVAILABLE:
            logger.info("Chroma not installed; semantic memory disabled.")
            return
        try:
            self.vector_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self.vector_dir))

            # Probe the embedding dimension we will use for new vectors.
            probe_dim = len(self._embed("probe"))

            # If existing collections were built with a different dimension
            # (e.g. hash-256 → MiniLM-384 upgrade), wipe them so ChromaDB
            # does not reject mismatched embeddings.  Data is re-indexed
            # from SQLite which is the source of truth.
            for coll_name in ("tasks", "learnings"):
                try:
                    coll = client.get_collection(coll_name)
                    sample = coll.get(limit=1, include=["embeddings"])
                    embs = sample.get("embeddings") or []
                    if embs and embs[0] and len(embs[0]) != probe_dim:
                        logger.info(
                            f"[Memory] Embedding dim changed "
                            f"{len(embs[0])}→{probe_dim}; recreating '{coll_name}'"
                        )
                        client.delete_collection(coll_name)
                except Exception:
                    pass  # Collection doesn't exist yet — fine

            self._task_collection = client.get_or_create_collection(
                "tasks", metadata={"hnsw:space": "cosine"}
            )
            self._learning_collection = client.get_or_create_collection(
                "learnings", metadata={"hnsw:space": "cosine"}
            )
            self._vector_enabled = True
            model_tag = "MiniLM" if SMODEL_AVAILABLE else "hash-fallback"
            logger.info(f"[Memory] Vector store ready ({model_tag}, dim={probe_dim})")
            self._reindex_vectors()
        except Exception as e:
            logger.warning(f"Failed to init vector store: {e}")
            self._vector_enabled = False

    def _reindex_vectors(self):
        if not self._vector_enabled:
            return
        try:
            for row in self._conn().execute("SELECT id,task,status,result,created_at FROM tasks ORDER BY id DESC LIMIT 500"):
                self._index_task(row[0], row[1], row[2], row[3], row[4])
            for row in self._conn().execute("SELECT id,category,insight,created_at FROM learnings ORDER BY id DESC LIMIT 500"):
                self._index_learning(row[0], row[1], row[2], row[3])
        except Exception as e:
            logger.warning(f"Vector reindex skipped: {e}")

    def _index_task(self, task_id: int, task: str, status: str, result: str, created_at: str = ""):
        if not self._vector_enabled:
            return
        doc = f"Task: {task}\nStatus: {status}\nResult: {result[:500]}"
        meta = {
            "task": task[:500],
            "status": status[:60],
            "result": result[:500],
            "created_at": created_at or self._ts(),
        }
        self._task_collection.upsert(
            ids=[f"task:{task_id}"],
            documents=[doc],
            metadatas=[meta],
            embeddings=[self._embed(doc)],
        )

    def _index_learning(self, learning_id: int, category: str, insight: str, created_at: str = ""):
        if not self._vector_enabled:
            return
        doc = f"[{category}] {insight}"
        meta = {
            "category": category[:120],
            "insight": insight[:500],
            "created_at": created_at or self._ts(),
        }
        self._learning_collection.upsert(
            ids=[f"learning:{learning_id}"],
            documents=[doc],
            metadatas=[meta],
            embeddings=[self._embed(doc)],
        )

    # ── Class-level model cache (loaded once, shared across instances) ─────────
    _smodel = None   # SentenceTransformer instance, False if unavailable

    def _embed(self, text: str) -> list[float]:
        """Return a normalised embedding vector for *text*.

        Uses sentence-transformers all-MiniLM-L6-v2 (384-dim, ~90 MB RAM)
        when the library is installed, otherwise falls back to a SHA-1
        hash projection (lightweight, no extra dependencies).
        """
        text = (text or "").strip()
        if SMODEL_AVAILABLE:
            try:
                if Memory._smodel is None:
                    logger.info("[Memory] Loading all-MiniLM-L6-v2 (~90 MB, one-time)...")
                    Memory._smodel = _ST("all-MiniLM-L6-v2")
                    logger.info("[Memory] Embedding model ready.")
                if Memory._smodel is not False:
                    vec = Memory._smodel.encode(text or " ", normalize_embeddings=True)
                    return vec.tolist()
            except Exception as e:
                logger.warning(f"[Memory] MiniLM encode failed ({e}), falling back to hash")
                Memory._smodel = False  # disable for this session
        return self._embed_hash(text)

    def _embed_hash(self, text: str) -> list[float]:
        """SHA-1 hash projection fallback — no external dependencies."""
        dim = max(int(self.config.EMBEDDING_DIM), 64)
        vec = [0.0] * dim
        tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        for tok in tokens:
            h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
            vec[h % dim] += 1.0 if ((h >> 8) & 1) else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def _conn(self):
        return self._db

    def _load_json(self) -> dict:
        if self.json_path.exists():
            try: return json.loads(self.json_path.read_text())
            except Exception: return {}
        return {}

    def _save_json(self):
        self.json_path.write_text(json.dumps(self._data, indent=2))

    def _ts(self, when: float | None = None) -> str:
        """Format an epoch float as our standard SQL string. When called
        with no argument, returns now()."""
        dt = datetime.fromtimestamp(when) if when is not None \
            else datetime.now()
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # ────────────────────────────────────────────────────────────
    # Phase 17 — Autonomous Task Chains
    #
    # A "chain" is a long-running goal that may need MANY orchestrator
    # runs to complete. Sub-goals (one per orchestrator.run() call) live
    # in `chain_tasks` keyed back to the parent `chains` row. The CRUD
    # methods below are intentionally tiny and best-effort: any sqlite
    # error is logged and surfaced as a None / empty return so a flaky
    # DB never breaks the higher-level runner. Status flow is strictly
    # pending → running → (completed | failed); resume() in the runner
    # demotes any stuck "running" row back to "pending" on startup.
    # ────────────────────────────────────────────────────────────
    def create_chain(self, goal: str,
                     system_generated: bool = False,
                     confidence: float = 1.0,
                     auto_source: str = "") -> int | None:
        """Create a new chain row and return its id. Returns None on
        DB failure (logged) so the caller can short-circuit.

        Phase 19 — `system_generated` flags chains produced by the
        goal engine. `confidence` (0..1) is the engine's score for
        this goal; `auto_source` records the signal that produced it
        (e.g. "repeated_failure:network_timeout"). User-created
        chains keep the existing defaults (system_generated=0,
        confidence=1.0, auto_source='') so behaviour is unchanged."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sg = 1 if system_generated else 0
            conf = max(0.0, min(1.0, float(confidence)))
            cur = self._db.execute(
                "INSERT INTO chains(goal, status, created_at, "
                "updated_at, system_generated, confidence, "
                "auto_source) "
                "VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                (goal, ts, ts, sg, conf, (auto_source or "")[:200]))
            self._db.commit()
            return cur.lastrowid
        except sqlite3.OperationalError as e:
            logger.warning(f"create_chain failed: {e}")
            return None

    def enqueue_task(self, chain_id: int, goal: str,
                     parent_id: int | None = None,
                     importance: int = 5,
                     depends_on: list[int] | None = None,
                     priority: float = 0.0) -> int | None:
        """Append a pending task to a chain. Returns task id or None.

        Phase 18 — accepts importance (1-10), depends_on (list of task
        ids that must complete first), and an initial priority score
        (the runner recomputes this lazily anyway, but seeding it lets
        the next_pending_task ORDER BY work on the very first pop)."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            deps_str = ",".join(str(int(d)) for d in (depends_on or []))
            # Clamp importance to the 1-10 contract so a bad input
            # can't poison the priority calculation downstream.
            imp = max(1, min(int(importance), 10))
            cur = self._db.execute(
                "INSERT INTO chain_tasks(chain_id, goal, status, "
                "parent_id, importance, depends_on, priority, "
                "created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (chain_id, goal, parent_id, imp, deps_str,
                 float(priority), ts, ts))
            self._db.execute(
                "UPDATE chains SET updated_at = ? WHERE id = ?",
                (ts, chain_id))
            self._db.commit()
            return cur.lastrowid
        except sqlite3.OperationalError as e:
            logger.warning(f"enqueue_task failed: {e}")
            return None

    def next_pending_task(self, chain_id: int) -> dict | None:
        """Return the highest-priority pending task for `chain_id` whose
        dependencies are all satisfied (completed or skipped). None if
        the chain has nothing eligible.

        Phase 18: ORDER BY priority DESC, id ASC. Dependency filtering
        is done in Python rather than SQL because depends_on is stored
        as a CSV string — keeping the SQL portable and the join cost
        zero on the common (no-deps) path."""
        try:
            rows = self._db.execute(
                "SELECT id, chain_id, goal, status, parent_id, attempts, "
                "last_error, created_at, updated_at, "
                "priority, importance, depends_on, skipped_reason "
                "FROM chain_tasks "
                "WHERE chain_id = ? AND status = 'pending' "
                "ORDER BY priority DESC, id ASC",
                (chain_id,)).fetchall()
            if not rows:
                return None
            # Build a lookup of terminal-status tasks for dep checks.
            # Terminal = completed | skipped. Failed deps DO block —
            # if a prerequisite failed, the dependent task can't run.
            terminal = {r[0] for r in self._db.execute(
                "SELECT id FROM chain_tasks "
                "WHERE chain_id = ? AND status IN "
                "('completed', 'skipped')",
                (chain_id,)).fetchall()}
            cols = ("id", "chain_id", "goal", "status", "parent_id",
                    "attempts", "last_error", "created_at", "updated_at",
                    "priority", "importance", "depends_on",
                    "skipped_reason")
            for row in rows:
                d = dict(zip(cols, row))
                deps = self._parse_deps(d.get("depends_on", "") or "")
                if all(dep in terminal for dep in deps):
                    return d
            # All pending tasks are gated by unmet deps.
            return None
        except sqlite3.OperationalError as e:
            logger.warning(f"next_pending_task failed: {e}")
            return None

    @staticmethod
    def _parse_deps(deps_str: str) -> list[int]:
        """Parse the comma-separated `depends_on` column into a list of
        ints, ignoring blanks and non-numeric tokens defensively."""
        out = []
        for tok in (deps_str or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok))
            except ValueError:
                continue
        return out

    def mark_task_running(self, task_id: int) -> bool:
        """Flip pending → running and bump attempts. Returns False if
        the task wasn't in pending (race with another runner)."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._db.execute(
                "UPDATE chain_tasks "
                "SET status = 'running', attempts = attempts + 1, "
                "    updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (ts, task_id))
            self._db.commit()
            return cur.rowcount > 0
        except sqlite3.OperationalError as e:
            logger.warning(f"mark_task_running failed: {e}")
            return False

    def mark_task_done(self, task_id: int, ok: bool,
                       reason: str = "") -> bool:
        """Terminal transition: (running | pending) → completed | failed.

        Phase 18 — refuses to overwrite a row that's ALREADY in a
        terminal state (`completed` / `failed` / `skipped`). This
        prevents a late duplicate terminal hook from clobbering the
        result of an earlier handler, but still permits the legitimate
        cases of (a) a `running` task finishing, and (b) the runner's
        early 'attempts exhausted' branch terminalising a task that
        was popped in `pending` state without ever being marked
        running. Returns True iff the row was actually transitioned;
        False on no-op / not-found."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            new_status = "completed" if ok else "failed"
            cur = self._db.execute(
                "UPDATE chain_tasks "
                "SET status = ?, last_error = ?, updated_at = ? "
                "WHERE id = ? AND status IN ('running','pending')",
                (new_status, (reason or "")[:500], ts, task_id))
            if cur.rowcount == 0:
                # Already in a terminal state, or the row vanished.
                # No chain roll-up because there was no transition.
                self._db.commit()
                return False
            # Roll up: if any task failed, mark chain failed; else if
            # all completed, mark chain completed; else leave pending.
            row = self._db.execute(
                "SELECT chain_id FROM chain_tasks WHERE id = ?",
                (task_id,)).fetchone()
            if row:
                cid = row[0]
                progress = self.chain_progress(cid)
                if progress.get("failed", 0) > 0:
                    chain_status = "failed"
                elif progress.get("pending", 0) == 0 \
                        and progress.get("running", 0) == 0:
                    chain_status = "completed"
                else:
                    chain_status = "pending"
                self._db.execute(
                    "UPDATE chains SET status = ?, updated_at = ? "
                    "WHERE id = ?", (chain_status, ts, cid))
            self._db.commit()
            return True
        except sqlite3.OperationalError as e:
            logger.warning(f"mark_task_done failed: {e}")
            return False

    def chain_progress(self, chain_id: int) -> dict:
        """Status histogram for a chain. Always returns a dict with the
        five keys (zeros if the chain doesn't exist) so callers don't
        have to defensively .get() each one."""
        out = {"pending": 0, "running": 0, "completed": 0,
               "failed": 0, "total": 0}
        try:
            rows = self._db.execute(
                "SELECT status, COUNT(*) FROM chain_tasks "
                "WHERE chain_id = ? GROUP BY status",
                (chain_id,)).fetchall()
            for status, n in rows:
                if status in out:
                    out[status] = n
                out["total"] += n
            return out
        except sqlite3.OperationalError as e:
            logger.warning(f"chain_progress failed: {e}")
            return out

    def get_chain(self, chain_id: int) -> dict | None:
        """Return {chain: {...}, tasks: [...]} or None if missing.

        Phase 18: surfaces priority / importance / depends_on /
        skipped_reason on tasks and replan_count on the chain so the
        UI can render the new self-management state.

        Phase 19: also surfaces `system_generated` / `confidence` /
        `auto_source` so the chain-completion hook can authoritatively
        loop-guard the goal engine without falling back to a fragile
        list_chains() scan."""
        try:
            chain_row = self._db.execute(
                "SELECT id, goal, status, created_at, updated_at, "
                "COALESCE(replan_count,0), "
                "COALESCE(system_generated,0), "
                "COALESCE(confidence,1.0), "
                "COALESCE(auto_source,'') "
                "FROM chains WHERE id = ?",
                (chain_id,)).fetchone()
            if not chain_row:
                return None
            chain = dict(zip(
                ("id", "goal", "status", "created_at", "updated_at",
                 "replan_count", "system_generated", "confidence",
                 "auto_source"),
                chain_row))
            chain["system_generated"] = bool(chain["system_generated"])
            task_rows = self._db.execute(
                "SELECT id, chain_id, goal, status, parent_id, attempts, "
                "last_error, created_at, updated_at, "
                "priority, importance, depends_on, skipped_reason "
                "FROM chain_tasks WHERE chain_id = ? ORDER BY id ASC",
                (chain_id,)).fetchall()
            cols = ("id", "chain_id", "goal", "status", "parent_id",
                    "attempts", "last_error", "created_at", "updated_at",
                    "priority", "importance", "depends_on",
                    "skipped_reason")
            tasks = [dict(zip(cols, r)) for r in task_rows]
            return {"chain": chain, "tasks": tasks}
        except sqlite3.OperationalError as e:
            logger.warning(f"get_chain failed: {e}")
            return None

    def get_task(self, task_id: int) -> dict | None:
        """Single-row lookup. Used by the runner to refresh a task's
        live state (deps may have changed since it was queued)."""
        try:
            row = self._db.execute(
                "SELECT id, chain_id, goal, status, parent_id, attempts, "
                "last_error, created_at, updated_at, "
                "priority, importance, depends_on, skipped_reason "
                "FROM chain_tasks WHERE id = ?",
                (task_id,)).fetchone()
            if not row:
                return None
            cols = ("id", "chain_id", "goal", "status", "parent_id",
                    "attempts", "last_error", "created_at", "updated_at",
                    "priority", "importance", "depends_on",
                    "skipped_reason")
            return dict(zip(cols, row))
        except sqlite3.OperationalError as e:
            logger.warning(f"get_task failed: {e}")
            return None

    def reset_task_for_retry(self, task_id: int,
                             last_error: str = "") -> bool:
        """Phase 18 — flip a `failed` (or `running`) task back to
        `pending` while preserving its `attempts` counter and recording
        `last_error` so the runner's augmented-goal logic can read it.
        Returns False if the task was already in a non-resettable
        terminal state (`completed` / `skipped`) so callers can detect
        the case where they shouldn't retry."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._db.execute(
                "UPDATE chain_tasks "
                "SET status = 'pending', last_error = ?, "
                "    updated_at = ? "
                "WHERE id = ? AND status IN ('failed','running')",
                ((last_error or "")[:500], ts, task_id))
            self._db.commit()
            # rowcount==0 means the task wasn't in a state we'd reset
            # (e.g. someone else already retried it, or it's a terminal
            # completed/skipped row). Don't raise — that's the caller's
            # signal to stop trying.
            return cur.rowcount > 0
        except sqlite3.OperationalError as e:
            logger.warning(f"reset_task_for_retry failed: {e}")
            return False

    def task_has_replan_children(self, task_id: int) -> bool:
        """Phase 18 idempotency guard — True if any task in the same
        chain already lists `task_id` as its `parent_id`. Used by the
        replan path to avoid double-spawning children when both the
        in-process runner and the subprocess terminal hook fire for
        the same failed task."""
        try:
            row = self._db.execute(
                "SELECT 1 FROM chain_tasks WHERE parent_id = ? LIMIT 1",
                (task_id,)).fetchone()
            return row is not None
        except sqlite3.OperationalError as e:
            logger.warning(f"task_has_replan_children failed: {e}")
            # Fail safe: pretend children exist so we DON'T double-replan.
            return True

    def try_reserve_replan(self, chain_id: int, task_id: int,
                           cap: int) -> tuple[bool, str, int]:
        """Phase 18 — atomic per-task replan reservation.

        Inside a single SQLite IMMEDIATE transaction this method:
          1. Tries to claim the failed task by atomically setting
             `chain_tasks.replanned_at` from empty/NULL to a timestamp
             via `UPDATE ... WHERE id=? AND COALESCE(replanned_at,'')=''`.
             If `rowcount == 0` someone else has already claimed it
             (or the row doesn't exist) — refuse.
          2. Reads the chain's current replan_count and, if >= cap,
             ROLLS BACK so the marker is NOT persisted (we only own
             the slot if we actually consume budget).
          3. Bumps replan_count by 1 and commits everything.

        The marker is the per-task lock: even if a duplicate caller
        slips between our reservation commit and the children INSERT
        in maybe_replan_after_failure, that caller will fail step 1
        because replanned_at is already set. This is strictly stronger
        than the previous `parent_id`-based children-exist check.

        Returns (True, "ok", new_count) on success or
        (False, reason, current_count_or_-1) on refusal."""
        try:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cur = self._db.execute(
                    "UPDATE chain_tasks "
                    "SET replanned_at = ?, updated_at = ? "
                    "WHERE id = ? "
                    "  AND COALESCE(replanned_at, '') = ''",
                    (ts, ts, task_id))
                if cur.rowcount == 0:
                    # Already claimed by another caller — or row gone.
                    self._db.commit()
                    return (False, "task already replanned "
                                   "(reservation claimed)", -1)
                row = self._db.execute(
                    "SELECT COALESCE(replan_count, 0) FROM chains "
                    "WHERE id = ?", (chain_id,)).fetchone()
                if row is None:
                    self._db.rollback()
                    return (False, "chain not found", -1)
                used = int(row[0])
                if used >= cap:
                    # Budget exhausted — release the marker so a future
                    # request after a budget reset could still replan.
                    self._db.rollback()
                    return (False,
                            f"chain replan budget exhausted "
                            f"({used}/{cap})", used)
                self._db.execute(
                    "UPDATE chains "
                    "SET replan_count = COALESCE(replan_count,0) + 1, "
                    "    updated_at = ? WHERE id = ?",
                    (ts, chain_id))
                self._db.commit()
                return (True, "ok", used + 1)
            except Exception:
                # Any failure inside the txn → rollback so neither the
                # marker nor the budget bump is persisted.
                self._db.rollback()
                raise
        except sqlite3.OperationalError as e:
            logger.warning(f"try_reserve_replan failed: {e}")
            return (False, f"db error: {e}", -1)

    def set_task_priority(self, task_id: int,
                          priority: float) -> bool:
        """Persist a recomputed priority. Returns True on success."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.execute(
                "UPDATE chain_tasks "
                "SET priority = ?, updated_at = ? WHERE id = ?",
                (float(priority), ts, task_id))
            self._db.commit()
            return True
        except sqlite3.OperationalError as e:
            logger.warning(f"set_task_priority failed: {e}")
            return False

    def set_task_skipped(self, task_id: int, reason: str = "") -> bool:
        """Adaptive-execution terminal status. 'skipped' tasks satisfy
        downstream dependencies (treated as 'completed' for dep gating)
        but are NOT counted as success in chain roll-up."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.execute(
                "UPDATE chain_tasks "
                "SET status = 'skipped', skipped_reason = ?, "
                "    updated_at = ? "
                "WHERE id = ?",
                ((reason or "")[:300], ts, task_id))
            # Re-roll the chain status the same way mark_task_done does.
            row = self._db.execute(
                "SELECT chain_id FROM chain_tasks WHERE id = ?",
                (task_id,)).fetchone()
            if row:
                cid = row[0]
                p = self.chain_progress(cid)
                if p.get("failed", 0) > 0:
                    chain_status = "failed"
                elif p.get("pending", 0) == 0 \
                        and p.get("running", 0) == 0:
                    chain_status = "completed"
                else:
                    chain_status = "pending"
                self._db.execute(
                    "UPDATE chains SET status = ?, updated_at = ? "
                    "WHERE id = ?", (chain_status, ts, cid))
            self._db.commit()
            return True
        except sqlite3.OperationalError as e:
            logger.warning(f"set_task_skipped failed: {e}")
            return False

    def bump_replan_count(self, chain_id: int) -> int:
        """Increment and return the chain's replan_count. Returns -1
        on DB failure so the caller can refuse to replan."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.execute(
                "UPDATE chains "
                "SET replan_count = COALESCE(replan_count, 0) + 1, "
                "    updated_at = ? WHERE id = ?",
                (ts, chain_id))
            self._db.commit()
            row = self._db.execute(
                "SELECT replan_count FROM chains WHERE id = ?",
                (chain_id,)).fetchone()
            return int(row[0]) if row else -1
        except sqlite3.OperationalError as e:
            logger.warning(f"bump_replan_count failed: {e}")
            return -1

    def chain_replan_count(self, chain_id: int) -> int:
        """Read-only access to a chain's replan budget consumption."""
        try:
            row = self._db.execute(
                "SELECT COALESCE(replan_count, 0) FROM chains "
                "WHERE id = ?", (chain_id,)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError as e:
            logger.warning(f"chain_replan_count failed: {e}")
            return 0

    def list_chains(self, limit: int = 50,
                    system_generated: bool | None = None) -> list:
        """Most-recent-first list of chains with their progress
        histogram pre-merged so the UI doesn't N+1.

        Phase 19 — when `system_generated` is True/False the result
        is filtered to that subset; None (default) returns both."""
        try:
            params = []
            where = ""
            if system_generated is True:
                where = "WHERE COALESCE(system_generated,0) = 1 "
            elif system_generated is False:
                where = "WHERE COALESCE(system_generated,0) = 0 "
            params.append(max(1, min(int(limit), 500)))
            rows = self._db.execute(
                "SELECT id, goal, status, created_at, updated_at, "
                "COALESCE(system_generated,0), COALESCE(confidence,1.0), "
                "COALESCE(auto_source,'') "
                f"FROM chains {where}"
                "ORDER BY id DESC LIMIT ?",
                params).fetchall()
            out = []
            for r in rows:
                cid = r[0]
                out.append({
                    "id": cid, "goal": r[1], "status": r[2],
                    "created_at": r[3], "updated_at": r[4],
                    "system_generated": bool(r[5]),
                    "confidence": float(r[6]),
                    "auto_source": r[7] or "",
                    "progress": self.chain_progress(cid),
                })
            return out
        except sqlite3.OperationalError as e:
            logger.warning(f"list_chains failed: {e}")
            return []

    # ────────────────────────────────────────────────────────────
    # Phase 19 — Self-driven goals: persistence + signal extraction
    # ────────────────────────────────────────────────────────────
    def record_goal_event(self, kind: str, chain_id: int | None,
                          payload: dict | None = None) -> int | None:
        """Append a goal lifecycle event. Returns the new event id.
        Always silent on error so a flaky DB never breaks the runner."""
        try:
            import json as _json
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                pj = _json.dumps(payload or {}, default=str)
            except Exception:
                pj = "{}"
            cur = self._db.execute(
                "INSERT INTO goal_events(ts, kind, chain_id, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (ts, str(kind)[:64], chain_id, pj[:4000]))
            self._db.commit()
            return cur.lastrowid
        except sqlite3.OperationalError as e:
            logger.warning(f"record_goal_event failed: {e}")
            return None

    def list_goal_events(self, since_id: int = 0,
                         limit: int = 200) -> list:
        """Return goal events with id > since_id, oldest first."""
        try:
            import json as _json
            rows = self._db.execute(
                "SELECT id, ts, kind, chain_id, payload_json "
                "FROM goal_events WHERE id > ? "
                "ORDER BY id ASC LIMIT ?",
                (int(since_id), max(1, min(int(limit), 1000)))
            ).fetchall()
            out = []
            for r in rows:
                try:
                    payload = _json.loads(r[4] or "{}")
                except Exception:
                    payload = {}
                out.append({
                    "id": r[0], "ts": r[1], "kind": r[2],
                    "chain_id": r[3], "payload": payload,
                })
            return out
        except sqlite3.OperationalError as e:
            logger.warning(f"list_goal_events failed: {e}")
            return []

    def count_system_chains_since(self, since_ts: str) -> int:
        """Count chains created at or after `since_ts` (inclusive)
        with system_generated=1. Used for the daily-cap safety check."""
        try:
            row = self._db.execute(
                "SELECT COUNT(*) FROM chains "
                "WHERE COALESCE(system_generated,0) = 1 "
                "  AND created_at >= ?",
                (since_ts,)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError as e:
            logger.warning(f"count_system_chains_since failed: {e}")
            return 0

    def failure_signals(self, window_seconds: int = 86400,
                        min_count: int = 3) -> list[dict]:
        """Phase 19 — extract candidate improvement signals from the
        `executions` table.

        Groups recent FAILED attempts by `error_type` and returns one
        dict per category that occurred at least `min_count` times in
        the window. Each dict carries enough context for the goal
        engine to phrase a concrete improvement goal:

            {kind: 'repeated_failure',
             key: '<error_type>',
             count: int,
             last_ts: str,
             sample_subtask: str,
             sample_task: str,
             confidence: float}  # 0..1, derived from count

        Confidence saturates at count >= 8 → 1.0; 3 → ~0.45.
        Empty list when nothing meets the threshold (the engine
        treats this as "nothing to do, skip cycle")."""
        try:
            cutoff = datetime.fromtimestamp(
                self._now() - max(60, int(window_seconds))
            ).strftime("%Y-%m-%d %H:%M:%S")
            rows = self._db.execute(
                "SELECT COALESCE(error_type,'generic') AS et, "
                "       COUNT(*) AS n, "
                "       MAX(created_at) AS last_ts, "
                "       MAX(subtask)    AS sample_subtask, "
                "       MAX(task)       AS sample_task "
                "FROM executions "
                "WHERE success = 0 AND created_at >= ? "
                "GROUP BY et "
                "HAVING n >= ? "
                "ORDER BY n DESC LIMIT 10",
                (cutoff, max(1, int(min_count)))).fetchall()
            out = []
            for r in rows:
                n = int(r[1])
                # Logarithmic-ish ramp: 3→0.45, 5→0.65, 8→0.90, 10+→1.0
                conf = max(0.0, min(1.0,
                                    0.30 + 0.10 * n))
                out.append({
                    "kind":           "repeated_failure",
                    "key":            r[0] or "generic",
                    "count":          n,
                    "last_ts":        r[2] or "",
                    "sample_subtask": (r[3] or "")[:200],
                    "sample_task":    (r[4] or "")[:200],
                    "confidence":     round(conf, 3),
                })
            return out
        except sqlite3.OperationalError as e:
            logger.warning(f"failure_signals failed: {e}")
            return []

    # ────────────────────────────────────────────────────────────
    # Phase 20.2 — Code Learning Memory (raw CRUD)
    # ────────────────────────────────────────────────────────────
    # Higher-level helpers (similarity matching, prompt formatting,
    # convenience wrappers) live in `code_learning.py` so memory.py
    # stays a thin persistence layer. Everything here is silent on
    # error to follow the rest-of-module convention — a flaky DB
    # must never break the runner or the critic loop.

    @staticmethod
    def _safe_int(value, default: int) -> int:
        """Coerce arbitrary input to int without raising. Returns
        ``default`` for None / non-numeric / overflow inputs. Used in
        the Phase 20.2 helpers so a runner that passes a stray None
        as `lesson_id` never breaks the silent-CRUD contract."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def insert_code_lesson(
        self,
        error_type: str,
        error_message: str,
        file: str = "",
        fix_applied: str = "",
        success: bool = False,
        context: str = "",
    ) -> int | None:
        """Insert a new code-lesson row. Used in two patterns:

          * Failure-first: caller knows the error but not the fix
            yet → success=False, fix_applied=''. Returns the row id
            so a later `update_code_lesson_fix(id, ...)` can attach
            the resolution.
          * One-shot: caller already has both → success=True,
            fix_applied=<the diff/snippet>. Skip the update step.

        Sizes are clamped defensively to keep one bad input from
        ballooning the DB. Returns the new id or None on failure.

        Silent contract: catches every exception so a flaky DB or
        a bad input never breaks the runner / critic loop.
        """
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._db.execute(
                "INSERT INTO code_lessons("
                "  error_type, error_message, file, fix_applied, "
                "  success, context, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(error_type or "")[:64],
                    str(error_message or "")[:4000],
                    str(file or "")[:512],
                    str(fix_applied or "")[:8000],
                    1 if success else 0,
                    str(context or "")[:8000],
                    ts, ts,
                ))
            self._db.commit()
            return cur.lastrowid
        except Exception as e:  # silent contract — see docstring
            logger.warning(f"insert_code_lesson failed: {e}")
            return None

    def update_code_lesson_fix(
        self,
        lesson_id: int,
        fix_applied: str,
        success: bool = True,
    ) -> bool:
        """Attach a resolution to a previously-recorded failure row.
        Returns False on missing row, bad id, or DB failure — never
        raises (silent contract)."""
        lid = self._safe_int(lesson_id, default=-1)
        if lid <= 0:
            return False
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._db.execute(
                "UPDATE code_lessons "
                "SET fix_applied = ?, success = ?, updated_at = ? "
                "WHERE id = ?",
                (str(fix_applied or "")[:8000],
                 1 if success else 0, ts, lid))
            self._db.commit()
            return cur.rowcount > 0
        except Exception as e:  # silent contract
            logger.warning(f"update_code_lesson_fix failed: {e}")
            return False

    def list_code_lessons(
        self,
        error_type: str | None = None,
        only_successful: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Return recent lessons, newest first. Optional pre-filter
        by `error_type` keeps the candidate set small before the
        Python-side similarity scoring runs in `code_learning.py`.

        Silent contract: returns [] on any failure or bad input."""
        # Coerce limit defensively before SQL — raising here would
        # break the runner's "lookup before fixing" path.
        n = max(1, min(self._safe_int(limit, default=200), 1000))
        try:
            where = []
            params: list = []
            if error_type:
                where.append("error_type = ?")
                params.append(str(error_type)[:64])
            if only_successful:
                where.append("success = 1")
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            params.append(n)
            rows = self._db.execute(
                "SELECT id, error_type, error_message, file, "
                "       fix_applied, success, context, "
                "       created_at, updated_at "
                f"FROM code_lessons {where_sql} "
                "ORDER BY id DESC LIMIT ?",
                tuple(params)).fetchall()
            return [{
                "id": r[0], "error_type": r[1], "error_message": r[2],
                "file": r[3], "fix_applied": r[4],
                "success": bool(r[5]), "context": r[6],
                "created_at": r[7], "updated_at": r[8],
            } for r in rows]
        except Exception as e:  # silent contract
            logger.warning(f"list_code_lessons failed: {e}")
            return []

    def reset_stuck_running(self, chain_id: int) -> int:
        """Resume helper: any task left in 'running' (because the
        process died mid-execution) is demoted back to 'pending' so
        the runner can pick it up again. Returns the count reset."""
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = self._db.execute(
                "UPDATE chain_tasks "
                "SET status = 'pending', updated_at = ? "
                "WHERE chain_id = ? AND status = 'running'",
                (ts, chain_id))
            self._db.commit()
            return cur.rowcount
        except sqlite3.OperationalError as e:
            logger.warning(f"reset_stuck_running failed: {e}")
            return 0

    @staticmethod
    def _now() -> float:
        import time
        return time.time()

    # Static mirrors of _now / _ts parsing so `experience_score` (which
    # is a @staticmethod for cheap reuse during merging) doesn't need
    # a `self` handle.
    @staticmethod
    def _now_static() -> float:
        import time
        return time.time()

    @staticmethod
    def _parse_ts_static(s: str) -> float:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()