"""
auth_system.py — Phase 50: Multi-Tenant SaaS  (Phase 19: hardened)
====================================================================
Handles user authentication, JWTs, and database isolation.
"""
import sqlite3
import jwt
import datetime
import bcrypt
import os
import logging
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

_DEFAULT_JWT_SECRET = "openhand_saas_secret_key_123"
SECRET_KEY = os.environ.get("JWT_SECRET", _DEFAULT_JWT_SECRET)

# Warn loudly if the default secret is still in use
if SECRET_KEY == _DEFAULT_JWT_SECRET:
    logger.warning(
        "[SECURITY] JWT_SECRET is using the default value. "
        "Set JWT_SECRET in your environment before exposing this app publicly."
    )

DB_PATH = "saas_platform.db"

# The ?dev=1 backdoor is disabled in production.
# Set ALLOW_DEV_AUTH=1 in your environment to re-enable it (development only).
_ALLOW_DEV_AUTH = os.environ.get("ALLOW_DEV_AUTH", "0").strip() == "1"


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


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            parts = request.headers["Authorization"].split(" ")
            if len(parts) == 2 and parts[0] == "Bearer":
                token = parts[1]

        # Developer bypass — only active when explicitly enabled via env var
        if not token and _ALLOW_DEV_AUTH and request.args.get("dev") == "1":
            logger.debug("[AUTH] dev=1 bypass used for request to %s", request.path)
            g.user_id = 1
            return f(*args, **kwargs)

        if not token:
            return jsonify({"ok": False, "error": "Unauthorized: Token is missing"}), 401

        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            g.user_id = data["user_id"]
        except jwt.ExpiredSignatureError:
            return jsonify({"ok": False, "error": "Token has expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"ok": False, "error": "Token is invalid"}), 401
        except Exception:
            return jsonify({"ok": False, "error": "Authentication failed"}), 401

        return f(*args, **kwargs)
    return decorated


def auth_signup(username, password):
    if not username or not password:
        return False, "Username and password required"
    if len(username) > 80:
        return False, "Username too long"
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        conn.commit()
        return True, "User created"
    except sqlite3.IntegrityError:
        return False, "Username already exists"
    finally:
        conn.close()


def auth_login(username, password):
    if not username or not password:
        return False, "Username and password required"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, password FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()

    if user and bcrypt.checkpw(password.encode("utf-8"), user[1].encode("utf-8")):
        token = jwt.encode({
            "user_id": user[0],
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=24),
        }, SECRET_KEY, algorithm="HS256")
        return True, token
    return False, "Invalid credentials"


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
    c.execute("SELECT total_tasks, total_tokens FROM users WHERE id = ?", (user_id,))
    user_stats = c.fetchone()
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

    if not user_stats:
        return {"total_tasks": 0, "total_tokens": 0, "success_rate": 0, "recent_workflows": []}

    total_tasks = user_stats[0]
    successes = sum(
        1 for w in recent
        if w["status"] in ("PASSED", "Success", "PARTIAL SUCCESS", "excellent", "good")
    )
    rate = round((successes / len(recent)) * 100, 1) if recent else 0

    return {
        "user_id": user_id,
        "total_tasks": total_tasks,
        "total_tokens": user_stats[1],
        "success_rate": rate,
        "recent_workflows": recent,
    }


def get_user_workspace(user_id):
    workspace = f"./workspace/{user_id}"
    artifacts = f"./artifacts/{user_id}"
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(artifacts, exist_ok=True)
    return os.path.abspath(workspace), os.path.abspath(artifacts)
