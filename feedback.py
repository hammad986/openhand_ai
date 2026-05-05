"""
feedback.py — User Feedback & Issue Reporting System
=====================================================
Stores bug reports, suggestions, and confusion reports
from users. Provides query helpers for the admin panel.
"""
import sqlite3
import uuid
import datetime
import logging
import threading

logger = logging.getLogger(__name__)

DB_PATH = "feedback.db"
_lock = threading.Lock()

VALID_CATEGORIES = {"bug", "suggestion", "confusion"}

# ── Rate limit: 5 submissions per user per hour ────────────────────────────────
_rate: dict = {}
_rate_lock = threading.Lock()
RATE_LIMIT = 5
RATE_WINDOW = 3600


def _rate_ok(user_id: str) -> bool:
    now = datetime.datetime.utcnow().timestamp()
    with _rate_lock:
        times = _rate.get(user_id, [])
        times = [t for t in times if now - t < RATE_WINDOW]
        if len(times) >= RATE_LIMIT:
            return False
        times.append(now)
        _rate[user_id] = times
    return True


def init_feedback_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feedback (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL DEFAULT 'anon',
                session_id  TEXT NOT NULL DEFAULT '',
                category    TEXT NOT NULL DEFAULT 'bug',
                message     TEXT NOT NULL,
                email       TEXT NOT NULL DEFAULT '',
                timestamp   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fb_user     ON feedback(user_id);
            CREATE INDEX IF NOT EXISTS idx_fb_category ON feedback(category);
            CREATE INDEX IF NOT EXISTS idx_fb_ts       ON feedback(timestamp);
        """)
        conn.commit()
        conn.close()
    logger.info("[Feedback] DB initialised at %s", DB_PATH)


def submit_feedback(user_id: str, session_id: str, category: str,
                    message: str, email: str = "") -> dict:
    if not _rate_ok(user_id):
        raise ValueError("Rate limit exceeded — please wait before submitting again.")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")
    message = message.strip()
    if not message or len(message) < 5:
        raise ValueError("Message is too short.")
    if len(message) > 4000:
        raise ValueError("Message is too long (max 4000 characters).")

    fid = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO feedback (id, user_id, session_id, category, message, email, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fid, user_id, session_id or "", category, message, email.strip(), now))
        conn.commit()
        conn.close()
    logger.info("[Feedback] New %s report from user=%s id=%s", category, user_id, fid)
    return {"id": fid, "category": category, "timestamp": now}


def list_feedback(category: str = "", user_id: str = "",
                  date_from: str = "", date_to: str = "",
                  limit: int = 100, offset: int = 0) -> dict:
    limit  = min(limit, 500)
    wheres = []
    params = []
    if category:
        wheres.append("category = ?")
        params.append(category)
    if user_id:
        wheres.append("user_id = ?")
        params.append(user_id)
    if date_from:
        wheres.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        wheres.append("timestamp <= ?")
        params.append(date_to)

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM feedback {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM feedback {where_sql}", params
        ).fetchone()[0]
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_feedback_stats() -> dict:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        by_cat = {r[0]: r[1] for r in conn.execute(
            "SELECT category, COUNT(*) FROM feedback GROUP BY category"
        ).fetchall()}
        recent = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE timestamp >= date('now', '-7 days')"
        ).fetchone()[0]
        conn.close()
    return {"total": total, "by_category": by_cat, "last_7_days": recent}


# Auto-init on import
try:
    init_feedback_db()
except Exception as _e:
    logger.warning("[Feedback] DB init failed: %s", _e)
