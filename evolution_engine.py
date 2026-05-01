"""
evolution_engine.py — Phase 37: Self-Evolving System
=====================================================
Handles self-reflection, strategy evolution, prompt optimization,
and proposes safe self-improvements (code patches) for the user.
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from model_router import get_router

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS strategy_stats (
    strategy TEXT PRIMARY KEY,
    attempts INTEGER DEFAULT 0,
    successes INTEGER DEFAULT 0,
    avg_score REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    task TEXT,
    success BOOLEAN,
    worked TEXT,
    failed TEXT,
    inefficiencies TEXT,
    meta_learning TEXT,
    ts REAL
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_id TEXT,
    version INTEGER,
    prompt_text TEXT,
    avg_quality REAL DEFAULT 0.0,
    uses INTEGER DEFAULT 0,
    PRIMARY KEY (prompt_id, version)
);

CREATE TABLE IF NOT EXISTS system_patches (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    target_file TEXT,
    patch_content TEXT,
    status TEXT DEFAULT 'pending',
    created_at REAL
);
"""

class EvolutionEngine:
    def __init__(self, db_path: str = "./data/evolution.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as c:
                c.executescript(_DDL)
                # Seed default strategies
                c.executemany(
                    "INSERT OR IGNORE INTO strategy_stats (strategy) VALUES (?)",
                    [("direct",), ("step_by_step",), ("alternate",)]
                )
                c.commit()
        except Exception as e:
            logger.error(f"[Evolution] DB Init error: {e}")

    # ── 1. Strategy Evolution ────────────────────────────────────────────────
    def record_strategy_result(self, strategy: str, success: bool, score: float):
        try:
            with sqlite3.connect(self.db_path) as c:
                c.execute("""
                    UPDATE strategy_stats 
                    SET attempts = attempts + 1,
                        successes = successes + ?,
                        avg_score = ((avg_score * attempts) + ?) / (attempts + 1)
                    WHERE strategy = ?
                """, (1 if success else 0, score, strategy))
                c.commit()
        except Exception as e:
            logger.error(f"[Evolution] Record strategy error: {e}")

    def get_ranked_strategies(self, limit: int = 3) -> List[str]:
        """Rank strategies based on a mix of success rate and avg_score (UCB-like could be added)."""
        try:
            with sqlite3.connect(self.db_path) as c:
                rows = c.execute("""
                    SELECT strategy, successes, attempts, avg_score 
                    FROM strategy_stats
                """).fetchall()
            
            # Simple scoring: success_rate * 0.7 + avg_score * 0.3
            def score(r):
                s, succ, att, avg = r
                if att == 0: return 0.5 # Exploration bonus
                return (succ / att) * 0.7 + (avg * 0.3)
            
            ranked = sorted(rows, key=score, reverse=True)
            return [r[0] for r in ranked][:limit]
        except Exception as e:
            logger.error(f"[Evolution] Get strategies error: {e}")
            return ["direct", "step_by_step", "alternate"]

    # ── 2. Self-Reflection Engine ─────────────────────────────────────────────
    def reflect_on_task(self, task: str, success: bool, history: str):
        """Analyze a completed task using LLM to extract meta-learnings."""
        router = get_router()
        prompt = (
            f"Task: {task}\nSuccess: {success}\nExecution History:\n{history[-2000:]}\n\n"
            "Perform a self-reflection. Output ONLY valid JSON:\n"
            "{\n"
            '  "worked": "what went well",\n'
            '  "failed": "what went wrong or bugs encountered",\n'
            '  "inefficiencies": "wasted steps or slow actions",\n'
            '  "meta_learning": "how to solve similar problems better in the future"\n'
            "}"
        )
        try:
            res = router.call(prompt, task_type="reason", max_tokens=300)
            text = res["text"]
            # Extract JSON
            import re
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                with sqlite3.connect(self.db_path) as c:
                    c.execute("""
                        INSERT INTO reflections (id, task, success, worked, failed, inefficiencies, meta_learning, ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        uuid.uuid4().hex[:12], task[:200], success,
                        data.get("worked", ""), data.get("failed", ""),
                        data.get("inefficiencies", ""), data.get("meta_learning", ""),
                        time.time()
                    ))
                    c.commit()
                
                # Chance to propose a system improvement if we noticed an inefficiency
                if not success and data.get("inefficiencies"):
                    self._maybe_propose_patch(data["inefficiencies"], data["meta_learning"])
                
                # Phase 38: Drift Detection
                try:
                    from governance_layer import get_governance_layer
                    get_governance_layer().detect_and_rollback_drift()
                except ImportError:
                    pass

        except Exception as e:
            logger.error(f"[Evolution] Reflection error: {e}")

    # ── 3. Prompt Optimization ────────────────────────────────────────────────
    def get_prompt(self, prompt_id: str, default_text: str) -> str:
        """Fetch the best version of a prompt."""
        try:
            with sqlite3.connect(self.db_path) as c:
                row = c.execute("""
                    SELECT prompt_text FROM prompts 
                    WHERE prompt_id = ? 
                    ORDER BY avg_quality DESC LIMIT 1
                """, (prompt_id,)).fetchone()
            
            if row:
                return row[0]
            
            # Initialize default
            with sqlite3.connect(self.db_path) as c:
                c.execute("""
                    INSERT INTO prompts (prompt_id, version, prompt_text, avg_quality, uses)
                    VALUES (?, 1, ?, 0.5, 0)
                """, (prompt_id, default_text))
            return default_text
        except Exception:
            return default_text

    def optimize_prompt(self, prompt_id: str, feedback: str):
        """Use LLM to rewrite and improve a prompt based on feedback."""
        try:
            with sqlite3.connect(self.db_path) as c:
                row = c.execute("""
                    SELECT version, prompt_text FROM prompts 
                    WHERE prompt_id = ? ORDER BY version DESC LIMIT 1
                """, (prompt_id,)).fetchone()
            if not row: return
            
            version, text = row
            router = get_router()
            prompt = (
                f"Original System Prompt:\n{text}\n\n"
                f"Feedback/Issues:\n{feedback}\n\n"
                "Rewrite this prompt to be more robust and solve the issues. "
                "Keep the same input variables. Output ONLY the raw new prompt text."
            )
            res = router.call(prompt, task_type="reason", max_tokens=500)
            new_text = res["text"].strip()
            if not new_text: return
            
            with sqlite3.connect(self.db_path) as c:
                c.execute("""
                    INSERT INTO prompts (prompt_id, version, prompt_text, avg_quality, uses)
                    VALUES (?, ?, ?, 0.5, 0)
                """, (prompt_id, version + 1, new_text))
                c.commit()
        except Exception as e:
            logger.error(f"[Evolution] Prompt optimize error: {e}")

    # ── 4. Safe Self-Code Improvement ─────────────────────────────────────────
    def _maybe_propose_patch(self, inefficiency: str, meta: str):
        """Generate a patch for a core system file if requested."""
        # For safety, we just log a proposal. In a real scenario, LLM would read `orchestrator.py`
        # and propose a diff.
        router = get_router()
        prompt = (
            f"Inefficiency: {inefficiency}\nMeta Learning: {meta}\n\n"
            "Propose a feature addition or python logic fix to 'orchestrator.py' or 'cognitive_agents.py' "
            "that would solve this permanently. Output ONLY a JSON with 'title', 'target_file', 'description'."
        )
        try:
            res = router.call(prompt, task_type="reason", max_tokens=300)
            import re
            match = re.search(r"\{.*\}", res["text"], re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                pid = uuid.uuid4().hex[:8]
                with sqlite3.connect(self.db_path) as c:
                    c.execute("""
                        INSERT INTO system_patches (id, title, description, target_file, patch_content, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        pid, data.get("title", "Optimization"),
                        data.get("description", "Auto-generated patch"),
                        data.get("target_file", "orchestrator.py"),
                        "# TODO: Actual patch content generated by LLM",
                        time.time()
                    ))
                    c.commit()
                
                # Phase 38: Immediate Governance Evaluation
                try:
                    from governance_layer import get_governance_layer
                    get_governance_layer().evaluate_patch(pid, data.get("target_file", "orchestrator.py"), "# TODO: Actual patch content generated by LLM")
                except ImportError:
                    pass
        except Exception as e:
            logger.error(f"[Evolution] Patch proposal error: {e}")

    # ── 5. Dashboard Data ─────────────────────────────────────────────────────
    def get_dashboard_stats(self) -> dict:
        try:
            with sqlite3.connect(self.db_path) as c:
                strats = c.execute("SELECT strategy, successes, attempts, avg_score FROM strategy_stats").fetchall()
                refs = c.execute("SELECT id, task, success, meta_learning FROM reflections ORDER BY ts DESC LIMIT 10").fetchall()
                patches = c.execute("SELECT id, title, target_file, status FROM system_patches WHERE status='pending' ORDER BY created_at DESC").fetchall()
                prompts = c.execute("SELECT prompt_id, max(version), avg_quality FROM prompts GROUP BY prompt_id").fetchall()
            
            return {
                "strategies": [{"strategy": s, "successes": su, "attempts": a, "win_rate": round(su/max(a,1)*100, 1), "score": round(sc, 2)} for s,su,a,sc in strats],
                "reflections": [{"id": r[0], "task": r[1], "success": r[2], "meta": r[3]} for r in refs],
                "patches": [{"id": p[0], "title": p[1], "file": p[2], "status": p[3]} for p in patches],
                "prompts": [{"id": p[0], "version": p[1], "quality": round(p[2],2)} for p in prompts]
            }
        except Exception:
            return {}

_engine_instance = None
def get_evolution_engine() -> EvolutionEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = EvolutionEngine()
    return _engine_instance
