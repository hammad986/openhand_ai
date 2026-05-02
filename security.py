"""
security.py — Phase 19: Security + Production Hardening
========================================================
Centralises:
  • In-memory per-IP rate limiting (sliding window)
  • Input sanitisation / validation helpers
  • Safe path validation (path traversal prevention)
  • Production configuration helpers
"""

import os
import re
import time
import threading
import logging
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """
    Sliding-window in-memory rate limiter, keyed by client IP.

    Usage:
        rl = RateLimiter(max_calls=60, window_seconds=60)
        ok, wait = rl.check("1.2.3.4")
        if not ok:
            return "Too many requests", 429
    """

    def __init__(self, max_calls: int, window_seconds: int):
        self.max_calls = max_calls
        self.window    = window_seconds
        self._buckets: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """
        Returns (allowed, retry_after_seconds).
        """
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max_calls:
                wait = dq[0] - cutoff
                return False, round(wait, 1)
            dq.append(now)
            return True, 0.0

    def reset(self, key: str):
        with self._lock:
            self._buckets.pop(key, None)

    def purge_old(self):
        """Drop buckets for IPs that have been idle for > 2 windows. Call periodically."""
        now = time.monotonic()
        cutoff = now - self.window * 2
        with self._lock:
            to_delete = [k for k, dq in self._buckets.items() if not dq or dq[-1] < cutoff]
            for k in to_delete:
                del self._buckets[k]


# Default limiters — tunable via env vars
_general_limiter   = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_GENERAL",   "120")),
    window_seconds=60,
)
_task_limiter      = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_TASKS",     "20")),
    window_seconds=60,
)
_auth_limiter      = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_AUTH",      "10")),
    window_seconds=60,
)
_scheduler_limiter = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_SCHEDULER", "30")),
    window_seconds=60,
)
_forgot_pw_limiter = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_FORGOT_PW", "5")),
    window_seconds=3600,  # 5 requests per hour per IP
)


def get_client_ip(request) -> str:
    """Extract real client IP, respecting X-Forwarded-For behind a proxy."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def check_rate_limit(limiter: RateLimiter, request) -> tuple[bool, float]:
    ip = get_client_ip(request)
    return limiter.check(ip)


# ─────────────────────────────────────────────────────────────────────────────
# Input Sanitisation
# ─────────────────────────────────────────────────────────────────────────────

# Maximum allowed lengths
MAX_PROMPT_LEN    = int(os.getenv("MAX_PROMPT_LEN",    "8000"))
MAX_TASK_NAME_LEN = int(os.getenv("MAX_TASK_NAME_LEN", "200"))
MAX_FILE_PATH_LEN = int(os.getenv("MAX_FILE_PATH_LEN", "500"))

# Characters that must never appear in file paths submitted by users
_DANGEROUS_PATH_CHARS = re.compile(r"[\x00\r\n]")

# Null bytes and control chars that should be stripped from text inputs
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitise_text(text: str, max_len: int | None = None) -> str:
    """Strip null/control bytes and truncate. Returns clean string."""
    if not isinstance(text, str):
        text = str(text)
    text = _CONTROL_CHARS.sub("", text)
    if max_len and len(text) > max_len:
        text = text[:max_len]
    return text


def sanitise_prompt(prompt: str) -> tuple[str, str | None]:
    """
    Sanitise a user-supplied prompt.
    Returns (cleaned_prompt, error_or_None).
    """
    if not isinstance(prompt, str):
        return "", "Prompt must be a string"
    prompt = sanitise_text(prompt, max_len=MAX_PROMPT_LEN)
    if not prompt.strip():
        return "", "Prompt cannot be empty"
    return prompt, None


def sanitise_task_name(name: str) -> str:
    return sanitise_text(name, max_len=MAX_TASK_NAME_LEN).strip() or "Unnamed Task"


def validate_file_path(path: str) -> tuple[bool, str]:
    """
    Lightweight path safety check on the raw string (before resolving).
    Full OS-level resolution happens in _safe_session_path.
    """
    if not isinstance(path, str):
        return False, "Path must be a string"
    if len(path) > MAX_FILE_PATH_LEN:
        return False, "Path too long"
    if _DANGEROUS_PATH_CHARS.search(path):
        return False, "Path contains illegal characters"
    if "\x00" in path:
        return False, "Path contains null byte"
    # Block obvious traversal sequences
    norm = path.replace("\\", "/")
    if "/../" in norm or norm.startswith("../") or norm.endswith("/.."):
        return False, "Path traversal detected"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Resource / concurrency limits
# ─────────────────────────────────────────────────────────────────────────────

MAX_CONCURRENT_SESSIONS   = int(os.getenv("MAX_CONCURRENT_SESSIONS",   "5"))
MAX_SCHEDULER_CONCURRENT  = int(os.getenv("MAX_SCHEDULER_CONCURRENT",  "3"))
MAX_TOKENS_PER_REQUEST    = int(os.getenv("MAX_TOKENS_PER_REQUEST",    "32000"))

# Emergency kill switch — set env var KILL_SWITCH=1 to refuse all task queuing
KILL_SWITCH = os.getenv("KILL_SWITCH", "0").strip() == "1"


def is_kill_switch_active() -> bool:
    """Re-reads env at call time so it can be toggled without restart."""
    return os.getenv("KILL_SWITCH", "0").strip() == "1"


# ─────────────────────────────────────────────────────────────────────────────
# Production config helpers
# ─────────────────────────────────────────────────────────────────────────────

def require_secret(env_var: str, default: str | None, context: str) -> str:
    """
    Return the env var value.  Log a warning if the weak default is in use.
    """
    val = os.getenv(env_var)
    if val:
        return val
    if default is not None:
        logger.warning(
            "[SECURITY] %s is using the default value. "
            "Set %s in your environment for production.", context, env_var
        )
        return default
    raise RuntimeError(f"[SECURITY] Required env var {env_var} is not set.")


def get_app_secret_key() -> bytes:
    """
    Return a stable secret key from the environment.
    Falls back to a random value in development (sessions don't survive restart).
    """
    raw = os.getenv("SECRET_KEY")
    if raw:
        return raw.encode()
    import secrets
    logger.warning(
        "[SECURITY] SECRET_KEY env var not set — using random key. "
        "Sessions will be invalidated on every restart. Set SECRET_KEY for production."
    )
    return secrets.token_bytes(32)


def is_production() -> bool:
    return os.getenv("FLASK_ENV", "development").lower() == "production"


# ─────────────────────────────────────────────────────────────────────────────
# CORS helper
# ─────────────────────────────────────────────────────────────────────────────

ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]


def apply_cors_headers(response, request):
    """
    Apply CORS headers to a response.
    In development allow all; in production restrict to ALLOWED_ORIGINS.
    """
    origin = request.headers.get("Origin", "")
    if not is_production():
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization, X-Requested-With"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    )
    return response
