"""
artifact_registry.py — Phase 41: Artifact Generation & Tracking
================================================================
Stores, versions, and tracks all real outputs produced by the AI team:
  - Websites (HTML/CSS/JS)
  - Code repositories
  - Markdown / PDF reports
  - API definitions
  - Deployment manifests

Schema: artifacts table + artifact_files table + versions table
All artifacts get a stable ID and live URL (if deployed).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT,           -- website | code | report | api | manifest | other
    status       TEXT DEFAULT 'draft',   -- draft | ready | deployed | failed
    platform     TEXT,           -- vercel | netlify | railway | render | local
    live_url     TEXT,
    local_path   TEXT,
    deployment_id TEXT,
    project_id   TEXT,
    version      INTEGER DEFAULT 1,
    tags         TEXT,           -- comma-separated
    metadata     TEXT,           -- JSON blob
    created_at   REAL,
    updated_at   REAL,
    created_by   TEXT            -- worker role that produced this
);

CREATE TABLE IF NOT EXISTS artifact_files (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT,
    file_path   TEXT,
    content     TEXT,
    size_bytes  INTEGER,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT,
    version     INTEGER,
    snapshot    TEXT,        -- JSON snapshot of key fields
    created_at  REAL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
"""

ARTIFACT_DIR = os.path.abspath("./workspace/artifacts")


@dataclass
class Artifact:
    id: str
    name: str
    type: str
    status: str = "draft"
    platform: str = ""
    live_url: str = ""
    local_path: str = ""
    version: int = 1
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    files: Dict[str, str] = field(default_factory=dict)
    created_by: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "type": self.type,
            "status": self.status, "platform": self.platform,
            "live_url": self.live_url, "local_path": self.local_path,
            "version": self.version, "tags": self.tags,
            "metadata": self.metadata, "created_by": self.created_by,
        }


class ArtifactRegistry:
    def __init__(self, db_path: str = "./data/artifacts.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.executescript(_DDL)
            c.commit()

    def _db(self):
        return sqlite3.connect(self.db_path)

    # ── Create ────────────────────────────────────────────────────────────────

    def create_artifact(self, name: str, artifact_type: str,
                        files: Optional[Dict[str, str]] = None,
                        tags: Optional[List[str]] = None,
                        metadata: Optional[dict] = None,
                        created_by: str = "",
                        base_dir: str = None) -> Artifact:
        """Register a new artifact and persist its files to workspace."""
        aid = uuid.uuid4().hex[:12]
        artifact = Artifact(
            id=aid, name=name, type=artifact_type,
            tags=tags or [], metadata=metadata or {},
            files=files or {}, created_by=created_by
        )

        # Save files to disk
        target_dir = base_dir if base_dir else ARTIFACT_DIR
        local_dir = os.path.join(target_dir, aid)
        os.makedirs(local_dir, exist_ok=True)
        for rel_path, content in (files or {}).items():
            full_path = os.path.join(local_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full_path) or local_dir, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
        artifact.local_path = local_dir

        # Persist to DB
        with self._db() as c:
            c.execute("""
                INSERT INTO artifacts (id, name, type, status, local_path, tags, metadata, created_at, updated_at, created_by)
                VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)
            """, (aid, name, artifact_type, local_dir,
                  ",".join(tags or []),
                  json.dumps(metadata or {}),
                  time.time(), time.time(), created_by))

            for rel_path, content in (files or {}).items():
                c.execute("""
                    INSERT INTO artifact_files (id, artifact_id, file_path, content, size_bytes)
                    VALUES (?, ?, ?, ?, ?)
                """, (uuid.uuid4().hex[:10], aid, rel_path,
                      content[:50000], len(content.encode())))
            c.commit()

        logger.info("[ArtifactRegistry] Created artifact %s (%s): %s", aid, artifact_type, name)
        return artifact

    # ── Update after deployment ───────────────────────────────────────────────

    def mark_deployed(self, artifact_id: str, platform: str, live_url: str,
                      deployment_id: str = "", project_id: str = ""):
        """Update artifact status after successful deployment."""
        with self._db() as c:
            # Bump version
            row = c.execute("SELECT version FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            new_version = (row[0] + 1) if row else 1

            c.execute("""
                UPDATE artifacts SET status='deployed', platform=?, live_url=?, deployment_id=?,
                project_id=?, version=?, updated_at=? WHERE id=?
            """, (platform, live_url, deployment_id, project_id, new_version, time.time(), artifact_id))

            # Snapshot this version
            art = c.execute("SELECT name, type, metadata FROM artifacts WHERE id=?",
                            (artifact_id,)).fetchone()
            if art:
                c.execute("""
                    INSERT INTO artifact_versions (id, artifact_id, version, snapshot, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (uuid.uuid4().hex[:10], artifact_id, new_version,
                      json.dumps({"live_url": live_url, "platform": platform,
                                  "name": art[0], "type": art[1]}),
                      time.time()))
            c.commit()
        logger.info("[ArtifactRegistry] Artifact %s deployed → %s", artifact_id, live_url)

    def mark_failed(self, artifact_id: str, error: str):
        with self._db() as c:
            c.execute("UPDATE artifacts SET status='failed', metadata=json_patch(metadata, ?) WHERE id=?",
                      (json.dumps({"last_error": error[:500]}), artifact_id))
            c.commit()

    # ── Query ─────────────────────────────────────────────────────────────────

    def list_artifacts(self, status: Optional[str] = None,
                       artifact_type: Optional[str] = None,
                       limit: int = 50) -> List[dict]:
        with self._db() as c:
            q = "SELECT id, name, type, status, platform, live_url, local_path, version, tags, metadata, created_at, updated_at, created_by FROM artifacts"
            filters, params = [], []
            if status: filters.append("status=?"); params.append(status)
            if artifact_type: filters.append("type=?"); params.append(artifact_type)
            if filters: q += " WHERE " + " AND ".join(filters)
            q += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(q, params).fetchall()

        return [{
            "id": r[0], "name": r[1], "type": r[2], "status": r[3],
            "platform": r[4], "live_url": r[5], "local_path": r[6],
            "version": r[7], "tags": (r[8] or "").split(","),
            "metadata": _safe_json(r[9]), "created_at": r[10],
            "updated_at": r[11], "created_by": r[12],
        } for r in rows]

    def get_versions(self, artifact_id: str) -> List[dict]:
        with self._db() as c:
            rows = c.execute("""
                SELECT version, snapshot, created_at FROM artifact_versions
                WHERE artifact_id=? ORDER BY version DESC
            """, (artifact_id,)).fetchall()
        return [{"version": r[0], "snapshot": _safe_json(r[1]), "created_at": r[2]} for r in rows]

    def get_files(self, artifact_id: str) -> Dict[str, str]:
        with self._db() as c:
            rows = c.execute("SELECT file_path, content FROM artifact_files WHERE artifact_id=?",
                             (artifact_id,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def dashboard_stats(self) -> dict:
        with self._db() as c:
            total     = c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
            deployed  = c.execute("SELECT COUNT(*) FROM artifacts WHERE status='deployed'").fetchone()[0]
            by_type   = c.execute("SELECT type, COUNT(*) FROM artifacts GROUP BY type").fetchall()
            by_plat   = c.execute("SELECT platform, COUNT(*) FROM artifacts WHERE platform!='' GROUP BY platform").fetchall()
            recent    = self.list_artifacts(limit=10)
        return {
            "total": total, "deployed": deployed,
            "by_type": dict(by_type), "by_platform": dict(by_plat),
            "recent": recent,
        }


def _safe_json(v) -> Any:
    try:
        return json.loads(v) if v else {}
    except Exception:
        return {}


_registry_instance = None
def get_artifact_registry() -> ArtifactRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ArtifactRegistry()
    return _registry_instance
