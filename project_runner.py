"""
project_runner.py — Phase 41: End-to-End Project Execution
===========================================================
Orchestrates the full lifecycle:
  Research → Code → Validate → Deploy → Track

Built on top of the TeamOrchestrator (Phase 40) and integrates:
  - ArtifactRegistry  (Phase 41)
  - DeploymentEngine  (Phase 41)
  - QualityValidator  (Phase 41 — built-in)
  - Auto-Retry Loop   (Phase 41)

Project types understood:
  website        — HTML/CSS/JS site
  api            — REST API (Python/FastAPI)
  report         — Markdown document
  saas           — Full-stack SaaS (website + api)

Each project is stored in the ArtifactRegistry with full version history.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT,            -- website | api | report | saas
    goal         TEXT,
    status       TEXT DEFAULT 'queued',   -- queued|planning|building|validating|deploying|completed|failed
    platform     TEXT,
    live_url     TEXT,
    artifact_id  TEXT,
    error        TEXT,
    retry_count  INTEGER DEFAULT 0,
    max_retries  INTEGER DEFAULT 2,
    created_at   REAL,
    completed_at REAL,
    log          TEXT             -- JSON array of log entries
);
"""

MAX_RETRIES = int(os.environ.get("PROJECT_MAX_RETRIES", "2"))


# ─────────────────────────────────────────────────────────────────────────────
# Quality Validator
# ─────────────────────────────────────────────────────────────────────────────
class QualityValidator:
    """Runs lightweight validation checks on generated artifacts."""

    def validate(self, artifact_type: str, files: Dict[str, str]) -> dict:
        issues = []
        score  = 100

        if artifact_type == "website":
            html = files.get("index.html", "")
            if not html:
                issues.append("Missing index.html"); score -= 40
            else:
                if "<html" not in html.lower():
                    issues.append("Missing <html> tag"); score -= 15
                if "<title" not in html.lower():
                    issues.append("Missing <title> tag"); score -= 5
                if len(html) < 200:
                    issues.append("index.html suspiciously short"); score -= 10

        elif artifact_type == "api":
            main = files.get("main.py", "")
            if not main:
                issues.append("Missing main.py"); score -= 40
            else:
                if "app" not in main.lower() and "flask" not in main.lower() and "fastapi" not in main.lower():
                    issues.append("No recognizable web framework detected"); score -= 20

        elif artifact_type == "report":
            content = list(files.values())[0] if files else ""
            if len(content) < 100:
                issues.append("Report is too short"); score -= 30
            if not any(line.startswith("#") for line in content.splitlines()):
                issues.append("No markdown headings found"); score -= 10

        passed = score >= 70
        return {"passed": passed, "score": score, "issues": issues}


# ─────────────────────────────────────────────────────────────────────────────
# Project Runner
# ─────────────────────────────────────────────────────────────────────────────
class ProjectRunner:
    """
    Runs an end-to-end project through the AI team pipeline.
    Each project runs in its own background thread.
    """

    def __init__(self, db_path: str = "./data/projects.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        self._validator = QualityValidator()
        self._lock = threading.Lock()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.executescript(_DDL)
            c.commit()

    def _db(self): return sqlite3.connect(self.db_path)

    def _update(self, pid: str, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [pid]
        with self._db() as c:
            c.execute(f"UPDATE projects SET {sets} WHERE id=?", vals)
            c.commit()

    def _append_log(self, pid: str, entry: str):
        with self._db() as c:
            row = c.execute("SELECT log FROM projects WHERE id=?", (pid,)).fetchone()
            logs = json.loads(row[0]) if row and row[0] else []
            logs.append({"ts": time.time(), "msg": entry})
            c.execute("UPDATE projects SET log=? WHERE id=?", (json.dumps(logs[-100:]), pid))
            c.commit()
        logger.info("[Project:%s] %s", pid[:8], entry)

    # ── Public API ────────────────────────────────────────────────────────────

    def submit_project(self, name: str, goal: str,
                       project_type: str = "website",
                       platform: str = "local",
                       max_retries: int = MAX_RETRIES) -> str:
        """Queue a new project and kick off execution in a background thread."""
        pid = uuid.uuid4().hex[:12]
        with self._db() as c:
            c.execute("""
                INSERT INTO projects (id, name, type, goal, platform, max_retries, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (pid, name, project_type, goal, platform, max_retries, time.time()))
            c.commit()

        t = threading.Thread(target=self._run_project, args=(pid,),
                             daemon=True, name=f"Project-{pid[:8]}")
        t.start()
        logger.info("[ProjectRunner] Started project %s: %s", pid, name)
        return pid

    # ── Execution Pipeline ────────────────────────────────────────────────────

    def _run_project(self, pid: str):
        """Full pipeline: plan → fetch assets → build → validate → explain → deploy → complete."""
        with self._db() as c:
            row = c.execute("SELECT name, type, goal, platform, max_retries, retry_count FROM projects WHERE id=?",
                            (pid,)).fetchone()
        if not row: return
        name, ptype, goal, platform, max_retries, retry_count = row

        try:
            self._update(pid, status="planning")
            self._append_log(pid, f"Planning project: {name}")

            # ── Step 0: Fetch Assets (Part 3) ─────────────────────────────
            self._append_log(pid, "Fetching relevant assets...")
            from asset_pipeline import get_asset_pipeline
            asset_pipe = get_asset_pipeline()
            # Basic keyword extraction from name/goal for asset search
            search_query = f"{name} background professional"
            if ptype in ["website", "saas"]:
                fetched_assets = asset_pipe.search_and_fetch_images(search_query, "background", count=2, project_id=pid)
                if fetched_assets:
                    self._append_log(pid, f"Fetched {len(fetched_assets)} assets for UI.")
                    # Inject asset info into the goal so the LLM uses it
                    asset_info = "Available assets for use in HTML/CSS (use relative paths): " + ", ".join([f"'/{a}'" for a in fetched_assets])
                    goal = f"{goal}\n\n[Asset Pipeline]\n{asset_info}"

            # ── Step 1: Generate files via LLM ────────────────────────────
            files = self._generate_files(pid, name, ptype, goal)
            if not files:
                raise RuntimeError("File generation produced no output")

            self._append_log(pid, f"Generated {len(files)} file(s): {list(files.keys())}")

            # ── Step 2: Quality Validation ────────────────────────────────
            self._update(pid, status="validating")
            self._append_log(pid, "Running quality validation...")
            validation = self._validator.validate(ptype, files)
            self._append_log(pid, f"Validation score: {validation['score']}/100 — "
                             f"{'PASS' if validation['passed'] else 'FAIL'}")

            if not validation["passed"]:
                issues_str = "; ".join(validation["issues"])
                if retry_count < max_retries:
                    self._append_log(pid, f"Validation failed ({issues_str}), regenerating...")
                    self._update(pid, retry_count=retry_count + 1, status="building")
                    files = self._generate_files(pid, name, ptype,
                                                 goal + f"\n\nFix these issues: {issues_str}",
                                                 retry=True)
                else:
                    raise RuntimeError(f"Validation failed after {retry_count} retries: {issues_str}")

            # ── Step 3: Explanation Engine (Part 7) ───────────────────────
            self._append_log(pid, "Generating explanation report...")
            explanation = f"# Project: {name}\n\n## What was built\n{ptype.capitalize()} application based on your requirements.\n\n## How it was built\nUsing Nexora AI Autonomous Pipeline with LLM code generation and asset automation.\n\n## Technologies\nHTML/CSS/JS or Python/FastAPI (based on project type).\n\n## How to use\nIf deployed, visit the live URL. If local, check the workspace/artifacts directory."
            files["EXPLANATION.md"] = explanation

            # ── Step 4: Register Artifact ─────────────────────────────────
            self._update(pid, status="building")
            from artifact_registry import get_artifact_registry
            registry = get_artifact_registry()
            artifact = registry.create_artifact(
                name=name, artifact_type=ptype, files=files,
                tags=[ptype, platform, "ai-generated", "explained"],
                metadata={"goal": goal[:300], "validation_score": validation["score"]},
                created_by="project_runner"
            )
            self._update(pid, artifact_id=artifact.id)
            self._append_log(pid, f"Artifact registered: {artifact.id} → {artifact.local_path}")

            # ── Step 5: Deploy ─────────────────────────────────────────────
            live_url = artifact.local_path   # default: local path
            if platform != "local":
                self._update(pid, status="deploying")
                self._append_log(pid, f"Deploying to {platform}...")
                from deployment_engine import get_deployment_engine
                engine = get_deployment_engine()
                dep_result = engine.deploy(platform, name, files)
                self._append_log(pid, f"Deploy result: ok={dep_result.ok}, url={dep_result.live_url}, error={dep_result.error}")

                if dep_result.ok:
                    live_url = dep_result.live_url
                    registry.mark_deployed(artifact.id, platform, live_url,
                                           dep_result.deployment_id, dep_result.project_id)
                else:
                    # Auto-retry deploy
                    if retry_count < max_retries:
                        self._append_log(pid, "Deployment failed, retrying...")
                        self._update(pid, retry_count=retry_count + 1)
                        dep_result2 = engine.deploy(platform, name, files)
                        if dep_result2.ok:
                            live_url = dep_result2.live_url
                            registry.mark_deployed(artifact.id, platform, live_url,
                                                   dep_result2.deployment_id)
                        else:
                            registry.mark_failed(artifact.id, dep_result2.error)
                            self._append_log(pid, f"Deploy failed permanently: {dep_result2.error}")
                            live_url = artifact.local_path
                    else:
                        registry.mark_failed(artifact.id, dep_result.error)
                        live_url = artifact.local_path
            else:
                from artifact_registry import get_artifact_registry as _reg
                _reg().mark_deployed(artifact.id, "local", live_url)

            self._update(pid, status="completed", live_url=live_url, completed_at=time.time())
            self._append_log(pid, f"✅ Project complete! Live at: {live_url}")

        except Exception as e:
            logger.error("[Project:%s] Error: %s", pid[:8], e)
            self._update(pid, status="failed", error=str(e)[:500], completed_at=time.time())
            self._append_log(pid, f"❌ Project failed: {e}")

    def _generate_files(self, pid: str, name: str, ptype: str,
                         goal: str, retry: bool = False) -> Dict[str, str]:
        """Use LLM to generate project files."""
        from model_router import get_router
        router = get_router()

        templates = {
            "website": (
                "Generate a complete, beautiful, modern HTML/CSS/JS website.\n"
                "Output ONLY a JSON object: {\"index.html\": \"...\", \"style.css\": \"...\", \"script.js\": \"...\"}\n"
                "Make it visually stunning with gradients, animations, and responsive design."
            ),
            "api": (
                "Generate a complete Python FastAPI application.\n"
                "Output ONLY a JSON object: {\"main.py\": \"...\", \"requirements.txt\": \"...\", \"README.md\": \"...\"}\n"
                "Include all routes, error handling, and documentation."
            ),
            "report": (
                "Generate a comprehensive research report in Markdown format.\n"
                "Output ONLY a JSON object: {\"report.md\": \"...\"}\n"
                "Include executive summary, analysis, findings, and recommendations."
            ),
            "saas": (
                "Generate files for a complete SaaS product.\n"
                "Output ONLY a JSON object with: {\"index.html\": \"...\", \"app.py\": \"...\", \"requirements.txt\": \"...\", \"README.md\": \"...\"}\n"
                "Make it production-quality."
            ),
        }

        template = templates.get(ptype, templates["website"])
        prompt = (
            f"Project: {name}\nGoal: {goal}\n\n"
            f"{template}"
            + ("\n\nThis is a retry. Be thorough and fix all previous issues." if retry else "")
        )

        try:
            res = router.call(prompt, task_type="code", max_tokens=2000)
            text = res.get("text", "")
            # Find JSON object
            match = re.search(r'\{["\s\S]*\}', text)
            if match:
                data = json.loads(match.group(0))
                if isinstance(data, dict) and data:
                    return {k: str(v) for k, v in data.items()}
        except Exception as e:
            logger.error("[Project:%s] File gen error: %s", pid[:8], e)

        # Hardcoded fallback for website
        if ptype == "website":
            return {
                "index.html": f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #0d1117 0%, #161b22 100%); color: #e6edf3; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .hero {{ text-align: center; padding: 60px 40px; }}
  h1 {{ font-size: 3.5rem; font-weight: 800; background: linear-gradient(90deg, #58a6ff, #a371f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 20px; }}
  p {{ font-size: 1.2rem; color: #8b949e; max-width: 600px; margin: 0 auto 40px; }}
  .btn {{ display: inline-block; padding: 14px 40px; background: linear-gradient(135deg, #238636, #2ea043); border-radius: 8px; color: white; font-weight: 700; font-size: 1rem; text-decoration: none; transition: transform 0.2s, box-shadow 0.2s; }}
  .btn:hover {{ transform: translateY(-2px); box-shadow: 0 8px 25px rgba(35,134,54,0.4); }}
</style>
</head>
<body>
  <div class="hero">
    <h1>{name}</h1>
    <p>{goal[:200]}</p>
    <a href="#" class="btn">Get Started</a>
  </div>
</body>
</html>"""
            }
        return {"output.txt": f"Project: {name}\nGoal: {goal}\n\nGenerated by Nexora AI Platform"}

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_projects(self, limit: int = 30) -> List[dict]:
        with self._db() as c:
            rows = c.execute("""
                SELECT id, name, type, status, platform, live_url, artifact_id,
                       error, retry_count, created_at, completed_at, log
                FROM projects ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [{
            "id": r[0], "name": r[1], "type": r[2], "status": r[3],
            "platform": r[4], "live_url": r[5], "artifact_id": r[6],
            "error": r[7], "retries": r[8], "created_at": r[9],
            "completed_at": r[10],
            "log": json.loads(r[11] or "[]")[-5:],   # Last 5 log entries
        } for r in rows]

    def get_project(self, pid: str) -> Optional[dict]:
        projects = [p for p in self.list_projects(100) if p["id"] == pid]
        return projects[0] if projects else None

    def dashboard_stats(self) -> dict:
        with self._db() as c:
            total     = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            completed = c.execute("SELECT COUNT(*) FROM projects WHERE status='completed'").fetchone()[0]
            failed    = c.execute("SELECT COUNT(*) FROM projects WHERE status='failed'").fetchone()[0]
            running   = c.execute("SELECT COUNT(*) FROM projects WHERE status NOT IN ('completed','failed','queued')").fetchone()[0]
        return {"total": total, "completed": completed, "failed": failed, "running": running,
                "success_rate": round(completed / max(total, 1) * 100, 1)}


_runner_instance = None
def get_project_runner() -> ProjectRunner:
    global _runner_instance
    if _runner_instance is None:
        _runner_instance = ProjectRunner()
    return _runner_instance
