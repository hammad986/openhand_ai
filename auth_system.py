"""
auth_system.py — Enterprise Authentication System (Phase 50 v2)
================================================================
Features:
- Email/password signup + login (bcrypt)
- OAuth (Google, GitHub) — callback routes in web_app.py
- Short-lived JWT access tokens (15 min)
- Long-lived refresh tokens (30 days) with rotation
- Multi-device session tracking in DB
- Brute-force / rate-limit protection (in-memory sliding window)
- Backward-compatible with existing username-based users
"""
import sqlite3
import secrets
import uuid
import jwt
import datetime
import bcrypt
import os
import logging
import threading
from collections import defaultdict, deque
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

_DEFAULT_JWT_SECRET = "nexora_saas_secret_key_change_in_production"
SECRET_KEY = os.environ.get("JWT_SECRET", _DEFAULT_JWT_SECRET)

if SECRET_KEY == _DEFAULT_JWT_SECRET:
    logger.warning(
        "[SECURITY] JWT_SECRET is using the default value. "
        "Set JWT_SECRET in your environment before exposing this app publicly."
    )

DB_PATH = "saas_platform.db"
ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS = int(os.environ.get("REFRESH_TOKEN_DAYS", "30"))

_ALLOW_DEV_AUTH = os.environ.get("ALLOW_DEV_AUTH", "0").strip() == "1"


# ─── Brute-force protection ───────────────────────────────────────────────────

_bf_lock = threading.Lock()
_failed_attempts: dict = defaultdict(deque)
MAX_FAILED = int(os.environ.get("MAX_LOGIN_ATTEMPTS", "5"))
LOCKOUT_WINDOW = int(os.environ.get("LOGIN_LOCKOUT_SECS", "60"))


def _record_failed(key: str):
    now = datetime.datetime.utcnow().timestamp()
    with _bf_lock:
        dq = _failed_attempts[key]
        dq.append(now)
        cutoff = now - LOCKOUT_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()


def _is_locked_out(key: str) -> bool:
    now = datetime.datetime.utcnow().timestamp()
    cutoff = now - LOCKOUT_WINDOW
    with _bf_lock:
        dq = _failed_attempts[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        return len(dq) >= MAX_FAILED


def _clear_failed(key: str):
    with _bf_lock:
        _failed_attempts.pop(key, None)


# ─── DB init + migration ──────────────────────────────────────────────────────

def _add_col(cursor, table: str, col: str, col_def: str):
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
    except sqlite3.OperationalError:
        pass  # Column already exists


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  username TEXT UNIQUE,
                  password TEXT,
                  total_tasks INTEGER DEFAULT 0,
                  total_tokens INTEGER DEFAULT 0
                 )''')

    _add_col(c, 'users', 'email',      'TEXT')
    _add_col(c, 'users', 'name',       'TEXT')
    _add_col(c, 'users', 'provider',   "TEXT DEFAULT 'local'")
    _add_col(c, 'users', 'created_at', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
    _add_col(c, 'users', 'updated_at', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
    _add_col(c, 'users', 'role',       "TEXT DEFAULT 'user'")
    _add_col(c, 'users', 'is_banned',  'INTEGER DEFAULT 0')

    try:
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email "
            "ON users(email) WHERE email IS NOT NULL"
        )
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS auth_sessions (
                  id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  refresh_token TEXT UNIQUE NOT NULL,
                  device_info TEXT DEFAULT '',
                  ip_address TEXT DEFAULT '',
                  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                  expires_at TEXT NOT NULL,
                  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                 )''')

    c.execute('''CREATE TABLE IF NOT EXISTS workflows (
                  id TEXT PRIMARY KEY,
                  user_id INTEGER,
                  task TEXT,
                  score INTEGER,
                  status TEXT,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                 )''')

    conn.commit()
    conn.close()


init_db()


# ─── Token helpers ────────────────────────────────────────────────────────────

def _make_access_token(user_id: int, email: str = "", name: str = "", role: str = "user") -> str:
    payload = {
        "user_id": user_id,
        "email":   email or "",
        "name":    name or "",
        "role":    role or "user",
        "type":    "access",
        "iat":     datetime.datetime.utcnow(),
        "exp":     datetime.datetime.utcnow() + datetime.timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def _make_refresh_token() -> str:
    return secrets.token_hex(64)


def _get_user_info(cursor, user_id: int):
    cursor.execute("SELECT id, email, name, provider, role, is_banned FROM users WHERE id = ?", (user_id,))
    return cursor.fetchone()


# ─── Session management ───────────────────────────────────────────────────────

def create_session(user_id: int, device_info: str = "", ip_address: str = "") -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    user = _get_user_info(c, user_id)

    session_id = str(uuid.uuid4())
    refresh = _make_refresh_token()
    expires_at = (
        datetime.datetime.utcnow() + datetime.timedelta(days=REFRESH_TOKEN_DAYS)
    ).isoformat()

    c.execute(
        "INSERT INTO auth_sessions (id, user_id, refresh_token, device_info, ip_address, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, user_id, refresh, device_info[:255], ip_address[:64], expires_at),
    )
    conn.commit()
    conn.close()

    email = user[1] if user else ""
    name  = user[2] if user else ""
    role  = user[4] if user and len(user) > 4 else "user"
    access = _make_access_token(user_id, email or "", name or "", role or "user")
    return {
        "access_token":  access,
        "refresh_token": refresh,
        "session_id":    session_id,
        "expires_in":    ACCESS_TOKEN_MINUTES * 60,
    }


def refresh_access_token(refresh_token: str, ip_address: str = ""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, expires_at, device_info FROM auth_sessions WHERE refresh_token = ?",
        (refresh_token,),
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "Invalid refresh token"

    session_id, user_id, expires_at_str, device_info = row
    try:
        expires_at = datetime.datetime.fromisoformat(expires_at_str)
    except Exception:
        conn.close()
        return False, "Session corrupt"

    if datetime.datetime.utcnow() > expires_at:
        c.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()
        return False, "Refresh token expired"

    new_refresh = _make_refresh_token()
    new_expires = (
        datetime.datetime.utcnow() + datetime.timedelta(days=REFRESH_TOKEN_DAYS)
    ).isoformat()
    c.execute(
        "UPDATE auth_sessions SET refresh_token = ?, expires_at = ?, ip_address = ? WHERE id = ?",
        (new_refresh, new_expires, ip_address[:64], session_id),
    )

    user = _get_user_info(c, user_id)
    conn.commit()
    conn.close()

    email = user[1] if user else ""
    name  = user[2] if user else ""
    role  = user[4] if user and len(user) > 4 else "user"
    access = _make_access_token(user_id, email or "", name or "", role or "user")
    return True, {
        "access_token":  access,
        "refresh_token": new_refresh,
        "expires_in":    ACCESS_TOKEN_MINUTES * 60,
    }


def revoke_session(refresh_token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM auth_sessions WHERE refresh_token = ?", (refresh_token,))
    conn.commit()
    conn.close()


def revoke_session_by_id(session_id: str, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM auth_sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
    conn.commit()
    conn.close()


def revoke_all_sessions(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def list_sessions(user_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, device_info, ip_address, created_at, expires_at "
        "FROM auth_sessions WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    rows = c.fetchall()
    conn.close()
    return [
        {
            "id":          r[0],
            "device_info": r[1] or "",
            "ip_address":  r[2] or "",
            "created_at":  r[3],
            "expires_at":  r[4],
        }
        for r in rows
    ]


# ─── Signup ───────────────────────────────────────────────────────────────────

def auth_signup(identifier: str, password: str, name: str = "", email: str = None):
    """
    Unified signup. `identifier` may be an email or a legacy username.
    Returns (True, user_id) on success, (False, error_msg) on failure.
    """
    if not identifier or not password:
        return False, "Email and password required"
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(identifier) > 120:
        return False, "Identifier too long"

    is_email = "@" in identifier
    resolved_email    = email or (identifier if is_email else None)
    resolved_username = identifier if not is_email else identifier.split("@")[0]

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        if resolved_email:
            c.execute(
                "INSERT INTO users (username, email, name, password, provider) "
                "VALUES (?, ?, ?, ?, 'local')",
                (resolved_username, resolved_email, name or resolved_username, hashed),
            )
        else:
            c.execute(
                "INSERT INTO users (username, password, provider) VALUES (?, ?, 'local')",
                (resolved_username, hashed),
            )
        user_id = c.lastrowid
        conn.commit()
        return True, user_id
    except sqlite3.IntegrityError as e:
        err = str(e).lower()
        if "email" in err:
            return False, "Email already registered"
        return False, "Username already exists"
    finally:
        conn.close()


# ─── Login ────────────────────────────────────────────────────────────────────

def auth_login(identifier: str, password: str, ip: str = ""):
    """
    Login with email or username.
    Returns (True, user_id) on success, (False, error_msg) on failure.
    """
    if not identifier or not password:
        return False, "Email and password required"

    bf_key = f"login:{ip}:{identifier.lower()}"
    if _is_locked_out(bf_key):
        return False, "Too many failed attempts. Please wait 1 minute."

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if "@" in identifier:
        c.execute("SELECT id, password FROM users WHERE email = ?", (identifier,))
    else:
        c.execute("SELECT id, password FROM users WHERE username = ?", (identifier,))
    user = c.fetchone()
    conn.close()

    if user and user[1] and bcrypt.checkpw(password.encode("utf-8"), user[1].encode("utf-8")):
        _clear_failed(bf_key)
        return True, user[0]

    _record_failed(bf_key)
    return False, "Invalid credentials"


# ─── OAuth user ───────────────────────────────────────────────────────────────

def get_or_create_oauth_user(email: str, name: str, provider: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = c.fetchone()
    if row:
        user_id = row[0]
        c.execute(
            "UPDATE users SET name = ?, provider = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, provider, user_id),
        )
        conn.commit()
        conn.close()
        return user_id

    username = email.split("@")[0] + "_" + secrets.token_hex(4)
    c.execute(
        "INSERT INTO users (username, email, name, provider) VALUES (?, ?, ?, ?)",
        (username, email, name, provider),
    )
    user_id = c.lastrowid
    conn.commit()
    conn.close()
    return user_id


# ─── Auth decorator ───────────────────────────────────────────────────────────

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        if not token and _ALLOW_DEV_AUTH and request.args.get("dev") == "1":
            logger.debug("[AUTH] dev=1 bypass used for %s", request.path)
            g.user_id    = 1
            g.user_email = ""
            g.user_name  = ""
            return f(*args, **kwargs)

        if not token:
            return jsonify({"ok": False, "error": "Unauthorized: Token is missing"}), 401

        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            if data.get("type") != "access":
                return jsonify({"ok": False, "error": "Invalid token type"}), 401
            g.user_id    = data["user_id"]
            g.user_email = data.get("email", "")
            g.user_name  = data.get("name",  "")
            g.user_role  = data.get("role",  "user")
        except jwt.ExpiredSignatureError:
            return jsonify({"ok": False, "error": "Token has expired", "code": "TOKEN_EXPIRED"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"ok": False, "error": "Token is invalid"}), 401
        except Exception:
            return jsonify({"ok": False, "error": "Authentication failed"}), 401

        # Ban check — live DB lookup so bans take effect without token expiry
        try:
            _conn = sqlite3.connect(DB_PATH)
            _c = _conn.cursor()
            _c.execute("SELECT is_banned FROM users WHERE id = ?", (g.user_id,))
            _row = _c.fetchone()
            _conn.close()
            if _row and _row[0]:
                return jsonify({"ok": False, "error": "Account suspended", "code": "BANNED"}), 403
        except Exception:
            pass

        return f(*args, **kwargs)
    return decorated


# ─── Usage tracking + dashboard ───────────────────────────────────────────────

def track_usage(user_id, wid, task, score, status, tokens=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO workflows (id, user_id, task, score, status) VALUES (?, ?, ?, ?, ?)",
        (wid, user_id, task, score, status),
    )
    c.execute(
        "UPDATE users SET total_tasks = total_tasks + 1, total_tokens = total_tokens + ? WHERE id = ?",
        (tokens, user_id),
    )
    conn.commit()
    conn.close()


def get_dashboard(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT total_tasks, total_tokens, email, name, provider FROM users WHERE id = ?",
        (user_id,),
    )
    stats = c.fetchone()
    c.execute(
        "SELECT id, task, score, status, timestamp FROM workflows "
        "WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10",
        (user_id,),
    )
    recent = [
        {"id": r[0], "task": r[1], "score": r[2], "status": r[3], "timestamp": r[4]}
        for r in c.fetchall()
    ]
    conn.close()

    if not stats:
        return {"total_tasks": 0, "total_tokens": 0, "success_rate": 0, "recent_workflows": []}

    successes = sum(
        1 for w in recent
        if w["status"] in ("PASSED", "Success", "PARTIAL SUCCESS", "excellent", "good")
    )
    rate = round((successes / len(recent)) * 100, 1) if recent else 0
    return {
        "user_id":          user_id,
        "email":            stats[2] or "",
        "name":             stats[3] or "",
        "provider":         stats[4] or "local",
        "total_tasks":      stats[0],
        "total_tokens":     stats[1],
        "success_rate":     rate,
        "recent_workflows": recent,
    }


def get_user_workspace(user_id):
    workspace = f"./workspace/{user_id}"
    artifacts = f"./artifacts/{user_id}"
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)
    return os.path.abspath(workspace), os.path.abspath(artifacts)
