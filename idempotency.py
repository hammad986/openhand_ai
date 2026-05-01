"""
idempotency.py — Transaction Safety + Idempotency Engine
=========================================================
Prevents duplicate payments, duplicate AI executions, duplicate DB writes,
and webhook replay issues.

Usage:
    from idempotency import idempotent, check_idempotency, store_idempotency
    from idempotency import billing_dedup, daily_token_check, daily_token_consume

    @app.route("/api/queue-task", methods=["POST"])
    @idempotent(ttl_hours=24)
    def api_queue_task(): ...
"""
import sqlite3
import hashlib
import json
import uuid
import time
import datetime
import logging
import threading
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

DB_PATH    = "saas_platform.db"
_db_lock   = threading.Lock()

# ─── DB init ─────────────────────────────────────────────────────────────────

def _init_idempotency_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id                TEXT PRIMARY KEY,
            user_id           TEXT NOT NULL DEFAULT 'anon',
            idempotency_key   TEXT NOT NULL,
            endpoint          TEXT NOT NULL,
            request_hash      TEXT NOT NULL,
            response_snapshot TEXT NOT NULL,
            status_code       INTEGER NOT NULL DEFAULT 200,
            created_at        TEXT NOT NULL,
            expires_at        TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_idem_key_endpoint
        ON request_logs(idempotency_key, endpoint)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_idem_created
        ON request_logs(created_at)
    """)

    # Daily token tracking table
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_token_usage (
            user_id    TEXT NOT NULL,
            date_utc   TEXT NOT NULL,
            tokens     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, date_utc)
        )
    """)

    # Billing dedup table (prevents double-activation)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payment_dedup (
            payment_id   TEXT PRIMARY KEY,
            order_id     TEXT NOT NULL,
            user_id      TEXT NOT NULL DEFAULT 'default',
            plan         TEXT NOT NULL,
            activated_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


try:
    _init_idempotency_db()
except Exception as e:
    logger.error("[Idempotency] DB init failed: %s", e)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _hash_request(endpoint: str, body: dict, user_id: str) -> str:
    """Deterministic hash of endpoint + sorted body + user_id."""
    payload = json.dumps({"e": endpoint, "b": body, "u": user_id}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _get_user_id() -> str:
    """Extract user_id from Flask g, falling back to 'anon'."""
    return str(getattr(g, "user_id", "anon"))


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def _expires_iso(hours: int) -> str:
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=hours)).isoformat()


# ─── Core idempotency check/store ────────────────────────────────────────────

def check_idempotency(key: str, endpoint: str):
    """
    Check if `key` was already processed for `endpoint`.
    Returns (True, cached_response_dict) if a stored result exists and is still valid.
    Returns (False, None) if this is a new request.
    """
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT response_snapshot, status_code FROM request_logs "
        "WHERE idempotency_key = ? AND endpoint = ? AND expires_at > ?",
        (key, endpoint, _now_iso()),
    )
    row = c.fetchone()
    conn.close()
    if row:
        try:
            return True, (json.loads(row["response_snapshot"]), row["status_code"])
        except Exception:
            return False, None
    return False, None


def store_idempotency(key: str, endpoint: str, user_id: str,
                      request_hash: str, response_body: dict,
                      status_code: int, ttl_hours: int = 24):
    """Persist the response for this idempotency key."""
    try:
        conn = _db()
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO request_logs "
            "(id, user_id, idempotency_key, endpoint, request_hash, "
            " response_snapshot, status_code, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                user_id,
                key,
                endpoint,
                request_hash,
                json.dumps(response_body, default=str),
                status_code,
                _now_iso(),
                _expires_iso(ttl_hours),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Idempotency] store failed: %s", e)


def purge_expired():
    """Remove expired idempotency entries. Call periodically."""
    try:
        conn = _db()
        conn.execute("DELETE FROM request_logs WHERE expires_at < ?", (_now_iso(),))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Idempotency] purge failed: %s", e)


# ─── Decorator ───────────────────────────────────────────────────────────────

def idempotent(ttl_hours: int = 24):
    """
    Flask route decorator that adds idempotency.

    Reads Idempotency-Key header (or falls back to request hash).
    If the key was already processed, returns the cached response immediately.
    Otherwise executes the view and caches the response.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            endpoint  = request.endpoint or request.path
            user_id   = _get_user_id()

            # Prefer explicit header; fall back to deterministic hash of body
            idem_key = request.headers.get("Idempotency-Key", "").strip()
            try:
                body = request.get_json(silent=True) or {}
            except Exception:
                body = {}

            if not idem_key:
                idem_key = _hash_request(endpoint, body, user_id)

            req_hash = _hash_request(endpoint, body, user_id)

            # Check cache
            found, cached = check_idempotency(idem_key, endpoint)
            if found:
                cached_body, cached_code = cached
                logger.debug("[Idempotency] Cache hit key=%s endpoint=%s", idem_key[:16], endpoint)
                resp = jsonify(cached_body)
                resp.headers["X-Idempotency-Replayed"] = "true"
                resp.status_code = cached_code
                return resp

            # Execute original view
            result = f(*args, **kwargs)

            # Normalize response
            if isinstance(result, tuple):
                resp_obj, status_code = result[0], result[1] if len(result) > 1 else 200
            else:
                resp_obj, status_code = result, 200

            # Only cache 2xx responses
            if 200 <= int(status_code) < 300:
                try:
                    if hasattr(resp_obj, "get_json"):
                        resp_data = resp_obj.get_json() or {}
                    elif hasattr(resp_obj, "json"):
                        resp_data = resp_obj.json or {}
                    else:
                        resp_data = {}
                    store_idempotency(idem_key, endpoint, user_id,
                                      req_hash, resp_data, int(status_code), ttl_hours)
                except Exception as e:
                    logger.warning("[Idempotency] failed to cache response: %s", e)

            return result
        return wrapper
    return decorator


# ─── Billing dedup ────────────────────────────────────────────────────────────

def billing_dedup_check(payment_id: str) -> bool:
    """Returns True if payment_id was already processed (duplicate)."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT 1 FROM payment_dedup WHERE payment_id = ?", (payment_id,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def billing_dedup_store(payment_id: str, order_id: str, user_id: str, plan: str):
    """Mark payment_id as processed to prevent double-activation."""
    try:
        conn = _db()
        conn.execute(
            "INSERT OR IGNORE INTO payment_dedup "
            "(payment_id, order_id, user_id, plan, activated_at) VALUES (?, ?, ?, ?, ?)",
            (payment_id, order_id, str(user_id), plan, _now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Idempotency] billing_dedup_store failed: %s", e)


# ─── Daily token cap ─────────────────────────────────────────────────────────

DAILY_TOKEN_CAP = {
    "free":  int(__import__("os").environ.get("TOKEN_CAP_FREE",  "50000")),
    "pro":   int(__import__("os").environ.get("TOKEN_CAP_PRO",   "500000")),
    "elite": int(__import__("os").environ.get("TOKEN_CAP_ELITE", "2000000")),
}


def _today_utc() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def daily_token_check(user_id: str, plan: str = "free") -> tuple:
    """
    Returns (allowed: bool, used: int, cap: int, remaining: int).
    """
    cap   = DAILY_TOKEN_CAP.get(plan.lower(), DAILY_TOKEN_CAP["free"])
    today = _today_utc()

    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT tokens FROM daily_token_usage WHERE user_id = ? AND date_utc = ?",
        (str(user_id), today),
    )
    row = c.fetchone()
    conn.close()

    used = row["tokens"] if row else 0
    remaining = max(0, cap - used)
    return (used < cap), used, cap, remaining


def daily_token_consume(user_id: str, tokens: int):
    """Increment the daily token counter for a user."""
    today = _today_utc()
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO daily_token_usage (user_id, date_utc, tokens) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, date_utc) DO UPDATE SET tokens = tokens + ?",
            (str(user_id), today, tokens, tokens),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Idempotency] daily_token_consume failed: %s", e)


def daily_token_stats(user_id: str, plan: str = "free") -> dict:
    """Return daily token stats for a user."""
    allowed, used, cap, remaining = daily_token_check(user_id, plan)
    return {
        "used":      used,
        "cap":       cap,
        "remaining": remaining,
        "allowed":   allowed,
        "plan":      plan,
        "date_utc":  _today_utc(),
        "pct_used":  round(used / cap * 100, 1) if cap else 0,
    }


# ─── Retry control ────────────────────────────────────────────────────────────

MAX_RETRIES      = int(__import__("os").environ.get("MAX_TASK_RETRIES",  "3"))
BASE_BACKOFF_SEC = float(__import__("os").environ.get("RETRY_BASE_BACKOFF", "2.0"))
MAX_BACKOFF_SEC  = float(__import__("os").environ.get("RETRY_MAX_BACKOFF",  "30.0"))


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 2, 4, 8 … capped at MAX_BACKOFF_SEC."""
    return min(BASE_BACKOFF_SEC ** attempt, MAX_BACKOFF_SEC)


def retry_allowed(current_retries: int) -> bool:
    """Return True if another retry attempt is permitted."""
    return current_retries < MAX_RETRIES


# ─── Provider failure log ─────────────────────────────────────────────────────

def log_provider_failure(provider: str, error: str, user_id: str = "anon"):
    """Log a provider failure event to the DB."""
    try:
        conn = _db()
        c = conn.cursor()
        # Create table if not exists (lazy init)
        c.execute("""
            CREATE TABLE IF NOT EXISTS provider_failures (
                id          TEXT PRIMARY KEY,
                provider    TEXT NOT NULL,
                error       TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT 'anon',
                created_at  TEXT NOT NULL
            )
        """)
        c.execute(
            "INSERT INTO provider_failures (id, provider, error, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, provider, error[:500], str(user_id), _now_iso()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Idempotency] log_provider_failure failed: %s", e)


# ─── Background cleanup ───────────────────────────────────────────────────────

def _start_cleanup_thread():
    def _cleanup_loop():
        while True:
            time.sleep(3600)  # Every hour
            try:
                purge_expired()
                logger.debug("[Idempotency] Purged expired entries")
            except Exception as e:
                logger.warning("[Idempotency] cleanup error: %s", e)

    t = threading.Thread(target=_cleanup_loop, daemon=True)
    t.start()


_start_cleanup_thread()

logger.info("[Idempotency] Engine initialized — dedup, token cap, retry control active.")
