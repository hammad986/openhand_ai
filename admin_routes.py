"""
admin_routes.py — Admin Control Panel API Routes
=================================================
Blueprint: /admin/*
Roles: super_admin > admin > support
"""

import sqlite3
import json
import datetime
import logging
import os
import psutil

from functools import wraps
from flask import Blueprint, request, jsonify, render_template, g
import jwt as pyjwt

import admin_config as cfg

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

DB_PATH = "saas_platform.db"
BILLING_DB = "billing.db"
JWT_SECRET = os.environ.get("JWT_SECRET", "nexora_saas_secret_key_change_in_production")

ROLE_HIERARCHY = {"super_admin": 3, "admin": 2, "support": 1, "user": 0}


# ── Auth decorators ───────────────────────────────────────────────────────────

def _decode_token():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        data = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if data.get("type") != "access":
            return None
        return data
    except Exception:
        return None


def require_role(min_role: str = "admin"):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            data = _decode_token()
            if not data:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            user_role = data.get("role", "user")
            if ROLE_HIERARCHY.get(user_role, 0) < ROLE_HIERARCHY.get(min_role, 99):
                return jsonify({"ok": False, "error": "Forbidden: insufficient role"}), 403
            g.admin_user_id = data.get("user_id")
            g.admin_email = data.get("email", "")
            g.admin_role = user_role
            return f(*args, **kwargs)
        return wrapper
    return decorator


def _admin_identity():
    return g.get("admin_email") or f"user_{g.get('admin_user_id', '?')}"


# ── PART 1: Admin UI route ────────────────────────────────────────────────────

@admin_bp.route("/")
def admin_dashboard():
    return render_template("admin.html")


# ── PART 2: System config CRUD ───────────────────────────────────────────────

@admin_bp.route("/api/config", methods=["GET"])
@require_role("support")
def get_all_config():
    return jsonify({"ok": True, "configs": cfg.get_all_configs()})


@admin_bp.route("/api/config/<key>", methods=["GET"])
@require_role("support")
def get_config(key):
    val = cfg.get(key)
    if val is None:
        return jsonify({"ok": False, "error": "Key not found"}), 404
    return jsonify({"ok": True, "key": key, "value": val})


@admin_bp.route("/api/config/<key>", methods=["POST"])
@require_role("admin")
def set_config(key):
    body = request.get_json(force=True) or {}
    value = body.get("value")
    if value is None:
        return jsonify({"ok": False, "error": "value is required"}), 400
    result = cfg.set_config(key, value, changed_by=_admin_identity())
    return jsonify(result), (200 if result["ok"] else 400)


@admin_bp.route("/api/config/<key>/history", methods=["GET"])
@require_role("support")
def config_history(key):
    limit = min(int(request.args.get("limit", 20)), 100)
    return jsonify({"ok": True, "history": cfg.get_history(key, limit)})


@admin_bp.route("/api/config/history/all", methods=["GET"])
@require_role("support")
def all_config_history():
    limit = min(int(request.args.get("limit", 50)), 200)
    return jsonify({"ok": True, "history": cfg.get_all_history(limit)})


@admin_bp.route("/api/config/<key>/rollback", methods=["POST"])
@require_role("admin")
def rollback_config(key):
    body = request.get_json(force=True) or {}
    history_id = body.get("history_id")
    if history_id is None:
        return jsonify({"ok": False, "error": "history_id is required"}), 400
    result = cfg.rollback(key, int(history_id), changed_by=_admin_identity())
    return jsonify(result), (200 if result["ok"] else 400)


# ── PART 3: Instant controls ─────────────────────────────────────────────────

@admin_bp.route("/api/features", methods=["GET"])
@require_role("support")
def get_features():
    return jsonify({"ok": True, "features": cfg.get("features")})


@admin_bp.route("/api/features", methods=["POST"])
@require_role("admin")
def set_features():
    body = request.get_json(force=True) or {}
    current = cfg.get("features") or {}
    current.update({k: bool(v) for k, v in body.items() if k in current})
    result = cfg.set_config("features", current, changed_by=_admin_identity())
    return jsonify(result)


@admin_bp.route("/api/pricing", methods=["GET"])
@require_role("support")
def get_pricing():
    return jsonify({"ok": True, "pricing": cfg.get("pricing")})


@admin_bp.route("/api/pricing", methods=["POST"])
@require_role("admin")
def set_pricing():
    body = request.get_json(force=True) or {}
    current = cfg.get("pricing") or {}
    for k, v in body.items():
        if k in current:
            current[k] = int(v)
    result = cfg.set_config("pricing", current, changed_by=_admin_identity())
    return jsonify(result)


@admin_bp.route("/api/token-limits", methods=["GET"])
@require_role("support")
def get_token_limits():
    return jsonify({"ok": True, "token_limits": cfg.get("token_limits")})


@admin_bp.route("/api/token-limits", methods=["POST"])
@require_role("admin")
def set_token_limits():
    body = request.get_json(force=True) or {}
    current = cfg.get("token_limits") or {}
    for k, v in body.items():
        if k in current:
            current[k] = int(v)
    result = cfg.set_config("token_limits", current, changed_by=_admin_identity())
    return jsonify(result)


@admin_bp.route("/api/rate-limits", methods=["GET"])
@require_role("support")
def get_rate_limits():
    return jsonify({"ok": True, "rate_limits": cfg.get("rate_limits")})


@admin_bp.route("/api/rate-limits", methods=["POST"])
@require_role("admin")
def set_rate_limits():
    body = request.get_json(force=True) or {}
    current = cfg.get("rate_limits") or {}
    for k, v in body.items():
        if k in current:
            current[k] = int(v)
    result = cfg.set_config("rate_limits", current, changed_by=_admin_identity())
    return jsonify(result)


# ── PART 4: Coupons ──────────────────────────────────────────────────────────

@admin_bp.route("/api/coupons", methods=["GET"])
@require_role("support")
def get_coupons():
    return jsonify({"ok": True, "coupons": cfg.get("coupons")})


@admin_bp.route("/api/coupons", methods=["POST"])
@require_role("admin")
def add_coupon():
    body = request.get_json(force=True) or {}
    code = str(body.get("code", "")).strip().upper()
    discount = body.get("discount_pct")
    max_uses = int(body.get("max_uses", 0))
    if not code or discount is None:
        return jsonify({"ok": False, "error": "code and discount_pct required"}), 400
    current = cfg.get("coupons") or {}
    current[code] = {
        "discount_pct": float(discount),
        "max_uses": max_uses,
        "used": 0,
        "active": True,
    }
    result = cfg.set_config("coupons", current, changed_by=_admin_identity())
    return jsonify(result)


@admin_bp.route("/api/coupons/<code>", methods=["DELETE"])
@require_role("admin")
def delete_coupon(code):
    current = cfg.get("coupons") or {}
    if code.upper() not in current:
        return jsonify({"ok": False, "error": "Coupon not found"}), 404
    del current[code.upper()]
    result = cfg.set_config("coupons", current, changed_by=_admin_identity())
    return jsonify(result)


@admin_bp.route("/api/coupons/<code>/toggle", methods=["POST"])
@require_role("admin")
def toggle_coupon(code):
    current = cfg.get("coupons") or {}
    key = code.upper()
    if key not in current:
        return jsonify({"ok": False, "error": "Coupon not found"}), 404
    current[key]["active"] = not current[key].get("active", True)
    result = cfg.set_config("coupons", current, changed_by=_admin_identity())
    return jsonify(result)


# ── PART 5: Provider flags ────────────────────────────────────────────────────

@admin_bp.route("/api/providers/flags", methods=["GET"])
@require_role("support")
def get_provider_flags():
    return jsonify({"ok": True, "provider_flags": cfg.get("provider_flags")})


@admin_bp.route("/api/providers/flags", methods=["POST"])
@require_role("admin")
def set_provider_flags():
    body = request.get_json(force=True) or {}
    current = cfg.get("provider_flags") or {}
    for k, v in body.items():
        current[k] = bool(v)
    result = cfg.set_config("provider_flags", current, changed_by=_admin_identity())
    return jsonify(result)


# ── PART 6: Model routing (restart-required) ──────────────────────────────────

@admin_bp.route("/api/model-routing", methods=["GET"])
@require_role("support")
def get_model_routing():
    return jsonify({"ok": True, "model_routing": cfg.get("model_routing"), "restart_required": True})


@admin_bp.route("/api/model-routing", methods=["POST"])
@require_role("admin")
def set_model_routing():
    body = request.get_json(force=True) or {}
    current = cfg.get("model_routing") or {}
    # Deep merge incoming changes
    for plan, roles in body.items():
        if plan not in current:
            current[plan] = {}
        for role, role_cfg in roles.items():
            current[plan][role] = role_cfg
    result = cfg.set_config("model_routing", current, changed_by=_admin_identity())
    if result["ok"]:
        result["restart_required"] = True
        result["message"] = "Model routing saved. Restart server to apply."
    return jsonify(result)


# ── PART 7: Concurrency (restart-required) ───────────────────────────────────

@admin_bp.route("/api/concurrency", methods=["GET"])
@require_role("support")
def get_concurrency():
    return jsonify({"ok": True, "concurrency": cfg.get("concurrency"), "restart_required": True})


@admin_bp.route("/api/concurrency", methods=["POST"])
@require_role("admin")
def set_concurrency():
    body = request.get_json(force=True) or {}
    current = cfg.get("concurrency") or {}
    for k, v in body.items():
        if k in current:
            current[k] = int(v)
    result = cfg.set_config("concurrency", current, changed_by=_admin_identity())
    if result["ok"]:
        result["restart_required"] = True
    return jsonify(result)


# ── PART 8: Dynamic provider management ──────────────────────────────────────

@admin_bp.route("/api/dynamic-providers", methods=["GET"])
@require_role("support")
def list_dynamic_providers():
    return jsonify({"ok": True, "providers": cfg.list_providers()})


@admin_bp.route("/api/dynamic-providers", methods=["POST"])
@require_role("admin")
def add_dynamic_provider():
    body = request.get_json(force=True) or {}
    pid = body.get("id", "").strip().lower().replace(" ", "_")
    name = body.get("name", "").strip()
    base_url = body.get("base_url", "").strip()
    if not pid or not name or not base_url:
        return jsonify({"ok": False, "error": "id, name, base_url required"}), 400
    result = cfg.add_provider(
        provider_id=pid, name=name, base_url=base_url,
        auth_type=body.get("auth_type", "bearer"),
        api_key=body.get("api_key", ""),
        priority=int(body.get("priority", 50)),
        roles=body.get("roles", ["lite", "pro", "elite"]),
        created_by=_admin_identity(),
    )
    return jsonify(result), (200 if result["ok"] else 400)


@admin_bp.route("/api/dynamic-providers/<pid>", methods=["PATCH"])
@require_role("admin")
def update_dynamic_provider(pid):
    body = request.get_json(force=True) or {}
    result = cfg.update_provider(pid, **body)
    return jsonify(result), (200 if result["ok"] else 404)


@admin_bp.route("/api/dynamic-providers/<pid>", methods=["DELETE"])
@require_role("super_admin")
def delete_dynamic_provider(pid):
    result = cfg.delete_provider(pid)
    return jsonify(result), (200 if result["ok"] else 404)


@admin_bp.route("/api/dynamic-providers/<pid>/toggle", methods=["POST"])
@require_role("admin")
def toggle_dynamic_provider(pid):
    providers = cfg.list_providers()
    target = next((p for p in providers if p["id"] == pid), None)
    if not target:
        return jsonify({"ok": False, "error": "Provider not found"}), 404
    result = cfg.update_provider(pid, enabled=0 if target["enabled"] else 1)
    return jsonify(result)


# ── PART 9: User management ───────────────────────────────────────────────────

@admin_bp.route("/api/users", methods=["GET"])
@require_role("support")
def list_users():
    page = max(1, int(request.args.get("page", 1)))
    limit = min(int(request.args.get("limit", 50)), 200)
    search = request.args.get("search", "").strip()
    offset = (page - 1) * limit

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if search:
        like = f"%{search}%"
        c.execute("""
            SELECT id, username, email, name, provider, role, is_banned,
                   total_tasks, total_tokens, created_at
            FROM users WHERE username LIKE ? OR email LIKE ? OR name LIKE ?
            ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (like, like, like, limit, offset))
    else:
        c.execute("""
            SELECT id, username, email, name, provider, role, is_banned,
                   total_tasks, total_tokens, created_at
            FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset))
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    conn.close()

    users = [
        {
            "id": r[0], "username": r[1], "email": r[2], "name": r[3],
            "provider": r[4], "role": r[5] or "user", "is_banned": bool(r[6]),
            "total_tasks": r[7], "total_tokens": r[8], "created_at": r[9],
        }
        for r in rows
    ]
    return jsonify({"ok": True, "users": users, "total": total, "page": page, "limit": limit})


@admin_bp.route("/api/users/<int:uid>", methods=["GET"])
@require_role("support")
def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id, username, email, name, provider, role, is_banned,
               total_tasks, total_tokens, created_at
        FROM users WHERE id = ?
    """, (uid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "user": {
        "id": row[0], "username": row[1], "email": row[2], "name": row[3],
        "provider": row[4], "role": row[5] or "user", "is_banned": bool(row[6]),
        "total_tasks": row[7], "total_tokens": row[8], "created_at": row[9],
    }})


@admin_bp.route("/api/users/<int:uid>/ban", methods=["POST"])
@require_role("admin")
def ban_user(uid):
    _require_not_self(uid)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 1 WHERE id = ?", (uid,))
    # Revoke all sessions
    c.execute("DELETE FROM auth_sessions WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()
    logger.info("[Admin] User %d banned by %s", uid, _admin_identity())
    return jsonify({"ok": True, "is_banned": True})


@admin_bp.route("/api/users/<int:uid>/unban", methods=["POST"])
@require_role("admin")
def unban_user(uid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned = 0 WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    logger.info("[Admin] User %d unbanned by %s", uid, _admin_identity())
    return jsonify({"ok": True, "is_banned": False})


@admin_bp.route("/api/users/<int:uid>/role", methods=["POST"])
@require_role("super_admin")
def set_user_role(uid):
    _require_not_self(uid)
    body = request.get_json(force=True) or {}
    role = body.get("role", "user")
    if role not in ROLE_HIERARCHY:
        return jsonify({"ok": False, "error": f"Invalid role: {role}"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    conn.commit()
    conn.close()
    logger.info("[Admin] User %d role set to %s by %s", uid, role, _admin_identity())
    return jsonify({"ok": True, "role": role})


@admin_bp.route("/api/users/<int:uid>/adjust-usage", methods=["POST"])
@require_role("admin")
def adjust_usage(uid):
    body = request.get_json(force=True) or {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if "total_tokens" in body:
        c.execute("UPDATE users SET total_tokens = ? WHERE id = ?", (int(body["total_tokens"]), uid))
    if "total_tasks" in body:
        c.execute("UPDATE users SET total_tasks = ? WHERE id = ?", (int(body["total_tasks"]), uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@admin_bp.route("/api/users/<int:uid>/set-plan", methods=["POST"])
@require_role("admin")
def set_user_plan(uid):
    body = request.get_json(force=True) or {}
    plan = body.get("plan", "").lower()
    if plan not in ("lite", "pro", "elite"):
        return jsonify({"ok": False, "error": "plan must be lite/pro/elite"}), 400
    now = datetime.datetime.utcnow()
    expiry = (now + datetime.timedelta(days=365)).isoformat()
    try:
        conn = sqlite3.connect(BILLING_DB)
        c = conn.cursor()
        # Check if subscription exists for this user
        c.execute("SELECT id FROM subscriptions WHERE user_id = ?", (str(uid),))
        existing = c.fetchone()
        if existing:
            c.execute(
                "UPDATE subscriptions SET plan=?, status='active', expiry_date=? WHERE user_id=?",
                (plan, expiry, str(uid))
            )
        else:
            c.execute("""
                INSERT INTO subscriptions (id, user_id, plan, status, billing_cycle, start_date, expiry_date, created_at)
                VALUES (?, ?, ?, 'active', 'manual', ?, ?, ?)
            """, (f"manual_{uid}_{int(now.timestamp())}", str(uid), plan, now.isoformat(), expiry, now.isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    logger.info("[Admin] User %d plan set to %s by %s", uid, plan, _admin_identity())
    return jsonify({"ok": True, "plan": plan})


def _require_not_self(uid):
    if g.get("admin_user_id") == uid:
        from flask import abort
        abort(400)


# ── PART 10: Monitoring dashboard ─────────────────────────────────────────────

@admin_bp.route("/api/metrics/system", methods=["GET"])
@require_role("support")
def system_metrics():
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return jsonify({
        "ok": True,
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_mb": round(mem.used / 1024 / 1024, 1),
        "ram_total_mb": round(mem.total / 1024 / 1024, 1),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 2),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 2),
    })


@admin_bp.route("/api/metrics/users", methods=["GET"])
@require_role("support")
def user_metrics():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1")
    banned = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE created_at >= date('now', '-7 days')")
    new_this_week = c.fetchone()[0]
    c.execute("SELECT SUM(total_tokens) FROM users")
    total_tokens = c.fetchone()[0] or 0
    c.execute("SELECT SUM(total_tasks) FROM users")
    total_tasks = c.fetchone()[0] or 0
    conn.close()
    return jsonify({
        "ok": True,
        "total_users": total_users,
        "banned": banned,
        "new_this_week": new_this_week,
        "total_tokens_consumed": total_tokens,
        "total_tasks_run": total_tasks,
    })


@admin_bp.route("/api/metrics/workflows", methods=["GET"])
@require_role("support")
def workflow_metrics():
    hours = int(request.args.get("hours", 24))
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM workflows WHERE timestamp >= ?", (cutoff,))
    total = c.fetchone()[0]
    c.execute("SELECT status, COUNT(*) FROM workflows WHERE timestamp >= ? GROUP BY status", (cutoff,))
    by_status = {r[0]: r[1] for r in c.fetchall()}
    c.execute("""
        SELECT user_id, COUNT(*) as cnt FROM workflows
        WHERE timestamp >= ? GROUP BY user_id ORDER BY cnt DESC LIMIT 10
    """, (cutoff,))
    top_users = [{"user_id": r[0], "count": r[1]} for r in c.fetchall()]
    conn.close()
    return jsonify({
        "ok": True, "hours": hours,
        "total_workflows": total,
        "by_status": by_status,
        "top_users": top_users,
    })


@admin_bp.route("/api/metrics/billing", methods=["GET"])
@require_role("support")
def billing_metrics():
    try:
        conn = sqlite3.connect(BILLING_DB)
        c = conn.cursor()
        c.execute("SELECT plan, COUNT(*) FROM subscriptions WHERE status='active' GROUP BY plan")
        by_plan = {r[0]: r[1] for r in c.fetchall()}
        c.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active'")
        active = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM invoices")
        total_invoices = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM invoices")
        total_revenue = c.fetchone()[0] or 0
        conn.close()
        return jsonify({
            "ok": True,
            "active_subscriptions": active,
            "by_plan": by_plan,
            "total_invoices": total_invoices,
            "total_revenue_paise": total_revenue,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── PART 11: Kill switch (super_admin) ───────────────────────────────────────

@admin_bp.route("/api/kill-switch", methods=["GET"])
@require_role("support")
def kill_switch_status():
    active = os.environ.get("KILL_SWITCH", "0") == "1"
    return jsonify({"ok": True, "active": active})


@admin_bp.route("/api/kill-switch", methods=["POST"])
@require_role("super_admin")
def toggle_kill_switch():
    body = request.get_json(force=True) or {}
    action = body.get("action", "")
    if action == "enable":
        os.environ["KILL_SWITCH"] = "1"
        logger.warning("[Admin] Kill switch ENABLED by %s", _admin_identity())
        return jsonify({"ok": True, "active": True})
    elif action == "disable":
        os.environ["KILL_SWITCH"] = "0"
        logger.info("[Admin] Kill switch DISABLED by %s", _admin_identity())
        return jsonify({"ok": True, "active": False})
    return jsonify({"ok": False, "error": "action must be 'enable' or 'disable'"}), 400


# ── PART 12: Self-info ────────────────────────────────────────────────────────

@admin_bp.route("/api/me", methods=["GET"])
@require_role("support")
def admin_me():
    return jsonify({
        "ok": True,
        "user_id": g.admin_user_id,
        "email": g.admin_email,
        "role": g.admin_role,
    })
