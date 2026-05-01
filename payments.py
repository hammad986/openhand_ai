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
EMAIL_FROM          = os.getenv("EMAIL_FROM", "billing@nexora.ai")
APP_NAME            = "Nexora"
APP_FULL_NAME       = "Nexora AI Platform"

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
    """Activate or upgrade a subscription after verified payment.

    IDEMPOTENT: If payment_id was already activated, return the existing
    subscription without creating a duplicate. This prevents double-activation
    from webhook replays or client retries.
    """
    # ── Billing dedup: prevent double-activation ──────────────────────────────
    try:
        from idempotency import billing_dedup_check, billing_dedup_store
        if billing_dedup_check(payment_id):
            logger.warning(
                "[Billing] Duplicate activation attempt for payment_id=%s — returning cached result",
                payment_id,
            )
            # Return the existing subscription info
            sub = get_active_subscription(user_id)
            if sub:
                return {
                    "subscription_id": sub["id"],
                    "invoice_id":      sub.get("payment_id", ""),
                    "expiry":          sub["expiry_date"][:10],
                    "duplicate":       True,
                }
    except ImportError:
        pass

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

    # ── Record in dedup table so future activations for this payment are skipped ──
    try:
        from idempotency import billing_dedup_store
        billing_dedup_store(payment_id, order_id, user_id, plan)
    except ImportError:
        pass

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

    nexora_svg = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z" fill="#bc8cff" stroke="rgba(188,140,255,0.3)" stroke-width="0.5"/></svg>'
    cust_row   = f"<div class='inv-row'><span class='inv-label'>Customer</span><span class='inv-value'>{user_name}</span></div>" if user_name else ""
    email_row  = f"<div class='inv-row'><span class='inv-label'>Email</span><span class='inv-value'>{user_email}</span></div>" if user_email else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Invoice {inv_id[:8].upper()} — {APP_FULL_NAME}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
        background:#0d1117;color:#e6edf3;padding:40px 20px;}}
  .inv-wrap{{max-width:700px;margin:0 auto}}
  .inv-card{{background:#161b22;border:1px solid #30363d;border-radius:16px;
             padding:48px;box-shadow:0 8px 32px rgba(0,0,0,.4)}}
  .inv-header{{display:flex;justify-content:space-between;align-items:flex-start;
               border-bottom:1px solid #30363d;padding-bottom:28px;margin-bottom:28px}}
  .inv-brand{{display:flex;align-items:center;gap:10px}}
  .inv-brand-name{{font-size:24px;font-weight:800;color:#bc8cff;letter-spacing:-.5px}}
  .inv-brand-sub{{font-size:11px;color:#8b949e;margin-top:2px;text-transform:uppercase;letter-spacing:.06em}}
  .inv-paid-badge{{background:rgba(63,185,80,.12);color:#3fb950;border:1px solid rgba(63,185,80,.3);
                   border-radius:20px;padding:6px 16px;font-size:12px;font-weight:700;letter-spacing:.04em}}
  .inv-doc-title{{font-size:32px;font-weight:800;color:#e6edf3;margin-bottom:6px}}
  .inv-id{{font-size:12px;color:#8b949e;font-family:monospace;margin-bottom:28px}}
  .inv-section{{background:#0d1117;border-radius:10px;border:1px solid #21262d;
                overflow:hidden;margin-bottom:20px}}
  .inv-row{{display:flex;justify-content:space-between;align-items:center;
            padding:12px 18px;border-bottom:1px solid #21262d;font-size:14px}}
  .inv-row:last-child{{border-bottom:none}}
  .inv-label{{color:#8b949e;font-weight:500}}
  .inv-value{{font-weight:600;color:#e6edf3;text-align:right}}
  .inv-value.mono{{font-family:monospace;font-size:12px;color:#8b949e}}
  .inv-total-block{{background:linear-gradient(135deg,rgba(188,140,255,.12),rgba(188,140,255,.05));
                    border:1px solid rgba(188,140,255,.25);border-radius:12px;
                    padding:22px 24px;display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}}
  .inv-total-label{{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:.06em;font-weight:600}}
  .inv-total-value{{font-size:36px;font-weight:800;color:#bc8cff}}
  .inv-footer{{border-top:1px solid #30363d;padding-top:24px;text-align:center}}
  .inv-footer-msg{{font-size:14px;color:#e6edf3;margin-bottom:8px;font-weight:500}}
  .inv-footer-brand{{font-size:11px;color:#8b949e;letter-spacing:.04em}}
  .inv-footer-brand span{{color:#bc8cff;font-weight:700}}
  @media print{{body{{background:white;color:black;padding:20px}}
                .inv-card{{border:none;background:white;box-shadow:none}}
                .inv-section,.inv-total-block{{border-color:#ddd;background:#f9f9f9}}
                .inv-brand-name,.inv-total-value{{color:#6b21e8}}
                .inv-label,.inv-footer-brand{{color:#666}}
                .inv-value{{color:#111}}}}
</style>
</head>
<body>
<div class="inv-wrap">
<div class="inv-card">

  <div class="inv-header">
    <div class="inv-brand">
      <div style="width:40px;height:40px;background:rgba(188,140,255,.12);border-radius:10px;display:flex;align-items:center;justify-content:center;border:1px solid rgba(188,140,255,.2)">{nexora_svg}</div>
      <div>
        <div class="inv-brand-name">{APP_NAME}</div>
        <div class="inv-brand-sub">AI Platform · Official Receipt</div>
      </div>
    </div>
    <div class="inv-paid-badge">✓ PAID</div>
  </div>

  <div class="inv-doc-title">Invoice</div>
  <div class="inv-id">#{inv_id.upper()}</div>

  <div class="inv-section">
    <div class="inv-row">
      <span class="inv-label">Invoice Date</span>
      <span class="inv-value">{date_str}</span>
    </div>
    <div class="inv-row">
      <span class="inv-label">Subscription Plan</span>
      <span class="inv-value">{plan_name} · {billing_cycle.title()}</span>
    </div>
    {cust_row}
    {email_row}
    <div class="inv-row">
      <span class="inv-label">Payment Reference</span>
      <span class="inv-value mono">{payment_id or "—"}</span>
    </div>
    <div class="inv-row">
      <span class="inv-label">Status</span>
      <span class="inv-paid-badge" style="font-size:11px;padding:3px 12px">PAID</span>
    </div>
  </div>

  <div class="inv-total-block">
    <div>
      <div class="inv-total-label">Total Amount Paid</div>
      <div style="font-size:12px;color:#8b949e;margin-top:4px">Inclusive of all taxes</div>
    </div>
    <div class="inv-total-value">{amount_inr}</div>
  </div>

  <div class="inv-footer">
    <div class="inv-footer-msg">Thank you for subscribing to {APP_NAME}! Your {plan_name} plan is now active.</div>
    <div style="margin-top:6px;font-size:12px;color:#8b949e">For support, reach us at support@nexora.ai</div>
    <div style="margin-top:16px" class="inv-footer-brand">Powered by <span>{APP_FULL_NAME}</span> · nexora.ai</div>
  </div>

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
    html_body  = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#0d1117;padding:40px 20px">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="background:#161b22;border-radius:16px;border:1px solid #30363d;overflow:hidden;max-width:600px;width:100%">

      <!-- Header -->
      <tr><td style="background:linear-gradient(135deg,#161b22,#1a1f2a);padding:32px 40px;border-bottom:1px solid #30363d">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:26px;font-weight:800;color:#bc8cff;letter-spacing:-.5px">⚡ {APP_NAME}</div>
              <div style="font-size:11px;color:#8b949e;margin-top:3px;text-transform:uppercase;letter-spacing:.08em">AI Platform · Payment Receipt</div>
            </td>
            <td align="right">
              <span style="background:rgba(63,185,80,.12);color:#3fb950;border:1px solid rgba(63,185,80,.3);border-radius:20px;padding:5px 14px;font-size:12px;font-weight:700">✓ PAID</span>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:36px 40px">
        <h2 style="color:#e6edf3;font-size:22px;font-weight:800;margin:0 0 8px 0">Payment Successful!</h2>
        <p style="color:#8b949e;margin:0 0 28px 0;font-size:14px">Hi {user_name or 'there'}, your subscription is now active.</p>

        <!-- Details card -->
        <table width="100%" cellpadding="0" cellspacing="0" style="background:#0d1117;border:1px solid #21262d;border-radius:10px;overflow:hidden;margin-bottom:24px">
          <tr style="border-bottom:1px solid #21262d">
            <td style="padding:12px 18px;color:#8b949e;font-size:13px;font-weight:500">Subscription Plan</td>
            <td style="padding:12px 18px;color:#e6edf3;font-size:13px;font-weight:700;text-align:right">{plan_name} · {billing_cycle.title()}</td>
          </tr>
          <tr style="border-bottom:1px solid #21262d">
            <td style="padding:12px 18px;color:#8b949e;font-size:13px;font-weight:500">Amount Paid</td>
            <td style="padding:12px 18px;color:#bc8cff;font-size:18px;font-weight:800;text-align:right">{amount_inr}</td>
          </tr>
          <tr style="border-bottom:1px solid #21262d">
            <td style="padding:12px 18px;color:#8b949e;font-size:13px;font-weight:500">Valid Until</td>
            <td style="padding:12px 18px;color:#e6edf3;font-size:13px;font-weight:700;text-align:right">{expiry}</td>
          </tr>
          <tr>
            <td style="padding:12px 18px;color:#8b949e;font-size:13px;font-weight:500">Invoice ID</td>
            <td style="padding:12px 18px;color:#8b949e;font-size:11px;font-family:monospace;text-align:right">#{invoice_id}</td>
          </tr>
        </table>

        <p style="color:#8b949e;font-size:13px;margin:0 0 20px 0">You can download your invoice from the billing section in your dashboard anytime.</p>
        <p style="color:#8b949e;font-size:13px;margin:0">For support, contact us at <a href="mailto:support@nexora.ai" style="color:#bc8cff;text-decoration:none">support@nexora.ai</a></p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="padding:20px 40px;border-top:1px solid #30363d;text-align:center">
        <p style="color:#8b949e;font-size:11px;margin:0">Powered by <strong style="color:#bc8cff">{APP_FULL_NAME}</strong> · nexora.ai</p>
        <p style="color:#484f58;font-size:10px;margin:6px 0 0 0">You received this email because you subscribed to {APP_NAME}. © 2026 {APP_FULL_NAME}</p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""

    payload = json.dumps({
        "from":    EMAIL_FROM,
        "to":      [to_email],
        "subject": f"✓ Payment Confirmed — {plan_name} Plan Active | {APP_NAME}",
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

def log_webhook_event(event_type: str, payment_id: str, order_id: str, payload: dict) -> bool:
    """Log a webhook event. Returns False if this exact event was already processed (dedup)."""
    eid = f"evt_{uuid.uuid4().hex[:16]}"
    now = datetime.datetime.utcnow().isoformat()
    try:
        with _db_lock, _get_conn() as conn:
            # Idempotency: check if (event_type, payment_id) already logged
            if payment_id:
                existing = conn.execute(
                    "SELECT 1 FROM payment_events WHERE event_type=? AND payment_id=?",
                    (event_type, payment_id),
                ).fetchone()
                if existing:
                    logger.warning(
                        "[Billing] Duplicate webhook event %s for payment_id=%s — skipping",
                        event_type, payment_id,
                    )
                    return False  # Already processed

            conn.execute("""
                INSERT INTO payment_events (id, event_type, payment_id, order_id, payload, created_at)
                VALUES (?,?,?,?,?,?)
            """, (eid, event_type, payment_id, order_id, json.dumps(payload), now))
        return True  # New event, should be processed
    except Exception as e:
        logger.warning("[Billing] Could not log webhook event: %s", e)
        return True  # On error, allow processing to continue
