"""
notifications.py — Real-Time Notification & Event System
=========================================================
Production-grade notification engine for Nexora AI Platform.

Features:
- Persistent DB storage (saas_platform.db)
- SSE push via in-memory per-user queues
- Priority levels: info / warning / critical
- Types: task / support / billing / system
- Email fallback for critical events (via Resend)
- Automatic expiry cleanup (30 days)

Usage:
    from notifications import notify, get_notifications, mark_read, mark_all_read
    from notifications import subscribe_sse, unsubscribe_sse, sse_stream

    # Create a notification
    notify(user_id="user123", type="task", priority="info",
           title="Task Complete", message="Your agent finished the task.")
"""
import sqlite3
import uuid
import json
import time
import datetime
import os
import threading
import queue
import logging
from typing import Optional, Generator

logger = logging.getLogger(__name__)

DB_PATH    = "saas_platform.db"
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "noreply@nexora.ai")
APP_NAME      = "Nexora AI Platform"

VALID_TYPES     = {"task", "support", "billing", "system"}
VALID_PRIORITIES = {"info", "warning", "critical"}
NOTIFY_EXPIRY_DAYS = 30

# ── SSE subscriber registry ──────────────────────────────────────────────────
# Maps user_id -> list of Queue objects (one per open browser tab)
_sse_subscribers: dict[str, list[queue.Queue]] = {}
_sse_lock = threading.Lock()


# ── DB init ──────────────────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            type        TEXT NOT NULL DEFAULT 'system',
            priority    TEXT NOT NULL DEFAULT 'info',
            title       TEXT NOT NULL,
            message     TEXT NOT NULL,
            link        TEXT DEFAULT NULL,
            is_read     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_user    ON notifications(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_read    ON notifications(user_id, is_read)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at)")
    conn.commit()
    conn.close()


try:
    _init_db()
    logger.info("[Notifications] DB initialized")
except Exception as e:
    logger.error("[Notifications] DB init failed: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _expires() -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(days=NOTIFY_EXPIRY_DAYS)).isoformat()


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["is_read"] = bool(d.get("is_read", 0))
    return d


# ── Core: create notification ─────────────────────────────────────────────────

def notify(
    user_id: str,
    title: str,
    message: str,
    type: str = "system",
    priority: str = "info",
    link: Optional[str] = None,
) -> dict:
    """
    Create and persist a notification, then push to any open SSE streams.
    Returns the created notification dict.
    """
    type     = type     if type     in VALID_TYPES      else "system"
    priority = priority if priority in VALID_PRIORITIES else "info"
    uid = str(user_id)
    nid = uuid.uuid4().hex
    now = _now()

    try:
        conn = _db()
        conn.execute(
            "INSERT INTO notifications "
            "(id, user_id, type, priority, title, message, link, is_read, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (nid, uid, type, priority, title[:200], message[:1000], link, now, _expires()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Notifications] DB insert failed: %s", e)

    n = {
        "id": nid, "user_id": uid, "type": type, "priority": priority,
        "title": title, "message": message, "link": link,
        "is_read": False, "created_at": now,
    }

    # Push to SSE subscribers
    _push_sse(uid, n)

    # Email fallback for critical events
    if priority == "critical":
        _email_critical(uid, title, message)

    return n


# ── SSE push ─────────────────────────────────────────────────────────────────

def _push_sse(user_id: str, notification: dict):
    """Push notification to all open SSE streams for this user."""
    with _sse_lock:
        queues = _sse_subscribers.get(user_id, [])
        dead = []
        for q in queues:
            try:
                q.put_nowait(notification)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                queues.remove(q)
            except ValueError:
                pass


def subscribe_sse(user_id: str) -> queue.Queue:
    """Register a new SSE listener for a user. Returns the Queue to read from."""
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_subscribers.setdefault(str(user_id), []).append(q)
    return q


def unsubscribe_sse(user_id: str, q: queue.Queue):
    """Deregister an SSE listener."""
    with _sse_lock:
        queues = _sse_subscribers.get(str(user_id), [])
        try:
            queues.remove(q)
        except ValueError:
            pass


def sse_stream(user_id: str) -> Generator[str, None, None]:
    """
    Generator for a Flask SSE Response.
    Yields `data: <json>\\n\\n` for each notification.
    Sends a heartbeat ping every 12 seconds.
    """
    uid = str(user_id)
    q   = subscribe_sse(uid)

    # Immediately send unread count on connect
    unread = count_unread(uid)
    yield f"event: init\ndata: {json.dumps({'unread': unread})}\n\n"

    try:
        last_ping = time.time()
        while True:
            try:
                notification = q.get(timeout=12)
                yield f"data: {json.dumps(notification)}\n\n"
                last_ping = time.time()
            except queue.Empty:
                # Heartbeat
                if time.time() - last_ping >= 12:
                    yield f": ping\n\n"
                    last_ping = time.time()
    except GeneratorExit:
        pass
    finally:
        unsubscribe_sse(uid, q)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def get_notifications(
    user_id: str,
    limit: int = 30,
    unread_only: bool = False,
    type: Optional[str] = None,
) -> list:
    conn = _db()
    c = conn.cursor()
    where = ["user_id = ?", "expires_at > ?"]
    params = [str(user_id), _now()]
    if unread_only:
        where.append("is_read = 0")
    if type and type in VALID_TYPES:
        where.append("type = ?")
        params.append(type)
    sql = ("SELECT * FROM notifications WHERE " + " AND ".join(where)
           + " ORDER BY created_at DESC LIMIT ?")
    params.append(min(limit, 100))
    c.execute(sql, params)
    rows = [_row_to_dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def count_unread(user_id: str) -> int:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0 AND expires_at>?",
        (str(user_id), _now()),
    )
    n = c.fetchone()[0]
    conn.close()
    return n


def get_notification(nid: str, user_id: str) -> Optional[dict]:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT * FROM notifications WHERE id=? AND user_id=?", (nid, str(user_id)))
    row = c.fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def mark_read(nid: str, user_id: str) -> bool:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
        (nid, str(user_id)),
    )
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def mark_all_read(user_id: str) -> int:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0",
        (str(user_id),),
    )
    n = c.rowcount
    conn.commit()
    conn.close()
    return n


def delete_notification(nid: str, user_id: str) -> bool:
    conn = _db()
    c = conn.cursor()
    c.execute("DELETE FROM notifications WHERE id=? AND user_id=?", (nid, str(user_id)))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def purge_expired():
    """Remove notifications older than NOTIFY_EXPIRY_DAYS."""
    try:
        conn = _db()
        conn.execute("DELETE FROM notifications WHERE expires_at < ?", (_now(),))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Notifications] purge failed: %s", e)


# ── Pre-built notification helpers ───────────────────────────────────────────

def notify_task_complete(user_id: str, task_label: str, sid: str = ""):
    link = f"/?session={sid}" if sid else None
    return notify(
        user_id=user_id,
        type="task",
        priority="info",
        title="Task Completed",
        message=f"Your AI agent successfully completed: {task_label[:120]}",
        link=link,
    )


def notify_task_failed(user_id: str, task_label: str, reason: str = "", sid: str = ""):
    link = f"/?session={sid}" if sid else None
    return notify(
        user_id=user_id,
        type="task",
        priority="warning",
        title="Task Failed",
        message=f"Task failed: {task_label[:80]}" + (f" — {reason[:80]}" if reason else ""),
        link=link,
    )


def notify_support_reply(user_id: str, ticket_id: str, subject: str):
    return notify(
        user_id=user_id,
        type="support",
        priority="info",
        title="Support Reply Received",
        message=f"The support team replied to your ticket: {subject[:100]}",
        link="#support",
    )


def notify_payment_success(user_id: str, plan: str, expiry: str):
    plan_label = plan.title() if plan else "Pro"
    return notify(
        user_id=user_id,
        type="billing",
        priority="info",
        title=f"{plan_label} Plan Activated!",
        message=f"Your {plan_label} subscription is active until {expiry}. Enjoy all features!",
        link="#billing",
    )


def notify_plan_expiry_warning(user_id: str, plan: str, days_left: int):
    return notify(
        user_id=user_id,
        type="billing",
        priority="warning" if days_left > 3 else "critical",
        title="Plan Expiring Soon",
        message=f"Your {plan.title()} plan expires in {days_left} day{'s' if days_left != 1 else ''}. Renew to keep access.",
        link="#billing",
    )


def notify_system(user_id: str, title: str, message: str, priority: str = "info"):
    return notify(user_id=user_id, type="system", priority=priority,
                  title=title, message=message)


# ── Email fallback ────────────────────────────────────────────────────────────

def _email_critical(user_id: str, title: str, message: str):
    """Send email for critical notifications if EMAIL_API_KEY is configured."""
    if not EMAIL_API_KEY:
        return
    try:
        # Try to get user email from DB
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT email, name FROM users WHERE id=?", (str(user_id),))
        row = c.fetchone()
        conn.close()
        if not row or not row[0]:
            return
        to_email, name = row[0], row[1] or "User"
        _send_critical_email(to_email, name, title, message)
    except Exception as e:
        logger.warning("[Notifications] email fallback failed: %s", e)


def _send_critical_email(to: str, name: str, title: str, message: str):
    import urllib.request
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
      <div style="background:#0d1117;border-radius:12px;padding:24px;color:#e6edf3;border:1px solid #f8514944">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
          <span style="font-size:20px">🔴</span>
          <h2 style="color:#f85149;margin:0;font-size:16px">Critical Alert — {APP_NAME}</h2>
        </div>
        <p style="color:#8b949e;margin:0 0 12px">Hi {name},</p>
        <div style="background:#161b22;border-radius:8px;padding:16px;margin-bottom:16px">
          <div style="font-weight:700;color:#e6edf3;margin-bottom:8px">{title}</div>
          <div style="color:#8b949e;font-size:13px">{message}</div>
        </div>
        <p style="color:#8b949e;font-size:12px;margin-top:16px">Log in to Nexora to take action.</p>
        <p style="color:#30363d;font-size:11px;margin-top:12px">{APP_NAME} · noreply@nexora.ai</p>
      </div>
    </div>
    """
    try:
        payload = json.dumps({
            "from":    EMAIL_FROM,
            "to":      [to],
            "subject": f"[Nexora] Critical: {title}",
            "html":    html,
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {EMAIL_API_KEY}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        logger.warning("[Notifications] critical email send failed: %s", e)


# ── Background cleanup ────────────────────────────────────────────────────────

def _start_cleanup():
    def _loop():
        while True:
            time.sleep(7200)  # every 2 hours
            try:
                purge_expired()
                logger.debug("[Notifications] Purged expired entries")
            except Exception as e:
                logger.warning("[Notifications] cleanup error: %s", e)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


_start_cleanup()
logger.info("[Notifications] Real-time notification engine initialized.")
