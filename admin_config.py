"""
admin_config.py — Centralized Configuration Engine
====================================================
- DB-backed config with in-memory cache
- Full versioning + rollback
- Thread-safe reads/writes
- Safe deployment: changes apply to new tasks only
"""

import sqlite3
import json
import threading
import datetime
import logging
import os

logger = logging.getLogger(__name__)

DB_PATH = "saas_platform.db"
_cache: dict = {}
_cache_lock = threading.RLock()

# ── Default config values ─────────────────────────────────────────────────────

DEFAULTS = {
    # Pricing (INR paise)
    "pricing": {
        "pro_monthly":   2000,
        "pro_yearly":    20000,
        "elite_monthly": 5000,
        "elite_yearly":  50000,
    },
    # Token limits per plan per day
    "token_limits": {
        "lite":  50000,
        "pro":   500000,
        "elite": 5000000,
    },
    # Feature toggles
    "features": {
        "signup_enabled":       True,
        "billing_enabled":      True,
        "support_enabled":      True,
        "oauth_google_enabled": True,
        "oauth_github_enabled": True,
        "memory_enabled":       True,
        "terminal_enabled":     True,
        "scheduler_enabled":    True,
    },
    # Rate limits (requests per minute)
    "rate_limits": {
        "login_per_minute":  10,
        "api_per_minute":    60,
        "agent_per_minute":  5,
    },
    # Coupons: { code: { discount_pct, max_uses, used, active } }
    "coupons": {},
    # Provider enable/disable flags
    "provider_flags": {
        "openai":      True,
        "anthropic":   True,
        "gemini":      True,
        "groq":        True,
        "openrouter":  True,
        "deepseek":    True,
        "mistral":     True,
        "together":    True,
        "nvidia":      True,
        "xai":         True,
        "fireworks":   True,
        "cohere":      True,
        "huggingface": True,
        "replicate":   True,
        "local":       True,
    },
    # Model routing per plan (restart-required)
    "model_routing": {
        "lite": {
            "planning": {"provider": "groq",     "model": "llama-3.3-70b-versatile", "context_limit": 8192},
            "coding":   {"provider": "deepseek", "model": "deepseek-chat",            "context_limit": 8192},
            "debug":    {"provider": "groq",     "model": "llama-3.3-70b-versatile", "context_limit": 4096},
        },
        "pro": {
            "planning": {"provider": "gemini",      "model": "gemini-1.5-pro",                     "context_limit": 32768},
            "coding":   {"provider": "openrouter",  "model": "qwen/qwen2.5-coder-32b-instruct",    "context_limit": 32768},
            "debug":    {"provider": "gemini",      "model": "gemini-1.5-pro",                     "context_limit": 16384},
        },
        "elite": {
            "planning": {"provider": "deepseek", "model": "deepseek-reasoner", "context_limit": 65536},
            "coding":   {"provider": "deepseek", "model": "deepseek-reasoner", "context_limit": 65536},
            "debug":    {"provider": "deepseek", "model": "deepseek-reasoner", "context_limit": 32768},
        },
    },
    # Concurrency limits (restart-required)
    "concurrency": {
        "max_agent_loops":    40,
        "planner_max_steps":  10,
        "terminal_timeout":   10,
        "max_queue_size":     50,
    },
    # Dynamic providers (admin-added)
    "dynamic_providers": {},
}


# ── DB init ───────────────────────────────────────────────────────────────────

def init_admin_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            version    INTEGER DEFAULT 1,
            updated_at TEXT NOT NULL,
            updated_by TEXT DEFAULT 'system'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS config_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT NOT NULL,
            old_value  TEXT,
            new_value  TEXT NOT NULL,
            version    INTEGER NOT NULL,
            changed_by TEXT NOT NULL,
            timestamp  TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS api_providers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            base_url    TEXT NOT NULL,
            auth_type   TEXT DEFAULT 'bearer',
            api_key     TEXT DEFAULT '',
            priority    INTEGER DEFAULT 50,
            enabled     INTEGER DEFAULT 1,
            roles       TEXT DEFAULT '["lite","pro","elite"]',
            created_at  TEXT NOT NULL,
            created_by  TEXT DEFAULT 'system'
        )
    """)

    conn.commit()
    conn.close()

    # Seed defaults that are not yet in DB
    for key, default_val in DEFAULTS.items():
        if not _db_exists(key):
            _db_set(key, default_val, version=1, changed_by="system", old_value=None)


def _db_exists(key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM system_config WHERE key = ?", (key,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def _db_set(key: str, value, version: int, changed_by: str, old_value):
    now = datetime.datetime.utcnow().isoformat()
    val_str = json.dumps(value)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO system_config (key, value, version, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            version=excluded.version,
            updated_at=excluded.updated_at,
            updated_by=excluded.updated_by
    """, (key, val_str, version, now, changed_by))
    if old_value is not None:
        c.execute("""
            INSERT INTO config_history (key, old_value, new_value, version, changed_by, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (key, json.dumps(old_value), val_str, version, changed_by, now))
    conn.commit()
    conn.close()


# ── Public API ────────────────────────────────────────────────────────────────

def get(key: str):
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM system_config WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if row:
        val = json.loads(row[0])
        with _cache_lock:
            _cache[key] = val
        return val
    return DEFAULTS.get(key)


def set_config(key: str, value, changed_by: str = "admin") -> dict:
    """
    Save a config value. Returns {ok, version}.
    Validates that key is known (or a dynamic provider key).
    """
    if key not in DEFAULTS and not key.startswith("dynamic_provider_"):
        return {"ok": False, "error": f"Unknown config key: {key}"}

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value, version FROM system_config WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()

    old_value = json.loads(row[0]) if row else None
    new_version = (row[1] + 1) if row else 1

    # Validate before save
    ok, err = _validate(key, value)
    if not ok:
        return {"ok": False, "error": err}

    _db_set(key, value, new_version, changed_by, old_value)

    with _cache_lock:
        _cache[key] = value

    logger.info("[AdminConfig] %s updated to v%d by %s", key, new_version, changed_by)
    return {"ok": True, "version": new_version}


def get_history(key: str, limit: int = 20) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, key, old_value, new_value, version, changed_by, timestamp
        FROM config_history WHERE key = ?
        ORDER BY timestamp DESC LIMIT ?
    """, (key, limit))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "key": r[1],
            "old_value": json.loads(r[2]) if r[2] else None,
            "new_value": json.loads(r[3]),
            "version": r[4], "changed_by": r[5], "timestamp": r[6],
        }
        for r in rows
    ]


def get_all_history(limit: int = 50) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, key, old_value, new_value, version, changed_by, timestamp
        FROM config_history
        ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "key": r[1],
            "old_value": json.loads(r[2]) if r[2] else None,
            "new_value": json.loads(r[3]),
            "version": r[4], "changed_by": r[5], "timestamp": r[6],
        }
        for r in rows
    ]


def rollback(key: str, history_id: int, changed_by: str = "admin") -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT old_value FROM config_history WHERE id = ? AND key = ?", (history_id, key))
    row = c.fetchone()
    conn.close()
    if not row or row[0] is None:
        return {"ok": False, "error": "No rollback target found"}
    target = json.loads(row[0])
    return set_config(key, target, changed_by=f"rollback_by_{changed_by}")


def get_all_configs() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT key, value, version, updated_at, updated_by FROM system_config")
    rows = c.fetchall()
    conn.close()
    result = {}
    for r in rows:
        result[r[0]] = {
            "value": json.loads(r[1]),
            "version": r[2],
            "updated_at": r[3],
            "updated_by": r[4],
        }
    return result


# ── Provider management ───────────────────────────────────────────────────────

def add_provider(provider_id: str, name: str, base_url: str,
                 auth_type: str = "bearer", api_key: str = "",
                 priority: int = 50, roles: list = None,
                 created_by: str = "admin") -> dict:
    if roles is None:
        roles = ["lite", "pro", "elite"]
    now = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO api_providers (id, name, base_url, auth_type, api_key, priority, enabled, roles, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        """, (provider_id, name, base_url, auth_type, api_key, priority, json.dumps(roles), now, created_by))
        conn.commit()
        conn.close()
        return {"ok": True}
    except sqlite3.IntegrityError:
        conn.close()
        return {"ok": False, "error": "Provider ID already exists"}


def update_provider(provider_id: str, **kwargs) -> dict:
    allowed = {"name", "base_url", "auth_type", "api_key", "priority", "enabled", "roles"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "Nothing to update"}
    if "roles" in updates and isinstance(updates["roles"], list):
        updates["roles"] = json.dumps(updates["roles"])
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [provider_id]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE api_providers SET {cols} WHERE id = ?", vals)
    affected = c.rowcount
    conn.commit()
    conn.close()
    if affected == 0:
        return {"ok": False, "error": "Provider not found"}
    return {"ok": True}


def delete_provider(provider_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM api_providers WHERE id = ?", (provider_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return {"ok": True} if affected else {"ok": False, "error": "Provider not found"}


def list_providers() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, base_url, auth_type, priority, enabled, roles, created_at, created_by FROM api_providers ORDER BY priority ASC")
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "name": r[1], "base_url": r[2], "auth_type": r[3],
            "priority": r[4], "enabled": bool(r[5]),
            "roles": json.loads(r[6]) if r[6] else [],
            "created_at": r[7], "created_by": r[8],
        }
        for r in rows
    ]


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(key: str, value) -> tuple:
    try:
        if key == "pricing":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, (int, float)) and v >= 0, f"Invalid price: {k}"
        elif key == "token_limits":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, int) and v > 0, f"Invalid token limit: {k}"
        elif key == "features":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, bool), f"Feature {k} must be bool"
        elif key == "rate_limits":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, int) and v > 0, f"Invalid rate limit: {k}"
        elif key == "coupons":
            assert isinstance(value, dict)
            for code, cfg in value.items():
                assert isinstance(cfg.get("discount_pct"), (int, float)), f"Bad coupon: {code}"
                assert 0 < cfg["discount_pct"] <= 100, f"Discount must be 1-100: {code}"
        elif key == "provider_flags":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, bool), f"Flag {k} must be bool"
        elif key == "model_routing":
            assert isinstance(value, dict)
            for plan in ["lite", "pro", "elite"]:
                assert plan in value
                for role in ["planning", "coding", "debug"]:
                    assert role in value[plan]
                    cfg = value[plan][role]
                    assert "provider" in cfg and "model" in cfg
        elif key == "concurrency":
            assert isinstance(value, dict)
            for k, v in value.items():
                assert isinstance(v, int) and v > 0
    except (AssertionError, KeyError, TypeError) as e:
        return False, str(e) or "Validation failed"
    return True, ""


# ── Init on import ────────────────────────────────────────────────────────────

try:
    init_admin_db()
    logger.info("[AdminConfig] Config engine initialized.")
except Exception as e:
    logger.error("[AdminConfig] Failed to init: %s", e)
