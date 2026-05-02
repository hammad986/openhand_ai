"""
account_recovery.py — Account Recovery & Email Verification (Enterprise Grade)
===============================================================================
Features:
- Forgot-password flow with secure token (SHA-256 hash stored, never plaintext)
- Reset-password via token (single-use, 30-min expiry)
- Email verification on signup (single-use, 24h expiry)
- Full session invalidation on password reset
- Rate-limit-safe (all functions return generic messages)
- Resend API for email delivery
"""

import os
import secrets
import hashlib
import sqlite3
import uuid
import datetime
import threading
import logging
import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)

DB_PATH    = "saas_platform.db"
EMAIL_API_KEY = os.environ.get("EMAIL_API_KEY", "")
EMAIL_FROM    = os.environ.get("EMAIL_FROM", "noreply@nexora.ai")
APP_NAME      = "Nexora"

RESET_TOKEN_MINUTES  = int(os.environ.get("RESET_TOKEN_MINUTES",  "30"))
VERIFY_TOKEN_HOURS   = int(os.environ.get("VERIFY_TOKEN_HOURS",   "24"))


# ─── DB init ──────────────────────────────────────────────────────────────────

def init_recovery_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS password_resets (
        id          TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        token_hash  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        used        INTEGER NOT NULL DEFAULT 0,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS email_verifications (
        id          TEXT PRIMARY KEY,
        user_id     INTEGER NOT NULL,
        token_hash  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        verified    INTEGER NOT NULL DEFAULT 0,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

    # Add email_verified column to users if missing
    try:
        c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    logger.info("[AccountRecovery] DB tables ready.")


init_recovery_db()


# ─── Token helpers ────────────────────────────────────────────────────────────

def _gen_token() -> str:
    """Generate a 48-byte (96 hex char) cryptographically secure token."""
    return secrets.token_urlsafe(48)


def _hash_token(token: str) -> str:
    """SHA-256 hash of the raw token — the only thing stored in DB."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ─── Email sending ────────────────────────────────────────────────────────────

def _send_email_async(to: str, subject: str, html: str):
    """Send email via Resend API in a background thread."""
    if not EMAIL_API_KEY or not to:
        logger.debug("[AccountRecovery] Email not sent (no API key or recipient).")
        return
    def _send():
        try:
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
                logger.info("[AccountRecovery] Email sent to %s — status %s", to, resp.status)
        except Exception as exc:
            logger.warning("[AccountRecovery] Email send failed: %s", exc)
    threading.Thread(target=_send, daemon=True).start()


def _reset_email_html(name: str, reset_link: str) -> str:
    return f"""
<div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
  <div style="background:#0d1117;border-radius:12px;padding:32px;color:#e6edf3">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px">
      <svg width="22" height="22" viewBox="0 0 32 32" fill="none"><polygon points="16,3 20.5,11.5 30,13 23,20 24.7,29.5 16,25 7.3,29.5 9,20 2,13 11.5,11.5" fill="#bc8cff" stroke="#6b3fa0" stroke-width="0.5"/></svg>
      <span style="font-size:1.1rem;font-weight:800;color:#e6edf3">Nexora <span style="color:#bc8cff">AI</span></span>
    </div>
    <h2 style="color:#e6edf3;margin:0 0 8px;font-size:1.3rem">Reset Your Password</h2>
    <p style="color:#8b949e;margin:0 0 24px;line-height:1.6">
      Hi {name or 'there'},<br><br>
      We received a request to reset the password for your Nexora account.
      Click the button below to choose a new password.
    </p>
    <a href="{reset_link}" style="display:inline-block;background:#bc8cff;color:#0d1117;font-weight:700;font-size:0.95rem;padding:12px 28px;border-radius:8px;text-decoration:none;margin-bottom:24px">
      Reset Password
    </a>
    <div style="background:#161b22;border-radius:8px;padding:14px;margin-bottom:20px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Or copy this link:</div>
      <div style="font-size:11px;color:#58a6ff;word-break:break-all">{reset_link}</div>
    </div>
    <p style="color:#8b949e;font-size:13px;margin:0 0 8px">
      ⏰ This link expires in <strong style="color:#d29922">{RESET_TOKEN_MINUTES} minutes</strong>.
    </p>
    <p style="color:#8b949e;font-size:13px;margin:0 0 20px">
      If you didn't request a password reset, you can safely ignore this email — your account is secure.
    </p>
    <hr style="border:none;border-top:1px solid #30363d;margin:20px 0">
    <p style="color:#30363d;font-size:11px;margin:0">
      {APP_NAME} AI Platform · This is an automated message, please do not reply.
    </p>
  </div>
</div>
"""


def _verify_email_html(name: str, verify_link: str) -> str:
    return f"""
<div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:24px">
  <div style="background:#0d1117;border-radius:12px;padding:32px;color:#e6edf3">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:24px">
      <svg width="22" height="22" viewBox="0 0 32 32" fill="none"><polygon points="16,3 20.5,11.5 30,13 23,20 24.7,29.5 16,25 7.3,29.5 9,20 2,13 11.5,11.5" fill="#bc8cff" stroke="#6b3fa0" stroke-width="0.5"/></svg>
      <span style="font-size:1.1rem;font-weight:800;color:#e6edf3">Nexora <span style="color:#bc8cff">AI</span></span>
    </div>
    <h2 style="color:#e6edf3;margin:0 0 8px;font-size:1.3rem">Verify Your Email</h2>
    <p style="color:#8b949e;margin:0 0 24px;line-height:1.6">
      Hi {name or 'there'},<br><br>
      Thanks for signing up for Nexora AI Platform! Please verify your email address
      to unlock all features and secure your account.
    </p>
    <a href="{verify_link}" style="display:inline-block;background:#3fb950;color:#0d1117;font-weight:700;font-size:0.95rem;padding:12px 28px;border-radius:8px;text-decoration:none;margin-bottom:24px">
      Verify Email Address
    </a>
    <div style="background:#161b22;border-radius:8px;padding:14px;margin-bottom:20px">
      <div style="font-size:11px;color:#8b949e;margin-bottom:4px">Or copy this link:</div>
      <div style="font-size:11px;color:#58a6ff;word-break:break-all">{verify_link}</div>
    </div>
    <p style="color:#8b949e;font-size:13px;margin:0 0 8px">
      ⏰ This link expires in <strong style="color:#d29922">{VERIFY_TOKEN_HOURS} hours</strong>.
    </p>
    <p style="color:#8b949e;font-size:13px;margin:0 0 20px">
      If you didn't create a Nexora account, you can safely ignore this email.
    </p>
    <hr style="border:none;border-top:1px solid #30363d;margin:20px 0">
    <p style="color:#30363d;font-size:11px;margin:0">
      {APP_NAME} AI Platform · This is an automated message, please do not reply.
    </p>
  </div>
</div>
"""


# ─── Forgot password ──────────────────────────────────────────────────────────

def request_password_reset(email: str, base_url: str) -> bool:
    """
    Initiate a password reset for the given email.
    Always returns True to prevent email enumeration.
    Sends reset email only if email is found.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return True

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, email FROM users WHERE LOWER(email) = ?", (email,))
    user = c.fetchone()

    if not user:
        conn.close()
        logger.debug("[AccountRecovery] Forgot-password: email not found (silent).")
        return True

    user_id, name, user_email = user

    # Invalidate all previous unused tokens for this user
    c.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))

    # Generate new token
    token     = _gen_token()
    tok_hash  = _hash_token(token)
    rid       = str(uuid.uuid4())
    expires   = (datetime.datetime.utcnow() + datetime.timedelta(minutes=RESET_TOKEN_MINUTES)).isoformat()

    c.execute(
        "INSERT INTO password_resets (id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
        (rid, user_id, tok_hash, expires),
    )
    conn.commit()
    conn.close()

    reset_link = f"{base_url}/reset-password?token={token}"
    _send_email_async(
        user_email,
        f"Reset your {APP_NAME} password",
        _reset_email_html(name or user_email, reset_link),
    )
    logger.info("[AccountRecovery] Password reset requested for user_id=%s", user_id)
    return True


def verify_reset_token(token: str) -> tuple[bool, str, int | None]:
    """
    Validate a reset token.
    Returns (valid, error_message, user_id).
    """
    if not token:
        return False, "Token is required.", None

    tok_hash = _hash_token(token)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, expires_at, used FROM password_resets WHERE token_hash = ?",
        (tok_hash,),
    )
    row = c.fetchone()
    conn.close()

    if not row:
        return False, "Invalid or expired reset link.", None

    rid, user_id, expires_at_str, used = row
    if used:
        return False, "This reset link has already been used.", None

    try:
        expires_at = datetime.datetime.fromisoformat(expires_at_str)
    except Exception:
        return False, "Token data is corrupt.", None

    if datetime.datetime.utcnow() > expires_at:
        return False, "This reset link has expired. Please request a new one.", None

    return True, "", user_id


def do_password_reset(token: str, new_password: str) -> tuple[bool, str]:
    """
    Reset the password using the provided token.
    Invalidates the token and all active sessions on success.
    """
    if not new_password or len(new_password) < 8:
        return False, "Password must be at least 8 characters."
    if len(new_password) > 128:
        return False, "Password is too long."

    valid, err, user_id = verify_reset_token(token)
    if not valid:
        return False, err

    import bcrypt
    tok_hash = _hash_token(token)
    hashed   = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Mark token as used
    c.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?", (tok_hash,))
    # Invalidate all other reset tokens for this user
    c.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))
    # Update password
    c.execute(
        "UPDATE users SET password = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (hashed, user_id),
    )
    # Revoke all sessions (all devices) — security requirement
    c.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    conn.commit()
    conn.close()
    logger.info("[AccountRecovery] Password reset complete for user_id=%s — all sessions revoked.", user_id)
    return True, "Password has been reset. Please sign in with your new password."


# ─── Email verification ───────────────────────────────────────────────────────

def send_verification_email(user_id: int, email: str, name: str, base_url: str) -> bool:
    """Send an email verification link. Invalidates any previous pending tokens."""
    email = (email or "").strip()
    if not email or "@" not in email:
        return False

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check if already verified
    c.execute("SELECT email_verified FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0]:
        conn.close()
        logger.debug("[AccountRecovery] User %s already verified.", user_id)
        return True

    # Invalidate previous unverified tokens
    c.execute(
        "UPDATE email_verifications SET verified = 1 WHERE user_id = ? AND verified = 0",
        (user_id,),
    )

    token    = _gen_token()
    tok_hash = _hash_token(token)
    vid      = str(uuid.uuid4())
    expires  = (datetime.datetime.utcnow() + datetime.timedelta(hours=VERIFY_TOKEN_HOURS)).isoformat()

    c.execute(
        "INSERT INTO email_verifications (id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
        (vid, user_id, tok_hash, expires),
    )
    conn.commit()
    conn.close()

    verify_link = f"{base_url}/api/auth/verify-email?token={token}"
    _send_email_async(
        email,
        f"Verify your {APP_NAME} email address",
        _verify_email_html(name or email, verify_link),
    )
    logger.info("[AccountRecovery] Verification email sent for user_id=%s", user_id)
    return True


def do_verify_email(token: str) -> tuple[bool, str]:
    """
    Mark email as verified via token.
    Returns (success, message).
    """
    if not token:
        return False, "Verification token is required."

    tok_hash = _hash_token(token)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, expires_at, verified FROM email_verifications WHERE token_hash = ?",
        (tok_hash,),
    )
    row = c.fetchone()

    if not row:
        conn.close()
        return False, "Invalid or expired verification link."

    vid, user_id, expires_at_str, verified = row

    if verified:
        conn.close()
        return False, "This verification link has already been used."

    try:
        expires_at = datetime.datetime.fromisoformat(expires_at_str)
    except Exception:
        conn.close()
        return False, "Verification data is corrupt."

    if datetime.datetime.utcnow() > expires_at:
        conn.close()
        return False, "This verification link has expired. Please request a new one."

    # Mark token used + user verified
    c.execute("UPDATE email_verifications SET verified = 1 WHERE id = ?", (vid,))
    c.execute(
        "UPDATE users SET email_verified = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
    logger.info("[AccountRecovery] Email verified for user_id=%s", user_id)
    return True, "Your email has been verified successfully!"


def get_verification_status(user_id: int) -> bool:
    """Return True if the user's email is verified."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT email_verified FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])
