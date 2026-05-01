"""
worker_team.py — Phase 40: Multi-Worker Intelligence System (AI Team)
======================================================================
Builds a team of specialized AutonomousWorker instances that:
  - Run in parallel with role-specific tool access
  - Communicate through a shared SQLite message bus
  - Are orchestrated by a Manager Worker that decomposes high-level goals
  - Share a global memory pool for results/context

Architecture:
  TeamOrchestrator
    ├── ManagerWorker          (goal decomposition + delegation)
    ├── ResearchWorker         (web + analysis)
    ├── CodingWorker           (filesystem + github)
    └── DeploymentWorker       (api_caller + infra)

All workers share one message bus DB but have separate goal queues.

Design rules:
  - Each RoleWorker extends AutonomousWorker with a role-filtered tool list
  - Manager uses LLM to split a mega-goal into sub-goals per role
  - Workers post results to message bus; Manager polls and unblocks dependents
  - No worker ever raises into the team layer (all errors are messages)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from autonomous_worker import AutonomousWorker, Goal, MAX_ITER_DEFAULT, LOOP_SLEEP_SEC

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Schema — shared team DB (message bus + global memory)
# ─────────────────────────────────────────────────────────────────────────────
_TEAM_DDL = """
CREATE TABLE IF NOT EXISTS team_messages (
    id          TEXT PRIMARY KEY,
    from_role   TEXT,
    to_role     TEXT,        -- NULL = broadcast
    subject     TEXT,
    body        TEXT,
    read        BOOLEAN DEFAULT 0,
    ts          REAL
);

CREATE TABLE IF NOT EXISTS team_global_goals (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    description   TEXT,
    status        TEXT DEFAULT 'pending',   -- pending | delegating | in_progress | completed | failed
    created_at    REAL,
    completed_at  REAL,
    result        TEXT
);

CREATE TABLE IF NOT EXISTS team_sub_goals (
    id            TEXT PRIMARY KEY,
    global_goal_id TEXT,
    assigned_role TEXT,
    worker_goal_id TEXT,    -- ID in the worker's own goal table
    title         TEXT,
    status        TEXT DEFAULT 'pending',   -- pending | delegated | completed | failed
    result        TEXT,
    depends_on    TEXT,     -- comma-separated sub_goal IDs
    created_at    REAL,
    FOREIGN KEY (global_goal_id) REFERENCES team_global_goals(id)
);

CREATE TABLE IF NOT EXISTS team_shared_memory (
    key   TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);
"""

# ─────────────────────────────────────────────────────────────────────────────
# Role definitions
# ─────────────────────────────────────────────────────────────────────────────
ROLES: Dict[str, Dict] = {
    "manager": {
        "label": "Manager",
        "emoji": "🧠",
        "color": "#d29922",
        "tools": [],          # Manager uses LLM only — no direct tools
        "description": "Decomposes high-level goals and delegates to specialists",
    },
    "research": {
        "label": "Research",
        "emoji": "🔍",
        "color": "#58a6ff",
        "tools": ["web", "api_caller"],
        "description": "Web search, scraping, data gathering and analysis",
    },
    "coding": {
        "label": "Coding",
        "emoji": "💻",
        "color": "#3fb950",
        "tools": ["filesystem", "github"],
        "description": "Code generation, file creation, GitHub integration",
    },
    "deployment": {
        "label": "Deployment",
        "emoji": "🚀",
        "color": "#a371f7",
        "tools": ["api_caller", "filesystem"],
        "description": "API orchestration, service management, infrastructure",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# RoleWorker — AutonomousWorker with a fixed tool filter
# ─────────────────────────────────────────────────────────────────────────────
class RoleWorker(AutonomousWorker):
    """
    A specialised worker that only exposes the tools assigned to its role.
    Adds role identity and team message bus integration.
    """

    def __init__(self, role: str, team_db: str, **kwargs):
        db_path = kwargs.pop("db_path", f"./data/worker_{role}.db")
        super().__init__(db_path=db_path, **kwargs)
        self.role   = role
        self.team_db = team_db
        meta = ROLES.get(role, {})
        self._allowed_tools: List[str] = meta.get("tools", [])
        self._role_label = meta.get("label", role)

    # ── Filtered tool registry ────────────────────────────────────────────────
    def _get_registry(self):
        if self._registry is None:
            from tool_integrations import get_tool_registry, ToolRegistry
            full_reg = get_tool_registry()
            if not self._allowed_tools:          # Manager — no tools
                self._registry = full_reg
                return self._registry
            # Build a filtered view
            class _FilteredRegistry:
                def __init__(self, full, allowed):
                    self._full = full
                    self._allowed = allowed
                def run(self, name, params):
                    if name not in self._allowed:
                        from tool_integrations import ToolResult
                        return ToolResult(ok=False, tool=name,
                                          error=f"Tool '{name}' not available for role")
                    return self._full.run(name, params)
                def list_tools(self):
                    return [t for t in self._full.list_tools() if t["name"] in self._allowed]
            self._registry = _FilteredRegistry(full_reg, self._allowed_tools)
        return self._registry

    # ── Message bus helpers ───────────────────────────────────────────────────
    def _post_message(self, to_role: Optional[str], subject: str, body: str):
        try:
            with sqlite3.connect(self.team_db) as c:
                c.execute(
                    "INSERT INTO team_messages (id, from_role, to_role, subject, body, ts) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:10], self.role, to_role, subject[:200], body[:2000], time.time())
                )
                c.commit()
        except Exception as e:
            logger.error("[%s] Message post error: %s", self.role, e)

    def _read_messages(self, unread_only: bool = True) -> List[dict]:
        try:
            with sqlite3.connect(self.team_db) as c:
                q = """SELECT id, from_role, subject, body, ts
                       FROM team_messages
                       WHERE (to_role=? OR to_role IS NULL)"""
                q += " AND read=0" if unread_only else ""
                rows = c.execute(q, (self.role,)).fetchall()
                if unread_only and rows:
                    ids = [r[0] for r in rows]
                    c.execute(f"UPDATE team_messages SET read=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
                    c.commit()
            return [{"id": r[0], "from": r[1], "subject": r[2], "body": r[3], "ts": r[4]} for r in rows]
        except Exception:
            return []

    def _write_shared_memory(self, key: str, value: Any):
        try:
            with sqlite3.connect(self.team_db) as c:
                c.execute(
                    "INSERT OR REPLACE INTO team_shared_memory (key, value, updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value, default=str)[:4000], time.time())
                )
                c.commit()
        except Exception as e:
            logger.error("[%s] Shared mem write error: %s", self.role, e)

    def _read_shared_memory(self, key: str) -> Any:
        try:
            with sqlite3.connect(self.team_db) as c:
                row = c.execute("SELECT value FROM team_shared_memory WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    # ── Override goal execution to broadcast results ──────────────────────────
    def _execute_goal(self, goal: Goal):
        super()._execute_goal(goal)
        # After execution, post result to message bus
        with self._db() as c:
            row = c.execute("SELECT status, result, error FROM worker_goals WHERE id=?", (goal.id,)).fetchone()
        if row:
            status, result, error = row
            self._post_message(
                to_role="manager",
                subject=f"goal_complete:{goal.id}",
                body=json.dumps({"goal_id": goal.id, "title": goal.title,
                                 "status": status, "result": (result or "")[:500],
                                 "error": error or ""})
            )
            # Write result to shared memory for other workers
            self._write_shared_memory(f"result:{goal.id}", {"title": goal.title, "status": status,
                                                             "result": (result or "")[:500]})


# ─────────────────────────────────────────────────────────────────────────────
# ManagerWorker — decomposes mega-goals, delegates, monitors
# ─────────────────────────────────────────────────────────────────────────────
class ManagerWorker(RoleWorker):
    """
    Listens for high-level global goals, decomposes them with LLM,
    assigns sub-goals to specialist workers, and monitors progress.
    """

    def __init__(self, team_db: str, workers: Dict[str, RoleWorker]):
        super().__init__(role="manager", team_db=team_db,
                         db_path="./data/worker_manager.db")
        self._workers = workers    # {role: RoleWorker}
        self._team_db = team_db

    def _decompose_global_goal(self, global_goal_id: str, title: str, description: str) -> List[dict]:
        """Ask LLM to split a mega-goal into role-specific sub-goals with dependencies."""
        from model_router import get_router
        router = get_router()

        roles_summary = "\n".join(
            f"  - {r}: {m['description']} (tools: {', '.join(m['tools']) or 'LLM only'})"
            for r, m in ROLES.items() if r != "manager"
        )

        prompt = (
            f"You are a Manager AI. Decompose this goal into sub-goals for specialist workers.\n\n"
            f"GOAL: {title}\n"
            f"DESCRIPTION: {description}\n\n"
            f"AVAILABLE WORKERS:\n{roles_summary}\n\n"
            f"Output ONLY a JSON array:\n"
            f'[{{"role": "research|coding|deployment", "title": "sub-goal title", '
            f'"description": "what to do", "depends_on": []}}]\n'
            f"Max 6 sub-goals. Order them by logical sequence. "
            f"depends_on contains the 0-based index of prerequisite sub-goals."
        )

        try:
            res = router.call(prompt, task_type="plan", max_tokens=800)
            import re
            match = re.search(r"\[.*?\]", res["text"], re.DOTALL)
            if match:
                items = json.loads(match.group(0))
                if isinstance(items, list):
                    return items[:6]
        except Exception as e:
            logger.error("[Manager] Decompose error: %s", e)

        # Fallback: one sub-goal per worker
        return [
            {"role": "research",    "title": f"Research: {title}", "description": description, "depends_on": []},
            {"role": "coding",      "title": f"Implement: {title}", "description": description, "depends_on": [0]},
            {"role": "deployment",  "title": f"Deploy: {title}", "description": description, "depends_on": [1]},
        ]

    def delegate_global_goal(self, title: str, description: str = "") -> str:
        """Create a global goal and begin delegation pipeline."""
        gid = uuid.uuid4().hex[:10]
        with sqlite3.connect(self._team_db) as c:
            c.execute(
                "INSERT INTO team_global_goals (id, title, description, created_at) VALUES (?,?,?,?)",
                (gid, title, description or title, time.time())
            )
            c.commit()

        # Run decomposition + delegation in a background thread
        t = threading.Thread(target=self._do_delegate, args=(gid, title, description),
                             daemon=True, name=f"Delegate-{gid}")
        t.start()
        logger.info("[Manager] Global goal created: %s — %s", gid, title)
        return gid

    def _do_delegate(self, gid: str, title: str, description: str):
        try:
            with sqlite3.connect(self._team_db) as c:
                c.execute("UPDATE team_global_goals SET status='delegating' WHERE id=?", (gid,))
                c.commit()

            sub_goals_raw = self._decompose_global_goal(gid, title, description)
            sub_goal_ids = []

            # Register sub-goals in team DB
            with sqlite3.connect(self._team_db) as c:
                for i, sg in enumerate(sub_goals_raw):
                    sgid = uuid.uuid4().hex[:10]
                    dep_indices = sg.get("depends_on", [])
                    dep_ids = ",".join(sub_goal_ids[j] for j in dep_indices if j < len(sub_goal_ids))
                    c.execute("""
                        INSERT INTO team_sub_goals
                        (id, global_goal_id, assigned_role, title, depends_on, created_at)
                        VALUES (?,?,?,?,?,?)
                    """, (sgid, gid, sg.get("role", "coding"), sg.get("title", ""),
                          dep_ids, time.time()))
                    sub_goal_ids.append(sgid)
                c.commit()

            with sqlite3.connect(self._team_db) as c:
                c.execute("UPDATE team_global_goals SET status='in_progress' WHERE id=?", (gid,))
                c.commit()

            # Begin dispatching sub-goals respecting dependency order
            self._dispatch_ready_sub_goals(gid, sub_goal_ids)

        except Exception as e:
            logger.error("[Manager] Delegation error: %s", e)
            with sqlite3.connect(self._team_db) as c:
                c.execute("UPDATE team_global_goals SET status='failed' WHERE id=?", (gid,))
                c.commit()

    def _dispatch_ready_sub_goals(self, global_goal_id: str, all_ids: List[str]):
        """Iteratively dispatch sub-goals that have all dependencies met."""
        max_wait = 600  # 10 min timeout for entire delegation
        t0 = time.time()
        dispatched = set()

        while time.time() - t0 < max_wait:
            with sqlite3.connect(self._team_db) as c:
                subs = c.execute("""
                    SELECT id, assigned_role, title, status, depends_on
                    FROM team_sub_goals WHERE global_goal_id=?
                """, (global_goal_id,)).fetchall()

            all_done = all(s[3] in ("completed", "failed") for s in subs)
            if all_done:
                break

            for s in subs:
                sgid, role, title, status, depends_on = s
                if sgid in dispatched or status not in ("pending",):
                    continue

                # Check deps
                dep_ids = [d.strip() for d in (depends_on or "").split(",") if d.strip()]
                deps_met = True
                for dep_id in dep_ids:
                    dep_row = next((x for x in subs if x[0] == dep_id), None)
                    if not dep_row or dep_row[3] != "completed":
                        deps_met = False
                        break

                if deps_met:
                    worker = self._workers.get(role)
                    if worker:
                        worker_goal_id = worker.add_goal(
                            title=title,
                            description=f"[Global Goal: {global_goal_id}] {title}",
                            priority=7,
                        )
                        with sqlite3.connect(self._team_db) as c:
                            c.execute("UPDATE team_sub_goals SET status='delegated', worker_goal_id=? WHERE id=?",
                                      (worker_goal_id, sgid))
                            c.commit()
                        dispatched.add(sgid)
                        self._post_message(role, f"assigned:{sgid}",
                                           json.dumps({"global_goal": global_goal_id, "sub_goal": sgid,
                                                       "title": title}))
                        logger.info("[Manager] Delegated sub-goal %s → %s worker", sgid, role)

            # Poll messages for completion updates
            msgs = self._read_messages()
            for msg in msgs:
                if msg["subject"].startswith("goal_complete:"):
                    worker_gid = msg["subject"].split(":", 1)[1]
                    # Find the matching sub-goal
                    with sqlite3.connect(self._team_db) as c:
                        c.execute("UPDATE team_sub_goals SET status='completed' WHERE worker_goal_id=?",
                                  (worker_gid,))
                        c.commit()

            time.sleep(3)

        # Finalise global goal
        with sqlite3.connect(self._team_db) as c:
            subs = c.execute("SELECT status FROM team_sub_goals WHERE global_goal_id=?",
                             (global_goal_id,)).fetchall()
            all_ok = all(s[0] == "completed" for s in subs)
            c.execute("UPDATE team_global_goals SET status=?, completed_at=? WHERE id=?",
                      ("completed" if all_ok else "failed", time.time(), global_goal_id))
            c.commit()


# ─────────────────────────────────────────────────────────────────────────────
# TeamOrchestrator — owns all workers and exposes the team API
# ─────────────────────────────────────────────────────────────────────────────
class TeamOrchestrator:
    """
    Creates and manages all role workers + manager.
    Provides a single control surface for the web API.
    """

    def __init__(self, team_db: str = "./data/team.db"):
        self.team_db = team_db
        os.makedirs("./data", exist_ok=True)
        self._init_team_db()

        # Specialist workers
        self._workers: Dict[str, RoleWorker] = {
            role: RoleWorker(role=role, team_db=team_db)
            for role in ("research", "coding", "deployment")
        }

        # Manager last (needs worker refs)
        self._manager = ManagerWorker(team_db=team_db, workers=self._workers)
        self._all: Dict[str, RoleWorker] = {**self._workers, "manager": self._manager}

    def _init_team_db(self):
        with sqlite3.connect(self.team_db) as c:
            c.executescript(_TEAM_DDL)
            c.commit()

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def start_all(self) -> dict:
        results = {}
        for role, w in self._all.items():
            results[role] = w.start()
        return {"ok": True, "workers": results}

    def stop_all(self) -> dict:
        results = {}
        for role, w in self._all.items():
            results[role] = w.stop()
        return {"ok": True, "workers": results}

    def start_worker(self, role: str) -> dict:
        w = self._all.get(role)
        return w.start() if w else {"ok": False, "error": f"Unknown role: {role}"}

    def stop_worker(self, role: str) -> dict:
        w = self._all.get(role)
        return w.stop() if w else {"ok": False, "error": f"Unknown role: {role}"}

    # ── Goal Delegation ───────────────────────────────────────────────────────
    def add_global_goal(self, title: str, description: str = "") -> str:
        return self._manager.delegate_global_goal(title, description)

    def add_worker_goal(self, role: str, title: str, description: str = "",
                         priority: int = 5) -> dict:
        w = self._workers.get(role)
        if not w:
            return {"ok": False, "error": f"Unknown role: {role}"}
        gid = w.add_goal(title, description, priority)
        return {"ok": True, "goal_id": gid, "role": role}

    # ── Status ────────────────────────────────────────────────────────────────
    def team_status(self) -> dict:
        workers_status = {}
        for role, w in self._all.items():
            ws = w.status()
            workers_status[role] = {
                "role": role,
                "label": ROLES[role]["label"],
                "emoji": ROLES[role]["emoji"],
                "color": ROLES[role]["color"],
                "running": ws["running"],
                "current_goal": ws["current_goal"],
                "counts": ws["counts"],
                "goals": ws["goals"][:10],
                "recent_tasks": ws["recent_tasks"][:5],
            }

        with sqlite3.connect(self.team_db) as c:
            global_goals = c.execute("""
                SELECT id, title, status, created_at, completed_at, result
                FROM team_global_goals ORDER BY created_at DESC LIMIT 20
            """).fetchall()

            sub_goals = c.execute("""
                SELECT id, global_goal_id, assigned_role, title, status, worker_goal_id
                FROM team_sub_goals ORDER BY created_at ASC
            """).fetchall()

            messages = c.execute("""
                SELECT from_role, to_role, subject, body, ts
                FROM team_messages ORDER BY ts DESC LIMIT 30
            """).fetchall()

            memory = c.execute("SELECT key, value, updated_at FROM team_shared_memory").fetchall()

        return {
            "workers": workers_status,
            "global_goals": [
                {"id": g[0], "title": g[1], "status": g[2],
                 "created_at": g[3], "completed_at": g[4],
                 "result": (g[5] or "")[:200]} for g in global_goals
            ],
            "sub_goals": [
                {"id": s[0], "global_goal_id": s[1], "role": s[2],
                 "title": s[3], "status": s[4], "worker_goal_id": s[5]} for s in sub_goals
            ],
            "messages": [
                {"from": m[0], "to": m[1], "subject": m[2],
                 "body": m[3][:200], "ts": m[4]} for m in messages
            ],
            "shared_memory": {row[0]: json.loads(row[1]) for row in memory},
        }

    def clear_messages(self):
        with sqlite3.connect(self.team_db) as c:
            c.execute("DELETE FROM team_messages WHERE read=1")
            c.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────
_team_instance: Optional[TeamOrchestrator] = None

def get_team() -> TeamOrchestrator:
    global _team_instance
    if _team_instance is None:
        _team_instance = TeamOrchestrator()
    return _team_instance
