"""
payments.py — Razorpay Billing Integration
==========================================
Full SaaS payment backend:
  - Order creation (Pro / Elite, monthly / yearly)
  - Payment verification (signature check)
  - Webhook handler (source of truth)
  - Subscription database (SQLite)
  - Invoice generation (HTML + stored)
  - Email notifications (Resend API)
"""

import os
import hmac
import json
import hashlib
import logging
import sqlite3
import datetime
import uuid
import threading
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config — loaded from environment
# ─────────────────────────────────────────────────────────────────────────────

RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
EMAIL_API_KEY       = os.getenv("EMAIL_API_KEY", "")       # Resend API key
EMAIL_FROM          = os.getenv("EMAIL_FROM", "billing@antigravity.ai")
APP_NAME            = "Antigravity AI"

RAZORPAY_ENABLED = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# Pricing in INR paise (1 INR = 100 paise)
PLANS = {
    "pro": {
        "name":    "Pro",
        "monthly": {"amount": 2000,  "label": "₹20/month"},
        "yearly":  {"amount": 20000, "label": "₹200/year"},
        "days_monthly": 30,
        "days_yearly":  365,
    },
    "elite": {
        "name":    "Elite",
        "monthly": {"amount": 5000,  "label": "₹50/month"},
        "yearly":  {"amount": 50000, "label": "₹500/year"},
        "days_monthly": 30,
        "days_yearly":  365,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

BILLING_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "billing.db")
_db_lock   = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(BILLING_DB, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_billing_db():
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT 'default',
            plan        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            billing_cycle TEXT NOT NULL DEFAULT 'monthly',
            start_date  TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            payment_id  TEXT,
            order_id    TEXT,
            amount      INTEGER,
            currency    TEXT DEFAULT 'INR',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL DEFAULT 'default',
            subscription_id TEXT,
            payment_id  TEXT,
            order_id    TEXT,
            plan        TEXT NOT NULL,
            billing_cycle TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            currency    TEXT DEFAULT 'INR',
            user_email  TEXT,
            user_name   TEXT,
            status      TEXT DEFAULT 'paid',
            issued_at   TEXT NOT NULL,
            html        TEXT
        );
        CREATE TABLE IF NOT EXISTS payment_events (
            id          TEXT PRIMARY KEY,
            event_type  TEXT,
            payment_id  TEXT,
            order_id    TEXT,
            payload     TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub_user   ON subscriptions(user_id);
        CREATE INDEX IF NOT EXISTS idx_inv_user   ON invoices(user_id);
        CREATE INDEX IF NOT EXISTS idx_inv_payid  ON invoices(payment_id);
        """)


try:
    init_billing_db()
except Exception as e:
    logger.error("[Billing] DB init failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Razorpay helpers
# ─────────────────────────────────────────────────────────────────────────────

def _razorpay_request(method: str, path: str, body: dict | None = None) -> dict:
    """Raw HTTPS request to Razorpay API with basic auth."""
    import base64
    url  = f"https://api.razorpay.com/v1{path}"
    cred = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {cred}",
        "Content-Type":  "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Razorpay {method} {path} → {e.code}: {msg}") from e


def create_razorpay_order(plan: str, billing_cycle: str) -> dict:
    """Create a Razorpay order and return order details."""
    if not RAZORPAY_ENABLED:
        raise RuntimeError("Razorpay keys not configured. Add RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET.")

    plan_info = PLANS.get(plan)
    if not plan_info:
        raise ValueError(f"Unknown plan: {plan}")
    if billing_cycle not in ("monthly", "yearly"):
        raise ValueError(f"Unknown billing_cycle: {billing_cycle}")

    pricing   = plan_info[billing_cycle]
    receipt   = f"rcpt_{uuid.uuid4().hex[:12]}"

    order = _razorpay_request("POST", "/orders", {
        "amount":   pricing["amount"],
        "currency": "INR",
        "receipt":  receipt,
        "notes": {
            "plan":          plan,
            "billing_cycle": billing_cycle,
            "app":           APP_NAME,
        },
    })
    return {
        "razorpay_order_id": order["id"],
        "amount":            order["amount"],
        "currency":          order["currency"],
        "plan":              plan,
        "plan_name":         plan_info["name"],
        "billing_cycle":     billing_cycle,
        "price_label":       pricing["label"],
        "key_id":            RAZORPAY_KEY_ID,
    }


def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Verify Razorpay payment signature. Returns True if valid."""
    if not RAZORPAY_KEY_SECRET:
        return False
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Verify Razorpay webhook signature."""
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_KEY_SECRET)
    if not webhook_secret:
        return False
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ─────────────────────────────────────────────────────────────────────────────
# Subscription management
# ─────────────────────────────────────────────────────────────────────────────

def activate_subscription(plan: str, billing_cycle: str, payment_id: str,
                           order_id: str, amount: int,
                           user_id: str = "default",
                           user_email: str = "",
                           user_name: str = "") -> dict:
    """Activate or upgrade a subscription after verified payment."""
    now    = datetime.datetime.utcnow()
    days   = PLANS[plan][f"days_{billing_cycle}"]
    expiry = now + datetime.timedelta(days=days)

    sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            UPDATE subscriptions SET status='cancelled'
            WHERE user_id=? AND status='active'
        """, (user_id,))
        conn.execute("""
            INSERT INTO subscriptions
            (id, user_id, plan, status, billing_cycle, start_date, expiry_date,
             payment_id, order_id, amount, currency, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (sub_id, user_id, plan, "active", billing_cycle,
              now.isoformat(), expiry.isoformat(),
              payment_id, order_id, amount, "INR", now.isoformat()))

    inv = _create_invoice(
        sub_id=sub_id, plan=plan, billing_cycle=billing_cycle,
        payment_id=payment_id, order_id=order_id, amount=amount,
        user_id=user_id, user_email=user_email, user_name=user_name,
        issued_at=now,
    )

    _update_p8_plan(plan, expiry.date().isoformat())

    if user_email:
        _send_payment_success_email(
            to_email=user_email,
            user_name=user_name or user_email,
            plan=plan,
            billing_cycle=billing_cycle,
            amount=amount,
            expiry=expiry.date().isoformat(),
            invoice_id=inv["id"],
        )

    return {"subscription_id": sub_id, "invoice_id": inv["id"], "expiry": expiry.date().isoformat()}


def get_active_subscription(user_id: str = "default") -> dict | None:
    """Return the current active subscription or None."""
    now = datetime.datetime.utcnow().isoformat()
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM subscriptions
            WHERE user_id=? AND status='active' AND expiry_date > ?
            ORDER BY created_at DESC LIMIT 1
        """, (user_id, now)).fetchone()
    return dict(row) if row else None


def cancel_subscription(user_id: str = "default") -> bool:
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            UPDATE subscriptions SET status='cancelled'
            WHERE user_id=? AND status='active'
        """, (user_id,))
    _update_p8_plan("free", None)
    return True


def check_and_expire_subscriptions():
    """Auto-expire subscriptions past their expiry date."""
    now = datetime.datetime.utcnow().isoformat()
    with _db_lock, _get_conn() as conn:
        rows = conn.execute("""
            SELECT id, user_id FROM subscriptions
            WHERE status='active' AND expiry_date <= ?
        """, (now,)).fetchall()
        for row in rows:
            conn.execute("UPDATE subscriptions SET status='expired' WHERE id=?", (row["id"],))
            _update_p8_plan("free", None)
    return len(rows)


def get_billing_info(user_id: str = "default") -> dict:
    """Return full billing info for the user."""
    sub  = get_active_subscription(user_id)
    invs = get_invoices(user_id)
    return {
        "subscription":    sub,
        "invoices":        invs,
        "razorpay_enabled": RAZORPAY_ENABLED,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Invoice generation
# ─────────────────────────────────────────────────────────────────────────────

def _create_invoice(sub_id, plan, billing_cycle, payment_id, order_id,
                    amount, user_id, user_email, user_name, issued_at) -> dict:
    inv_id  = f"inv_{uuid.uuid4().hex[:16]}"
    html    = _render_invoice_html(
        inv_id=inv_id, plan=plan, billing_cycle=billing_cycle,
        amount=amount, user_email=user_email, user_name=user_name,
        payment_id=payment_id, issued_at=issued_at,
    )
    with _db_lock, _get_conn() as conn:
        conn.execute("""
            INSERT INTO invoices
            (id, user_id, subscription_id, payment_id, order_id, plan, billing_cycle,
             amount, currency, user_email, user_name, status, issued_at, html)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (inv_id, user_id, sub_id, payment_id, order_id, plan, billing_cycle,
              amount, "INR", user_email, user_name, "paid",
              issued_at.isoformat(), html))
    return {"id": inv_id, "html": html}


def get_invoice(invoice_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    return dict(row) if row else None


def get_invoices(user_id: str = "default") -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT id, plan, billing_cycle, amount, currency, status, issued_at
            FROM invoices WHERE user_id=? ORDER BY issued_at DESC LIMIT 20
        """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def _render_invoice_html(inv_id, plan, billing_cycle, amount, user_email,
                          user_name, payment_id, issued_at) -> str:
    plan_info  = PLANS.get(plan, {})
    plan_name  = plan_info.get("name", plan.title())
    amount_inr = f"₹{amount // 100}"
    date_str   = issued_at.strftime("%B %d, %Y") if hasattr(issued_at, "strftime") else str(issued_at)[:10]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Invoice {inv_id} — {APP_NAME}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0d1117; color: #e6edf3; margin: 0; padding: 40px; }}
  .inv-card {{ max-width: 680px; margin: 0 auto; background: #161b22;
               border: 1px solid #30363d; border-radius: 12px; padding: 40px; }}
  .inv-header {{ display: flex; justify-content: space-between; align-items: flex-start;
                  border-bottom: 1px solid #30363d; padding-bottom: 24px; margin-bottom: 24px; }}
  .inv-brand {{ font-size: 22px; font-weight: 800; color: #bc8cff; }}
  .inv-badge {{ background: rgba(188,140,255,.15); color: #bc8cff;
                border: 1px solid rgba(188,140,255,.3); border-radius: 20px;
                padding: 4px 14px; font-size: 12px; font-weight: 700; }}
  .inv-title {{ font-size: 28px; font-weight: 800; margin-bottom: 4px; }}
  .inv-id {{ font-size: 12px; color: #8b949e; margin-bottom: 24px; }}
  .inv-row {{ display: flex; justify-content: space-between;
               border-bottom: 1px solid #21262d; padding: 10px 0; font-size: 14px; }}
  .inv-row:last-child {{ border-bottom: none; }}
  .inv-label {{ color: #8b949e; }}
  .inv-value {{ font-weight: 600; }}
  .inv-total {{ background: rgba(188,140,255,.08); border-radius: 8px; padding: 16px 20px;
                margin-top: 20px; display: flex; justify-content: space-between; align-items: center; }}
  .inv-total-label {{ font-size: 14px; color: #8b949e; }}
  .inv-total-value {{ font-size: 28px; font-weight: 800; color: #bc8cff; }}
  .inv-footer {{ margin-top: 32px; padding-top: 20px; border-top: 1px solid #30363d;
                  font-size: 12px; color: #8b949e; text-align: center; }}
  .inv-status {{ display: inline-block; background: rgba(63,185,80,.12); color: #3fb950;
                  border: 1px solid rgba(63,185,80,.3); border-radius: 20px;
                  padding: 3px 12px; font-size: 11px; font-weight: 700; }}
  @media print {{ body {{ background: white; color: black; }}
                   .inv-card {{ border: none; background: white; }} }}
</style>
</head>
<body>
<div class="inv-card">
  <div class="inv-header">
    <div>
      <div class="inv-brand">⚡ {APP_NAME}</div>
      <div style="font-size:12px;color:#8b949e;margin-top:4px">AI Development Platform</div>
    </div>
    <div class="inv-badge">PAID ✓</div>
  </div>

  <div class="inv-title">Invoice</div>
  <div class="inv-id">#{inv_id}</div>

  <div class="inv-row">
    <span class="inv-label">Invoice Date</span>
    <span class="inv-value">{date_str}</span>
  </div>
  <div class="inv-row">
    <span class="inv-label">Plan</span>
    <span class="inv-value">{plan_name} ({billing_cycle.title()})</span>
  </div>
  {"<div class='inv-row'><span class='inv-label'>Customer</span><span class='inv-value'>" + (user_name or "—") + "</span></div>" if user_name else ""}
  {"<div class='inv-row'><span class='inv-label'>Email</span><span class='inv-value'>" + user_email + "</span></div>" if user_email else ""}
  <div class="inv-row">
    <span class="inv-label">Payment ID</span>
    <span class="inv-value" style="font-family:monospace;font-size:12px">{payment_id or "—"}</span>
  </div>
  <div class="inv-row">
    <span class="inv-label">Status</span>
    <span class="inv-status">PAID</span>
  </div>

  <div class="inv-total">
    <div class="inv-total-label">Total Paid</div>
    <div class="inv-total-value">{amount_inr}</div>
  </div>

  <div class="inv-footer">
    <div>Thank you for your payment! Your {plan_name} plan is now active.</div>
    <div style="margin-top:8px">Questions? Contact support — {APP_NAME}</div>
  </div>
</div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Email (Resend)
# ─────────────────────────────────────────────────────────────────────────────

def _send_payment_success_email(to_email: str, user_name: str, plan: str,
                                 billing_cycle: str, amount: int,
                                 expiry: str, invoice_id: str):
    if not EMAIL_API_KEY or not to_email:
        logger.info("[Billing] Email skipped — no API key or recipient")
        return
    plan_name  = PLANS.get(plan, {}).get("name", plan.title())
    amount_inr = f"₹{amount // 100}"
    html_body  = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;padding:32px">
      <h1 style="color:#bc8cff;font-size:24px;margin-bottom:4px">⚡ {APP_NAME}</h1>
      <h2 style="font-size:20px;font-weight:800;margin-bottom:16px">Payment Successful ✅</h2>
      <p>Hi {user_name},</p>
      <p>Your <strong>{plan_name} ({billing_cycle})</strong> subscription is now active.</p>
      <div style="background:#f6f8fa;border-radius:8px;padding:16px;margin:20px 0">
        <div><b>Plan:</b> {plan_name}</div>
        <div><b>Amount Paid:</b> {amount_inr}</div>
        <div><b>Valid Until:</b> {expiry}</div>
        <div><b>Invoice:</b> #{invoice_id}</div>
      </div>
      <p style="color:#666">Thank you for choosing {APP_NAME}!</p>
    </div>"""

    payload = json.dumps({
        "from":    EMAIL_FROM,
        "to":      [to_email],
        "subject": f"Payment Confirmed — {plan_name} Plan Active | {APP_NAME}",
        "html":    html_body,
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
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("[Billing] Email sent to %s — %s", to_email, resp.status)
    except Exception as e:
        logger.warning("[Billing] Email failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Hook into existing p8 plan state
# ─────────────────────────────────────────────────────────────────────────────

def _update_p8_plan(plan: str, expires: str | None):
    """Update the existing Phase 8 subscription state to reflect a real payment."""
    try:
        from web_app import get_setting, set_setting
        _P8_KEY = "p8_subscription"
        state = get_setting(_P8_KEY, {})
        state["plan"]    = plan
        state["expires"] = expires
        set_setting(_P8_KEY, state)
        logger.info("[Billing] p8 plan updated → %s (expires %s)", plan, expires)
    except Exception as e:
        logger.warning("[Billing] Could not update p8 state: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Webhook event logging
# ─────────────────────────────────────────────────────────────────────────────

def log_webhook_event(event_type: str, payment_id: str, order_id: str, payload: dict):
    eid = f"evt_{uuid.uuid4().hex[:16]}"
    now = datetime.datetime.utcnow().isoformat()
    try:
        with _db_lock, _get_conn() as conn:
            conn.execute("""
                INSERT INTO payment_events (id, event_type, payment_id, order_id, payload, created_at)
                VALUES (?,?,?,?,?,?)
            """, (eid, event_type, payment_id, order_id, json.dumps(payload), now))
    except Exception as e:
        logger.warning("[Billing] Could not log webhook event: %s", e)
