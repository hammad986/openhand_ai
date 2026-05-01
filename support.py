"""
support.py — Customer Support & Communication System
=====================================================
Enterprise-grade support ticketing with:
- Ticket lifecycle management (open → in_progress → resolved → closed)
- Priority levels (low / medium / high)
- Threaded conversation per ticket
- Email notifications (via Resend)
- AI auto-tagging (billing / bug / feature / ai_error)
- Billing info auto-attachment for payment-related tickets
"""
import sqlite3
import uuid
import json
import datetime
import os
import logging
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "support.db"

# ─── Email config ──────────────────────────────────────────────────────────────
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "support@nexora.ai")
APP_NAME      = "Nexora AI Platform"

# ─── Rate limit: 3 tickets per user per hour ──────────────────────────────────
_ticket_rate: dict = {}
_rate_lock = threading.Lock()
TICKET_RATE_LIMIT = int(os.environ.get("TICKET_RATE_LIMIT", "3"))
TICKET_RATE_WINDOW = 3600  # seconds

VALID_STATUSES   = {"open", "in_progress", "resolved", "closed"}
VALID_PRIORITIES = {"low", "medium", "high"}

# AI auto-tag patterns
_TAG_PATTERNS = {
    "billing":     re.compile(r"payment|invoice|charge|refund|subscription|plan|billing|razorpay|paid|price|cost", re.I),
    "bug":         re.compile(r"bug|error|crash|broken|fail|exception|traceback|500|issue|not work", re.I),
    "feature":     re.compile(r"feature|request|suggest|improve|enhance|add|wish|would like|can you", re.I),
    "ai_error":    re.compile(r"ai|model|gpt|gemini|claude|llm|token|completion|response|hallucin", re.I),
}


# ─── DB init ───────────────────────────────────────────────────────────────────

def init_support_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            user_email  TEXT NOT NULL DEFAULT '',
            user_name   TEXT NOT NULL DEFAULT '',
            subject     TEXT NOT NULL,
            message     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            priority    TEXT NOT NULL DEFAULT 'medium',
            tag         TEXT NOT NULL DEFAULT 'general',
            billing_ref TEXT DEFAULT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ticket_user   ON support_tickets(user_id);
        CREATE INDEX IF NOT EXISTS idx_ticket_status ON support_tickets(status);

        CREATE TABLE IF NOT EXISTS ticket_messages (
            id         TEXT PRIMARY KEY,
            ticket_id  TEXT NOT NULL,
            sender     TEXT NOT NULL DEFAULT 'user',
            user_id    TEXT NOT NULL DEFAULT '',
            message    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_msg_ticket ON ticket_messages(ticket_id);
    """)
    conn.commit()
    conn.close()


try:
    init_support_db()
    logger.info("[Support] DB initialized")
except Exception as e:
    logger.error("[Support] DB init failed: %s", e)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _auto_tag(subject: str, message: str) -> str:
    text = f"{subject} {message}"
    for tag, pattern in _TAG_PATTERNS.items():
        if pattern.search(text):
            return tag
    return "general"


def _check_rate_limit(user_id: str) -> bool:
    """Returns True if user is allowed to create a ticket."""
    now = datetime.datetime.utcnow().timestamp()
    cutoff = now - TICKET_RATE_WINDOW
    with _rate_lock:
        times = _ticket_rate.get(user_id, [])
        times = [t for t in times if t > cutoff]
        if len(times) >= TICKET_RATE_LIMIT:
            return False
        times.append(now)
        _ticket_rate[user_id] = times
    return True


def _billing_info_for_ticket(user_id: str) -> Optional[str]:
    """Try to attach billing info for billing-tagged tickets."""
    try:
        import payments as _pay
        info = _pay.get_billing_info(user_id=str(user_id))
        if info.get("active_plan") and info["active_plan"] != "free":
            return json.dumps({
                "plan":         info.get("active_plan"),
                "billing_cycle": info.get("billing_cycle"),
                "expiry":       info.get("expiry_date"),
            })
    except Exception:
        pass
    return None


# ─── Email notifications ───────────────────────────────────────────────────────

def _send_email(to: str, subject: str, html: str):
    """Send email via Resend API."""
    if not EMAIL_API_KEY or not to:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "from":    EMAIL_FROM,
            "to":      [to],
            "subject": subject,
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("[Support] Email sent to %s — status %s", to, resp.status)
    except Exception as e:
        logger.warning("[Support] Email send failed: %s", e)


def _email_ticket_created(ticket: dict):
    if not ticket.get("user_email"):
        return
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
      <div style="background:#0d1117;border-radius:12px;padding:24px;color:#e6edf3">
        <h2 style="color:#bc8cff;margin:0 0 16px">Nexora Support</h2>
        <p style="color:#8b949e;margin:0 0 12px">Your support ticket has been received.</p>
        <div style="background:#161b22;border-radius:8px;padding:16px;margin-bottom:16px">
          <div style="font-size:12px;color:#8b949e;margin-bottom:4px">TICKET ID</div>
          <div style="font-weight:700;color:#58a6ff">#{ticket['id'][:8].upper()}</div>
          <div style="font-size:12px;color:#8b949e;margin-top:12px;margin-bottom:4px">SUBJECT</div>
          <div style="color:#e6edf3">{ticket['subject']}</div>
          <div style="font-size:12px;color:#8b949e;margin-top:12px;margin-bottom:4px">PRIORITY</div>
          <div style="color:#d29922;text-transform:uppercase;font-size:12px;font-weight:700">{ticket['priority']}</div>
        </div>
        <p style="color:#8b949e;font-size:13px">
          We'll respond within 24 hours. You can view your ticket status in the platform under Support.
        </p>
        <p style="color:#30363d;font-size:11px;margin-top:20px">Nexora AI Platform · support@nexora.ai</p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(ticket["user_email"], f"[Nexora Support] Ticket #{ticket['id'][:8].upper()} received", html),
        daemon=True,
    ).start()


def _email_ticket_reply(ticket: dict, reply_message: str, sender: str):
    if not ticket.get("user_email") or sender == "user":
        return
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
      <div style="background:#0d1117;border-radius:12px;padding:24px;color:#e6edf3">
        <h2 style="color:#bc8cff;margin:0 0 16px">Nexora Support — New Reply</h2>
        <p style="color:#8b949e;margin:0 0 12px">
          Ticket <strong style="color:#58a6ff">#{ticket['id'][:8].upper()}</strong>:
          <em>{ticket['subject']}</em>
        </p>
        <div style="background:#161b22;border-radius:8px;padding:16px;margin-bottom:16px;color:#e6edf3">
          {reply_message[:500]}
        </div>
        <p style="color:#8b949e;font-size:13px">Log in to Nexora to continue the conversation.</p>
        <p style="color:#30363d;font-size:11px;margin-top:20px">Nexora AI Platform · support@nexora.ai</p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(ticket["user_email"], f"[Nexora Support] Reply on #{ticket['id'][:8].upper()}", html),
        daemon=True,
    ).start()


def _email_status_change(ticket: dict, new_status: str):
    if not ticket.get("user_email"):
        return
    status_labels = {
        "open":        "Opened",
        "in_progress": "In Progress",
        "resolved":    "Resolved",
        "closed":      "Closed",
    }
    label = status_labels.get(new_status, new_status.title())
    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
      <div style="background:#0d1117;border-radius:12px;padding:24px;color:#e6edf3">
        <h2 style="color:#bc8cff;margin:0 0 16px">Nexora Support — Status Update</h2>
        <p style="color:#8b949e;margin:0 0 12px">
          Ticket <strong style="color:#58a6ff">#{ticket['id'][:8].upper()}</strong> is now
          <strong style="color:#3fb950">{label}</strong>.
        </p>
        <p style="color:#8b949e;font-size:13px">Log in to Nexora to view your ticket.</p>
        <p style="color:#30363d;font-size:11px;margin-top:20px">Nexora AI Platform · support@nexora.ai</p>
      </div>
    </div>
    """
    threading.Thread(
        target=_send_email,
        args=(ticket["user_email"], f"[Nexora Support] Ticket #{ticket['id'][:8].upper()} — {label}", html),
        daemon=True,
    ).start()


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def create_ticket(user_id: str, subject: str, message: str,
                  priority: str = "medium",
                  user_email: str = "", user_name: str = "") -> dict:
    if not _check_rate_limit(str(user_id)):
        raise ValueError("Rate limit: max 3 tickets per hour")

    subject  = subject.strip()[:200]
    message  = message.strip()[:5000]
    priority = priority if priority in VALID_PRIORITIES else "medium"

    if not subject or not message:
        raise ValueError("Subject and message are required")

    tag = _auto_tag(subject, message)
    billing_ref = _billing_info_for_ticket(user_id) if tag == "billing" else None

    now  = _now()
    tid  = uuid.uuid4().hex

    conn = _db()
    conn.execute(
        "INSERT INTO support_tickets "
        "(id, user_id, user_email, user_name, subject, message, status, priority, tag, billing_ref, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
        (tid, str(user_id), user_email[:200], user_name[:100],
         subject, message, priority, tag, billing_ref, now, now),
    )
    # Add initial message to thread
    conn.execute(
        "INSERT INTO ticket_messages (id, ticket_id, sender, user_id, message, created_at) "
        "VALUES (?, ?, 'user', ?, ?, ?)",
        (uuid.uuid4().hex, tid, str(user_id), message, now),
    )
    conn.commit()
    conn.close()

    ticket = get_ticket(tid, user_id=str(user_id))
    if ticket:
        _email_ticket_created(ticket)
    return ticket or {}


def get_ticket(ticket_id: str, user_id: str = None, is_admin: bool = False) -> Optional[dict]:
    conn = _db()
    c = conn.cursor()
    if is_admin or user_id is None:
        c.execute("SELECT * FROM support_tickets WHERE id = ?", (ticket_id,))
    else:
        c.execute("SELECT * FROM support_tickets WHERE id = ? AND user_id = ?", (ticket_id, str(user_id)))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    t = dict(row)
    t["messages"] = get_ticket_messages(ticket_id)
    return t


def list_tickets(user_id: str = None, status: str = None,
                 priority: str = None, limit: int = 50,
                 is_admin: bool = False) -> list:
    conn = _db()
    c = conn.cursor()
    where, params = [], []

    if not is_admin and user_id:
        where.append("user_id = ?")
        params.append(str(user_id))
    if status and status in VALID_STATUSES:
        where.append("status = ?")
        params.append(status)
    if priority and priority in VALID_PRIORITIES:
        where.append("priority = ?")
        params.append(priority)

    sql = "SELECT * FROM support_tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(min(limit, 200))

    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_ticket_messages(ticket_id: str) -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY created_at ASC",
        (ticket_id,),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def reply_to_ticket(ticket_id: str, message: str,
                    sender: str = "user", user_id: str = "",
                    is_admin: bool = False) -> dict:
    message = message.strip()[:5000]
    if not message:
        raise ValueError("Message cannot be empty")
    if sender not in ("user", "admin"):
        sender = "user"

    conn = _db()
    c = conn.cursor()
    # Permission check
    if not is_admin:
        c.execute("SELECT id, user_id FROM support_tickets WHERE id = ?", (ticket_id,))
        row = c.fetchone()
        if not row:
            raise PermissionError("Ticket not found")
        if str(row["user_id"]) != str(user_id):
            raise PermissionError("Access denied")

    mid = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO ticket_messages (id, ticket_id, sender, user_id, message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mid, ticket_id, sender, str(user_id), message, now),
    )
    # Auto-advance status when admin replies
    if sender == "admin":
        conn.execute(
            "UPDATE support_tickets SET status='in_progress', updated_at=? "
            "WHERE id=? AND status='open'",
            (now, ticket_id),
        )
    else:
        conn.execute(
            "UPDATE support_tickets SET updated_at=? WHERE id=?",
            (now, ticket_id),
        )
    conn.commit()
    conn.close()

    ticket = get_ticket(ticket_id, is_admin=True)
    if ticket:
        _email_ticket_reply(ticket, message, sender)
    return {"id": mid, "ticket_id": ticket_id, "sender": sender,
            "message": message, "created_at": now}


def update_ticket_status(ticket_id: str, new_status: str,
                         user_id: str = "", is_admin: bool = False) -> dict:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status}")

    conn = _db()
    c = conn.cursor()
    if not is_admin:
        c.execute("SELECT user_id FROM support_tickets WHERE id = ?", (ticket_id,))
        row = c.fetchone()
        if not row or str(row["user_id"]) != str(user_id):
            raise PermissionError("Access denied")

    now = _now()
    conn.execute(
        "UPDATE support_tickets SET status=?, updated_at=? WHERE id=?",
        (new_status, now, ticket_id),
    )
    conn.commit()
    conn.close()

    ticket = get_ticket(ticket_id, is_admin=True)
    if ticket:
        _email_status_change(ticket, new_status)
    return {"ok": True, "status": new_status}


def get_support_stats() -> dict:
    """Admin summary stats."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) as n FROM support_tickets GROUP BY status")
    by_status = {r["status"]: r["n"] for r in c.fetchall()}
    c.execute("SELECT priority, COUNT(*) as n FROM support_tickets GROUP BY priority")
    by_priority = {r["priority"]: r["n"] for r in c.fetchall()}
    c.execute("SELECT COUNT(*) as total FROM support_tickets")
    total = c.fetchone()["total"]
    conn.close()
    return {"total": total, "by_status": by_status, "by_priority": by_priority}
