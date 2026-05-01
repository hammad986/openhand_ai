"""web_app.py - Multi-session AI Agent SaaS Control Platform.

Wraps the CLI agent (main.py) without modifying agent.py / router.py / tools.py.

Phase 5.2 — Hybrid API system:
  - Managed mode (default): uses platform-provided env keys + built-in routing
  - BYOK mode: per-session providers + keys + fallback order, injected via env
"""

import os
import io
import re
import sys
import json
import time
import uuid
import base64
import hashlib
import signal
import sqlite3
import zipfile
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
from collections import deque
from flask import (Flask, render_template, request, jsonify,
                   send_from_directory, send_file, abort,
                   Response, stream_with_context)
from dotenv import load_dotenv

load_dotenv()

from mcp_context import MCPContext
mcp = MCPContext()

# ────────────────────────────────────────────────────────────────────────────
# Paths & app
# ────────────────────────────────────────────────────────────────────────────

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
WORKSPACE    = os.path.join(BASE_DIR, "workspace")
SESSIONS_DB  = os.path.join(BASE_DIR, "sessions.db")

os.makedirs(WORKSPACE, exist_ok=True)


# ── Phase 7.0 — per-session workspace helpers ────────────────────────────────
# Each session gets its own isolated folder under ./workspace/<sid>/. The agent
# subprocess is launched with $WORKSPACE_DIR pointed at this folder so tools.py
# (unchanged) writes everything there. Files are listed, served, and zipped via
# /api/files, /api/file, /api/download, and /preview/<sid>/.

# File extensions treated as text in the file viewer. Anything else is returned
# as base64 so the UI can decide whether to show it (image preview) or just
# offer a download.
_TEXT_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".less",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".md", ".markdown", ".txt", ".rst",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".sql", ".csv", ".tsv", ".log", ".gitignore",
    ".xml", ".svg", ".vue", ".svelte",
    ".dockerfile", ".makefile",
}

# Hard caps so a runaway agent can't crash the UI.
_MAX_TEXT_BYTES   = 2_000_000   # /api/file refuses larger files
_MAX_TREE_ENTRIES = 5_000       # /api/files truncates beyond this


def session_workspace(sid):
    """Return absolute path to this session's workspace dir, creating it."""
    p = os.path.join(WORKSPACE, sid)
    os.makedirs(p, exist_ok=True)
    return p


def _safe_session_path(sid, sub):
    """Resolve <ws>/<sub> and refuse anything that escapes the session ws."""
    ws = session_workspace(sid)
    real_ws = os.path.realpath(ws)
    target = os.path.realpath(os.path.join(ws, sub or ""))
    try:
        common = os.path.commonpath([real_ws, target])
    except ValueError:
        abort(403)
    if common != real_ws:
        abort(403)
    # Refuse symlinks anywhere along the path.
    walk = real_ws
    rel = os.path.relpath(target, real_ws)
    if rel != ".":
        for part in rel.split(os.sep):
            walk = os.path.join(walk, part)
            if os.path.islink(walk):
                abort(403)
    return target


# ── Phase 21.1 polish — derive a friendly "project name" slug from the
# task prompt the user originally typed.  Pure function, no I/O; safe to
# call on every /api/session/<sid> response.
import re as _re_pn

_PN_STOPWORDS = {
    "the","a","an","and","or","of","to","for","with","build","make","create",
    "system","app","application","please","using","use","that","this",
    "in","on","my","me","i","into","from","new","simple",
}

def _derive_project_name(task: str, max_words: int = 4, max_len: int = 40) -> str:
    """Slugify a task prompt into a short, kebab-case project name.

    Examples:
        "Build a Flask login system"      -> "flask-login"
        "make me a simple to-do app"      -> "todo"
        ""                                -> "untitled"
        "!!!"                             -> "untitled"
    """
    if not task:
        return "untitled"
    s = task.strip().lower()
    # Replace anything that isn't alphanumeric with a space, then split.
    tokens = [t for t in _re_pn.split(r"[^a-z0-9]+", s) if t]
    if not tokens:
        return "untitled"
    keep = [t for t in tokens if t not in _PN_STOPWORDS][:max_words]
    if not keep:
        keep = tokens[:max_words]
    name = "-".join(keep)
    if len(name) > max_len:
        name = name[:max_len].rstrip("-") or "untitled"
    return name or "untitled"


# ── Phase 21.1 polish — Browser host allowlist (foundation for Phase 21.3
# SSRF guard on Ollama base_url and any future agent-driven HTTP fetches).
# Stored as a flat JSON list at data/browser_allowlist.json with a sane
# default that mirrors typical local-dev setups.
_BROWSER_ALLOWLIST_PATH = os.path.join("data", "browser_allowlist.json")
_BROWSER_ALLOWLIST_DEFAULT = ["localhost", "127.0.0.1"]
_BROWSER_HOST_RE = _re_pn.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?))*$"
)
_BROWSER_LOCK = threading.Lock()


def _browser_allowlist_normalize(host: str) -> str:
    """Strip scheme/port/path and lowercase a user-supplied host string."""
    if not host:
        return ""
    h = host.strip().lower()
    # Strip a leading scheme (http:// https:// etc.) — we only store the host.
    if "://" in h:
        h = h.split("://", 1)[1]
    # Drop any path/query — keep only the host[:port], then drop the port.
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if h.startswith("["):  # bracketed IPv6 like [::1]
        end = h.find("]")
        h = h[1:end] if end > 0 else h.lstrip("[")
    elif ":" in h and h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h


def _browser_allowlist_valid(host: str) -> bool:
    """True if `host` looks like a hostname or IPv4/IPv6 address."""
    if not host or len(host) > 253:
        return False
    # IPv4 quick-check
    if _re_pn.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        parts = host.split(".")
        return all(0 <= int(p) <= 255 for p in parts)
    # IPv6 quick-check (loose; we just need to refuse garbage)
    if ":" in host and _re_pn.match(r"^[0-9a-f:]+$", host):
        return host.count(":") <= 7
    return bool(_BROWSER_HOST_RE.match(host))


def _browser_allowlist_load() -> list:
    try:
        with open(_BROWSER_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            seen, out = set(), []
            for h in data:
                hh = _browser_allowlist_normalize(str(h or ""))
                if hh and hh not in seen and _browser_allowlist_valid(hh):
                    out.append(hh)
                    seen.add(hh)
            return out
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return list(_BROWSER_ALLOWLIST_DEFAULT)


def _browser_allowlist_save(items: list) -> None:
    os.makedirs(os.path.dirname(_BROWSER_ALLOWLIST_PATH) or ".", exist_ok=True)
    tmp = _BROWSER_ALLOWLIST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, sort_keys=True)
    os.replace(tmp, _BROWSER_ALLOWLIST_PATH)


# ── Phase 21.2 — Editor review policy persistence ──────────────────────
# Lives at data/review_policy.json; mirrors the browser-allowlist pattern
# (atomic save, lock-guarded, defaults from Config).  Keeping it in a
# small JSON file (instead of the sqlite sessions DB) means the policy
# is server-wide rather than per-session, which matches the user's
# expectation of a single editor preference.
_REVIEW_POLICY_PATH = os.path.join("data", "review_policy.json")
_REVIEW_LOCK = threading.Lock()
# Hard caps on the hybrid thresholds — even if the user sets the slider
# to "999 lines", the editor should not silently auto-apply massive
# rewrites without human review.  These numbers intentionally cap above
# the project default (10 / 2) but well below "whole-file rewrite".
_REVIEW_MAX_LINES_CAP = 200
_REVIEW_MAX_HUNKS_CAP = 20
# Action allow-list for AGENT_DECIDES — must be a subset of the AI
# action names accepted by /api/code-action (`fix`, `optimize`,
# `refactor`, `explain`).  We re-enforce this in `_review_policy_normalize`.
_REVIEW_VALID_ACTIONS = {"fix", "optimize", "refactor", "explain"}


def _review_policy_defaults() -> dict:
    """Project default policy — pulled from Config so env-vars can shift
    the baseline without touching this file."""
    from config import Config as _C
    mode = (_C.REVIEW_MODE_DEFAULT or "REQUEST_REVIEW").strip().upper()
    if mode not in _C.REVIEW_MODES:
        mode = "REQUEST_REVIEW"
    return {
        "mode":      mode,
        "max_lines": max(1, min(_REVIEW_MAX_LINES_CAP, int(_C.REVIEW_HYBRID_MAX_LINES))),
        "max_hunks": max(1, min(_REVIEW_MAX_HUNKS_CAP, int(_C.REVIEW_HYBRID_MAX_HUNKS))),
        "actions":   sorted(set(_C.REVIEW_HYBRID_AUTO_ACTIONS) & _REVIEW_VALID_ACTIONS),
    }


def _review_policy_normalize(raw) -> dict:
    """Coerce a user-supplied policy dict back into a known-good shape.

    Bad / missing fields fall back to defaults rather than raising — the
    UI is the source of truth and we'd rather degrade gracefully than
    corrupt the file with a 4xx round-trip.
    """
    base = _review_policy_defaults()
    if not isinstance(raw, dict):
        return base
    from config import Config as _C
    mode = str(raw.get("mode") or "").strip().upper()
    if mode not in _C.REVIEW_MODES:
        mode = base["mode"]
    try:
        max_lines = int(raw.get("max_lines", base["max_lines"]))
    except (TypeError, ValueError):
        max_lines = base["max_lines"]
    try:
        max_hunks = int(raw.get("max_hunks", base["max_hunks"]))
    except (TypeError, ValueError):
        max_hunks = base["max_hunks"]
    max_lines = max(1, min(_REVIEW_MAX_LINES_CAP, max_lines))
    max_hunks = max(1, min(_REVIEW_MAX_HUNKS_CAP, max_hunks))
    actions_in = raw.get("actions")
    if isinstance(actions_in, list):
        actions = sorted({
            str(a).strip().lower() for a in actions_in
            if isinstance(a, str) and a.strip().lower() in _REVIEW_VALID_ACTIONS
        })
    else:
        actions = list(base["actions"])
    return {"mode": mode, "max_lines": max_lines,
            "max_hunks": max_hunks, "actions": actions}


def _review_policy_load() -> dict:
    """Load the persisted policy, or fall back to defaults on any
    file/JSON error.  Always returns a normalized dict."""
    try:
        with open(_REVIEW_POLICY_PATH, "r", encoding="utf-8") as fh:
            return _review_policy_normalize(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _review_policy_defaults()


def _review_policy_save(policy: dict) -> dict:
    """Atomically persist a policy and return the normalized version
    that hit disk (so callers can echo it back without a re-read)."""
    pol = _review_policy_normalize(policy)
    os.makedirs(os.path.dirname(_REVIEW_POLICY_PATH) or ".", exist_ok=True)
    tmp = _REVIEW_POLICY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(pol, fh, indent=2, sort_keys=True)
    os.replace(tmp, _REVIEW_POLICY_PATH)
    return pol


def _classify_diff(original: str, suggested: str) -> dict:
    """Return {lines_changed, hunks} describing the unified diff between
    `original` and `suggested`.  `lines_changed` counts +/- lines but
    NOT context or hunk headers.  Used by the hybrid AGENT_DECIDES
    branch to decide whether a suggestion is "small enough" to skip
    the diff modal.  Pure stdlib (difflib) so no extra deps."""
    import difflib
    a = (original or "").splitlines()
    b = (suggested or "").splitlines()
    diff = list(difflib.unified_diff(a, b, n=0, lineterm=""))
    lines_changed = 0
    hunks = 0
    for ln in diff:
        if ln.startswith("@@"):
            hunks += 1
        elif ln.startswith("+++") or ln.startswith("---"):
            continue
        elif ln.startswith("+") or ln.startswith("-"):
            lines_changed += 1
    return {"lines_changed": lines_changed, "hunks": hunks}


def _decide_auto_apply(action: str, diff_stats: dict, policy: dict) -> tuple:
    """Apply the review policy to one suggestion.

    Returns (auto_apply: bool, reason: str).  `reason` is a short
    machine-readable token (not user-facing prose) suitable for logging
    and surfacing to the UI for transparency ("why didn't this auto?").
    """
    mode = (policy or {}).get("mode") or "REQUEST_REVIEW"
    if mode == "ALWAYS_PROCEED":
        return True, "always_proceed"
    if mode == "REQUEST_REVIEW":
        return False, "request_review"
    # AGENT_DECIDES — apply the hybrid rule.
    allowed = set((policy or {}).get("actions") or ())
    if action not in allowed:
        return False, "action_not_allowed"
    max_lines = int((policy or {}).get("max_lines") or 0)
    max_hunks = int((policy or {}).get("max_hunks") or 0)
    lines = int((diff_stats or {}).get("lines_changed") or 0)
    hunks = int((diff_stats or {}).get("hunks") or 0)
    if lines > max_lines:
        return False, "too_many_lines"
    if hunks > max_hunks:
        return False, "too_many_hunks"
    return True, "hybrid_small_change"


def _walk_session_files(sid, max_entries=_MAX_TREE_ENTRIES):
    """Yield (rel_path, abs_path, size, mtime) for every file in the session ws.

    Skips dotfiles/dirs, __pycache__, node_modules, .git, .venv to keep the
    tree usable. **Symlinks (file or dir) are always skipped** so that an
    agent-created symlink can never expose host files outside the session
    workspace via `/api/files`, `/api/download`, or `/api/preview`.
    """
    ws = session_workspace(sid)
    skip_dirs = {"__pycache__", "node_modules", ".git", ".venv", ".cache",
                 ".pytest_cache", ".mypy_cache", "dist", "build", ".next"}
    count = 0
    # followlinks=False is the default but explicit for safety.
    for root, dirs, files in os.walk(ws, followlinks=False):
        # Filter dot/skip dirs AND drop any symlinked subdirs so we never
        # descend into them (defence in depth alongside followlinks=False).
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and d not in skip_dirs
            and not os.path.islink(os.path.join(root, d))
        )
        for f in sorted(files):
            if f.startswith("."):
                continue
            full = os.path.join(root, f)
            # Hard-skip symlinks: zipfile.write would otherwise follow them
            # and pull in arbitrary host files.
            if os.path.islink(full):
                continue
            try:
                size = os.path.getsize(full)
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            rel = os.path.relpath(full, ws).replace(os.sep, "/")
            yield rel, full, size, mtime
            count += 1
            if count >= max_entries:
                return


def _walk_session_dirs(sid, max_entries=_MAX_TREE_ENTRIES):
    """Yield rel_path for every directory in the session workspace.

    Phase 21.1 R1 fix #2 — empty folders created via /api/create-folder
    must remain visible in the Files tab tree (file-only walks would
    drop them). Same skip-rules / symlink protections as the file walk.
    The workspace root itself is NOT yielded.
    """
    ws = session_workspace(sid)
    skip_dirs = {"__pycache__", "node_modules", ".git", ".venv", ".cache",
                 ".pytest_cache", ".mypy_cache", "dist", "build", ".next"}
    count = 0
    for root, dirs, _files in os.walk(ws, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and d not in skip_dirs
            and not os.path.islink(os.path.join(root, d))
        )
        for d in dirs:
            rel = os.path.relpath(os.path.join(root, d), ws).replace(os.sep, "/")
            yield rel
            count += 1
            if count >= max_entries:
                return


def _looks_text(path, sample_bytes=2048):
    """Cheap text/binary sniff: read a slice and check for NUL bytes."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sample_bytes)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    # Heuristic: >30% non-printable → binary
    if not chunk:
        return True
    text_chars = bytes(range(32, 127)) + b"\n\r\t\f\b"
    non_text = sum(1 for b in chunk if b not in text_chars)
    return (non_text / len(chunk)) < 0.30

app = Flask(__name__)
app.secret_key = os.urandom(24)


# ────────────────────────────────────────────────────────────────────────────
# Provider registry — maps provider name to env-var bindings
# ────────────────────────────────────────────────────────────────────────────

PROVIDERS = {
    # ── Core ──────────────────────────────────────────────────────────────────
    "openai":      {
        "label": "OpenAI",          "key_env": "OPENAI_API_KEY",
        "category": "core",         "url": "https://api.openai.com/v1/chat/completions",
        "speed": "balanced",        "quality": "high",
        "caps": ["thinking", "coding", "debugging", "multimodal"],
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
        "plan_pref": ["pro", "elite"],
    },
    "gemini":      {
        "label": "Google Gemini",   "key_env": "GEMINI_API_KEY",
        "category": "core",         "url": "gemini-native",
        "speed": "fast",            "quality": "high",
        "caps": ["thinking", "coding", "debugging", "multimodal"],
        "models": ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash-exp"],
        "plan_pref": ["lite", "pro", "elite"],
    },
    "anthropic":   {
        "label": "Anthropic",       "key_env": "ANTHROPIC_API_KEY",
        "category": "core",         "url": "https://api.anthropic.com/v1/messages",
        "speed": "balanced",        "quality": "highest",
        "caps": ["thinking", "coding", "debugging", "multimodal"],
        "models": ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022", "claude-3-opus-20240229"],
        "plan_pref": ["elite"],
    },
    "groq":        {
        "label": "Groq",            "key_env": "GROQ_API_KEY",
        "category": "core",         "url": "https://api.groq.com/openai/v1/chat/completions",
        "speed": "fastest",         "quality": "good",
        "caps": ["coding", "debugging"],
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "plan_pref": ["lite"],
    },
    "openrouter":  {
        "label": "OpenRouter",      "key_env": "OPENROUTER_API_KEY",
        "category": "core",         "url": "https://openrouter.ai/api/v1/chat/completions",
        "speed": "balanced",        "quality": "high",
        "caps": ["thinking", "coding", "debugging", "multimodal"],
        "models": ["deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet", "openai/gpt-4o"],
        "plan_pref": ["pro", "elite"],
    },
    # ── High Value ─────────────────────────────────────────────────────────────
    "xai":         {
        "label": "xAI (Grok)",      "key_env": "XAI_API_KEY",
        "category": "high_value",   "url": "https://api.x.ai/v1/chat/completions",
        "speed": "balanced",        "quality": "high",
        "caps": ["thinking", "coding", "debugging"],
        "models": ["grok-beta", "grok-2", "grok-2-mini"],
        "plan_pref": ["elite"],
    },
    "bedrock":     {
        "label": "AWS Bedrock",     "key_env": "AWS_ACCESS_KEY_ID",
        "category": "high_value",   "url": "bedrock-native",
        "speed": "balanced",        "quality": "high",
        "caps": ["thinking", "coding", "debugging"],
        "models": ["anthropic.claude-3-haiku-20240307-v1:0", "anthropic.claude-3-sonnet-20240229-v1:0"],
        "plan_pref": ["elite"],
    },
    "azure":       {
        "label": "Azure OpenAI",    "key_env": "AZURE_OPENAI_KEY",
        "category": "high_value",   "url": "azure-native",
        "speed": "balanced",        "quality": "high",
        "caps": ["thinking", "coding", "debugging", "multimodal"],
        "models": ["gpt-4o", "gpt-4-turbo", "gpt-35-turbo"],
        "plan_pref": ["pro", "elite"],
    },
    "together":    {
        "label": "Together AI",     "key_env": "TOGETHER_API_KEY",
        "category": "high_value",   "url": "https://api.together.xyz/v1/chat/completions",
        "speed": "fast",            "quality": "good",
        "caps": ["coding", "debugging"],
        "models": ["Qwen/Qwen2.5-72B-Instruct-Turbo", "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"],
        "plan_pref": ["lite", "pro"],
    },
    "fireworks":   {
        "label": "Fireworks AI",    "key_env": "FIREWORKS_API_KEY",
        "category": "high_value",   "url": "https://api.fireworks.ai/inference/v1/chat/completions",
        "speed": "fast",            "quality": "good",
        "caps": ["coding", "debugging"],
        "models": ["accounts/fireworks/models/llama-v3p3-70b-instruct", "accounts/fireworks/models/qwen2p5-coder-32b-instruct"],
        "plan_pref": ["lite", "pro"],
    },
    # ── Open / Fallback ────────────────────────────────────────────────────────
    "deepseek":    {
        "label": "DeepSeek",        "key_env": "DEEPSEEK_API_KEY",
        "category": "open",         "url": "https://api.deepseek.com/v1/chat/completions",
        "speed": "fast",            "quality": "high",
        "caps": ["thinking", "coding", "debugging"],
        "models": ["deepseek-coder", "deepseek-chat", "deepseek-reasoner"],
        "plan_pref": ["lite", "pro"],
    },
    "mistral":     {
        "label": "Mistral",         "key_env": "MISTRAL_API_KEY",
        "category": "open",         "url": "https://api.mistral.ai/v1/chat/completions",
        "speed": "fast",            "quality": "good",
        "caps": ["coding", "debugging"],
        "models": ["mistral-small-latest", "mistral-medium-latest", "codestral-latest"],
        "plan_pref": ["lite"],
    },
    "cohere":      {
        "label": "Cohere",          "key_env": "COHERE_API_KEY",
        "category": "open",         "url": "https://api.cohere.ai/v2/chat",
        "speed": "fast",            "quality": "good",
        "caps": ["coding"],
        "models": ["command-r", "command-r-plus"],
        "plan_pref": ["lite"],
    },
    "huggingface": {
        "label": "HuggingFace",     "key_env": "HUGGINGFACE_API_KEY",
        "category": "open",         "url": "https://api-inference.huggingface.co/models",
        "speed": "slow",            "quality": "variable",
        "caps": ["coding"],
        "models": ["Qwen/Qwen2.5-Coder-32B-Instruct", "bigcode/starcoder2-15b"],
        "plan_pref": ["lite"],
    },
    # ── Multimodal ─────────────────────────────────────────────────────────────
    "replicate":   {
        "label": "Replicate",       "key_env": "REPLICATE_API_KEY",
        "category": "multimodal",   "url": "https://api.replicate.com/v1/predictions",
        "speed": "slow",            "quality": "high",
        "caps": ["multimodal"],
        "models": ["meta/llama-3-70b-instruct", "stability-ai/stable-diffusion-3"],
        "plan_pref": [],
    },
    "elevenlabs":  {
        "label": "ElevenLabs",      "key_env": "ELEVENLABS_API_KEY",
        "category": "multimodal",   "url": "https://api.elevenlabs.io/v1/text-to-speech",
        "speed": "fast",            "quality": "high",
        "caps": ["multimodal"],
        "models": ["eleven_flash_v2_5", "eleven_turbo_v2_5"],
        "plan_pref": [],
    },
    "deepgram":    {
        "label": "Deepgram",        "key_env": "DEEPGRAM_API_KEY",
        "category": "multimodal",   "url": "https://api.deepgram.com/v1/speak",
        "speed": "fast",            "quality": "high",
        "caps": ["multimodal"],
        "models": ["aura-asteria-en", "aura-luna-en"],
        "plan_pref": [],
    },
    # ── Local ──────────────────────────────────────────────────────────────────
    "nvidia":      {
        "label": "NVIDIA NIM",      "key_env": "NVIDIA_API_KEY",
        "category": "high_value",   "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "speed": "fast",            "quality": "good",
        "caps": ["coding", "debugging"],
        "models": ["meta/llama-3.3-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"],
        "plan_pref": ["lite", "pro"],
    },
    "local":       {
        "label": "Ollama (local)",  "key_env": None,
        "category": "open",         "url": "local",
        "speed": "variable",        "quality": "variable",
        "caps": ["coding", "debugging"],
        "models": ["codellama", "llama3", "deepseek-coder"],
        "plan_pref": ["lite"],
    },
}
PROVIDER_NAMES = list(PROVIDERS.keys())

# Phase 5 — Routing rules: plan mode → preferred provider category
P5_ROUTE_RULES = {
    "lite":  {"speed": "fastest",  "quality_min": "good",    "prefer_caps": ["coding"]},
    "pro":   {"speed": "balanced", "quality_min": "high",    "prefer_caps": ["thinking", "coding"]},
    "elite": {"speed": "any",      "quality_min": "highest", "prefer_caps": ["thinking", "debugging"]},
}

# Speed tier ordering (fastest = lowest index)
_P5_SPEED_RANK = {"fastest": 0, "fast": 1, "balanced": 2, "slow": 3, "variable": 4, "any": 5}
_P5_QUALITY_RANK = {"variable": 0, "good": 1, "high": 2, "highest": 3}


def p5_get_best_provider(plan_mode: str, available_keys: dict) -> str:
    """Return the best provider id for a plan mode given the user's available keys.
    Falls back through preference lists, skipping providers with no key configured.
    Returns 'auto' if nothing matches.
    """
    from config import Config
    prefs = Config.PLAN_PROVIDER_PREFERENCE.get(plan_mode, [])
    for pid in prefs:
        if pid not in PROVIDERS:
            continue
        meta = PROVIDERS[pid]
        key_env = meta.get("key_env")
        # Local needs no key
        if key_env is None:
            return pid
        # Check user-supplied key
        if available_keys.get(pid):
            return pid
        # Check platform env key
        if key_env and os.getenv(key_env):
            return pid
    return "auto"

# Default platform-managed limits (advisory; enforced in queue endpoint).
MANAGED_LIMITS = {
    "max_concurrent_tasks": 1,                                  # serial worker
    "max_pending_in_queue": int(os.getenv("MANAGED_MAX_QUEUE", "10")),
    "max_tasks_per_window":  int(os.getenv("MANAGED_RATE_LIMIT", "30")),
    "rate_window_seconds":   int(os.getenv("MANAGED_RATE_WINDOW", "3600")),
}

# ── Plan system capability gates (Phase 3) ───────────────────────────────────
PLAN_CAPABILITIES = {
    "lite": {
        "allow_planning":   False,
        "allow_reasoning":  False,
        "allow_debug":      False,
        "allow_self_correction": False,
        "max_steps":        5,
        "label":            "Lite",
        "description":      "Fast responses, no planning or deep reasoning",
        "color":            "#3fb950",
    },
    "pro": {
        "allow_planning":   True,
        "allow_reasoning":  True,
        "allow_debug":      False,
        "allow_self_correction": False,
        "max_steps":        15,
        "label":            "Pro",
        "description":      "Reasoning + planning, multi-step execution",
        "color":            "#388bfd",
    },
    "elite": {
        "allow_planning":   True,
        "allow_reasoning":  True,
        "allow_debug":      True,
        "allow_self_correction": True,
        "max_steps":        50,
        "label":            "Elite",
        "description":      "Full autonomy: planning, debugging, self-correction, tool chaining",
        "color":            "#bc8cff",
    },
}


def mask_key(value):
    """Return a safe display form of an API key. Never expose the full value."""
    if not value: return ""
    v = str(value)
    if len(v) <= 8: return "•" * len(v)
    return f"{v[:4]}…{v[-4:]}"


def default_managed_config():
    return {
        "mode": "managed",
        "providers": list(PROVIDERS.keys()),
        "api_keys": {},
        # Phase 5 — expanded fallback order (keeps existing first, adds new providers at end)
        "fallback_order": ["gemini", "openrouter", "groq", "nvidia", "together",
                           "openai", "anthropic", "deepseek", "mistral", "fireworks"],
        # Phase 8 — when True (default), tasks go through orchestrator.py
        # (THINK→PLAN→EXECUTE→VERIFY→REFLECT→ADAPT). When False, fall back
        # to the raw single-shot agent via main.py.
        "thinking_mode": True,
        # Phase 19 — Self-driven goals & proactive intelligence. The
        # engine analyses recent failures and (when ENABLED) creates
        # low-importance system_generated chains to address recurring
        # problems. Defaults are deliberately conservative: OFF, low
        # daily cap, slow cooldown, high confidence floor. Validated
        # in validate_config; surfaced safely via public_config.
        "auto_goals": {
            "enabled":          False,
            "max_per_day":      3,
            "min_confidence":   0.6,
            "cooldown_seconds": 1800,
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Persistence (SQLite)
# ────────────────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(SESSIONS_DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init_db():
    with _db_lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            id              TEXT PRIMARY KEY,
            task            TEXT NOT NULL,
            model           TEXT,
            status          TEXT NOT NULL,
            stage           TEXT,
            step            TEXT,
            current_model   TEXT,
            retry_count     INTEGER DEFAULT 0,
            error_category  TEXT,
            validation      TEXT,
            result          TEXT,
            success         INTEGER,
            exit_code       INTEGER,
            created_at      REAL NOT NULL,
            started_at      REAL,
            finished_at     REAL,
            files_json      TEXT,
            config_json     TEXT,
            mode            TEXT
        );
        CREATE TABLE IF NOT EXISTS logs(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            seq         INTEGER NOT NULL,
            ts          REAL NOT NULL,
            level       TEXT NOT NULL,
            text        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_logs_session_seq ON logs(session_id, seq);
        CREATE TABLE IF NOT EXISTS decisions(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          REAL NOT NULL,
            kind        TEXT NOT NULL,
            detail      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_dec_session ON decisions(session_id);
        CREATE TABLE IF NOT EXISTS settings(
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        # Best-effort backfill for older DBs created before these columns existed
        for col, ddl in (("config_json",   "ALTER TABLE sessions ADD COLUMN config_json TEXT"),
                         ("mode",          "ALTER TABLE sessions ADD COLUMN mode TEXT"),
                         ("usage_json",    "ALTER TABLE sessions ADD COLUMN usage_json TEXT"),
                         # Phase 17 — link a session to its parent chain task
                         # so run_session() can mark the chain task done when
                         # the subprocess terminates. Both nullable; existing
                         # ad-hoc tasks (no chain) leave them NULL.
                         ("chain_id",      "ALTER TABLE sessions ADD COLUMN chain_id INTEGER"),
                         ("chain_task_id", "ALTER TABLE sessions ADD COLUMN chain_task_id INTEGER")):
            try: c.execute(ddl)
            except sqlite3.OperationalError: pass


_init_db()


def get_setting(key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not r: return default
    try: return json.loads(r["value"])
    except Exception: return default


def set_setting(key, value):
    with _db_lock, _conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


def db_insert_session(s):
    with _db_lock, _conn() as c:
        c.execute("""INSERT INTO sessions
            (id,task,model,status,created_at,config_json,mode)
            VALUES (?,?,?,?,?,?,?)""",
            (s["id"], s["task"], s.get("model"), s["status"],
             s["created_at"], json.dumps(s.get("config") or {}),
             (s.get("config") or {}).get("mode", "managed")))


def db_update_session(sid, **fields):
    if not fields: return
    cols = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [sid]
    with _db_lock, _conn() as c:
        c.execute(f"UPDATE sessions SET {cols} WHERE id=?", vals)


def db_session(sid):
    with _conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None


def db_sessions(limit=200):
    with _conn() as c:
        rs = c.execute(
            "SELECT id,task,model,status,success,created_at,finished_at,mode "
            "FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rs]


def db_delete_session(sid):
    with _db_lock, _conn() as c:
        c.execute("DELETE FROM logs WHERE session_id=?", (sid,))
        c.execute("DELETE FROM decisions WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))


def db_insert_log(sid, seq, ts, level, text):
    with _db_lock, _conn() as c:
        c.execute("INSERT INTO logs(session_id,seq,ts,level,text) VALUES(?,?,?,?,?)",
                  (sid, seq, ts, level, text))


def db_logs(sid, since=0, limit=200):
    with _conn() as c:
        if since > 0:
            rs = c.execute(
                "SELECT seq,ts,level,text FROM logs WHERE session_id=? AND seq>? "
                "ORDER BY seq ASC LIMIT ?", (sid, since, limit)).fetchall()
        else:
            rs = c.execute(
                "SELECT * FROM (SELECT seq,ts,level,text FROM logs WHERE session_id=? "
                "ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC", (sid, limit)).fetchall()
        return [dict(r) for r in rs]

def db_logs_older(sid, before_seq, limit=200):
    with _conn() as c:
        rs = c.execute(
            "SELECT * FROM (SELECT seq,ts,level,text FROM logs WHERE session_id=? AND seq<? "
            "ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC", (sid, before_seq, limit)).fetchall()
        return [dict(r) for r in rs]


def db_insert_decision(sid, ts, kind, detail):
    with _db_lock, _conn() as c:
        c.execute("INSERT INTO decisions(session_id,ts,kind,detail) VALUES(?,?,?,?)",
                  (sid, ts, kind, detail))


def db_decisions(sid):
    with _conn() as c:
        rs = c.execute("SELECT ts,kind,detail FROM decisions WHERE session_id=? ORDER BY id ASC",
                       (sid,)).fetchall()
        return [dict(r) for r in rs]


# ────────────────────────────────────────────────────────────────────────────
# Config validation & sanitisation
# ────────────────────────────────────────────────────────────────────────────

def validate_config(cfg):
    """Validate a posted config. Returns (ok, error_message, normalised_cfg)."""
    if not isinstance(cfg, dict):
        return False, "Config must be an object", None

    mode = (cfg.get("mode") or "managed").lower()
    if mode not in ("managed", "byok"):
        return False, "mode must be 'managed' or 'byok'", None

    providers = cfg.get("providers") or []
    if not isinstance(providers, list):
        return False, "providers must be a list", None
    providers = [p for p in providers if p in PROVIDERS]

    fallback = cfg.get("fallback_order") or providers
    if not isinstance(fallback, list):
        return False, "fallback_order must be a list", None
    fallback = [p for p in fallback if p in PROVIDERS]

    api_keys = cfg.get("api_keys") or {}
    if not isinstance(api_keys, dict):
        return False, "api_keys must be an object", None
    api_keys = {k: str(v) for k, v in api_keys.items() if k in PROVIDERS and v}

    if mode == "byok":
        if not providers:
            return False, "BYOK mode requires at least one provider", None
        if not fallback:
            return False, "BYOK mode requires a fallback order", None

        # Phase 6.7 — Relaxed BYOK validation:
        # Only require AT LEAST ONE selected provider to have a usable key
        # (or be a no-key provider like local Ollama). Providers without a
        # key are kept in `providers` so the UI can mark them as "inactive",
        # but they are stripped from the *active* fallback so the router
        # never tries to route to them.
        active = []
        for p in providers:
            need_key = PROVIDERS[p]["key_env"]
            if not need_key or api_keys.get(p):
                active.append(p)
        if not active:
            return False, ("Add at least one API key to run in BYOK mode. "
                           "Selected providers without a key will be skipped."), None
        # Drop disabled / unkeyed entries from the fallback order so routing
        # only ever sees providers we can actually call.
        fallback = [p for p in fallback if p in active]
        if not fallback:
            # Default the routing order to the active set if the user hasn't
            # ordered anything reachable yet.
            fallback = list(active)

    # Phase 8 — coerce optional thinking_mode flag to bool (default True).
    thinking_mode = cfg.get("thinking_mode")
    if thinking_mode is None:
        thinking_mode = True
    thinking_mode = bool(thinking_mode)

    # Phase 19 — auto_goals settings. Merge over the documented
    # defaults so a partial dict from the UI doesn't drop the rest of
    # the safety knobs. Type-coerce + clamp to safe ranges.
    auto_defaults = default_managed_config()["auto_goals"]
    raw_ag = cfg.get("auto_goals")
    if not isinstance(raw_ag, dict):
        raw_ag = {}
    auto_goals = dict(auto_defaults)
    try:
        if "enabled" in raw_ag:
            auto_goals["enabled"] = bool(raw_ag["enabled"])
        if "max_per_day" in raw_ag:
            auto_goals["max_per_day"] = max(0, min(
                int(raw_ag["max_per_day"]), 50))
        if "min_confidence" in raw_ag:
            auto_goals["min_confidence"] = max(0.0, min(
                float(raw_ag["min_confidence"]), 1.0))
        if "cooldown_seconds" in raw_ag:
            auto_goals["cooldown_seconds"] = max(60, min(
                int(raw_ag["cooldown_seconds"]), 86400))
    except (TypeError, ValueError):
        # Any malformed value → fall back to defaults silently. The
        # engine's own _coerce_settings is a second line of defense.
        auto_goals = dict(auto_defaults)

    return True, None, {
        "mode": mode,
        # Keep the user's full selection (so the UI can show inactive rows),
        # but the router-facing fallback_order is already filtered to active.
        "providers": providers if mode == "byok" else default_managed_config()["providers"],
        "api_keys": api_keys,
        "fallback_order": fallback if mode == "byok"
                          else default_managed_config()["fallback_order"],
        "thinking_mode": thinking_mode,
        "auto_goals": auto_goals,
    }


def public_config(cfg):
    """Return a public-safe copy of a config — keys masked, never raw."""
    if not cfg: return None
    safe = {
        "mode": cfg.get("mode", "managed"),
        "providers": cfg.get("providers", []),
        "fallback_order": cfg.get("fallback_order", []),
        "api_keys_masked": {p: mask_key(v) for p, v in (cfg.get("api_keys") or {}).items()},
        "api_keys_set": {p: bool(v) for p, v in (cfg.get("api_keys") or {}).items()},
        # Phase 8 — surface the thinking-mode toggle to the UI.
        "thinking_mode": bool(cfg.get("thinking_mode", True)),
        # Phase 19 — surface the auto-goals settings (no secrets in
        # here; the whole sub-dict is safe to expose).
        "auto_goals": dict(
            cfg.get("auto_goals")
            or default_managed_config()["auto_goals"]),
    }
    return safe


def env_for_session(cfg):
    """
    Build the environment dict to pass to the agent subprocess.

    - Managed mode: inherit current process env unchanged (uses platform secrets).
    - BYOK mode:    strip every known provider env var, then set ONLY the
                    selected providers' keys + an API_PRIORITY override that
                    matches the user's fallback_order.

    API keys are NEVER logged or stored in plaintext on disk in BYOK temp form
    beyond the lifetime of the subprocess env (which OS-level isolates them).
    """
    env = os.environ.copy()
    if not cfg or cfg.get("mode") != "byok":
        return env

    # 1) wipe any inherited provider keys so BYOK is hermetic
    for p, meta in PROVIDERS.items():
        ev = meta["key_env"]
        if ev:
            env.pop(ev, None)
    # 2) inject the user-supplied keys for selected providers only
    keys = cfg.get("api_keys") or {}
    for p in cfg.get("providers", []):
        ev = PROVIDERS[p]["key_env"]
        if ev and keys.get(p):
            env[ev] = keys[p]

    # 3) constrain the router's priority to the user's fallback order
    order = [p for p in cfg.get("fallback_order", []) if p in PROVIDERS]
    if order:
        env["API_PRIORITY"] = ",".join(order)

    # 4) explicitly control local fallback based on user selection — never
    #    let an inherited env enable Ollama when the user did not pick it.
    env["ALLOW_LOCAL_FALLBACK"] = "1" if "local" in cfg.get("providers", []) else "0"

    # 5) Plan mode capability gates (Phase 3)
    plan_mode = cfg.get("plan_mode", "elite")
    caps = PLAN_CAPABILITIES.get(plan_mode, PLAN_CAPABILITIES["elite"])
    env["PLAN_MODE"]               = plan_mode
    env["ALLOW_PLANNING"]          = "1" if caps["allow_planning"]        else "0"
    env["ALLOW_REASONING"]         = "1" if caps["allow_reasoning"]       else "0"
    env["ALLOW_DEBUG"]             = "1" if caps["allow_debug"]           else "0"
    env["ALLOW_SELF_CORRECTION"]   = "1" if caps["allow_self_correction"] else "0"
    env["MAX_AGENT_STEPS"]         = str(caps["max_steps"])

    return env


# ────────────────────────────────────────────────────────────────────────────
# Log parsing
# ────────────────────────────────────────────────────────────────────────────

ANSI_RE       = re.compile(r"\x1b\[[0-9;]*m")
STAGE_RE      = re.compile(r"\[STAGE\]\s*(.+)", re.IGNORECASE)
STEP_RE       = re.compile(r"\[STEP\s*(\d+)/?(\d+)?\]\s*(.*)", re.IGNORECASE)
ROUTE_RE      = re.compile(r"\[ROUTE\]\s*(.+)", re.IGNORECASE)
ESCALATE_RE   = re.compile(r"\[ESCALATION\]\s*(.+)", re.IGNORECASE)
FALLBACK_RE   = re.compile(r"\[FALLBACK\]\s*(.+)", re.IGNORECASE)
MODEL_RE      = re.compile(r"(?:using|via|model[:=]|→)\s*([a-z0-9_\-./]+)", re.IGNORECASE)
RETRY_RE      = re.compile(r"\[RETRY(?:\s+(\d+))?\]", re.IGNORECASE)
ERROR_CAT_RE  = re.compile(r"\[(?:ERROR|FAIL|FAILED)(?::|\s+)([A-Za-z_\-]+)\]", re.IGNORECASE)
VALIDATION_RE = re.compile(r"\[VALIDATION\]\s*(.+)", re.IGNORECASE)
FINAL_RE      = re.compile(r"\[FINAL CHECK\]\s*(.+)", re.IGNORECASE)
RESULT_RE     = re.compile(r"^\s*(?:📋\s*)?Result:\s*(.+)", re.IGNORECASE)
SUCCESS_HINT  = re.compile(r"(?:✅|\bsuccess\b|\bdone\b|\bcompleted\b)", re.IGNORECASE)
FAILURE_HINT  = re.compile(r"(?:❌|\bfailed\b|\berror\b|\bunable to\b)", re.IGNORECASE)


def classify(line):
    s = line.upper()
    if any(t in s for t in ("[ERROR]", "❌", "TRACEBACK", "EXCEPTION", "[FAIL")):
        return "error"
    if "[VALIDATION]" in s:
        return "validation"
    if any(t in s for t in ("[ROUTE]", "[STAGE]", "[STEP", "[ESCALATION]",
                            "[RETRY", "[FALLBACK]", "[MODE]")):
        return "info"
    if "✅" in line or "[SUCCESS]" in s or " DONE" in s:
        return "success"
    return "log"


def parse_decisions(line, sid, ts):
    for kind, rx in (("ROUTE", ROUTE_RE), ("ESCALATION", ESCALATE_RE),
                     ("FALLBACK", FALLBACK_RE), ("VALIDATION", VALIDATION_RE),
                     ("FINAL CHECK", FINAL_RE)):
        m = rx.search(line)
        if m:
            db_insert_decision(sid, ts, kind, m.group(1).strip())


def update_state_from_line(sid, line):
    upd = {}
    m = STAGE_RE.search(line)
    if m: upd["stage"] = m.group(1).strip()[:120]
    m = STEP_RE.search(line)
    if m:
        cur, total, desc = m.group(1), m.group(2), (m.group(3) or "").strip()
        upd["step"] = f"{cur}/{total} {desc}".strip() if total else f"{cur} {desc}".strip()
    m = ROUTE_RE.search(line)
    if m:
        target = m.group(1).strip()
        upd["current_model"] = target[:80]
        mm = MODEL_RE.search(target)
        if mm: upd["current_model"] = mm.group(1)
    m = RETRY_RE.search(line)
    if m:
        n = m.group(1)
        cur = (db_session(sid) or {}).get("retry_count") or 0
        upd["retry_count"] = int(n) if n else cur + 1
    m = ERROR_CAT_RE.search(line)
    if m: upd["error_category"] = m.group(1).strip()
    m = VALIDATION_RE.search(line)
    if m: upd["validation"] = m.group(1).strip()
    m = FINAL_RE.search(line)
    if m:
        text = m.group(1).strip()
        upd["result"] = text
        if SUCCESS_HINT.search(text):  upd["success"] = 1
        elif FAILURE_HINT.search(text): upd["success"] = 0
    m = RESULT_RE.match(line)
    if m and not (db_session(sid) or {}).get("result"):
        upd["result"] = m.group(1).strip()
    if upd:
        db_update_session(sid, **upd)


# ────────────────────────────────────────────────────────────────────────────
# Queue & runner
# ────────────────────────────────────────────────────────────────────────────

queue_lock     = threading.Lock()
pending_queue  = deque()             # session ids waiting
running        = {"sid": None, "proc": None, "seq": 0}
managed_runs   = deque()             # timestamps of recent managed-mode runs


def snapshot_workspace(ws_dir=None):
    """Snapshot of (relpath, mtime) tuples. Defaults to the shared root for
    backward compat; the queue worker now passes the per-session dir."""
    ws = ws_dir or WORKSPACE
    out = set()
    for root, _dirs, files in os.walk(ws):
        for f in files:
            full = os.path.join(root, f)
            try:
                out.add((os.path.relpath(full, ws), os.path.getmtime(full)))
            except OSError:
                pass
    return out


def diff_workspace(before, ws_dir=None):
    after = snapshot_workspace(ws_dir)
    added = sorted({p for (p, _) in after} - {p for (p, _) in before})
    modified = sorted({p for (p, m) in after if (p, m) not in before
                       and p in {x for (x, _) in before}})
    return list(dict.fromkeys(added + modified))


def run_session(sid):
    sess = db_session(sid)
    if not sess: return

    cfg = json.loads(sess.get("config_json") or "{}") or default_managed_config()
    mode = cfg.get("mode", "managed")

    # Phase 8 — pick entry point based on the thinking_mode flag.
    # ON  → orchestrator.py (THINK→PLAN→EXECUTE→VERIFY→REFLECT→ADAPT,
    #       multi-strategy retries, automatic decomposition)
    # OFF → main.py (raw single-shot agent, original behaviour)
    # Both entry points accept the same `--model` flag, so a forced
    # provider chosen via Settings → Force Model is honoured in either
    # mode. The orchestrator forwards it to Agent(force_model=…).
    thinking_mode = bool(cfg.get("thinking_mode", True))
    if thinking_mode:
        cmd = [sys.executable, "-u", "orchestrator.py"]
        if sess["model"]:
            cmd += ["--model", sess["model"]]
        cmd.append(sess["task"])
    else:
        cmd = [sys.executable, "-u", "main.py"]
        if sess["model"]:
            cmd += ["--model", sess["model"]]
        cmd.append(sess["task"])

    started = time.time()
    db_update_session(sid, status="running", started_at=started, stage="starting",
                      step=None, current_model=None, retry_count=0,
                      error_category=None, validation=None, result=None,
                      success=None, exit_code=None, files_json=None)

    # Phase 7.0 — each session writes into its own folder so files never
    # collide and the UI can list/serve/zip them per session.
    ws_dir = session_workspace(sid)
    ws_before = snapshot_workspace(ws_dir)
    seq = 0

    # ── Phase 6: per-session usage tracking (estimated, never authoritative) ──
    # Tokens are approximated as bytes/4 of agent output; calls are counted from
    # [ROUTE] markers. Recorded per current model so the UI can show a breakdown.
    usage = {
        "calls": 0,
        "tokens_in_est": max(1, len(sess["task"]) // 4),
        "tokens_out_est": 0,
        "by_model": {},     # {model: {"calls": n, "tokens_out_est": n}}
        "started_at": started,
    }
    state = {"current_model": None}

    def _flush_usage():
        db_update_session(sid, usage_json=json.dumps(usage))

    def emit(level, text):
        nonlocal seq
        seq += 1
        ts = time.time()
        db_insert_log(sid, seq, ts, level, text)
        running["seq"] = seq

    def track_usage_from_line(line):
        m = ROUTE_RE.search(line)
        if m:
            usage["calls"] += 1
            target = m.group(1).strip()
            mm = MODEL_RE.search(target)
            model = (mm.group(1) if mm else target.split()[0]).lower()[:32]
            state["current_model"] = model
            slot = usage["by_model"].setdefault(
                model, {"calls": 0, "tokens_out_est": 0})
            slot["calls"] += 1
            return
        # Approximate output tokens from any non-marker content while a model is active
        if state["current_model"] and not line.startswith(("[", "▶", "  Forced")):
            n = max(1, len(line) // 4)
            usage["tokens_out_est"] += n
            slot = usage["by_model"].setdefault(
                state["current_model"], {"calls": 0, "tokens_out_est": 0})
            slot["tokens_out_est"] += n

    if mode == "byok":
        order = " → ".join(cfg.get("fallback_order") or [])
        provs = ", ".join(cfg.get("providers") or [])
        emit("info", f"[MODE] Using BYOK APIs — providers: {provs}")
        emit("info", f"[MODE] BYOK fallback preference: {order}")
    else:
        emit("info", "[MODE] Using Managed API (platform-provided keys)")
    emit("system", f"▶ Starting task: {sess['task']}")
    if sess["model"]:
        emit("system", f"  Forced model: {sess['model']}")

    proc_env = env_for_session(cfg)
    # config.py honors $WORKSPACE_DIR — point the agent at this session's
    # isolated folder. tools.py reads config.WORKSPACE_DIR at __init__ time
    # so this picks up cleanly without modifying any agent file.
    proc_env["WORKSPACE_DIR"] = ws_dir

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", bufsize=1, cwd=BASE_DIR,
            env={**proc_env, "PYTHONUNBUFFERED": "1"},
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        running["proc"] = proc
        usage_flush_at = time.time()
        for raw in proc.stdout:
            line = ANSI_RE.sub("", raw.rstrip("\n"))
            if not line: continue
            
            # Phase 28.1 - Strip noisy logs
            if "%|" in line or line.startswith("Batches: ") or "onnx.tar.gz" in line or "it/s]" in line:
                continue

            level = classify(line)
            emit(level, line)
            update_state_from_line(sid, line)
            parse_decisions(line, sid, time.time())
            track_usage_from_line(line)
            # Flush usage to DB at most every 1s to avoid hammering writes
            if time.time() - usage_flush_at > 1.0:
                _flush_usage()
                usage_flush_at = time.time()
        proc.wait()
        exit_code = proc.returncode
    except Exception as e:
        emit("error", f"[EXCEPTION] {e}")
        exit_code = -1
    finally:
        running["proc"] = None
        files = diff_workspace(ws_before, ws_dir)
        cur = db_session(sid) or {}
        success = cur.get("success")
        if success is None:
            success = 1 if exit_code == 0 else 0
        status = "success" if success else (
            "stopped" if exit_code in (-15, -9, 143, 137) else "failed")
        finished = time.time()
        usage["finished_at"] = finished
        usage["elapsed_seconds"] = round(finished - started, 2)
        db_update_session(
            sid, status=status, finished_at=finished, exit_code=exit_code,
            success=success, files_json=json.dumps(files),
            usage_json=json.dumps(usage),
        )
        emit("system",
             f"[Task finished — exit={exit_code} status={status} "
             f"calls={usage['calls']} ~tokens={usage['tokens_in_est']+usage['tokens_out_est']} "
             f"elapsed={usage['elapsed_seconds']}s]")
        # Phase 17 — if this session is part of a chain, mark the
        # parent chain task done now that the subprocess has finished.
        # The chain row's status auto-rolls-up inside mark_task_done.
        chain_task_id = cur.get("chain_task_id")
        chain_id      = cur.get("chain_id")
        if chain_task_id is not None:
            try:
                runner = _get_chain_runner()
                if runner is not None:
                    # ── Phase 18 idempotency: only act on tasks that
                    # are still mid-flight. If the row is already in
                    # a terminal status (completed / failed / skipped)
                    # then a previous hook run already handled it —
                    # firing again would re-emit events, double-bump
                    # the replan budget, and stamp over the existing
                    # last_error. Bail early. ──
                    cur_task = runner.memory.get_task(
                        int(chain_task_id))
                    if cur_task and cur_task.get("status") not in (
                            "completed", "failed", "skipped"):
                        # ── Phase 18 retry-aware terminal handling
                        # for the subprocess path. Mirrors run_next:
                        # on retryable failure flip back to pending so
                        # the next /run-next call uses the augmented
                        # goal; on terminal failure mark done + try to
                        # replan. ──
                        attempts_used = int(cur_task.get("attempts", 0)
                                            or 0)
                        max_attempts = runner.max_attempts_per_task
                        retry_reason = (f"session={sid} status={status} "
                                        f"exit={exit_code}")
                        if (not success
                                and attempts_used < max_attempts):
                            # Retryable — flip back to pending so the
                            # next /run-next call picks it up with the
                            # augmented goal. If reset_ok is False
                            # another writer already moved the row off
                            # `running`; emit a progress event and let
                            # whoever owns the row finish — the new
                            # status-conditional mark_task_done makes
                            # that path idempotent.
                            reset_ok = runner.memory.reset_task_for_retry(
                                int(chain_task_id),
                                last_error=retry_reason[:500])
                            runner._emit("task_progress", {
                                "chain_id": chain_id,
                                "task_id": chain_task_id,
                                "status": ("pending_retry" if reset_ok
                                           else "race_lost"),
                                "attempt": attempts_used,
                                "last_error": retry_reason[:200],
                            })
                        else:
                            runner.memory.mark_task_done(
                                int(chain_task_id),
                                ok=bool(success), reason=retry_reason)
                            runner._emit("task_completed", {
                                "chain_id": chain_id,
                                "task_id": chain_task_id,
                                "ok": bool(success),
                                "reason": f"session {sid} "
                                          f"{status}"[:200],
                            })
                            if not success:
                                try:
                                    runner.maybe_replan_after_failure(
                                        int(chain_id),
                                        int(chain_task_id))
                                except Exception as e:
                                    emit("warn",
                                         f"[chain-hook] replan "
                                         f"failed: {e}")
                            # ── Phase 19 — chain rollup hook for the
                            # goal engine. mark_task_done already
                            # rolled the chain up; if the chain is
                            # now in a terminal state we (a) emit
                            # `goal_completed` for system_generated
                            # chains so the UI can mark them done,
                            # and (b) ask the engine whether a new
                            # goal cycle should run. The engine has
                            # its own enabled/cooldown/cap/loop
                            # gates — this call is fire-and-forget. ──
                            try:
                                chain_row = runner.memory.get_chain(
                                    int(chain_id))
                                chain_meta = (chain_row or {}).get(
                                    "chain", {})
                                cstatus = chain_meta.get("status")
                                # Phase 19 — authoritative lookup: the
                                # `system_generated` flag is now
                                # surfaced directly by get_chain so the
                                # loop-guard works regardless of how
                                # old the chain is. (Earlier revisions
                                # scanned list_chains(limit=200) which
                                # silently misclassified old chains.)
                                sys_gen = bool(
                                    chain_meta.get("system_generated",
                                                    False))
                                if cstatus in ("completed", "failed"):
                                    if sys_gen:
                                        _emit_goal_event(
                                            "goal_completed",
                                            int(chain_id),
                                            {"status": cstatus,
                                             "via_session": sid})
                                    # Trigger a new analysis cycle.
                                    # The engine refuses if the parent
                                    # chain was system_generated, so
                                    # there's no recursive spawn.
                                    _maybe_trigger_goal_engine_async(
                                        reason=("chain_failed"
                                                if cstatus == "failed"
                                                else "chain_completed"),
                                        parent_chain_id=int(chain_id),
                                        parent_system_generated=sys_gen,
                                    )
                            except Exception as ge:
                                emit("warn",
                                     f"[goal-engine] post-chain "
                                     f"trigger failed: {ge}")
            except Exception as e:
                emit("warn", f"[chain-hook] mark_task_done failed: {e}")


def queue_worker():
    while True:
        sid = None
        with queue_lock:
            if pending_queue and running["sid"] is None:
                sid = pending_queue.popleft()
                running["sid"] = sid
        if sid is None:
            time.sleep(0.4)
            continue
        try:
            run_session(sid)
        except Exception as e:
            db_update_session(sid, status="failed", exit_code=-1)
            print("worker error:", e, file=sys.stderr)
        finally:
            with queue_lock:
                running["sid"] = None
                running["seq"] = 0


_worker = threading.Thread(target=queue_worker, daemon=True)
_worker.start()


def enqueue_task(task, model=None, cfg=None,
                 chain_id=None, chain_task_id=None):
    """Queue a task for the next free worker.

    Phase 17: when called from the chain runner, `chain_id` and
    `chain_task_id` are passed so run_session() can mark the parent
    chain task done when this session terminates."""
    sid = uuid.uuid4().hex[:12]
    cfg = cfg or get_setting("default_config", default_managed_config())
    sess = {"id": sid, "task": task, "model": model,
            "status": "queued", "created_at": time.time(), "config": cfg,
            "chain_id": chain_id, "chain_task_id": chain_task_id}
    db_insert_session(sess)
    # Phase 17: backfill the chain link columns separately so the
    # base INSERT statement stays unchanged (and existing call sites
    # don't break).
    if chain_id is not None or chain_task_id is not None:
        db_update_session(sid, chain_id=chain_id,
                          chain_task_id=chain_task_id)
    with queue_lock:
        pending_queue.append(sid)
        if (cfg or {}).get("mode") == "managed":
            now = time.time()
            managed_runs.append(now)
            # prune old timestamps
            cutoff = now - MANAGED_LIMITS["rate_window_seconds"]
            while managed_runs and managed_runs[0] < cutoff:
                managed_runs.popleft()
    return sid


def stop_running_session(sid):
    proc = running.get("proc")
    if running.get("sid") != sid or not proc:
        return False, "Session is not currently running"

    def _kill_tree(p, sig):
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(p.pid), sig)
            else:
                if sig == signal.SIGKILL:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True)
                else:
                    p.terminate()
        except (ProcessLookupError, OSError):
            pass

    _kill_tree(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
        return True, "Stopped (graceful)"
    except subprocess.TimeoutExpired:
        _kill_tree(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=3)
            return True, "Force-killed"
        except subprocess.TimeoutExpired:
            db_update_session(sid, error_category="stop_timeout")
            return False, "Process did not exit after SIGKILL — still running"


# ────────────────────────────────────────────────────────────────────────────
# Routes — UI
# ────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ────────────────────────────────────────────────────────────────────────────
# Routes — Config (Phase 5.2 hybrid API system)
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/providers")
def api_providers():
    """Full Phase 5 provider catalogue with capabilities, categories, and key status."""
    stored_cfg = get_setting("default_config", default_managed_config())
    stored_keys = stored_cfg.get("api_keys") or {}
    result = []
    for p, meta in PROVIDERS.items():
        key_env = meta.get("key_env")
        has_platform_key = bool(os.getenv(key_env)) if key_env else False
        has_byok_key = bool(stored_keys.get(p))
        result.append({
            "id":          p,
            "label":       meta["label"],
            "category":    meta.get("category", "core"),
            "needs_key":   bool(key_env),
            "speed":       meta.get("speed", "balanced"),
            "quality":     meta.get("quality", "good"),
            "caps":        meta.get("caps", []),
            "models":      meta.get("models", []),
            "plan_pref":   meta.get("plan_pref", []),
            "has_platform_key": has_platform_key,
            "has_byok_key":     has_byok_key,
            "available":   (not bool(key_env)) or has_platform_key or has_byok_key,
        })
    return jsonify({"providers": result})


@app.route("/api/p5/routing")
def api_p5_routing():
    """Phase 5 — Return intelligent routing recommendation based on plan mode and available keys."""
    plan_mode = request.args.get("plan", "pro").lower()
    stored_cfg = get_setting("default_config", default_managed_config())
    stored_keys = stored_cfg.get("api_keys") or {}
    best = p5_get_best_provider(plan_mode, stored_keys)
    # Build availability matrix
    avail = {}
    for p, meta in PROVIDERS.items():
        key_env = meta.get("key_env")
        has_key = (not key_env) or bool(os.getenv(key_env)) or bool(stored_keys.get(p))
        avail[p] = {"available": has_key, "label": meta["label"], "speed": meta.get("speed"), "caps": meta.get("caps", [])}
    from config import Config
    fallback_chain = [p for p in Config.PLAN_PROVIDER_PREFERENCE.get(plan_mode, [])
                      if avail.get(p, {}).get("available")]
    return jsonify({
        "ok": True,
        "plan": plan_mode,
        "recommended": best,
        "fallback_chain": fallback_chain[:5],
        "availability": avail,
    })


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 6 — Decision Intelligence Layer
# ══════════════════════════════════════════════════════════════════════════════

_P6_PERF_KEY   = "p6_provider_perf"    # settings key for persistent perf data
_P6_PRIO_KEY   = "p6_user_priority"    # settings key for user priority preference

# Static intelligence matrix — static estimates (ms latency, cost tier)
# Runtime measurements overlay these defaults.
_P6_INTEL = {
    "openai":      {"latency_est": 900,  "cost_tier": "medium", "quality_tier": "high",   "best_for": ["coding", "reasoning", "debugging"]},
    "anthropic":   {"latency_est": 1200, "cost_tier": "high",   "quality_tier": "highest","best_for": ["reasoning", "debugging", "long-context"]},
    "gemini":      {"latency_est": 600,  "cost_tier": "low",    "quality_tier": "high",   "best_for": ["coding", "multimodal", "fast-tasks"]},
    "groq":        {"latency_est": 200,  "cost_tier": "free",   "quality_tier": "good",   "best_for": ["fast-tasks", "coding", "prototyping"]},
    "openrouter":  {"latency_est": 800,  "cost_tier": "low",    "quality_tier": "high",   "best_for": ["coding", "reasoning", "access-all"]},
    "xai":         {"latency_est": 1000, "cost_tier": "high",   "quality_tier": "high",   "best_for": ["reasoning", "current-events"]},
    "bedrock":     {"latency_est": 1100, "cost_tier": "medium", "quality_tier": "high",   "best_for": ["enterprise", "reasoning"]},
    "azure":       {"latency_est": 850,  "cost_tier": "medium", "quality_tier": "high",   "best_for": ["enterprise", "coding", "debugging"]},
    "fireworks":   {"latency_est": 300,  "cost_tier": "low",    "quality_tier": "good",   "best_for": ["fast-tasks", "coding"]},
    "deepseek":    {"latency_est": 400,  "cost_tier": "lowest", "quality_tier": "high",   "best_for": ["coding", "reasoning", "budget"]},
    "mistral":     {"latency_est": 350,  "cost_tier": "low",    "quality_tier": "good",   "best_for": ["coding", "fast-tasks"]},
    "cohere":      {"latency_est": 500,  "cost_tier": "low",    "quality_tier": "good",   "best_for": ["summarization", "search"]},
    "huggingface": {"latency_est": 2000, "cost_tier": "free",   "quality_tier": "variable","best_for": ["experimentation"]},
    "together":    {"latency_est": 400,  "cost_tier": "low",    "quality_tier": "good",   "best_for": ["coding", "fast-tasks"]},
    "nvidia":      {"latency_est": 350,  "cost_tier": "low",    "quality_tier": "good",   "best_for": ["coding", "fast-tasks"]},
    "replicate":   {"latency_est": 3000, "cost_tier": "medium", "quality_tier": "high",   "best_for": ["image-generation", "multimodal"]},
    "elevenlabs":  {"latency_est": 400,  "cost_tier": "medium", "quality_tier": "highest","best_for": ["text-to-speech"]},
    "deepgram":    {"latency_est": 300,  "cost_tier": "low",    "quality_tier": "high",   "best_for": ["speech-to-text"]},
    "local":       {"latency_est": 1500, "cost_tier": "free",   "quality_tier": "variable","best_for": ["privacy", "offline", "experimentation"]},
}

# Task keyword → capability tags (used for recommendation)
_P6_TASK_PATTERNS = [
    (["fast", "quick", "simple", "prototype", "demo"],           "fast-tasks"),
    (["debug", "fix", "error", "bug", "traceback", "exception"], "debugging"),
    (["reason", "think", "analyze", "explain", "understand"],     "reasoning"),
    (["code", "build", "implement", "function", "class", "api"],  "coding"),
    (["image", "picture", "photo", "vision", "multimodal"],       "multimodal"),
    (["cheap", "free", "budget", "cost"],                         "budget"),
    (["enterprise", "corporate", "compliance", "gdpr"],           "enterprise"),
    (["long", "document", "context", "large"],                    "long-context"),
    (["speech", "voice", "audio", "tts", "transcribe"],           "text-to-speech"),
]


def p6_analyze_task(task_text: str) -> list:
    """Extract capability needs from task text. Returns list of capability tags."""
    lower = task_text.lower()
    needs = []
    for keywords, tag in _P6_TASK_PATTERNS:
        if any(kw in lower for kw in keywords):
            needs.append(tag)
    if not needs:
        needs = ["coding"]  # default
    return needs


def p6_get_perf() -> dict:
    """Load stored per-provider performance data."""
    return get_setting(_P6_PERF_KEY, {})


def p6_record_perf(provider: str, latency_ms: float, success: bool, was_fallback: bool = False):
    """Record a provider call result and update rolling stats."""
    perf = p6_get_perf()
    if provider not in perf:
        perf[provider] = {
            "calls": 0, "successes": 0, "failures": 0,
            "fallbacks": 0, "total_latency_ms": 0,
            "min_latency_ms": None, "max_latency_ms": None,
            "last_used": None,
        }
    p = perf[provider]
    p["calls"] += 1
    if success:
        p["successes"] += 1
        p["total_latency_ms"] += latency_ms
        if p["min_latency_ms"] is None or latency_ms < p["min_latency_ms"]:
            p["min_latency_ms"] = latency_ms
        if p["max_latency_ms"] is None or latency_ms > p["max_latency_ms"]:
            p["max_latency_ms"] = latency_ms
    else:
        p["failures"] += 1
    if was_fallback:
        p["fallbacks"] += 1
    p["last_used"] = time.time()
    set_setting(_P6_PERF_KEY, perf)


def p6_compute_badges(perf: dict) -> dict:
    """Compute performance badges for providers with enough data."""
    badges = {}
    if not perf:
        return badges
    # Only score providers with >= 3 calls
    eligible = {pid: p for pid, p in perf.items() if p.get("calls", 0) >= 3}
    if not eligible:
        return badges
    # Fastest: lowest avg latency
    def avg_lat(p): return p["total_latency_ms"] / max(p["successes"], 1)
    def sr(p):      return p["successes"] / max(p["calls"], 1)
    fastest = min(eligible, key=lambda pid: avg_lat(eligible[pid]))
    most_reliable = max(eligible, key=lambda pid: sr(eligible[pid]))
    badges[fastest]       = badges.get(fastest, [])
    badges[fastest].append("⚡ Fastest today")
    badges[most_reliable] = badges.get(most_reliable, [])
    badges[most_reliable].append("✅ Most reliable")
    # Cheapest: by cost_tier from _P6_INTEL
    cost_rank = {"free": 0, "lowest": 1, "low": 2, "medium": 3, "high": 4}
    cheapest = min(eligible, key=lambda pid: cost_rank.get(_P6_INTEL.get(pid, {}).get("cost_tier", "high"), 4))
    badges[cheapest] = badges.get(cheapest, [])
    badges[cheapest].append("💰 Cheapest")
    return badges


def p6_recommend(task_text: str, plan_mode: str, user_priority: str, available_keys: dict) -> dict:
    """Full recommendation: analyze task, score providers, return ranked list with reasons."""
    from config import Config
    needs  = p6_analyze_task(task_text)
    perf   = p6_get_perf()
    badges = p6_compute_badges(perf)

    priority_weights = {
        "cheap": {"cost": 0.6, "speed": 0.2, "quality": 0.2},
        "fast":  {"cost": 0.1, "speed": 0.7, "quality": 0.2},
        "smart": {"cost": 0.1, "speed": 0.2, "quality": 0.7},
    }.get(user_priority, {"cost": 0.2, "speed": 0.4, "quality": 0.4})

    cost_score  = {"free": 1.0, "lowest": 0.9, "low": 0.75, "medium": 0.4, "high": 0.1}
    qual_score  = {"highest": 1.0, "high": 0.8, "good": 0.6, "variable": 0.3}
    speed_score = lambda lat: max(0, 1.0 - lat / 3000)  # 0ms=1.0, 3000ms=0.0

    results = []
    for pid, meta in PROVIDERS.items():
        key_env = meta.get("key_env")
        has_key = (not key_env) or bool(os.getenv(key_env)) or bool(available_keys.get(pid))
        if not has_key:
            continue
        intel = _P6_INTEL.get(pid, {})
        best_for = intel.get("best_for", [])
        caps     = meta.get("caps", [])

        # Capability match score
        cap_match = sum(1 for n in needs if n in best_for or n in caps)

        # Runtime latency if available
        p_perf = perf.get(pid, {})
        runtime_lat = (p_perf.get("total_latency_ms", 0) / max(p_perf.get("successes", 1), 1)
                       if p_perf.get("successes", 0) > 0
                       else intel.get("latency_est", 1500))

        c = cost_score.get(intel.get("cost_tier", "medium"), 0.4)
        q = qual_score.get(intel.get("quality_tier", "good"),  0.5)
        s = speed_score(runtime_lat)
        sr = p_perf.get("successes", 0) / max(p_perf.get("calls", 1), 1) if p_perf.get("calls", 0) > 0 else 0.7

        total = (priority_weights["cost"]    * c +
                 priority_weights["speed"]   * s +
                 priority_weights["quality"] * q +
                 0.15 * cap_match +
                 0.05 * sr)

        # Build reason
        reasons = []
        if "fast-tasks" in needs or user_priority == "fast":
            if intel.get("latency_est", 9999) < 400: reasons.append("fastest response")
        if "debugging" in needs and "debugging" in caps: reasons.append("great at debugging")
        if "reasoning" in needs and "thinking" in caps:  reasons.append("strong reasoning")
        if "budget" in needs or user_priority == "cheap":
            if intel.get("cost_tier") in ("free", "lowest"): reasons.append("lowest cost")
        if not reasons:
            reasons.append(f"{intel.get('quality_tier','good')} quality")
        plan_prefs = meta.get("plan_pref", [])
        if plan_mode in plan_prefs: reasons.insert(0, f"ideal for {plan_mode} mode")

        results.append({
            "id":      pid,
            "label":   meta["label"],
            "score":   round(total, 4),
            "reasons": reasons[:2],
            "badges":  badges.get(pid, []),
            "latency_est": int(runtime_lat),
            "cost_tier":   intel.get("cost_tier", "medium"),
            "quality_tier":intel.get("quality_tier", "good"),
            "caps":    caps,
            "best_for":best_for,
            "calls":   p_perf.get("calls", 0),
            "success_rate": round(sr * 100, 1),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"needs": needs, "ranked": results}


@app.route("/api/p6/performance")
def api_p6_performance():
    """Return per-provider performance stats + badges."""
    perf   = p6_get_perf()
    badges = p6_compute_badges(perf)
    # Merge with static intel
    out = {}
    for pid, intel in _P6_INTEL.items():
        p = perf.get(pid, {})
        sr = round(p.get("successes", 0) / max(p.get("calls", 1), 1) * 100, 1) if p.get("calls", 0) > 0 else None
        avg_l = round(p["total_latency_ms"] / max(p.get("successes", 1), 1)) if p.get("successes", 0) > 0 else intel.get("latency_est")
        out[pid] = {
            "calls":        p.get("calls", 0),
            "success_rate": sr,
            "avg_latency_ms": avg_l,
            "min_latency_ms": p.get("min_latency_ms"),
            "max_latency_ms": p.get("max_latency_ms"),
            "fallbacks":    p.get("fallbacks", 0),
            "last_used":    p.get("last_used"),
            "badges":       badges.get(pid, []),
            "cost_tier":    intel.get("cost_tier"),
            "quality_tier": intel.get("quality_tier"),
            "latency_est":  intel.get("latency_est"),
            "best_for":     intel.get("best_for", []),
        }
    return jsonify({"ok": True, "performance": out, "badges": badges})


@app.route("/api/p6/perf/record", methods=["POST"])
def api_p6_record_perf():
    """Record a provider call result. Called by the frontend after each session completes."""
    d = request.get_json() or {}
    provider     = d.get("provider", "")
    latency_ms   = float(d.get("latency_ms", 0))
    success      = bool(d.get("success", True))
    was_fallback = bool(d.get("was_fallback", False))
    if provider not in PROVIDERS:
        return jsonify({"ok": False, "error": "unknown provider"}), 400
    p6_record_perf(provider, latency_ms, success, was_fallback)
    perf   = p6_get_perf()
    badges = p6_compute_badges(perf)
    return jsonify({"ok": True, "badges": badges.get(provider, [])})


@app.route("/api/p6/recommend", methods=["POST"])
def api_p6_recommend():
    """Analyze task text and return ranked provider recommendations."""
    d         = request.get_json() or {}
    task_text = d.get("task", "")
    plan_mode = d.get("plan", "pro").lower()
    priority  = d.get("priority", get_setting(_P6_PRIO_KEY, "fast"))
    stored    = get_setting("default_config", default_managed_config())
    keys      = stored.get("api_keys") or {}
    rec = p6_recommend(task_text, plan_mode, priority, keys)
    top = rec["ranked"][0] if rec["ranked"] else None
    return jsonify({
        "ok":       True,
        "needs":    rec["needs"],
        "top":      top,
        "ranked":   rec["ranked"][:6],
    })


@app.route("/api/p6/priority", methods=["GET", "POST"])
def api_p6_priority():
    """Get or set user routing priority (cheap / fast / smart)."""
    if request.method == "POST":
        d = request.get_json() or {}
        prio = d.get("priority", "fast").lower()
        if prio not in ("cheap", "fast", "smart"):
            return jsonify({"ok": False, "error": "priority must be cheap/fast/smart"}), 400
        set_setting(_P6_PRIO_KEY, prio)
        return jsonify({"ok": True, "priority": prio})
    return jsonify({"ok": True, "priority": get_setting(_P6_PRIO_KEY, "fast")})


@app.route("/api/get-config")
def api_get_config():
    cfg = get_setting("default_config", default_managed_config())
    # Detect platform-managed providers (have env keys present) for the UI status.
    managed_available = {
        p: bool(os.getenv(meta["key_env"])) if meta["key_env"] else False
        for p, meta in PROVIDERS.items()
    }
    return jsonify({
        "config": public_config(cfg),
        "managed_available": managed_available,
        "managed_limits": MANAGED_LIMITS,
        "managed_recent_runs": len(managed_runs),
    })


@app.route("/api/set-config", methods=["POST"])
def api_set_config():
    payload = request.get_json() or {}
    # Merge with stored config so omitted api_keys keep their previous values.
    stored = get_setting("default_config", default_managed_config())
    if isinstance(payload.get("api_keys"), dict):
        merged = dict((stored.get("api_keys") or {}))
        for p, v in payload["api_keys"].items():
            if v == "" or v is None:
                merged.pop(p, None)         # explicit clear
            elif v == "__keep__":
                pass                         # placeholder = leave existing
            else:
                merged[p] = v
        payload["api_keys"] = merged

    # Phase 6.8 — preserve ollama_model when caller omits it
    if "ollama_model" not in payload:
        payload["ollama_model"] = stored.get("ollama_model", "")

    ok, err, normalised = validate_config(payload)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400

    set_setting("default_config", normalised)
    return jsonify({"ok": True, "config": public_config(normalised)})


# ─── Phase 6.8 — granular key + provider control ──────────────────────────

@app.route("/api/key/<provider>", methods=["DELETE"])
def api_delete_key(provider):
    """Permanently remove a stored BYOK key for one provider."""
    if provider not in PROVIDERS:
        return jsonify({"ok": False, "error": f"Unknown provider: {provider}"}), 400
    stored = get_setting("default_config", default_managed_config())
    keys = dict(stored.get("api_keys") or {})
    if provider not in keys:
        return jsonify({"ok": True, "config": public_config(stored), "removed": False})
    keys.pop(provider, None)
    new_cfg = dict(stored)
    new_cfg["api_keys"] = keys
    ok, err, normalised = validate_config(new_cfg)
    if not ok:
        # Validation can fail in BYOK if removing this key leaves no active
        # provider. We still drop the key — flip back to managed gracefully.
        new_cfg["mode"] = "managed"
        ok, err, normalised = validate_config(new_cfg)
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
    set_setting("default_config", normalised)
    return jsonify({"ok": True, "config": public_config(normalised), "removed": True})


# Lightweight, low-traffic endpoints designed for one-off user clicks. The
# remote calls use small payloads + short timeouts so a slow provider can't
# stall the UI.
_TEST_TIMEOUT = 8  # seconds


def _http_get(url, headers=None, timeout=_TEST_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow 3xx so SSRF-validated hosts can't be redirected
    to a non-validated target (e.g. Ollama allowlisted host returning
    `302 Location: https://attacker.example/`)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect_blocked_to:{newurl}", headers, fp)


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler())


def _http_get_no_redirect(url, headers=None, timeout=_TEST_TIMEOUT):
    """GET that refuses to follow redirects. Use for any SSRF-guarded
    request where the host has already been validated and a redirect to
    a different host would defeat that validation."""
    req = urllib.request.Request(url, headers=headers or {})
    return _no_redirect_opener.open(req, timeout=timeout)


def _http_post(url, data, headers=None, timeout=_TEST_TIMEOUT):
    body = json.dumps(data).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


# Phase 13 — Per-provider test config. For groq/together a GET on the
# /models listing is known to 403 in some regions even with a valid
# key, so we exercise a real (1-token) chat completion. Pick stable,
# free-tier-friendly models. Easy to update if a model gets retired.
_PROVIDER_TEST_MODELS = {
    "groq":     "llama-3.1-8b-instant",
    "together": "mistralai/Mixtral-8x7B-Instruct-v0.1",
}

def _classify_test_status(status: int) -> str:
    """Friendly message for an HTTP status from a provider test call."""
    if status == 200: return "Key works"
    if status == 401: return "Invalid API key"
    if status == 403: return "Access denied / model issue"
    if status == 404: return "Endpoint or model not found"
    if status == 429: return "Rate-limited (key likely valid)"
    if 500 <= status < 600: return f"Provider error (HTTP {status})"
    return f"HTTP {status}"


def _test_provider(provider, key):
    """Return a rich result dict so the UI can show response time +
    model used + a clear status:
        {ok, status, message, ms, model}
    `ok` is True only on HTTP 200. 429 is reported as not-ok but the
    message hints the key is probably valid.
    """
    started = time.time()
    model_used = ""

    def _result(ok, status, message, model=""):
        return {
            "ok":      bool(ok),
            "status":  int(status) if status else 0,
            "message": message,
            "ms":      int((time.time() - started) * 1000),
            "model":   model,
        }

    try:
        if provider == "gemini":
            url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
                   + urllib.request.quote(key))
            with _http_get(url) as r:
                return _result(r.status == 200, r.status,
                               _classify_test_status(r.status))

        if provider == "openrouter":
            with _http_get("https://openrouter.ai/api/v1/models",
                           headers={"Authorization": f"Bearer {key}"}) as r:
                return _result(r.status == 200, r.status,
                               _classify_test_status(r.status))

        if provider == "nvidia":
            with _http_get("https://integrate.api.nvidia.com/v1/models",
                           headers={"Authorization": f"Bearer {key}"}) as r:
                return _result(r.status == 200, r.status,
                               _classify_test_status(r.status))

        if provider in ("groq", "together"):
            # POST a tiny chat completion — most reliable signal that
            # the key actually grants chat access (which is what the
            # orchestrator needs).
            url = ("https://api.groq.com/openai/v1/chat/completions"
                   if provider == "groq"
                   else "https://api.together.xyz/v1/chat/completions")
            model_used = _PROVIDER_TEST_MODELS[provider]
            body = {
                "model": model_used,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
            }
            with _http_post(url, body, headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type":  "application/json"}) as r:
                return _result(r.status == 200, r.status,
                               _classify_test_status(r.status),
                               model=model_used)

        if provider == "local":
            return _result(False, 0,
                           "Use /api/check-ollama for local provider")
        return _result(False, 0, "Unknown provider")

    except urllib.error.HTTPError as e:
        # Provider replied with a non-2xx — read a short snippet of
        # the body so a "model not found" / "wrong region" message
        # actually reaches the user instead of being swallowed.
        snippet = ""
        try:
            snippet = e.read().decode("utf-8", "replace")[:240].strip()
        except Exception:
            pass
        msg = _classify_test_status(e.code)
        if snippet:
            msg = f"{msg} — {snippet}"
        return _result(False, e.code, msg, model=model_used)
    except urllib.error.URLError as e:
        return _result(False, 0, f"Network error: {e.reason}",
                       model=model_used)
    except Exception as e:
        return _result(False, 0, f"Error: {e.__class__.__name__}: {e}",
                       model=model_used)


@app.route("/api/test-key", methods=["POST"])
def api_test_key():
    """Test a single API key against the provider's cheapest endpoint.
    Body: {provider, key?}  —  if key is omitted, falls back to the stored
    BYOK key for that provider so the user can verify a key without retyping.
    """
    data = request.get_json() or {}
    provider = (data.get("provider") or "").strip()
    key = (data.get("key") or "").strip()
    if provider not in PROVIDERS:
        return jsonify({"ok": False, "error": "Unknown provider"}), 400
    if not key:
        stored = get_setting("default_config", default_managed_config())
        key = (stored.get("api_keys") or {}).get(provider, "")
    if not key:
        return jsonify({"ok": False, "error": "No key to test"}), 400
    result = _test_provider(provider, key)
    # Back-compat: keep the legacy `message` key alongside the new
    # rich payload so older frontend builds don't blow up.
    return jsonify({**result, "message": result.get("message", "")})


@app.route("/api/check-ollama")
def api_check_ollama():
    """Ping the local Ollama daemon. Optionally returns the running model list."""
    from model_router import get_ollama_base_url
    base = get_ollama_base_url()
    try:
        with _http_get(base + "/api/tags", timeout=3) as r:
            body = r.read().decode("utf-8", "replace")[:4000]
            try:
                data = json.loads(body)
                models = [m.get("name") for m in (data.get("models") or [])]
            except Exception:
                models = []
            return jsonify({"ok": True, "host": base, "models": models})
    except urllib.error.URLError as e:
        return jsonify({"ok": False, "host": base, "error": f"Not reachable: {e.reason}"}), 200
    except Exception as e:
        return jsonify({"ok": False, "host": base, "error": str(e)}), 200


@app.route("/api/system/metrics")
def api_system_metrics():
    """Expose advanced ModelRouter telemetry and health metrics."""
    from model_router import ModelRouter, get_router
    metrics = ModelRouter.get_metrics()
    router = get_router()
    # Safely compute avg latency
    latencies = metrics.get("ollama_latency", [])
    avg_lat = sum(latencies)/len(latencies) if latencies else 0
    return jsonify({
        "ok": True,
        "metrics": {
            "total_calls": metrics.get("total_calls", 0),
            "retries": metrics.get("retries", 0),
            "fallbacks": metrics.get("fallbacks", 0),
            "avg_ollama_latency_sec": round(avg_lat, 3)
        },
        "providers": router.provider_status()
    })

# ────────────────────────────────────────────────────────────────────────────
# Routes — Sessions / Queue
# ────────────────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────────
# Phase 17 — Autonomous Task Chains
#
# A "chain" is a long-running goal split into ordered "tasks", one per
# orchestrator.run() call. Tasks are persisted in the agent Memory DB
# (separate from sessions.db) so a chain naturally survives a process
# restart — POST /resume picks up where it left off.
#
# Each chain task is executed via the SAME subprocess pipeline used
# for ad-hoc /api/queue-task: enqueue_task(..., chain_id, chain_task_id)
# tags the session, and run_session()'s terminal hook calls
# memory.mark_task_done() with the subprocess exit status.
#
# A single in-process Orchestrator + Memory is constructed lazily on
# first chain request and reused. It's only used for two cheap
# operations — orch.plan() (decomposition) and Memory CRUD — never for
# orch.run() (which would block the Flask thread). The actual run goes
# through the existing subprocess worker.
# ────────────────────────────────────────────────────────────────────
_chain_lock   = threading.Lock()
_chain_runner = None
_chain_init_error = None
# Phase 19 — Self-driven goals engine. Lazily instantiated alongside
# the chain runner so it shares the same Memory + planner. Kept
# OPTIONAL: if construction fails the chain endpoints stay healthy.
_goal_engine     = None
_goal_init_error = None

def _emit_goal_event(kind: str, chain_id, payload: dict):
    """Phase 19 — single helper used by the goal engine + chain hook
    to (a) persist a goal lifecycle event into the new goal_events
    table (polled by /api/goals/events) and (b) mirror it onto the
    chain runner's existing emit channel so any consumer already
    listening to runner events sees it too. Never raises."""
    try:
        runner = _chain_runner
        if runner is not None:
            try:
                runner.memory.record_goal_event(kind, chain_id, payload)
            except Exception:
                pass
            try:
                runner._emit(kind, dict(payload or {}))
            except Exception:
                pass
        # Best-effort stdout breadcrumb so it shows up in the workflow
        # console alongside the rest of the orchestrator output.
        print(f"[GOAL_ENGINE] {kind} chain_id={chain_id} "
              f"payload={payload}", flush=True)
    except Exception:
        pass


def _get_chain_runner():
    """Lazy singleton. Returns None on init failure (logged once)."""
    global _chain_runner, _chain_init_error, _goal_engine
    if _chain_runner is not None:
        return _chain_runner
    with _chain_lock:
        if _chain_runner is not None:
            return _chain_runner
        if _chain_init_error is not None:
            return None
        try:
            from config import Config as AgentConfig
            from memory import Memory as AgentMemory
            from orchestrator import Orchestrator as AgentOrchestrator
            from task_chains import TaskChainRunner
            cfg = AgentConfig()
            mem = AgentMemory(cfg)
            orch = AgentOrchestrator(cfg, memory=mem)
            # We pass orch._emit so any task_* events flow through the
            # same SSE channel the rest of the agent already uses.
            emit_fn = getattr(orch, "_emit", None) or (lambda k, p: None)
            _chain_runner = TaskChainRunner(orch, mem, emit_fn=emit_fn)
            # Phase 19 — co-locate the goal engine.
            try:
                from goal_engine import GoalEngine
                _goal_engine = GoalEngine(
                    runner=_chain_runner,
                    memory=mem,
                    settings_loader=_load_auto_goals_settings,
                    emit_event=_emit_goal_event,
                )
            except Exception as ge:
                # Engine failure is non-fatal: chains keep working,
                # only the proactive layer is disabled.
                global _goal_init_error
                _goal_init_error = str(ge)
                print(f"[goal-engine] init failed (auto-goals disabled): "
                      f"{ge}", file=sys.stderr)
            return _chain_runner
        except Exception as e:
            _chain_init_error = str(e)
            print(f"[chains] init failed (chain endpoints will 503): {e}",
                  file=sys.stderr)
            return None


def _load_auto_goals_settings() -> dict:
    """Pulled by the goal engine on every gate-walk so toggle changes
    take effect immediately. Reads the same `default_config` settings
    blob the rest of the app uses."""
    cfg = get_setting("default_config", default_managed_config()) or {}
    return cfg.get("auto_goals") or default_managed_config()["auto_goals"]


def _get_goal_engine():
    """Returns the engine if initialised, else None. Never constructs
    on its own — the chain runner factory does that as a side-effect."""
    if _chain_runner is None:
        _get_chain_runner()
    return _goal_engine


def _maybe_trigger_goal_engine_async(reason: str,
                                     parent_chain_id=None,
                                     parent_system_generated=False):
    """Daemon-thread shim so chain-hook callers can fire-and-forget.
    Catches everything — a goal-engine error must never bubble into
    the chain hook and abort a session finalisation."""
    eng = _get_goal_engine()
    if eng is None:
        return
    def _run():
        try:
            eng.maybe_trigger(
                reason,
                parent_chain_id=parent_chain_id,
                parent_system_generated=bool(parent_system_generated),
            )
        except Exception as e:
            print(f"[goal-engine] trigger failed: {e}", file=sys.stderr)
    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/chains", methods=["POST"])
def api_chain_create():
    """Create a new chain. Body: {goal: str, importance?: int 1-10}.
    Decomposes via the orchestrator's planner and returns the chain id
    + initial tasks. Phase 18: importance flows into the priority
    score of every sub-task generated by the planner."""
    data = request.get_json() or {}
    goal = (data.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "goal required"}), 400
    # Phase 18 — importance is optional; clamp to the contract.
    try:
        importance = int(data.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5
    importance = max(1, min(importance, 10))
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    try:
        result = runner.create_chain(goal, importance=importance)
    except Exception as e:
        return jsonify({"error": f"create_chain failed: {e}"}), 500
    if result.get("chain_id") is None:
        return jsonify({"error": result.get("error",
                        "create_chain returned no id")}), 500
    return jsonify({"ok": True, **result})


@app.route("/api/chains/<int:cid>/tasks", methods=["POST"])
def api_chain_add_task(cid):
    """Manually append a task to an existing chain. Body:
    {goal: str, importance?: int 1-10, depends_on?: [task_id, ...]}.
    Useful for letting the user tack on a follow-up after the planner
    has already decomposed the goal."""
    data = request.get_json() or {}
    goal = (data.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "goal required"}), 400
    try:
        importance = int(data.get("importance", 5))
    except (TypeError, ValueError):
        importance = 5
    importance = max(1, min(importance, 10))
    deps_raw = data.get("depends_on") or []
    depends_on: list[int] = []
    if isinstance(deps_raw, list):
        for d in deps_raw:
            try:
                depends_on.append(int(d))
            except (TypeError, ValueError):
                continue
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    # Confirm the chain exists before mutating — otherwise enqueue_task
    # would silently insert a row with a dangling chain_id.
    if runner.memory.get_chain(cid) is None:
        return jsonify({"error": "chain not found"}), 404
    tid = runner.memory.enqueue_task(
        cid, goal,
        importance=importance,
        depends_on=depends_on,
    )
    if tid is None:
        return jsonify({"error": "enqueue_task failed"}), 500
    # Rescore the chain so the new task takes its rightful place in
    # the priority queue (and so the UI gets a fresh task_priority
    # event reflecting the addition).
    try:
        runner.recompute_priorities(cid)
    except Exception:
        pass
    runner._emit("task_created", {
        "chain_id": cid, "task_id": tid,
        "goal": goal[:200],
        "importance": importance,
        "depends_on": depends_on,
        "via": "manual",
    })
    return jsonify({"ok": True, "task_id": tid,
                    "chain_id": cid, "goal": goal,
                    "importance": importance,
                    "depends_on": depends_on})


@app.route("/api/chains/<int:cid>/recompute-priorities",
           methods=["POST"])
def api_chain_recompute_priorities(cid):
    """Re-score every pending task on the chain. Useful after a
    dependency change or when historical-success numbers have shifted
    significantly under other chains."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    if runner.memory.get_chain(cid) is None:
        return jsonify({"error": "chain not found"}), 404
    try:
        scored = runner.recompute_priorities(cid)
    except Exception as e:
        return jsonify({"error": f"recompute failed: {e}"}), 500
    return jsonify({"ok": True, "chain_id": cid,
                    "scored": scored,
                    "count": len(scored)})


@app.route("/api/chains")
def api_chain_list():
    """List recent chains (most-recent-first) with their progress."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"chains": [], "warn": "chain runner unavailable"})
    return jsonify({"chains": runner.memory.list_chains(limit=100)})


@app.route("/api/chains/<int:cid>")
def api_chain_get(cid):
    """Full chain detail: chain row + all tasks + progress histogram."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    detail = runner.memory.get_chain(cid)
    if detail is None:
        return jsonify({"error": "chain not found"}), 404
    detail["progress"] = runner.memory.chain_progress(cid)
    return jsonify(detail)


@app.route("/api/chains/<int:cid>/run-next", methods=["POST"])
def api_chain_run_next(cid):
    """Pop the next pending task and enqueue it as a session. Does NOT
    block — the session runs through the normal subprocess worker and
    its completion hook (in run_session) marks the chain task done."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    row = runner.memory.next_pending_task(cid)
    if row is None:
        return jsonify({"ok": True, "ran": False,
                        "reason": "nothing pending",
                        "progress": runner.memory.chain_progress(cid)})

    # ── Phase 18 parity with TaskChainRunner.run_next ──
    # The subprocess execution path (this endpoint) was bypassing
    # priority recompute, adaptive skip, and retry-augmented goal.
    # Replicate the runner's logic here so the web pipeline gets the
    # same self-management behaviour.
    from task_chains import Task, COMPLETED, SKIPPED, PENDING
    task_obj = Task.from_row(row)
    detail = runner.memory.get_chain(cid) or {"tasks": []}
    terminal_ids = {t["id"] for t in detail["tasks"]
                    if t["status"] in (COMPLETED, SKIPPED)}
    new_pri = runner._compute_priority(task_obj, terminal_ids)
    if abs(new_pri - task_obj.priority) > 1e-6:
        runner.memory.set_task_priority(task_obj.id, new_pri)
        task_obj.priority = new_pri
    runner._emit("task_priority", {
        "chain_id": cid, "task_id": task_obj.id,
        "priority": round(new_pri, 4),
        "importance": task_obj.importance,
        "reason": "pre-run recompute (web)",
    })

    # Adaptive skip — drop low-value tasks only when better
    # alternatives are still pending.
    if new_pri < runner.skip_priority_threshold:
        other_pending = [t for t in detail["tasks"]
                         if t["status"] == PENDING
                         and t["id"] != task_obj.id
                         and float(t.get("priority", 0.0))
                             >= runner.skip_priority_threshold]
        if other_pending:
            skip_reason = (f"priority {new_pri:.3f} < threshold "
                           f"{runner.skip_priority_threshold} with "
                           f"{len(other_pending)} better alternatives")
            runner.memory.set_task_skipped(task_obj.id, skip_reason)
            runner._emit("task_priority", {
                "chain_id": cid, "task_id": task_obj.id,
                "skipped": True, "priority": round(new_pri, 4),
                "reason": skip_reason,
            })
            return jsonify({"ok": True, "ran": False,
                            "skipped": True,
                            "task_id": task_obj.id,
                            "reason": skip_reason})

    # Per-task attempt cap (same as in-process runner).
    if task_obj.attempts >= runner.max_attempts_per_task:
        runner.memory.mark_task_done(
            task_obj.id, ok=False,
            reason=f"attempts exhausted "
                   f"({runner.max_attempts_per_task})")
        # Give the budget-capped replan a chance.
        try:
            runner.maybe_replan_after_failure(cid, task_obj.id)
        except Exception:
            pass
        return jsonify({"ok": True, "ran": False,
                        "reason": "attempts exhausted",
                        "task_id": task_obj.id})

    if not runner.memory.mark_task_running(task_obj.id):
        return jsonify({"ok": False,
                        "error": "lost race for task"}), 409
    attempt_no = task_obj.attempts + 1   # post-bump

    # Retry-with-feedback — if this is a retry, augment the goal.
    run_goal = task_obj.goal
    if task_obj.attempts > 0 and task_obj.last_error:
        run_goal = runner._augment_goal_for_retry(
            task_obj.goal, task_obj.last_error)
        runner._emit("task_retry", {
            "chain_id": cid, "task_id": task_obj.id,
            "attempt": attempt_no,
            "previous_error": task_obj.last_error[:200],
            "augmented_goal_preview": run_goal[:200],
        })

    # Hand off to the existing subprocess worker. The session is tagged
    # with chain_id+chain_task_id so run_session's terminal hook closes
    # the loop by marking the chain task done.
    cfg = get_setting("default_config", default_managed_config())
    cfg_ok, err, _ = validate_config(cfg)
    if not cfg_ok:
        # Roll the running task back to pending so the user can retry
        # after fixing config — otherwise it'd be stuck mid-state.
        runner.memory._db.execute(
            "UPDATE chain_tasks SET status='pending', "
            "attempts=attempts-1 WHERE id=?", (task_obj.id,))
        runner.memory._db.commit()
        return jsonify({"error": f"Stored config invalid: {err}"}), 400

    sid = enqueue_task(run_goal, None, cfg,
                       chain_id=cid, chain_task_id=task_obj.id)
    runner._emit("task_progress", {
        "chain_id": cid, "task_id": task_obj.id,
        "status": "running", "session_id": sid,
        "attempt": attempt_no,
        "priority": round(new_pri, 4),
        "goal": task_obj.goal[:200],
    })
    return jsonify({"ok": True, "ran": True,
                    "task_id": task_obj.id, "session_id": sid,
                    "attempt": attempt_no,
                    "priority": round(new_pri, 4),
                    "goal": task_obj.goal})


@app.route("/api/chains/<int:cid>/resume", methods=["POST"])
def api_chain_resume(cid):
    """Demote any task stuck in 'running' (because the previous server
    died mid-task) back to 'pending' and kick off the next one."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    n_reset = runner.memory.reset_stuck_running(cid)
    # Auto-trigger run-next so the user only has to call resume once.
    next_resp = api_chain_run_next(cid)
    body = next_resp.get_json() if hasattr(next_resp, "get_json") \
        else next_resp[0].get_json()
    body["reset_running"] = n_reset
    return jsonify(body)


# ────────────────────────────────────────────────────────────────────
# Phase 19 — Self-driven goals API surface
# ────────────────────────────────────────────────────────────────────
@app.route("/api/goals", methods=["GET"])
def api_goals_list():
    """List recent system_generated chains (the engine's output) along
    with engine status. Read-only convenience endpoint for the UI."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    chains = runner.memory.list_chains(
        limit=max(1, min(limit, 200)),
        system_generated=True)
    eng = _get_goal_engine()
    return jsonify({
        "ok": True,
        "engine_available": eng is not None,
        "engine_init_error": _goal_init_error,
        "status":  eng.status() if eng else None,
        "chains":  chains,
    })


@app.route("/api/goals/events", methods=["GET"])
def api_goals_events():
    """Polling endpoint for goal lifecycle events. Pass ?since=N to
    get only events with id > N. Returns oldest-first so the client
    can append directly without re-sorting."""
    runner = _get_chain_runner()
    if runner is None:
        return jsonify({"error": "chain runner unavailable"}), 503
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    events = runner.memory.list_goal_events(
        since_id=max(0, since),
        limit=max(1, min(limit, 1000)))
    last_id = events[-1]["id"] if events else since
    return jsonify({"ok": True, "events": events, "last_id": last_id})


@app.route("/api/goals/toggle", methods=["POST"])
def api_goals_toggle():
    """Quick on/off without going through the full /api/config flow.
    Body (optional): {enabled: bool}. If omitted, flips the current
    value. Persists into the same `default_config` blob the rest of
    the app uses, so a page reload reflects it."""
    data = request.get_json(silent=True) or {}
    cfg = get_setting("default_config", default_managed_config()) \
          or default_managed_config()
    ag = dict(cfg.get("auto_goals")
              or default_managed_config()["auto_goals"])
    if "enabled" in data:
        ag["enabled"] = bool(data["enabled"])
    else:
        ag["enabled"] = not bool(ag.get("enabled", False))
    cfg["auto_goals"] = ag
    # Re-validate so any drift gets sanitised.
    ok, err, normalised = validate_config(cfg)
    if not ok:
        return jsonify({"error": err or "invalid config"}), 400
    set_setting("default_config", normalised)
    return jsonify({
        "ok": True,
        "auto_goals": normalised["auto_goals"],
    })


@app.route("/api/goals/run-now", methods=["POST"])
def api_goals_run_now():
    """Force a single analysis cycle, bypassing cooldown. Daily cap +
    confidence threshold + loop guard still apply. Returns the
    structured result dict from the engine so the user can see why
    nothing was generated when nothing meets the bar."""
    eng = _get_goal_engine()
    if eng is None:
        return jsonify({"error":
                        "goal engine unavailable",
                        "init_error": _goal_init_error}), 503
    # Phase 19 — use the lock-protected force_run_once entry point so
    # concurrent /run-now callers can't race the regular trigger and
    # exceed the daily cap. Cooldown is bypassed for this single
    # cycle; everything else (cap / confidence / loop guard) holds.
    result = eng.force_run_once()
    return jsonify({"ok": True, "result": result,
                    "status": eng.status()})


@app.route("/api/queue-task", methods=["POST"])
def api_queue_task():
    data = request.get_json() or {}
    task = (data.get("task") or "").strip()
    model = data.get("model") or None
    plan_mode = (data.get("plan_mode") or "elite").lower()
    if plan_mode not in PLAN_CAPABILITIES:
        plan_mode = "elite"
    if not task:
        return jsonify({"error": "Task text required"}), 400

    # ── Phase 8: Subscription plan gate ──────────────────────────────────────
    try:
        _p8_state = p8_get_state()
        _p8_plan  = p8_effective_plan(_p8_state)
        _p8_ok, _p8_reason = p8_check_and_increment(plan_mode, _p8_plan, _p8_state)
        if not _p8_ok:
            return jsonify({"error": _p8_reason, "plan_gate": True,
                            "current_plan": _p8_plan,
                            "upgrade_needed": True}), 403
    except Exception:
        pass  # Never let billing logic break task submission

    cfg = get_setting("default_config", default_managed_config())
    ok, err, _ = validate_config(cfg)
    if not ok:
        return jsonify({"error": f"Stored config invalid: {err}. Open settings to fix."}), 400

    # Inject plan mode into session config
    cfg["plan_mode"] = plan_mode

    # Managed-mode rate limiting
    if cfg.get("mode") == "managed":
        if len(pending_queue) >= MANAGED_LIMITS["max_pending_in_queue"]:
            return jsonify({"error": "Managed queue is full. Try again shortly."}), 429
        cutoff = time.time() - MANAGED_LIMITS["rate_window_seconds"]
        recent = sum(1 for ts in managed_runs if ts >= cutoff)
        if recent >= MANAGED_LIMITS["max_tasks_per_window"]:
            return jsonify({
                "error": f"Managed-mode rate limit reached "
                         f"({MANAGED_LIMITS['max_tasks_per_window']}/"
                         f"{MANAGED_LIMITS['rate_window_seconds']}s). "
                         "Switch to BYOK or wait."}), 429

    sid = enqueue_task(task, model, cfg)
    caps = PLAN_CAPABILITIES[plan_mode]
    return jsonify({"ok": True, "session_id": sid, "plan_mode": plan_mode,
                    "plan_label": caps["label"], "max_steps": caps["max_steps"]})


@app.route("/api/plan-config")
def api_plan_config():
    """Return available plan modes and their capabilities."""
    return jsonify({"plans": PLAN_CAPABILITIES})


@app.route("/api/queue")
def api_queue():
    with queue_lock:
        return jsonify({
            "running": running.get("sid"),
            "pending": list(pending_queue),
        })


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"sessions": db_sessions()})


@app.route("/api/session/<sid>")
def api_session(sid):
    s = db_session(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    s["files"] = json.loads(s.get("files_json") or "[]")
    s.pop("files_json", None)
    s["config"] = public_config(json.loads(s.get("config_json") or "{}"))
    s.pop("config_json", None)
    s["usage"] = json.loads(s.get("usage_json") or "{}") or None
    s.pop("usage_json", None)
    s["is_running"] = (running.get("sid") == sid)
    s["is_queued"] = sid in pending_queue
    # Phase 21.1 polish — derive a friendly project name from the task prompt
    # so the UI header can show "Project: flask-login" instead of just the sid.
    s["name"] = _derive_project_name(s.get("task") or "")
    return jsonify(s)


@app.route("/api/session/<sid>", methods=["DELETE"])
def api_session_delete(sid):
    if running.get("sid") == sid:
        return jsonify({"error": "Stop the session before deleting"}), 409
    with queue_lock:
        if sid in pending_queue:
            pending_queue.remove(sid)
    db_delete_session(sid)
    return jsonify({"ok": True})


@app.route("/api/session/<sid>/stop", methods=["POST"])
def api_session_stop(sid):
    with queue_lock:
        if sid in pending_queue:
            pending_queue.remove(sid)
            db_update_session(sid, status="cancelled", finished_at=time.time())
            return jsonify({"ok": True, "message": "Removed from queue"})
    ok, msg = stop_running_session(sid)
    code = 200 if ok else 400
    return jsonify({"ok": ok, "message": msg}), code


@app.route("/api/session/<sid>/restart", methods=["POST"])
def api_session_restart(sid):
    s = db_session(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    cfg = json.loads(s.get("config_json") or "{}") or get_setting(
        "default_config", default_managed_config())
    new_sid = enqueue_task(s["task"], s["model"], cfg)
    return jsonify({"ok": True, "session_id": new_sid})


# ────────────────────────────────────────────────────────────────────────────
# Routes — Logs / Decisions / Memory
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    sid = request.args.get("session_id")
    if not sid: return jsonify({"error": "session_id required"}), 400

    before = request.args.get("before")
    if before:
        try: before = int(before)
        except ValueError: before = 0
        if before > 0:
            logs = db_logs_older(sid, before, 200)
            return jsonify({"logs": logs})

    try: since = int(request.args.get("since", 0))
    except ValueError: since = 0
    logs = db_logs(sid, since, 200)
    last = logs[-1]["seq"] if logs else since
    return jsonify({"logs": logs, "last_seq": last})


# Real terminal statuses written by run_session() — reused by both
# the SSE close-condition and any future client-visible "is finished"
# checks. Do NOT include "running" / "queued" here.
TERMINAL_STATUSES = {"success", "failed", "stopped", "cancelled"}


@app.route("/api/session/<sid>/stream")
def api_session_stream(sid):
    """Phase 13 — Server-Sent Events for one session.

    Tails new log rows in a tight loop, yielding `data: {...}\\n\\n`
    per event so the browser's EventSource can append in real time.
    Sends a heartbeat comment every 15s so proxies don't drop the
    connection. Closes when the session is in a terminal state AND no
    new logs have arrived for ~2s. The existing /api/logs polling
    loop remains as a fallback if SSE drops mid-stream.
    """
    # Reject unknown sessions up-front so a forged sid can't keep an
    # SSE generator running indefinitely.
    sess0 = db_session(sid)
    if not sess0:
        return jsonify({"error": "session not found"}), 404

    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0

    def _event_stream():
        last_seq    = since
        last_event  = time.time()
        idle_after_finish = 0.0
        # Tell the client what cursor we resumed from (lets the JS
        # de-dup against what it has from a previous polling pass).
        yield f"event: hello\ndata: {json.dumps({'since': since})}\n\n"
        while True:
            try:
                rows = db_logs(sid, last_seq, limit=500)
            except Exception as e:
                yield (f"event: error\ndata: "
                       f"{json.dumps({'error': str(e)})}\n\n")
                return
            if rows:
                for row in rows:
                    yield f"data: {json.dumps(row)}\n\n"
                last_seq    = rows[-1]["seq"]
                last_event  = time.time()
                idle_after_finish = 0.0
            else:
                # Heartbeat every 15s to keep proxies happy. SSE
                # comments (`: ...`) are ignored by EventSource.
                if time.time() - last_event > 15:
                    yield ": ping\n\n"
                    last_event = time.time()

            # Has the session finished? If yes, give it a small grace
            # window so trailing logs flush, then close cleanly.
            try:
                sess = db_session(sid) or {}
            except Exception:
                sess = {}
            done = sess.get("status") in TERMINAL_STATUSES
            if done and not rows:
                idle_after_finish += 0.25
                if idle_after_finish >= 2.0:
                    yield (f"event: end\ndata: "
                           f"{json.dumps({'last_seq': last_seq, 'status': sess.get('status')})}\n\n")
                    return
            time.sleep(0.25)

    headers = {
        "Content-Type":      "text/event-stream",
        "Cache-Control":     "no-cache, no-transform",
        "X-Accel-Buffering": "no",        # nginx: don't buffer
        "Connection":        "keep-alive",
    }
    return Response(stream_with_context(_event_stream()),
                    headers=headers)


@app.route("/api/decisions")
def api_decisions():
    sid = request.args.get("session_id")
    if not sid: return jsonify({"error": "session_id required"}), 400
    return jsonify({"decisions": db_decisions(sid)})


@app.route("/api/memory")
def api_memory():
    info = {"learnings": [], "tasks": [], "snippets": [], "kv": {}}
    try:
        c = sqlite3.connect(os.path.join(BASE_DIR, "memory.db"))
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT category,insight,created_at FROM learnings "
                           "ORDER BY id DESC LIMIT 30"):
            info["learnings"].append(dict(r))
        for r in c.execute("SELECT task,status,api_used,created_at FROM tasks "
                           "ORDER BY id DESC LIMIT 20"):
            info["tasks"].append(dict(r))
        for r in c.execute("SELECT name,lang,used_count FROM snippets "
                           "ORDER BY used_count DESC LIMIT 20"):
            info["snippets"].append(dict(r))
        c.close()
    except Exception as e:
        info["memory_db_error"] = str(e)
    try:
        with open(os.path.join(BASE_DIR, "memory.json"), "r", encoding="utf-8") as f:
            jdata = json.load(f)
        kv = jdata.get("kv") or {}
        info["kv"] = {k: (str(v)[:200]) for k, v in list(kv.items())[:20]}
        info["message_count"] = len(jdata.get("messages") or [])
    except Exception:
        pass
    return jsonify(info)


# ────────────────────────────────────────────────────────────────────────────
# Routes — Workspace files, preview, and download (Phase 7.0 / 7.1)
# Each session has its own folder under /workspace/<sid>/. These endpoints
# all operate inside that folder with traversal + symlink guards.
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/files/<sid>")
def api_files(sid):
    """Return the full file tree for the session as a flat list. The UI
    builds the nested view client-side (cheaper than nesting on the server
    when the tree is small)."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    items = []
    truncated = False
    for rel, _full, size, mtime in _walk_session_files(sid):
        items.append({"path": rel, "size": size, "mtime": mtime})
    if len(items) >= _MAX_TREE_ENTRIES:
        truncated = True
    # Phase 21.1 R1 fix #2 — also surface directory paths so empty folders
    # created via /api/create-folder remain visible in the Files tab tree.
    # Cheap to compute; the cap is shared with the file walk.
    dirs = list(_walk_session_dirs(sid, max_entries=_MAX_TREE_ENTRIES))
    return jsonify({
        "ok": True,
        "sid": sid,
        "files": items,
        "dirs":  dirs,
        "count": len(items),
        "truncated": truncated,
    })


@app.route("/api/file/<sid>")
def api_file(sid):
    """Return one file's content. Text inline, binary as base64 (capped)."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    rel = (request.args.get("path") or "").strip().lstrip("/")
    if not rel:
        return jsonify({"ok": False, "error": "path_required"}), 400
    target = _safe_session_path(sid, rel)
    if not os.path.isfile(target):
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        size = os.path.getsize(target)
    except OSError:
        return jsonify({"ok": False, "error": "stat_failed"}), 500
    if size > _MAX_TEXT_BYTES:
        return jsonify({
            "ok": False, "error": "too_large", "size": size,
            "limit": _MAX_TEXT_BYTES,
        }), 413
    ext = os.path.splitext(target)[1].lower()
    is_text = ext in _TEXT_EXTS or _looks_text(target)
    try:
        if is_text:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            return jsonify({
                "ok": True, "path": rel, "encoding": "text",
                "content": content, "size": size, "ext": ext,
            })
        with open(target, "rb") as fh:
            content = base64.b64encode(fh.read()).decode("ascii")
        return jsonify({
            "ok": True, "path": rel, "encoding": "base64",
            "content": content, "size": size, "ext": ext,
        })
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────────────
# Phase 20.6 — Integrated Code Editor + AI interaction layer.
#
# Four additive endpoints that power the Monaco-based editor in the
# Files tab:
#   POST /api/save-file/<sid>    — persist edits to disk
#   POST /api/run-code/<sid>     — Phase 20.4 sandboxed runner
#   POST /api/run-tests/<sid>    — Phase 20.3 self-testing system
#   POST /api/code-action/<sid>  — LLM-driven Fix / Optimize / Explain
#                                   / Refactor (best-effort; falls back
#                                   to {ok:false, reason:"no_llm"} when
#                                   no provider is configured)
#
# All endpoints reuse the existing `_safe_session_path` guard, refuse
# binary blobs, and cap input/output sizes.  None of code_runner,
# code_testing, dev_loop, agent, router, or tools is modified.
# ─────────────────────────────────────────────────────────────────────

# Conservative caps for editor-driven runs.  Kept small on purpose so
# a stray browser click can never burn the host: callers that want
# longer runs should still go through the dev_loop / chains.
_EDITOR_RUN_TIMEOUT_SEC   = 8.0
_EDITOR_TEST_TIMEOUT_SEC  = 8.0
_EDITOR_RUN_MEM_MB        = 256
_EDITOR_RUN_OUTPUT_BYTES  = 256_000
_EDITOR_MAX_TESTS         = 8
_EDITOR_MAX_AI_INPUT      = 60_000   # max chars of code/selection sent to LLM

# Best-effort LLMRouter shared across editor AI calls.  Lazily built
# on first /api/code-action request; if no API key is configured the
# router constructor still succeeds but `chat()` raises — we catch
# that and return {ok:false, reason:"no_llm"} so the UI degrades
# gracefully rather than 500ing.
_editor_llm_router = None
_editor_llm_lock   = threading.Lock()


def _get_editor_llm():
    """Return a cached LLMRouter, or None if construction failed.

    A `False` sentinel is cached on init failure so we don't retry
    LLMRouter() on every request.  Both the cache hit branches and the
    return statement convert that sentinel back to `None` so callers
    only ever see a real router or `None` — no in-band falsy value.
    """
    global _editor_llm_router
    cached = _editor_llm_router
    if cached is not None:
        return cached if cached else None
    with _editor_llm_lock:
        cached = _editor_llm_router
        if cached is not None:
            return cached if cached else None
        try:
            from config import Config as _Cfg
            from router import LLMRouter as _Router
            _editor_llm_router = _Router(_Cfg())
        except Exception as e:
            print(f"[editor] LLMRouter init failed: {e}")
            _editor_llm_router = False  # sentinel: don't retry every call
        return _editor_llm_router or None


def _editor_log(event, **fields):
    """Lightweight observability hook (#8).  Writes a single structured
    line to stdout so the existing log pipeline picks it up.  We don't
    plug into the per-session SSE channel because editor actions are
    cross-session and don't need to be streamed live."""
    try:
        payload = {"event": event, "ts": int(time.time() * 1000)}
        payload.update(fields)
        print(f"[editor] {json.dumps(payload, default=str)[:1000]}")
    except Exception:
        pass


def _editor_read_text_file(sid, rel_path):
    """Read a session-scoped text file with all the same guards as
    /api/file.  Returns (content, error_dict).  On success error_dict
    is None; on failure content is None and error_dict is a JSON-able
    dict the caller should jsonify."""
    if not rel_path:
        return None, {"ok": False, "error": "path_required"}
    target = _safe_session_path(sid, rel_path)
    if not os.path.isfile(target):
        return None, {"ok": False, "error": "not_found"}
    try:
        size = os.path.getsize(target)
    except OSError:
        return None, {"ok": False, "error": "stat_failed"}
    if size > _MAX_TEXT_BYTES:
        return None, {"ok": False, "error": "too_large",
                      "size": size, "limit": _MAX_TEXT_BYTES}
    ext = os.path.splitext(target)[1].lower()
    is_text = ext in _TEXT_EXTS or _looks_text(target)
    if not is_text:
        return None, {"ok": False, "error": "binary"}
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(), None
    except OSError as e:
        return None, {"ok": False, "error": str(e)}


def _detect_function_in_source(source):
    """Pick a function name from the source.  Tries code_intel first
    (handles classes / async / decorators properly), falls back to a
    plain `def NAME(` regex.  Returns "" when nothing is found."""
    try:
        import tempfile as _tf
        from code_intel import analyze_file as _af
        with _tf.NamedTemporaryFile(
                "w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(source)
            tmp = fh.name
        try:
            info = _af(tmp) or {}
            for fn in info.get("functions") or []:
                name = (fn or {}).get("name") if isinstance(fn, dict) else fn
                if name:
                    return name
        finally:
            try: os.unlink(tmp)
            except OSError: pass
    except Exception:
        pass
    m = re.search(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
                  source or "", re.M)
    return m.group(1) if m else ""


# ─── Action prompts.  Kept short and uniform so the LLM returns
# predictable shapes the diff modal can handle.
_AI_SYSTEM = (
    "You are a careful senior Python engineer integrated into a code "
    "editor.  Reply with ONLY the requested artifact — no preamble, no "
    "trailing prose.  When asked to modify code, return the FULL "
    "updated source so the editor can diff against the original.  "
    "Preserve indentation and existing comments unless explicitly "
    "asked otherwise."
)

_AI_PROMPTS = {
    "fix": (
        "Fix any bugs, undefined names, or runtime errors in the code "
        "below.  Return the entire corrected file in a single ```python "
        "fenced block."
    ),
    "optimize": (
        "Optimize the code below for clarity and performance without "
        "changing its public API.  Return the entire updated file in a "
        "single ```python fenced block."
    ),
    "refactor": (
        "Refactor the code below to improve structure (extract helpers, "
        "remove duplication, tighten names).  Keep external behaviour "
        "identical.  Return the entire updated file in a single ```python "
        "fenced block."
    ),
    "explain": (
        "Explain what the code below does.  Cover: purpose, key "
        "functions, control flow, and any subtle behaviour.  Plain "
        "prose, no code blocks."
    ),
}

# Phase 23 -- map each editor action to a routing role so the unified
# router can pick the user-pinned model for that role.  "explain" is the
# only reasoning-style action; everything else writes code.
_AI_ACTION_ROLES = {
    "fix":      "coding",
    "optimize": "coding",
    "refactor": "coding",
    "explain":  "reasoning",
}


_FENCE_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_first_fence(text):
    """Pull the first ```python ... ``` block; fall back to whole text."""
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).rstrip()
    return text.strip()


@app.route("/api/save-file/<sid>", methods=["POST"])
def api_save_file(sid):
    """Persist editor changes back to disk.  Body: {path, content}."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    rel = (body.get("path") or "").strip().lstrip("/")
    content = body.get("content")
    if not rel:
        return jsonify({"ok": False, "error": "path_required"}), 400
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content_required"}), 400
    if len(content.encode("utf-8", errors="replace")) > _MAX_TEXT_BYTES:
        return jsonify({"ok": False, "error": "too_large",
                        "limit": _MAX_TEXT_BYTES}), 413
    target = _safe_session_path(sid, rel)
    # Refuse to silently create new files unless the parent dir exists
    # (caller usually saves over a file they just opened).
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        return jsonify({"ok": False, "error": "parent_missing"}), 400
    # Refuse to overwrite a binary blob with text — same rule the
    # reader uses, applied symmetrically.
    if os.path.isfile(target):
        ext = os.path.splitext(target)[1].lower()
        if ext not in _TEXT_EXTS and not _looks_text(target):
            return jsonify({"ok": False, "error": "binary"}), 400
    try:
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        size = os.path.getsize(target)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    _editor_log("editor_action", action="save", sid=sid,
                path=rel, size=size)
    return jsonify({"ok": True, "path": rel, "size": size,
                    "saved_at": int(time.time() * 1000)})


@app.route("/api/run-code/<sid>", methods=["POST"])
def api_run_code(sid):
    """Execute file (or supplied code) in the Phase 20.4 sandbox.
    Body: {path?, code?}.  Either source must be provided; when both
    are given `code` wins (unsaved buffer).  Returns the RunResult
    dict with the heavy `extra` field stripped to keep the wire small."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    rel = (body.get("path") or "").strip().lstrip("/")
    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        text, err = _editor_read_text_file(sid, rel)
        if err:
            return jsonify(err), 400
        code = text
    if len(code.encode("utf-8", errors="replace")) > _MAX_TEXT_BYTES:
        return jsonify({"ok": False, "error": "too_large",
                        "limit": _MAX_TEXT_BYTES}), 413
    try:
        from code_runner import run_code as _run_code
    except Exception as e:
        return jsonify({"ok": False, "error": "runner_unavailable",
                        "detail": str(e)}), 500
    try:
        rr = _run_code(
            code,
            timeout=_EDITOR_RUN_TIMEOUT_SEC,
            memory_limit_mb=_EDITOR_RUN_MEM_MB,
            max_output_bytes=_EDITOR_RUN_OUTPUT_BYTES,
        )
        out = rr.to_dict() if hasattr(rr, "to_dict") else dict(rr or {})
    except Exception as e:
        return jsonify({"ok": False, "error": "run_failed",
                        "detail": str(e)}), 500
    # Strip the result-file channel — editor doesn't use it and it
    # bloats the JSON.
    if isinstance(out.get("extra"), dict):
        out["extra"].pop("result_file", None)
    _editor_log("editor_action", action="run", sid=sid, path=rel,
                status=out.get("status"), exit=out.get("exit_code"))
    return jsonify({"ok": True, "result": out})


@app.route("/api/run-tests/<sid>", methods=["POST"])
def api_run_tests(sid):
    """Generate + run smoke/failure tests for a function via Phase
    20.3.  Body: {path?, code?, function_name?}.  When function_name
    is omitted we let code_intel pick the first def in the file."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    rel = (body.get("path") or "").strip().lstrip("/")
    code = body.get("code")
    fn   = (body.get("function_name") or "").strip() or None
    if not isinstance(code, str) or not code.strip():
        text, err = _editor_read_text_file(sid, rel)
        if err:
            return jsonify(err), 400
        code = text
    if len(code.encode("utf-8", errors="replace")) > _MAX_TEXT_BYTES:
        return jsonify({"ok": False, "error": "too_large",
                        "limit": _MAX_TEXT_BYTES}), 413
    if not fn:
        fn = _detect_function_in_source(code)
    if not fn:
        return jsonify({"ok": False, "error": "no_function",
                        "message": "Could not detect a top-level "
                                   "function to test."}), 400
    try:
        from code_testing import generate_and_run as _gar
    except Exception as e:
        return jsonify({"ok": False, "error": "tester_unavailable",
                        "detail": str(e)}), 500
    try:
        result = _gar(
            code, fn,
            timeout=_EDITOR_TEST_TIMEOUT_SEC,
            max_tests=_EDITOR_MAX_TESTS,
        )
    except Exception as e:
        return jsonify({"ok": False, "error": "tests_failed",
                        "detail": str(e)}), 500
    _editor_log("editor_action", action="tests", sid=sid, path=rel,
                fn=fn, passed=result.get("passed"),
                total=result.get("total_tests"))
    return jsonify({"ok": True, "function_name": fn, "result": result})


@app.route("/api/code-action/<sid>", methods=["POST"])
def api_code_action(sid):
    """Run an AI action (fix/optimize/explain/refactor) against the
    current buffer.  Body: {action, code, path?, selection?}.  Returns
    {ok, action, suggested_code?, explanation?, llm_used}.  Falls back
    to {ok:false, reason:"no_llm"} when no LLM provider is reachable
    so the UI can show a useful message instead of a stack trace."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()
    code   = body.get("code") or ""
    sel    = (body.get("selection") or "").strip()
    rel    = (body.get("path") or "").strip().lstrip("/")
    if action not in _AI_PROMPTS:
        return jsonify({"ok": False, "error": "bad_action",
                        "valid": list(_AI_PROMPTS.keys())}), 400
    if not isinstance(code, str) or not code.strip():
        return jsonify({"ok": False, "error": "code_required"}), 400
    # Truncate aggressively — large files overrun token budgets and
    # the editor is meant for focused edits, not whole-repo refactors.
    if len(code) > _EDITOR_MAX_AI_INPUT:
        code = code[:_EDITOR_MAX_AI_INPUT]
    if sel and len(sel) > _EDITOR_MAX_AI_INPUT:
        sel = sel[:_EDITOR_MAX_AI_INPUT]

    router = _get_editor_llm()
    if router is None:
        _editor_log("ai_suggestion", action=action, sid=sid, path=rel,
                    llm_used=False, reason="no_llm")
        return jsonify({"ok": False, "reason": "no_llm",
                        "message": "No LLM provider is configured. "
                                   "Add an API key in Settings."}), 200

    instruction = _AI_PROMPTS[action]
    user_msg_parts = [instruction]
    if rel:
        user_msg_parts.append(f"FILE: {rel}")
    if sel:
        user_msg_parts.append(
            f"SELECTED REGION (focus your changes here):\n```python\n"
            f"{sel}\n```")
    user_msg_parts.append(f"FULL FILE:\n```python\n{code}\n```")
    user = "\n\n".join(user_msg_parts)

    # Phase 23 -- pick the routing role from the action and dispatch via
    # `chat_role` when the router supports it.  Older router builds (or
    # the lazy-init "no LLM" sentinel) fall back to `router.chat()` so
    # we don't break the editor when the new method isn't there yet.
    role = _AI_ACTION_ROLES.get(action, "coding")
    try:
        if hasattr(router, "chat_role") and callable(router.chat_role):
            raw = router.chat_role(
                role,
                messages=[{"role": "user", "content": user}],
                system=_AI_SYSTEM,
                max_tokens=2048,
            )
            _routing_log("dispatch", role=role, action=action, sid=sid,
                         provider=getattr(router, "last_role_provider", None),
                         model=getattr(router, "last_role_model", None),
                         llm_used=True)
        else:
            raw = router.chat(
                messages=[{"role": "user", "content": user}],
                system=_AI_SYSTEM,
                max_tokens=2048,
            )
    except Exception as e:
        _editor_log("ai_suggestion", action=action, sid=sid, path=rel,
                    llm_used=False, reason="llm_error", detail=str(e)[:200])
        _routing_log("dispatch_error", role=role, action=action, sid=sid,
                     reason="llm_error", detail=str(e)[:200])
        return jsonify({"ok": False, "reason": "llm_error",
                        "message": str(e)[:300]}), 200

    raw = (raw or "").strip()
    if not raw:
        _editor_log("ai_suggestion", action=action, sid=sid, path=rel,
                    llm_used=True, reason="empty")
        return jsonify({"ok": False, "reason": "empty",
                        "message": "LLM returned no output."}), 200

    out = {"ok": True, "action": action, "llm_used": True}
    if action == "explain":
        out["explanation"] = raw
    else:
        out["suggested_code"] = _extract_first_fence(raw)
        # Always include the raw response too, in case the UI wants
        # to show notes the model added outside the fence.
        out["raw"] = raw

    # Phase 21.2 — attach the review-policy decision so the UI can
    # decide whether to skip the diff modal and auto-apply.  `explain`
    # produces no diff so the decision falls back to "request_review"
    # (the prose modal is the review).
    policy = _review_policy_load()
    if action == "explain" or not out.get("suggested_code"):
        diff_stats = {"lines_changed": 0, "hunks": 0}
    else:
        diff_stats = _classify_diff(code, out["suggested_code"])
    auto_apply, reason = _decide_auto_apply(action, diff_stats, policy)
    # `explain` never auto-applies — there's nothing to apply.
    if action == "explain":
        auto_apply, reason = False, "explain_no_diff"
    out["policy_decision"] = {
        "auto_apply": bool(auto_apply),
        "reason":     reason,
        "diff_stats": diff_stats,
        "mode":       policy.get("mode"),
    }
    _editor_log("ai_suggestion", action=action, sid=sid, path=rel,
                llm_used=True, chars=len(raw),
                auto_apply=bool(auto_apply), policy_reason=reason,
                lines_changed=diff_stats["lines_changed"],
                hunks=diff_stats["hunks"])
    return jsonify(out)


# ─────────────────────────────────────────────────────────────────────
# Phase 21.1 — File system management endpoints.
#
# Four additive endpoints for the Files-tab toolbar (New File, New
# Folder, Rename, Delete).  All endpoints reuse `_safe_session_path`,
# refuse traversal/symlinks, validate path components, and emit
# structured `[editor]` log lines.  Path-only operations (no large
# blobs) so caps stay light; `create-file` accepts an optional initial
# `content` and reuses `_MAX_TEXT_BYTES`.
# ─────────────────────────────────────────────────────────────────────

# Hard caps to keep the workspace tidy and prevent abuse via the UI.
_FS_MAX_PATH_LEN     = 512
_FS_MAX_NAME_LEN     = 255
_FS_MAX_DEPTH        = 12
# Components we always reject — symlink-skipping is enforced by
# _safe_session_path; this is a UI-level guard for clarity in errors.
_FS_FORBIDDEN_NAMES  = {"", ".", ".."}


def _validate_fs_relpath(rel):
    """Pre-flight check for a user-supplied rel path.  Returns (ok, err).

    Lets `_safe_session_path` enforce the actual workspace boundary;
    this helper just rejects obviously malformed inputs early so the
    UI gets a clean error code instead of a bare 403.
    """
    if not isinstance(rel, str):
        return False, "path_required"
    rel = rel.strip().lstrip("/")
    if not rel:
        return False, "path_required"
    if len(rel) > _FS_MAX_PATH_LEN:
        return False, "path_too_long"
    parts = rel.split("/")
    if len(parts) > _FS_MAX_DEPTH:
        return False, "path_too_deep"
    for p in parts:
        if p in _FS_FORBIDDEN_NAMES:
            return False, "bad_path"
        if len(p) > _FS_MAX_NAME_LEN:
            return False, "name_too_long"
        # Reject NULs and control chars — Linux allows most names but
        # the editor toolbar should not.
        if any(ord(c) < 32 or c == "\x7f" for c in p):
            return False, "bad_path"
        # Reject Windows-reserved chars too so the workspace stays
        # portable when zipped + downloaded.
        if any(c in p for c in '<>:"\\|?*'):
            return False, "bad_path"
    return True, rel


@app.route("/api/create-file/<sid>", methods=["POST"])
def api_create_file(sid):
    """Create a new (empty by default) file at <sid>/<path>."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    ok, rel_or_err = _validate_fs_relpath(data.get("path"))
    if not ok:
        return jsonify({"ok": False, "error": rel_or_err}), 400
    rel = rel_or_err
    content = data.get("content") or ""
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "bad_content"}), 400
    if len(content.encode("utf-8", errors="replace")) > _MAX_TEXT_BYTES:
        return jsonify({"ok": False, "error": "too_large",
                        "limit": _MAX_TEXT_BYTES}), 413

    target = _safe_session_path(sid, rel)
    if os.path.exists(target):
        return jsonify({"ok": False, "error": "exists"}), 409
    parent = os.path.dirname(target)
    try:
        os.makedirs(parent, exist_ok=True)
        # O_EXCL closes a TOCTOU window between the os.path.exists
        # check above and the actual write.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(target, flags, 0o644)
        try:
            if content:
                os.write(fd, content.encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
    except FileExistsError:
        return jsonify({"ok": False, "error": "exists"}), 409
    except OSError as e:
        return jsonify({"ok": False, "error": "write_failed",
                        "message": str(e)[:200]}), 500

    size = os.path.getsize(target)
    _editor_log("editor_action", action="create_file", sid=sid,
                path=rel, size=size)
    return jsonify({"ok": True, "path": rel, "size": size})


@app.route("/api/create-folder/<sid>", methods=["POST"])
def api_create_folder(sid):
    """Create an empty folder at <sid>/<path>."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    ok, rel_or_err = _validate_fs_relpath(data.get("path"))
    if not ok:
        return jsonify({"ok": False, "error": rel_or_err}), 400
    rel = rel_or_err
    target = _safe_session_path(sid, rel)
    if os.path.exists(target):
        return jsonify({"ok": False, "error": "exists"}), 409
    try:
        os.makedirs(target, exist_ok=False)
    except FileExistsError:
        return jsonify({"ok": False, "error": "exists"}), 409
    except OSError as e:
        return jsonify({"ok": False, "error": "mkdir_failed",
                        "message": str(e)[:200]}), 500

    _editor_log("editor_action", action="create_folder",
                sid=sid, path=rel)
    return jsonify({"ok": True, "path": rel})


@app.route("/api/rename-file/<sid>", methods=["POST"])
def api_rename_file(sid):
    """Rename or move a file/folder within the session workspace."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    ok_o, old_or_err = _validate_fs_relpath(data.get("old_path"))
    if not ok_o:
        return jsonify({"ok": False, "error": "bad_old_path",
                        "detail": old_or_err}), 400
    ok_n, new_or_err = _validate_fs_relpath(data.get("new_path"))
    if not ok_n:
        return jsonify({"ok": False, "error": "bad_new_path",
                        "detail": new_or_err}), 400
    old_rel, new_rel = old_or_err, new_or_err
    if old_rel == new_rel:
        return jsonify({"ok": False, "error": "noop"}), 400

    src = _safe_session_path(sid, old_rel)
    dst = _safe_session_path(sid, new_rel)
    # Both endpoints already reject symlinks/traversal; we only need
    # to verify the source actually exists and the destination doesn't.
    if not os.path.exists(src):
        return jsonify({"ok": False, "error": "src_not_found"}), 404
    if os.path.exists(dst):
        return jsonify({"ok": False, "error": "dst_exists"}), 409
    parent = os.path.dirname(dst)
    try:
        os.makedirs(parent, exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        return jsonify({"ok": False, "error": "rename_failed",
                        "message": str(e)[:200]}), 500

    is_dir = os.path.isdir(dst)
    _editor_log("editor_action", action="rename", sid=sid,
                old_path=old_rel, new_path=new_rel, is_dir=is_dir)
    return jsonify({"ok": True, "old_path": old_rel,
                    "new_path": new_rel, "is_dir": is_dir})


@app.route("/api/delete-file/<sid>", methods=["POST"])
def api_delete_file(sid):
    """Delete a file or (recursively) a folder under the session ws."""
    if not db_session(sid):
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    ok, rel_or_err = _validate_fs_relpath(data.get("path"))
    if not ok:
        return jsonify({"ok": False, "error": rel_or_err}), 400
    rel = rel_or_err
    target = _safe_session_path(sid, rel)
    if not os.path.exists(target):
        return jsonify({"ok": False, "error": "not_found"}), 404
    # Don't allow deleting the workspace root itself (already blocked
    # by _safe_session_path rejecting empty rel, but doubly explicit).
    real_ws = os.path.realpath(session_workspace(sid))
    if os.path.realpath(target) == real_ws:
        return jsonify({"ok": False, "error": "refuse_root"}), 400

    is_dir = os.path.isdir(target)
    try:
        if is_dir:
            # shutil.rmtree follows the workspace boundary because
            # _safe_session_path already rejected symlinked components
            # along the path; rmtree itself does not follow symlinks
            # by default on POSIX.
            shutil.rmtree(target)
        else:
            os.remove(target)
    except OSError as e:
        return jsonify({"ok": False, "error": "delete_failed",
                        "message": str(e)[:200]}), 500

    _editor_log("editor_action", action="delete", sid=sid,
                path=rel, is_dir=is_dir)
    return jsonify({"ok": True, "path": rel, "is_dir": is_dir})


@app.route("/api/download/<sid>")
def api_download(sid):
    """Stream a zip of the entire session workspace."""
    if not db_session(sid):
        abort(404)
    ws = session_workspace(sid)
    files = list(_walk_session_files(sid))
    if not files:
        return jsonify({"ok": False, "error": "empty"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, full, _size, _mtime in files:
            try:
                zf.write(full, arcname=rel)
            except OSError:
                continue
    buf.seek(0)
    name = f"project-{sid[:8]}.zip"
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name=name)


# ── Live preview ─────────────────────────────────────────────────────────────
# /preview/<sid>/         → serves index.html (or first .html, or empty page)
# /preview/<sid>/<path>   → serves any file from that session ws

_EMPTY_PREVIEW_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>No preview yet</title>
<style>
  html,body{margin:0;height:100%;background:#0d1117;color:#8b949e;
            font-family:system-ui,sans-serif;display:grid;place-items:center;}
  .box{text-align:center;padding:24px 32px;border:1px dashed #30363d;border-radius:10px;}
  h2{margin:0 0 8px;color:#c9d1d9;font-size:16px;font-weight:600;}
  p{margin:0;font-size:13px;}
</style></head>
<body><div class="box"><h2>No preview yet</h2>
<p>The agent hasn't written any HTML files for this session.</p></div></body></html>"""


def _pick_index_html(ws):
    """Return the relpath of the best 'index' file to serve, or None."""
    # 1. prefer ./index.html
    candidate = os.path.join(ws, "index.html")
    if os.path.isfile(candidate) and not os.path.islink(candidate):
        return "index.html"
    # 2. fall back to the first .html anywhere (shallowest first)
    best = None
    best_depth = 99
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if not d.startswith(".") and
                   d not in {"__pycache__", "node_modules", ".git"}]
        depth = len(os.path.relpath(root, ws).split(os.sep)) if root != ws else 0
        for f in sorted(files):
            if f.lower().endswith((".html", ".htm")) and depth < best_depth:
                best = os.path.relpath(os.path.join(root, f), ws)
                best_depth = depth
                break
    return best


@app.route("/preview/<sid>/")
@app.route("/preview/<sid>/<path:fpath>")
def preview_session(sid, fpath=""):
    if not db_session(sid):
        abort(404)
    ws = session_workspace(sid)
    real_ws = os.path.realpath(ws)
    if not fpath:
        idx = _pick_index_html(ws)
        if idx is None:
            return _EMPTY_PREVIEW_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}
        fpath = idx
    target = _safe_session_path(sid, fpath)
    if not os.path.isfile(target):
        abort(404)
    return send_from_directory(real_ws, os.path.relpath(target, real_ws))


# ── Legacy aliases (kept so old saved sessions/links still work) ─────────────
# `/api/preview/<sid>` used to return {files, html_files}. Now scans the
# per-session ws directly rather than the old shared-root snapshot.
@app.route("/api/preview/<sid>")
def api_preview(sid):
    if not db_session(sid):
        return jsonify({"error": "Not found"}), 404
    files = []
    # `version` is a fast aggregate digest of (path,size,mtime) tuples so the
    # client can detect content edits — not just path-set changes — with one
    # cheap string compare per poll.
    h = hashlib.md5()
    for rel, _full, size, mtime in _walk_session_files(sid):
        files.append(rel)
        h.update(f"{rel}|{size}|{mtime}\n".encode("utf-8"))
    html = [f for f in files if f.lower().endswith((".html", ".htm"))]
    return jsonify({
        "files": files,
        "html_files": html,
        "count": len(files),
        "version": h.hexdigest(),
    })


@app.route("/preview-file/<sid>/<path:fpath>")
def preview_file(sid, fpath):
    """Legacy alias of /preview/<sid>/<path>."""
    return preview_session(sid, fpath)


# ────────────────────────────────────────────────────────────────────────────
# Routes — Config / utility
# ────────────────────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    """Quick API-keys snapshot used by the Run tab status panel."""
    apis = {p: bool(os.getenv(meta["key_env"])) if meta["key_env"] else False
            for p, meta in PROVIDERS.items()}
    return jsonify({"apis": apis})


# ── Phase 21.1 polish — Browser host allowlist (Settings → Advanced).
# Foundation for Phase 21.3's SSRF-guarded Ollama base_url.
@app.route("/api/browser-allowlist", methods=["GET"])
def api_browser_allowlist_get():
    with _BROWSER_LOCK:
        return jsonify({"hosts": _browser_allowlist_load(),
                        "default": list(_BROWSER_ALLOWLIST_DEFAULT)})


@app.route("/api/browser-allowlist", methods=["POST"])
def api_browser_allowlist_add():
    data = request.get_json(silent=True) or {}
    raw = data.get("host") or ""
    host = _browser_allowlist_normalize(str(raw))
    if not _browser_allowlist_valid(host):
        return jsonify({"ok": False, "error": "Invalid host"}), 400
    with _BROWSER_LOCK:
        items = _browser_allowlist_load()
        if host in items:
            return jsonify({"ok": False, "error": "Already in allowlist",
                            "hosts": items}), 409
        items.append(host)
        _browser_allowlist_save(items)
        return jsonify({"ok": True, "hosts": items, "added": host})


@app.route("/api/browser-allowlist", methods=["DELETE"])
def api_browser_allowlist_remove():
    data = request.get_json(silent=True) or {}
    raw = data.get("host") or ""
    host = _browser_allowlist_normalize(str(raw))
    if not host:
        return jsonify({"ok": False, "error": "Missing host"}), 400
    with _BROWSER_LOCK:
        items = _browser_allowlist_load()
        if host not in items:
            return jsonify({"ok": False, "error": "Not in allowlist",
                            "hosts": items}), 404
        items = [h for h in items if h != host]
        _browser_allowlist_save(items)
        return jsonify({"ok": True, "hosts": items, "removed": host})


# ── Phase 21.2 — Editor review-policy endpoints ────────────────────────
# GET returns the current policy + the (project) defaults so the
# Settings UI can show "(default)" hints next to each control.  POST
# replaces the whole policy in one shot — the client always sends the
# full normalized object, so there's no merge-with-existing footgun.

@app.route("/api/review-policy", methods=["GET"])
def api_review_policy_get():
    from config import Config as _C
    with _REVIEW_LOCK:
        return jsonify({
            "ok":       True,
            "policy":   _review_policy_load(),
            "defaults": _review_policy_defaults(),
            "modes":    list(_C.REVIEW_MODES),
            "valid_actions": sorted(_REVIEW_VALID_ACTIONS),
            "caps": {"max_lines": _REVIEW_MAX_LINES_CAP,
                     "max_hunks": _REVIEW_MAX_HUNKS_CAP},
        })


@app.route("/api/review-policy", methods=["POST"])
def api_review_policy_set():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "object_required"}), 400
    with _REVIEW_LOCK:
        try:
            saved = _review_policy_save(data)
        except OSError as e:
            return jsonify({"ok": False, "error": "save_failed",
                            "detail": str(e)}), 500
    _editor_log("review_policy_update", **saved)
    return jsonify({"ok": True, "policy": saved})


@app.route("/api/clear-memory", methods=["POST"])
def api_clear_memory():
    if running.get("sid"):
        return jsonify({"ok": False, "message": "Stop the running task first"}), 409
    try:
        result = subprocess.run(
            [sys.executable, "main.py", "--clear"],
            capture_output=True, text=True, timeout=10
        )
        return jsonify({"ok": True, "message": (result.stdout + result.stderr).strip()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ════════════════════════════════════════════════════════════════════════════
# Phase 22 — AI Developer Platform Extensions
# ════════════════════════════════════════════════════════════════════════════
# 9 new endpoints layered on top of the existing Phase 20/21 surface.  Every
# endpoint is read-only OR side-effect-bounded (terminal exec is sandboxed
# via subprocess + RLIMIT + token-blocklist; ollama calls go through an
# allowlist-guarded HTTP path).  Nothing here mutates code_intel /
# code_learning / code_runner / dev_loop / agent / tools modules.
from config import Config as _P22Config

# ── Phase 22 — Model routing persistence ──────────────────────────────────
_ROUTING_PATH = os.path.join("data", "model_routing.json")
_ROUTING_LOCK = threading.Lock()

def _routing_defaults() -> dict:
    return {
        "provider":  _P22Config.ROUTING_DEFAULT_PROVIDER,
        "providers": {
            p: {"planner_model": "", "coding_model": "", "reasoning_model": ""}
            for p in _P22Config.ROUTING_PROVIDERS if p != "auto"
        }
    }

def _routing_normalize(raw) -> dict:
    d = _routing_defaults()
    if not isinstance(raw, dict):
        return d
        
    prov = str(raw.get("provider", "")).strip().lower()
    if prov in _P22Config.ROUTING_PROVIDERS:
        d["provider"] = prov
        
    # Process per-provider definitions
    raw_providers = raw.get("providers", {})
    if isinstance(raw_providers, dict):
        for p in _P22Config.ROUTING_PROVIDERS:
            if p == "auto" or p not in raw_providers: continue
            prov_dict = raw_providers[p]
            if not isinstance(prov_dict, dict): continue
            
            for role in _P22Config.ROUTING_ROLES:
                v = prov_dict.get(role + "_model") or prov_dict.get(role)
                if isinstance(v, str) and len(v) <= 80 and re.match(r"^[A-Za-z0-9_./:\-]*$", v):
                    d["providers"][p][role + "_model"] = v.strip()

    # Fallback/Backward compat: if flat keys are sent, put them in the selected provider
    for role in _P22Config.ROUTING_ROLES:
        v = raw.get(role) or raw.get(role + "_model")
        if isinstance(v, str) and len(v) <= 80 and re.match(r"^[A-Za-z0-9_./:\-]*$", v):
            v_clean = v.strip()
            # Keep flat keys for compatibility with older reads
            d[role + "_model"] = v_clean
            if d["provider"] != "auto" and d["provider"] in d["providers"]:
                if not d["providers"][d["provider"]][role + "_model"]:
                    d["providers"][d["provider"]][role + "_model"] = v_clean

    return d

def _routing_load() -> dict:
    try:
        with open(_ROUTING_PATH, "r", encoding="utf-8") as fh:
            return _routing_normalize(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _routing_defaults()

def _routing_save(d: dict) -> dict:
    norm = _routing_normalize(d)
    with _ROUTING_LOCK:
        os.makedirs(os.path.dirname(_ROUTING_PATH) or ".", exist_ok=True)
        tmp = _ROUTING_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(norm, fh, indent=2, sort_keys=True)
        os.replace(tmp, _ROUTING_PATH)
    return norm


@app.route("/api/model-routing", methods=["GET"])
def api_routing_get():
    return jsonify({
        "ok":        True,
        "routing":   _routing_load(),
        "defaults":  _routing_defaults(),
        "providers": list(_P22Config.ROUTING_PROVIDERS),
        "roles":     list(_P22Config.ROUTING_ROLES),
    })


@app.route("/api/model-routing", methods=["POST"])
def api_routing_set():
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    try:
        saved = _routing_save(raw)
    except OSError as e:
        return jsonify({"ok": False, "error": "save_failed",
                        "detail": str(e)}), 500
    return jsonify({"ok": True, "routing": saved})


# Phase 23 -- structured stdout log line per role-based dispatch so the
# existing log pipeline can analyse routing decisions (which role landed
# on which provider+model, fallback path, latency).  Mirrors _editor_log.
def _routing_log(event, **fields):
    try:
        payload = {"event": event, "ts": int(time.time() * 1000)}
        payload.update(fields)
        print(f"[routing] {json.dumps(payload, default=str)[:1000]}")
    except Exception:
        pass


# ── Phase 24 — Multi-mode terminal (Sandbox vs. Local Bridge) ────────────
# Persists at data/terminal_mode.json:
#     {"mode": "sandbox"|"local",
#      "local_url":   "http://127.0.0.1:5002",
#      "local_token": "<optional shared secret>"}
# `mode` selects the dispatch path in `/api/terminal/run`.
# `local_url` is whatever the *user* configured — it must reach a process
# they themselves are running on a machine they own (loopback by default).
# We do NOT enforce SSRF private-IP guards here because the whole *point*
# of Local Mode is to escape the sandbox; we instead require an explicit
# scheme allowlist + reasonable length caps, and warn the user clearly in
# the UI before they switch.
_TERMINAL_MODE_PATH = os.path.join(BASE_DIR, "data", "terminal_mode.json")
_TERMINAL_MODE_LOCK = threading.Lock()
_TERMINAL_BRIDGE_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9._\-:\[\]]{1,200}(:\d{1,5})?(/[^\s]{0,200})?$"
)
_TERMINAL_BRIDGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=\-]{0,256}$")

def _terminal_mode_defaults() -> dict:
    return {
        "mode":        _P22Config.TERMINAL_MODE_DEFAULT,
        "local_url":   "http://127.0.0.1:5002",
        "local_token": "",
    }

def _terminal_mode_normalize(raw) -> dict:
    """Validate & coerce a {mode, local_url, local_token} dict.

    Falls back to defaults on invalid input — never raises.  Used by the
    on-disk loader so a corrupted file doesn't 500 the whole endpoint.
    For the POST handler (where we WANT to surface bad user input),
    use `_terminal_mode_validate` instead.
    """
    out = _terminal_mode_defaults()
    if not isinstance(raw, dict):
        return out
    mode = (raw.get("mode") or "").strip().lower()
    if mode in _P22Config.TERMINAL_MODES:
        out["mode"] = mode
    url = (raw.get("local_url") or "").strip()
    if url and len(url) <= _P22Config.TERMINAL_BRIDGE_URL_MAX \
            and _TERMINAL_BRIDGE_URL_RE.match(url):
        # Strip any trailing slash for consistent path-joining.
        out["local_url"] = url.rstrip("/")
    tok = (raw.get("local_token") or "").strip()
    if len(tok) <= _P22Config.TERMINAL_BRIDGE_TOKEN_MAX \
            and _TERMINAL_BRIDGE_TOKEN_RE.match(tok):
        out["local_token"] = tok
    return out


def _terminal_mode_validate(raw) -> tuple[dict, list[str]]:
    """Strictly validate a user-submitted {mode, local_url, local_token}.

    Returns (sanitized_dict, errors).  The dict is suitable for passing to
    `_terminal_mode_save` only when `errors` is empty.  Each error is a
    machine-readable code so the API can surface it as a 400 with a
    user-actionable detail.

    Rules:
      • mode (required): must be in TERMINAL_MODES
      • local_url: optional in sandbox mode; required in local mode.
        When present, must match the URL regex AND length cap.
      • local_token: optional. When present, must match the token regex
        AND length cap.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return _terminal_mode_defaults(), ["invalid_payload"]
    mode = (raw.get("mode") or "").strip().lower()
    if mode not in _P22Config.TERMINAL_MODES:
        errors.append("bad_mode")
    url = (raw.get("local_url") or "").strip()
    if url:
        if len(url) > _P22Config.TERMINAL_BRIDGE_URL_MAX:
            errors.append("url_too_long")
        elif not _TERMINAL_BRIDGE_URL_RE.match(url):
            errors.append("bad_url")
        else:
            url = url.rstrip("/")
    elif mode == "local":
        # Local mode without a URL is meaningless — refuse to save it.
        errors.append("local_url_required")
    tok = (raw.get("local_token") or "")
    # Only strip on the OUTSIDE — embedded whitespace is invalid for tokens
    # and we want it to fail validation rather than be silently rewritten.
    tok_stripped = tok.strip()
    if tok_stripped:
        if len(tok_stripped) > _P22Config.TERMINAL_BRIDGE_TOKEN_MAX:
            errors.append("token_too_long")
        elif not _TERMINAL_BRIDGE_TOKEN_RE.match(tok_stripped):
            errors.append("bad_token")
    sanitized = {
        "mode":        mode if mode in _P22Config.TERMINAL_MODES else
                       _terminal_mode_defaults()["mode"],
        "local_url":   url or _terminal_mode_defaults()["local_url"],
        "local_token": tok_stripped,
    }
    return sanitized, errors

def _terminal_mode_load() -> dict:
    try:
        with open(_TERMINAL_MODE_PATH, "r", encoding="utf-8") as fh:
            return _terminal_mode_normalize(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _terminal_mode_defaults()

def _terminal_mode_save(d: dict) -> dict:
    norm = _terminal_mode_normalize(d)
    with _TERMINAL_MODE_LOCK:
        os.makedirs(os.path.dirname(_TERMINAL_MODE_PATH) or ".",
                    exist_ok=True)
        tmp = _TERMINAL_MODE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(norm, fh, indent=2, sort_keys=True)
        os.replace(tmp, _TERMINAL_MODE_PATH)
    return norm

def _terminal_log(event: str, **fields) -> None:
    """Structured stdout line per terminal dispatch — mirrors _routing_log
    so the existing log pipeline can analyse mode usage / bridge errors."""
    try:
        payload = {"event": event, "ts": int(time.time() * 1000)}
        payload.update(fields)
        print(f"[terminal] {json.dumps(payload, default=str)[:1000]}")
    except Exception:
        pass


@app.route("/api/terminal/mode", methods=["GET"])
def api_terminal_mode_get():
    cfg = _terminal_mode_load()
    return jsonify({
        "ok":         True,
        "config":     {
            "mode":          cfg["mode"],
            "local_url":     cfg["local_url"],
            "token_set":     bool(cfg["local_token"]),
        },
        "defaults":   {
            "mode":      _terminal_mode_defaults()["mode"],
            "local_url": _terminal_mode_defaults()["local_url"],
        },
        "modes":      list(_P22Config.TERMINAL_MODES),
    })


@app.route("/api/terminal/mode", methods=["POST"])
def api_terminal_mode_set():
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400
    # Allow keeping the existing token when caller omits it (so the UI
    # doesn't have to re-prompt for it on every save).
    if "local_token" not in raw:
        raw = dict(raw)
        raw["local_token"] = _terminal_mode_load().get("local_token", "")
    sanitized, errors = _terminal_mode_validate(raw)
    if errors:
        # Surface the first machine-readable code as `error` for legacy
        # callers; the full list is in `errors` for the new UI.
        return jsonify({"ok": False, "error": errors[0],
                        "errors": errors}), 400
    try:
        saved = _terminal_mode_save(sanitized)
    except OSError as e:
        return jsonify({"ok": False, "error": "save_failed",
                        "detail": str(e)}), 500
    _terminal_log("mode_set", mode=saved["mode"],
                  url=saved["local_url"],
                  token_set=bool(saved["local_token"]))
    return jsonify({"ok": True, "config": {
        "mode":      saved["mode"],
        "local_url": saved["local_url"],
        "token_set": bool(saved["local_token"]),
    }})


def _bridge_call(method: str, path: str, body: dict | None = None,
                 timeout: float | None = None,
                 url_override: str | None = None,
                 token_override: str | None = None) -> tuple[int, dict]:
    """Make one HTTP call to the user-configured Local Bridge.

    Returns (http_status, json_body).  Network errors come back as
    (0, {"error": "...", "detail": "..."}) so callers don't need to
    distinguish urllib exceptions from JSON-shape errors.
    """
    cfg = _terminal_mode_load()
    base = (url_override if url_override is not None
            else cfg.get("local_url") or "")
    if not base or not _TERMINAL_BRIDGE_URL_RE.match(base):
        return 0, {"error": "bridge_not_configured"}
    token = (token_override if token_override is not None
             else cfg.get("local_token") or "")
    url = base.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MultiAgentDevSystem/Phase24",
    }
    if token:
        headers["X-Bridge-Token"] = token
    payload = (json.dumps(body or {}).encode("utf-8")
               if method.upper() == "POST" else None)
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method=method.upper())
    cap = _P22Config.TERMINAL_BRIDGE_MAX_RESPONSE_BYTES
    try:
        # Use the no-redirect opener directly so POST goes through; the
        # `_http_get_no_redirect` wrapper only handles GET URLs.  Bridges
        # should never 3xx — if one does, we'd rather fail loudly than
        # silently follow to an unvalidated host.
        with _no_redirect_opener.open(
                req,
                timeout=(timeout
                         or _P22Config.TERMINAL_BRIDGE_TIMEOUT_SEC)) as r:
            raw = r.read(cap + 1)
            if len(raw) > cap:
                return r.status or 200, {
                    "error": "bridge_response_too_large",
                    "limit_bytes": cap,
                }
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
                if not isinstance(data, dict):
                    data = {"raw": str(data)[:1000]}
            except Exception:
                data = {"raw": raw.decode("utf-8", "replace")[:2000]}
            return (r.status or 200), data
    except urllib.error.HTTPError as e:
        try:
            body_txt = e.read().decode("utf-8", "replace")[:2000]
        except Exception:
            body_txt = ""
        try:
            data = json.loads(body_txt) if body_txt.strip().startswith("{") \
                else {"raw": body_txt}
        except Exception:
            data = {"raw": body_txt}
        return e.code, data
    except urllib.error.URLError as e:
        return 0, {"error": "bridge_unreachable",
                   "detail": str(e.reason)[:200]}
    except Exception as e:
        return 0, {"error": "bridge_call_failed",
                   "detail": str(e)[:200]}


@app.route("/api/terminal/check-bridge", methods=["POST"])
def api_terminal_check_bridge():
    """Validate a (url, token) pair by calling the bridge's /health endpoint.

    Does NOT persist anything — purely a connection test the UI calls
    before the user clicks Save.
    """
    raw = request.get_json(silent=True) or {}
    url = (raw.get("local_url") or "").strip()
    token = (raw.get("local_token") or "").strip()
    if not url or not _TERMINAL_BRIDGE_URL_RE.match(url):
        return jsonify({"ok": False, "error": "bad_url"}), 400
    if len(url) > _P22Config.TERMINAL_BRIDGE_URL_MAX:
        return jsonify({"ok": False, "error": "url_too_long"}), 400
    if len(token) > _P22Config.TERMINAL_BRIDGE_TOKEN_MAX \
            or not _TERMINAL_BRIDGE_TOKEN_RE.match(token):
        return jsonify({"ok": False, "error": "bad_token"}), 400
    status, data = _bridge_call(
        "GET", "/health",
        timeout=_P22Config.TERMINAL_BRIDGE_HEALTH_TIMEOUT_SEC,
        url_override=url.rstrip("/"), token_override=token,
    )
    if status == 200 and data.get("ok"):
        return jsonify({"ok": True, "health": data})
    if status == 401:
        return jsonify({"ok": False, "error": "unauthorized",
                        "detail": data.get("error", "auth_failed")})
    if status == 0:
        return jsonify({"ok": False, "error": data.get("error",
                        "unreachable"),
                        "detail": data.get("detail", "")})
    return jsonify({"ok": False, "error": "bad_response",
                    "status": status, "detail": data})


# ── Phase 22 — Ollama models listing (SSRF-guarded) ──────────────────────
def _ollama_host_allowed(host: str, return_pinned_ip: bool = False):
    """Re-uses the Phase 21.1 browser allowlist plus the seed defaults.

    Returns `bool` by default. When `return_pinned_ip=True`, returns the
    first validated IP (str) on success or `None` on failure — callers
    can use the pinned IP to connect, eliminating the DNS-rebinding
    TOCTOU between validation and the actual outbound request.

    Phase 22 hardening (post-architect review): after the hostname-string
    allowlist passes, we additionally resolve the host with `socket` and
    require every resolved IP to be loopback/RFC1918/ULA so a
    DNS-rebinding attack cannot point an allowlisted hostname at a public
    or cloud-metadata IP.  Also rejects unicode/punycode confusables by
    requiring ASCII-only hostnames.
    """
    fail = (None if return_pinned_ip else False)
    if not host:
        return fail
    # Reject anything that isn't pure ASCII (catches unicode lookalikes
    # like `lоcalhost` with a Cyrillic 'о').
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        return fail
    h = _browser_allowlist_normalize(host)
    in_seed = h in _P22Config.OLLAMA_ALLOWED_HOSTS_DEFAULT
    in_user_allowlist = False
    try:
        in_user_allowlist = h in set(_browser_allowlist_load())
    except Exception:
        in_user_allowlist = False
    if not (in_seed or in_user_allowlist):
        return fail
    # DNS pinning: every resolved address must be private/loopback. This
    # blocks DNS-rebinding where an allowlisted hostname resolves to a
    # public or cloud-metadata (169.254.169.254) IP.
    try:
        import ipaddress
        import socket
        infos = socket.getaddrinfo(h, None)
    except (socket.gaierror, OSError):
        # If we can't resolve, only allow the loopback literals.
        if h == "127.0.0.1":
            return "127.0.0.1" if return_pinned_ip else True
        if h == "::1":
            return "::1" if return_pinned_ip else True
        if h == "localhost":
            return "127.0.0.1" if return_pinned_ip else True
        return fail
    # Hard deny-list for cloud-metadata endpoints. Even if a hostname
    # resolves to one of these and the IP is technically "private", we
    # refuse — these are the canonical SSRF-pivot targets on
    # AWS/GCP/Azure/OCI/Alibaba.
    # Phase 22 hardening (post-architect review #3): build the deny
    # set as `ip_address` *objects*, not strings. `ipaddress.ip_address`
    # canonicalises every textual variant (compressed, expanded,
    # zero-padded), so `fd00:ec2::254` and `fd00:0ec2:0000:…:0254`
    # compare equal — closing the IPv6-canonicalization bypass.
    _METADATA_DENY = {
        ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure / OCI
        ipaddress.ip_address("fd00:ec2::254"),    # AWS IMDSv6
        ipaddress.ip_address("100.100.100.200"),  # Alibaba Cloud
        ipaddress.ip_address("192.0.0.192"),      # Oracle Cloud
    }
    # Phase 22 hardening (post-architect review #4): replace the
    # permissive `is_loopback or is_private` rule (which still allows
    # ALL link-local addresses including 169.254.0.0/16 and fe80::/10)
    # with an explicit, narrow allow-list of the networks Ollama
    # actually runs on: loopback + RFC1918 + ULA. Link-local is now
    # rejected wholesale — this catches every metadata-style endpoint
    # in 169.254.0.0/16, not just the four canonical IPs above.
    _ALLOW_NETS = (
        ipaddress.ip_network("127.0.0.0/8"),     # IPv4 loopback
        ipaddress.ip_network("10.0.0.0/8"),      # RFC1918
        ipaddress.ip_network("172.16.0.0/12"),   # RFC1918
        ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
        ipaddress.ip_network("::1/128"),         # IPv6 loopback
        ipaddress.ip_network("fc00::/7"),        # IPv6 ULA
    )
    pinned = None
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None if return_pinned_ip else False
        # Normalise IPv4-mapped IPv6 (`::ffff:169.254.169.254`) down to
        # its embedded IPv4 BEFORE deny/allow evaluation, so the
        # mapped form can't smuggle a denied IPv4 past the IPv6 path.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip in _METADATA_DENY:
            return None if return_pinned_ip else False
        if not any(ip in net for net in _ALLOW_NETS):
            return None if return_pinned_ip else False
        # Capture the first valid IP so the caller can pin its
        # connection to it (closes DNS-rebinding TOCTOU between
        # validation and the actual outbound request).
        if pinned is None:
            pinned = str(ip)
    if return_pinned_ip:
        return pinned
    return True


def _ollama_validated_ip(host: str) -> str | None:
    """Return the first validated IP for `host`, or `None` if disallowed.
    Callers should connect to this IP literal — NOT the hostname — to
    eliminate the DNS-rebinding TOCTOU between validation and connect."""
    return _ollama_host_allowed(host, return_pinned_ip=True)  # type: ignore[arg-type]


@app.route("/api/ollama-models", methods=["GET"])
def api_ollama_models():
    """List local Ollama models. SSRF-guarded against the browser allowlist.

    Phase 22 hardening (post-architect review #5): the validated IP is
    pinned at validation time and used directly in the outbound URL so
    the request cannot resolve to a different IP than the one we
    approved (DNS-rebinding TOCTOU defence)."""
    base = (request.args.get("base")
            or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
    # Parse base URL once
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(base)
        host = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme or "http"
        path_root = parsed.path or ""
    except Exception:
        return jsonify({"ok": False, "error": "bad_base"}), 400
    pinned_ip = _ollama_validated_ip(host)
    if pinned_ip is None:
        return jsonify({"ok": False, "error": "host_not_allowed",
                        "host": host}), 403
    # Build a netloc using the pinned IP literal (bracket IPv6).
    netloc_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = f"{netloc_host}:{port}" if port else netloc_host
    pinned_base = urlunparse((scheme, netloc, path_root, "", "", "")).rstrip("/")
    # Pass the original Host header so virtual-hosted backends still
    # route correctly even though we connected to the IP.
    headers = {"Host": parsed.netloc} if parsed.netloc else None
    try:
        # Use the no-redirect opener: the IP has been allowlist+DNS-pin
        # validated, so a redirect to a different (un-validated) host
        # would defeat that check.
        with _http_get_no_redirect(pinned_base + "/api/tags",
                                    headers=headers, timeout=3) as r:
            body = r.read().decode("utf-8", "replace")[:8000]
            data = json.loads(body) if body.strip().startswith("{") else {}
            models = []
            for m in (data.get("models") or []):
                name = m.get("name") or ""
                if name and re.match(r"^[A-Za-z0-9_./:\-]{1,80}$", name):
                    models.append(name)
            return jsonify({"ok": True, "host": host, "models": models})
    except urllib.error.URLError as e:
        return jsonify({"ok": False, "host": host,
                        "error": f"unreachable: {e.reason}"}), 200
    except Exception as e:
        return jsonify({"ok": False, "host": host, "error": str(e)}), 200


# ── Phase 22 — Terminal sandbox ───────────────────────────────────────────
_TERMINAL_HISTORY_MAX = 50
_terminal_history: deque = deque(maxlen=_TERMINAL_HISTORY_MAX)
_terminal_lock = threading.Lock()

def _terminal_check_blocked(cmd: str) -> str | None:
    """Return a reason token if `cmd` contains any blocked binary, else None.

    We tokenize on shell-meaningful boundaries (whitespace, ;, &, |, `, $())
    and check every basename against the block-list.  We deliberately do NOT
    try to be clever — anything resembling sudo/rm/curl/etc. is rejected
    even when wrapped in quotes or eval, because this terminal is a
    *developer convenience surface*, not a primary deploy/maintain tool.

    Phase 22 hardening (post-architect review): block interpreter indirection
    (python -c, node -e, awk 'system()', etc.) AND any "interpret as code"
    flag (-c, -e, --eval) regardless of binary, since `env python -c …` and
    `/usr/bin/env -S python -c …` would otherwise smuggle the binary past
    the basename check.
    """
    if not cmd or not cmd.strip():
        return "empty"
    if len(cmd) > 4000:
        return "too_long"
    blocked  = set(_P22Config.TERMINAL_BLOCKED_TOKENS)
    blocked_flags = set(getattr(_P22Config, "TERMINAL_BLOCKED_FLAGS",
                                 ("-c", "-e", "--exec", "--eval", "-S")))
    # Refuse anything that escapes via $(), ``, eval, exec.
    # Phase 22 hardening (post-architect review #3): also refuse ANY `$`
    # so shell variable/parameter expansion (`$VAR`, `${x}`, `$'…'`,
    # `$"…"`) cannot reconstruct a blocked binary at parse time
    # (`pyt${x}hon` → `python` because `x` is unset). The terminal env
    # is scrubbed to a known whitelist anyway, so users have no
    # legitimate reason to expand variables here — they can use literal
    # paths or pipe through `printf`/`echo` if they really need a value.
    if any(s in cmd for s in ("$(", "`", "exec ", "eval ", "/dev/tcp/")):
        return "blocked_substitution"
    if "$" in cmd:
        return "blocked_expansion"
    # Refuse any access to system files
    if any(p in cmd for p in ("/etc/passwd", "/etc/shadow", "/root/",
                               "/proc/", "/sys/")):
        return "blocked_system_path"
    # Refuse process-substitution and >( ) <( )
    if any(s in cmd for s in (">(", "<(", "&>", ">|")):
        return "blocked_redirection"
    # Tokenize and check every basename + every flag.
    # Phase 22 hardening (post-architect review #2): bash collapses embedded
    # empty-quote pairs (`pyt''hon` → `python`, `p"y"thon` → `python`,
    # `p\ython` → `python`) at parse time, so a naive basename check on the
    # raw token would let `pyt''hon --version` slip past the `python` block.
    # We canonicalise each token by stripping every quote/backslash before
    # the basename comparison, mirroring what bash will actually execute.
    tokens = re.split(r"[\s;&|<>]+", cmd)
    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        # Collapse bash quoting tricks: remove every ', ", and \ so
        # `pyt''hon`, `p"y"thon`, and `p\ython` all normalise to `python`.
        canon = t.replace("'", "").replace('"', "").replace("\\", "")
        if not canon:
            continue
        # Strip path → check basename only
        base = os.path.basename(canon)
        if base in blocked:
            return f"blocked_token:{base}"
        # Block any "interpret as code" flag — defends against
        # `env python -c …` smuggling — and check both the canonical
        # form and the raw token so `-''c` is also caught.
        if canon in blocked_flags or t in blocked_flags:
            return f"blocked_flag:{canon}"
    return None


@app.route("/api/terminal/run", methods=["POST"])
def api_terminal_run():
    payload = request.get_json(silent=True) or {}
    cmd = (payload.get("cmd") or "").strip()
    sid = (payload.get("sid") or "").strip() or None
    # Phase 24 — explicit mode override on a per-call basis (optional).
    # When omitted, fall back to the persisted user choice.
    requested_mode = (payload.get("mode") or "").strip().lower()
    persisted = _terminal_mode_load()
    mode = (requested_mode if requested_mode in _P22Config.TERMINAL_MODES
            else persisted["mode"])

    # ── Local Bridge dispatch ────────────────────────────────────────────
    # Local Mode runs on the user's own machine and is NOT subject to the
    # sandbox blocklist — that's the entire point.  We still record the
    # call in `_terminal_history` for the History pane.
    if mode == "local":
        if not cmd:
            return jsonify({"ok": False, "error": "empty_command"}), 400
        if persisted["mode"] != "local" and requested_mode != "local":
            # Belt-and-braces: refuse to dispatch to local if neither the
            # persisted mode nor an explicit per-call override said so.
            return jsonify({"ok": False, "error": "local_mode_disabled"}), 400
        started = time.time()
        status, data = _bridge_call("POST", "/terminal/run",
                                    body={"cmd": cmd})
        duration = round(time.time() - started, 3)
        rec = {
            "ts": started, "cmd": cmd, "mode": "local",
            "ok": bool(data.get("ok")) and status == 200,
            "exit": data.get("exit_code", -1),
            "duration_sec": duration, "sid": sid,
        }
        with _terminal_lock:
            _terminal_history.append(rec)
        _terminal_log("dispatch", mode="local", sid=sid,
                      bridge_status=status,
                      ok=rec["ok"], exit=rec["exit"], duration=duration)
        if status == 0:
            return jsonify({
                "ok": False, "mode": "local",
                "error": data.get("error", "bridge_unreachable"),
                "detail": data.get("detail", ""),
                "stdout": "", "stderr": "",
                "exit": -1, "duration_sec": duration,
            }), 200
        if status == 401:
            return jsonify({"ok": False, "mode": "local",
                            "error": "unauthorized",
                            "detail": data.get("error", "auth_failed"),
                            "stdout": "", "stderr": "",
                            "exit": -1, "duration_sec": duration}), 200
        # Bridge returned a normal terminal-shaped envelope.
        return jsonify({
            "ok":           bool(data.get("ok")) and status == 200,
            "mode":         "local",
            "exit":         data.get("exit_code", -1),
            "stdout":       data.get("stdout", ""),
            "stderr":       data.get("stderr", ""),
            "duration_sec": data.get("duration_sec", duration),
            "timed_out":    bool(data.get("timed_out")),
            "truncated":    bool(data.get("truncated")),
            "platform":     data.get("platform"),
            "shell":        data.get("shell"),
        })

    # ── Sandbox dispatch (existing Phase 22 path, unchanged) ────────────
    reason = _terminal_check_blocked(cmd)
    if reason:
        with _terminal_lock:
            _terminal_history.append({
                "ts": time.time(), "cmd": cmd, "ok": False,
                "reason": reason, "exit": -1, "mode": "sandbox",
            })
        return jsonify({"ok": False, "error": "command_blocked",
                        "mode": "sandbox", "reason": reason}), 400
    cwd = session_workspace(sid) if sid else WORKSPACE
    timeout = _P22Config.TERMINAL_TIMEOUT_SEC
    max_bytes = _P22Config.TERMINAL_MAX_OUTPUT_BYTES
    started = time.time()
    out, err, code, timed_out = "", "", -1, False
    try:
        # Use bash -lc so users get aliases/quoting they expect, but the
        # blocklist above already neutralised dangerous binaries AND any
        # interpreter indirection.  We also strip the inherited environment
        # to a minimal whitelist so user commands can't read host secrets
        # (API keys, OAuth tokens, DB urls, etc.) via `env`/`printenv`/
        # `cat /proc/self/environ`.
        _env_keep = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
                     "USER", "LOGNAME", "TERM", "TZ", "PWD", "SHELL"}
        clean_env = {k: v for k, v in os.environ.items()
                     if k in _env_keep}
        clean_env["PYTHONUNBUFFERED"] = "1"
        clean_env["HOME"] = clean_env.get("HOME", cwd)
        clean_env["PATH"] = clean_env.get(
            "PATH", "/usr/local/bin:/usr/bin:/bin")
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            cwd=cwd, timeout=timeout,
            capture_output=True, text=True,
            env=clean_env,
        )
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout.decode() if isinstance(e.stdout, bytes)
               else (e.stdout or ""))
        err = (e.stderr.decode() if isinstance(e.stderr, bytes)
               else (e.stderr or "")) + f"\n[timeout after {timeout}s]"
        timed_out = True
    except Exception as e:
        err = str(e)
    duration = time.time() - started
    # Cap output
    truncated_out = len(out.encode()) > max_bytes
    truncated_err = len(err.encode()) > max_bytes
    if truncated_out:
        out = out.encode()[:max_bytes].decode("utf-8", "replace") + "\n[truncated]"
    if truncated_err:
        err = err.encode()[:max_bytes].decode("utf-8", "replace") + "\n[truncated]"
    rec = {
        "ts": started, "cmd": cmd, "ok": (code == 0 and not timed_out),
        "exit": code, "duration_sec": round(duration, 3),
        "timed_out": timed_out, "sid": sid,
    }
    rec["mode"] = "sandbox"
    with _terminal_lock:
        _terminal_history.append(rec)
    _terminal_log("dispatch", mode="sandbox", sid=sid,
                  ok=rec["ok"], exit=code,
                  duration=round(duration, 3))
    return jsonify({
        "ok":            (code == 0 and not timed_out),
        "mode":          "sandbox",
        "exit":          code,
        "stdout":        out,
        "stderr":        err,
        "duration_sec":  round(duration, 3),
        "timed_out":     timed_out,
        "truncated":     truncated_out or truncated_err,
        "cwd":           os.path.relpath(cwd, BASE_DIR),
    })


@app.route("/api/terminal/history", methods=["GET"])
def api_terminal_history():
    with _terminal_lock:
        return jsonify({"ok": True, "history": list(_terminal_history)})


@app.route("/api/terminal/stream", methods=["GET"])
def api_terminal_stream():
    """Phase 24 — SSE proxy to the Local Bridge's `/terminal/stream`.

    GET because EventSource is GET-only.  We pull the bridge URL + token
    from the persisted config (NOT from query params) so the token never
    leaves the server process or shows up in browser history / proxy logs.
    The command is the only query parameter — same as a normal `/run` POST,
    but the response is a streamed text/event-stream relay.

    Sandbox mode does NOT support streaming yet — sandbox commands are
    short (10s cap) so the existing buffered `/api/terminal/run` is
    sufficient.  Calls in sandbox mode return 400/sandbox_no_stream so the
    UI knows to fall back.
    """
    cmd = (request.args.get("cmd") or "").strip()
    sid = (request.args.get("sid") or "").strip() or None
    if not cmd:
        return jsonify({"ok": False, "error": "empty_command"}), 400
    cfg = _terminal_mode_load()
    if cfg["mode"] != "local":
        return jsonify({"ok": False, "error": "sandbox_no_stream"}), 400
    base = cfg.get("local_url") or ""
    if not base or not _TERMINAL_BRIDGE_URL_RE.match(base):
        return jsonify({"ok": False, "error": "bridge_not_configured"}), 400
    token = cfg.get("local_token") or ""
    url = base.rstrip("/") + "/terminal/stream"
    body = json.dumps({"cmd": cmd}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "MultiAgentDevSystem/Phase24",
    }
    if token:
        headers["X-Bridge-Token"] = token
    upstream_req = urllib.request.Request(
        url, data=body, headers=headers, method="POST")

    started = time.time()

    def _generate():
        cap = _P22Config.TERMINAL_BRIDGE_MAX_RESPONSE_BYTES
        sent = 0
        try:
            with _no_redirect_opener.open(
                    upstream_req,
                    timeout=_P22Config.TERMINAL_BRIDGE_TIMEOUT_SEC) as r:
                while True:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if sent > cap:
                        yield (b"event: error\n"
                               b"data: {\"error\":\"cap_exceeded\"}\n\n")
                        break
                    yield chunk
        except urllib.error.HTTPError as e:
            try:
                body_txt = e.read().decode("utf-8", "replace")[:1000]
            except Exception:
                body_txt = ""
            err = json.dumps({"error": "bridge_http_error",
                              "status": e.code,
                              "detail": body_txt})
            yield f"event: error\ndata: {err}\n\n".encode("utf-8")
        except urllib.error.URLError as e:
            err = json.dumps({"error": "bridge_unreachable",
                              "detail": str(e.reason)[:200]})
            yield f"event: error\ndata: {err}\n\n".encode("utf-8")
        except Exception as e:
            err = json.dumps({"error": "stream_failed",
                              "detail": str(e)[:200]})
            yield f"event: error\ndata: {err}\n\n".encode("utf-8")
        # Always emit a final delimiter so the client EventSource sees a
        # message boundary and can close.
        try:
            with _terminal_lock:
                _terminal_history.append({
                    "ts": started, "cmd": cmd, "mode": "local",
                    "ok": True, "exit": 0,
                    "duration_sec": round(time.time() - started, 3),
                    "sid": sid, "stream": True,
                })
            _terminal_log("stream_end", mode="local", sid=sid,
                          duration=round(time.time() - started, 3),
                          bytes=sent)
        except Exception:
            pass

    resp = Response(_generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"  # disable nginx buffering
    return resp


# ── Phase 22 — Code Lessons (searchable) ──────────────────────────────────
_LESSONS_DB_PATH = os.path.join(BASE_DIR, "memory.db")

def _lessons_query(search: str = "", limit: int = 50) -> list:
    """Read code_lessons rows from memory.db, optionally substring-searched.

    Independent of the agent's Memory class so this endpoint never blocks
    on the agent's worker threads.  Returns [] gracefully if the table
    doesn't exist yet (no lessons recorded).
    """
    try:
        if not os.path.exists(_LESSONS_DB_PATH):
            return []
        conn = sqlite3.connect(_LESSONS_DB_PATH, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            # Confirm table exists
            t = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='code_lessons'"
            ).fetchone()
            if not t:
                return []
            if search:
                q = f"%{search.lower()}%"
                rs = conn.execute(
                    "SELECT id, error_type, error_message, fix_summary, "
                    "       success, created_at "
                    "FROM code_lessons "
                    "WHERE LOWER(error_type)    LIKE ? "
                    "   OR LOWER(error_message) LIKE ? "
                    "   OR LOWER(IFNULL(fix_summary,'')) LIKE ? "
                    "ORDER BY id DESC LIMIT ?",
                    (q, q, q, max(1, min(int(limit), 500)))
                ).fetchall()
            else:
                rs = conn.execute(
                    "SELECT id, error_type, error_message, fix_summary, "
                    "       success, created_at "
                    "FROM code_lessons ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),)
                ).fetchall()
            return [dict(r) for r in rs]
        finally:
            conn.close()
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────
# Phase 25 — AI ↔ Terminal automation endpoints
# ─────────────────────────────────────────────────────────────────────────
# These endpoints expose the `command_layer` chokepoint to the UI and to
# in-process callers (the dev_loop reaches the layer directly, not via
# HTTP).  They share one global toggle (`data/ai_terminal_settings.json`)
# and one audit log (`data/ai_exec_log.jsonl`).  None of them bypass the
# Phase 22 sandbox blocklist or the Phase 24 bridge — they go *through*
# both via the layer's dispatcher.
try:
    import command_layer as _cmd_layer
except Exception as _cl_err:                # noqa: BLE001
    # Importing should never realistically fail (stdlib + config), but
    # if it does we want the rest of web_app to keep starting.
    _cmd_layer = None
    print(f"[phase25] command_layer import failed: {_cl_err}")


@app.route("/api/ai/terminal-settings", methods=["GET"])
def api_ai_terminal_settings_get():
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    return jsonify({"ok": True, "settings": _cmd_layer.load_settings()})


@app.route("/api/ai/terminal-settings", methods=["POST"])
def api_ai_terminal_settings_set():
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    raw = request.get_json(silent=True) or {}
    saved, errors = _cmd_layer.save_settings(raw)
    if errors:
        return jsonify({"ok": False, "errors": errors,
                        "settings": saved}), 400
    return jsonify({"ok": True, "settings": saved})


@app.route("/api/ai/exec", methods=["POST"])
def api_ai_exec():
    """Run a single command through the layer.  This is the endpoint the
    UI's terminal panel + any external tooling should call instead of
    `/api/terminal/run` when the *AI automation* gate must apply.

    Body: `{cmd, allow_install?, mode?, timeout?}`
    """
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    payload = request.get_json(silent=True) or {}
    cmd = (payload.get("cmd") or "").strip()
    if not cmd:
        return jsonify({"ok": False, "error": "empty_command"}), 400
    allow_install = bool(payload.get("allow_install"))
    requested_mode = (payload.get("mode") or "").strip().lower() or None
    if requested_mode is not None \
            and requested_mode not in _P22Config.TERMINAL_MODES:
        return jsonify({"ok": False, "error": "bad_mode"}), 400
    # Phase-25 architect-round-1 fix: do NOT honour per-request mode
    # overrides — the persisted ``data/terminal_mode.json`` is the single
    # source of truth.  Otherwise a caller could push an install/run into
    # the local-bridge mode out-of-band even when the user intentionally
    # left the workspace on sandbox (or vice versa).  We still accept a
    # ``mode`` field for forward compatibility but only as an *assertion*
    # that must match the persisted mode; mismatch is rejected.
    try:
        persisted_mode = _terminal_mode_load()["mode"]
    except Exception:                                  # noqa: BLE001
        persisted_mode = "sandbox"
    if requested_mode is not None and requested_mode != persisted_mode:
        return jsonify({
            "ok": False, "error": "mode_mismatch",
            "persisted_mode": persisted_mode,
        }), 400
    try:
        timeout = int(payload.get("timeout") or 0) or None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad_timeout"}), 400
    # Always pass mode=None — the layer's _DISPATCHER reads the same
    # persisted file, so this is effectively a re-assertion of
    # ``persisted_mode`` with no per-call override surface.
    result = _cmd_layer.execute_command(
        cmd, mode=None, allow_install=allow_install,
        sid=(payload.get("sid") or None),
        source=(payload.get("source") or "ui"),
        timeout=timeout,
    )
    # Disabled / blocked responses surface as 403 so the UI can prompt
    # the user — actual command failures (exit != 0) stay 200.
    if result.get("error") == "ai_disabled":
        return jsonify(result), 403
    if result.get("category") == "blocked":
        return jsonify(result), 400
    if result.get("error") == "confirmation_required":
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/ai/run-setup", methods=["POST"])
def api_ai_run_setup():
    """`pip install -r requirements.txt` with consent gating."""
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    result = _cmd_layer.run_setup(source="ui")
    if result.get("error") == "ai_disabled":
        return jsonify(result), 403
    if result.get("error") == "confirmation_required":
        return jsonify(result), 409
    return jsonify(result)


@app.route("/api/ai/auto-fix-env", methods=["POST"])
def api_ai_auto_fix_env():
    """Parse caller-supplied stderr/log text for ``ModuleNotFoundError``
    matches and pip-install each (capped at 5).  When the body has no
    ``text`` field we scan the most recent AI exec log entry's stderr."""
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    payload = request.get_json(silent=True) or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        # Pull the most recent failed exec's stderr as a convenience.
        recent = _cmd_layer.read_log(limit=20)
        text = "\n".join(
            (e.get("error") or "") for e in reversed(recent)
            if e.get("error")
        )
    if not _cmd_layer.load_settings()["ai_enabled"]:
        return jsonify({"ok": False, "error": "ai_disabled"}), 403
    if not _cmd_layer.load_settings()["allow_install_auto"]:
        return jsonify({"ok": False,
                        "error": "confirmation_required"}), 409
    res = _cmd_layer.auto_fix_env(text or "", source="ui", limit=5)
    return jsonify(res)


@app.route("/api/ai/exec-log", methods=["GET"])
def api_ai_exec_log():
    if _cmd_layer is None:
        return jsonify({"ok": False, "error": "command_layer_unavailable"}), 500
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"ok": True,
                    "entries": _cmd_layer.read_log(limit=limit)})


@app.route("/api/lessons", methods=["GET"])
def api_lessons():
    search = (request.args.get("q") or "").strip()[:200]
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    rows = _lessons_query(search, limit)
    return jsonify({"ok": True, "search": search,
                    "count": len(rows), "lessons": rows})


# ── Phase 22 — Task Timeline ──────────────────────────────────────────────
@app.route("/api/timeline", methods=["GET"])
def api_timeline():
    """Past tasks (sessions) + their decision counts for replay UI."""
    try:
        limit = int(request.args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    sessions = db_sessions(limit=max(1, min(limit, 500)))
    items = []
    for s in sessions:
        try:
            with _conn() as c:
                dn = c.execute(
                    "SELECT COUNT(*) AS n FROM decisions WHERE session_id=?",
                    (s["id"],)).fetchone()["n"]
                ln = c.execute(
                    "SELECT COUNT(*) AS n FROM logs WHERE session_id=?",
                    (s["id"],)).fetchone()["n"]
        except Exception:
            dn, ln = 0, 0
        items.append({
            "sid":          s["id"],
            "task":         s.get("task", ""),
            "model":        s.get("model"),
            "status":       s.get("status"),
            "success":      s.get("success"),
            "created_at":   s.get("created_at"),
            "finished_at": s.get("finished_at"),
            "decisions":    dn,
            "logs":         ln,
        })
    return jsonify({"ok": True, "count": len(items), "timeline": items})


# ── Phase 22 — Multi-Agent Status ─────────────────────────────────────────
# Reads the running orchestrator state if available; otherwise synthesises
# state from the most recent session's decisions table (which the agent
# emits markers like "[planner] step N", "[critic] ...", etc.).
_AGENT_ROLES = ("planner", "coding", "debugger", "reviewer")

def _infer_agent_state() -> dict:
    """Best-effort multi-agent status snapshot.

    Sources, in order of preference:
      1. `running` dict — if a session is active we can attribute roles.
      2. The most recent session's decisions table — kind values like
         "planner" / "executor" / "critic" / "reviewer" tell us which
         role last fired and when.
    """
    snapshot = {role: {"status": "idle", "current_task": None,
                       "last_activity": None, "events": 0}
                for role in _AGENT_ROLES}
    active_sid = (running or {}).get("sid")
    snapshot["_active_sid"] = active_sid
    # Pick the "current" session (running > most recent)
    sid = active_sid
    sessions = db_sessions(limit=5)
    if not sid and sessions:
        sid = sessions[0]["id"]
    if sid:
        try:
            decisions = db_decisions(sid)
        except Exception:
            decisions = []
        # Map decision.kind tokens to agent roles
        kind_to_role = {
            "planner":  "planner",   "plan":      "planner",
            "executor": "coding",    "code":      "coding",
            "debugger": "debugger",  "debug":     "debugger",
            "fix":      "debugger",  "critic":    "reviewer",
            "reviewer": "reviewer",  "review":    "reviewer",
            "reflect":  "reviewer",
        }
        for d in decisions:
            kind = (d.get("kind") or "").strip().lower()
            role = kind_to_role.get(kind)
            if role and role in snapshot:
                snapshot[role]["events"] += 1
                snapshot[role]["last_activity"] = d.get("ts")
                snapshot[role]["current_task"] = (d.get("detail") or "")[:200]
                snapshot[role]["status"] = ("active" if active_sid == sid
                                             else "completed")
    snapshot["_session"] = sid
    return snapshot


@app.route("/api/agents/state", methods=["GET"])
def api_agents_state():
    snap = _infer_agent_state()
    agents = [{"role": r, **snap[r]} for r in _AGENT_ROLES]
    return jsonify({
        "ok":     True,
        "active_sid": snap.get("_active_sid"),
        "session":    snap.get("_session"),
        "agents":     agents,
    })


# ── Phase 22 — Goal-Based Dashboard ───────────────────────────────────────
@app.route("/api/dashboard/<sid>", methods=["GET"])
def api_dashboard(sid):
    """Per-session task breakdown, progress, current step, history."""
    sess = db_session(sid)
    if not sess:
        return jsonify({"ok": False, "error": "session_not_found"}), 404
    try:
        decisions = db_decisions(sid)
    except Exception:
        decisions = []
    # Pull ordered "step" / "plan" decisions for the breakdown
    steps = []
    completed_steps = 0
    current_step = None
    history = []
    for d in decisions:
        kind = (d.get("kind") or "").strip().lower()
        det  = (d.get("detail") or "").strip()
        history.append({"ts": d.get("ts"), "kind": kind,
                        "detail": det[:400]})
        if kind in {"step", "plan_step", "planner"}:
            steps.append({"text": det[:300], "ts": d.get("ts"),
                          "done": False})
        elif kind in {"step_done", "ok", "step_ok", "success"}:
            if steps:
                # mark the most recent unfinished step as done
                for s in reversed(steps):
                    if not s["done"]:
                        s["done"] = True
                        completed_steps += 1
                        break
    # Determine current step = first not-done; else last step
    for s in steps:
        if not s["done"]:
            current_step = s
            break
    if current_step is None and steps:
        current_step = steps[-1]
    total = len(steps) or 1
    progress = int(round(100.0 * completed_steps / total))
    return jsonify({
        "ok":           True,
        "sid":          sid,
        "task":         sess.get("task", ""),
        "status":       sess.get("status"),
        "success":      sess.get("success"),
        "steps":        steps,
        "completed":    completed_steps,
        "total_steps":  len(steps),
        "progress_pct": progress,
        "current_step": current_step,
        "history":      history[-100:],
    })


# ── Phase 22 — Predictive UI ──────────────────────────────────────────────
# Returns a single suggested next action based on session state.  Lightweight
# rules engine — no LLM call, no DB writes.  Front-end shows it as a chip.
@app.route("/api/predict/<sid>", methods=["GET"])
def api_predict(sid):
    sess = db_session(sid)
    if not sess:
        return jsonify({"ok": False, "error": "session_not_found"}), 404
    status  = (sess.get("status") or "").lower()
    success = sess.get("success")
    suggestion = None
    if status == "running":
        suggestion = {"action": "wait", "label": "Task running…",
                      "priority": "low"}
    elif status == "failed" or success == 0:
        suggestion = {"action": "fix", "label": "Tests failed → Fix?",
                      "priority": "high"}
    elif status == "done" and success == 1:
        suggestion = {"action": "preview", "label": "Done → Open Preview",
                      "priority": "med"}
    else:
        suggestion = {"action": "run", "label": "Idle → Run a task",
                      "priority": "low"}
    # Look at the latest decisions for richer hints
    try:
        decisions = db_decisions(sid)[-10:]
    except Exception:
        decisions = []
    last_kinds = [(d.get("kind") or "").lower() for d in decisions]
    if "critic" in last_kinds[-3:] and not (status == "running"):
        suggestion = {"action": "review", "label":
                      "Critic flagged issues → Review",
                      "priority": "high"}
    return jsonify({
        "ok":          True,
        "sid":         sid,
        "status":      status,
        "success":     success,
        "suggestion":  suggestion,
        "tail_kinds":  last_kinds,
    })


# ── Phase 27/28 — Unified Model Routing & MCP ─────────────────────────────

@app.route("/api/model-routing", methods=["GET"])
def api_model_routing_get():
    """Retrieve the unified model routing configuration."""
    path = os.path.join("data", "model_routing.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return jsonify({"ok": True, "routing": data})
    except Exception as e:
        print(f"[routing] Failed to read model_routing.json: {e}")
    
    # Fallback default
    return jsonify({"ok": True, "routing": {
        "provider": "auto",
        "providers": {
            "gemini": {"planner_model": "gemini-1.5-flash", "coding_model": "gemini-1.5-pro", "reasoning_model": "gemini-1.5-pro"},
            "groq": {"planner_model": "llama-3.1-8b-instant", "coding_model": "llama-3.3-70b-versatile", "reasoning_model": "llama-3.3-70b-versatile"},
            "ollama": {"planner_model": "phi3:mini", "coding_model": "codellama", "reasoning_model": "codellama"}
        }
    }})

@app.route("/api/model-routing", methods=["POST"])
def api_model_routing_post():
    """Save the unified model routing configuration."""
    data = request.get_json(silent=True) or {}
    path = os.path.join("data", "model_routing.json")
    os.makedirs("data", exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return jsonify({"ok": True, "routing": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/mcp/human-response", methods=["POST"])
def api_mcp_human_response():
    """Submit a human decision for a pending uncertainty query (Phase 28)."""
    data = request.get_json(silent=True) or {}
    query_id = data.get("query_id")
    decision = data.get("decision")
    if not query_id or not decision:
        return jsonify({"ok": False, "error": "missing_fields"}), 400
    
    # Update the global MCP context. The uncertainty_engine.py wait loop
    # will detect this change and resume execution.
    mcp.update("world", "human_response", {"query_id": query_id, "decision": decision})
    return jsonify({"ok": True})


# ── Phase 31: Voice Input (Web Speech API relay) ─────────────────────────────
# The browser does STT natively (Web Speech API); this endpoint is a
# lightweight relay that logs the transcript and can optionally run it
# as an agent task directly.
@app.route("/api/voice/transcript", methods=["POST"])
def api_voice_transcript():
    """Receive a voice transcript from the browser STT engine and optionally
    queue it as a task in the current session."""
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    session_id = data.get("session_id")
    auto_run   = bool(data.get("auto_run", False))

    if not transcript:
        return jsonify({"ok": False, "error": "empty_transcript"}), 400

    # Log for audit trail
    print(f"[Voice] Transcript received: {transcript[:120]!r}")

    if auto_run:
        # Queue it as a new agent task
        cfg = get_setting("config") or default_managed_config()
        sid = str(uuid.uuid4())
        now = time.time()
        session = {
            "id": sid, "task": transcript,
            "model": None, "status": "queued",
            "created_at": now, "config": cfg,
        }
        db_insert_session(session)
        task_queue.append(sid)
        return jsonify({"ok": True, "session_id": sid, "queued": True,
                        "transcript": transcript})

    return jsonify({"ok": True, "transcript": transcript, "queued": False})


# ── Phase 31: File / Image Upload ────────────────────────────────────────────
_UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)

_ALLOWED_EXTENSIONS = {
    # Images (vision-capable models)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    # Code files
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".go", ".rs", ".cpp", ".c", ".h", ".java", ".kt", ".rb",
    # Text / config
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".env",
    ".csv", ".log", ".sh", ".bash",
}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB hard cap


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Accept file or image uploads, extract text/base64 content, and return
    context that can be injected into the next agent task prompt."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400

    f = request.files["file"]
    fname = f.filename or "upload"
    ext = os.path.splitext(fname)[1].lower()

    if ext not in _ALLOWED_EXTENSIONS:
        return jsonify({"ok": False, "error": f"file_type_not_allowed: {ext}"}), 415

    # Read up to the cap
    raw = f.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        return jsonify({"ok": False, "error": "file_too_large"}), 413

    # Persist to uploads/ for potential future re-use
    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(fname)}"
    dest = os.path.join(_UPLOAD_DIR, safe_name)
    with open(dest, "wb") as fh:
        fh.write(raw)

    # Determine content type
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    is_image   = ext in image_exts

    if is_image:
        import base64 as _b64
        b64_data = _b64.b64encode(raw).decode("ascii")
        mime = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".webp": "image/webp",
            ".bmp":  "image/bmp",
        }.get(ext, "image/png")
        return jsonify({
            "ok": True,
            "type": "image",
            "filename": fname,
            "mime": mime,
            "size": len(raw),
            "data_url": f"data:{mime};base64,{b64_data[:200]}…",  # preview only
            "context": f"[Image uploaded: {fname} ({len(raw)} bytes, {mime})]",
            "stored_as": safe_name,
        })
    else:
        # Text file — decode and return first 8 KB as context
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        preview = text[:8000]
        return jsonify({
            "ok": True,
            "type": "text",
            "filename": fname,
            "size": len(raw),
            "preview": preview,
            "context": f"File: {fname}\n```\n{preview}\n```",
            "stored_as": safe_name,
        })


# ── Phase 31: Human-in-the-Loop (HITL) Controls ──────────────────────────────
# Per-session pause/resume/inject state stored in memory (fast) and in MCP ctx.
_hitl_state: dict[str, dict] = {}  # {sid: {paused, inject_queue}}
_hitl_lock = threading.Lock()


def _hitl_get(sid):
    with _hitl_lock:
        return _hitl_state.setdefault(sid, {"paused": False, "inject_queue": []})


@app.route("/api/session/<sid>/pause", methods=["POST"])
def api_session_pause(sid):
    """Pause execution of a running session (HITL)."""
    s = db_session(sid)
    if not s:
        return jsonify({"ok": False, "error": "not_found"}), 404
    state = _hitl_get(sid)
    with _hitl_lock:
        state["paused"] = True
    # Signal via MCP context so orchestrator can check it
    mcp.update("world", f"hitl_{sid}", {"paused": True})
    db_update_session(sid, stage="paused")
    return jsonify({"ok": True, "paused": True})


@app.route("/api/session/<sid>/resume", methods=["POST"])
def api_session_resume(sid):
    """Resume a paused session."""
    s = db_session(sid)
    if not s:
        return jsonify({"ok": False, "error": "not_found"}), 404
    state = _hitl_get(sid)
    with _hitl_lock:
        state["paused"] = False
    mcp.update("world", f"hitl_{sid}", {"paused": False})
    db_update_session(sid, stage="running")
    return jsonify({"ok": True, "paused": False})


@app.route("/api/session/<sid>/inject", methods=["POST"])
def api_session_inject(sid):
    """Inject a mid-run instruction into the agent's next iteration."""
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"ok": False, "error": "empty_instruction"}), 400
    s = db_session(sid)
    if not s:
        return jsonify({"ok": False, "error": "not_found"}), 404
    state = _hitl_get(sid)
    with _hitl_lock:
        state["inject_queue"].append(instruction)
    mcp.update("world", f"inject_{sid}", {"instruction": instruction,
                                           "ts": time.time()})
    # Log injection as a decision for UI visibility
    db_insert_decision(sid, time.time(), "hitl_inject", instruction[:400])
    return jsonify({"ok": True, "queued": len(state["inject_queue"])})


@app.route("/api/session/<sid>/hitl-state", methods=["GET"])
def api_session_hitl_state(sid):
    """Return current HITL pause/inject state."""
    state = _hitl_get(sid)
    with _hitl_lock:
        return jsonify({
            "ok": True,
            "paused": state["paused"],
            "pending_injections": len(state["inject_queue"]),
        })


# ── Phase 31: System Health & Observability Dashboard ────────────────────────
@app.route("/api/health", methods=["GET"])
def api_health():
    """Return a real-time system health snapshot."""
    import platform

    # Resource stats (best-effort; graceful if psutil not installed)
    try:
        import psutil
        cpu_pct  = psutil.cpu_percent(interval=None)
        mem      = psutil.virtual_memory()
        mem_used = mem.percent
        mem_gb   = round(mem.used / 1024**3, 2)
    except ImportError:
        cpu_pct = mem_used = mem_gb = None

    # Load observability metrics from tracker if available
    metrics_path = os.path.join(BASE_DIR, "data", "metrics.json")
    metrics = {}
    try:
        with open(metrics_path, "r", encoding="utf-8") as fh:
            metrics = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Session counts
    with _conn() as c:
        total_sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        running = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE status='running'"
        ).fetchone()[0]
        success = c.execute(
            "SELECT COUNT(*) FROM sessions WHERE success=1"
        ).fetchone()[0]

    success_rate = round((success / total_sessions * 100), 1) if total_sessions else 0

    return jsonify({
        "ok": True,
        "system": {
            "platform": platform.system(),
            "python":   platform.python_version(),
            "cpu_pct":  cpu_pct,
            "mem_used_pct": mem_used,
            "mem_used_gb":  mem_gb,
        },
        "sessions": {
            "total":   total_sessions,
            "running": running,
            "success_rate_pct": success_rate,
        },
        "observability": metrics,
        "safety": {
            "max_iterations": int(os.environ.get("MAX_ITER", 3)),
            "max_commands":   int(os.environ.get("MAX_CMD", 10)),
            "max_runtime_s":  int(os.environ.get("MAX_RUNTIME", 60)),
        },
    })



# ══════════════════════════════════════════════════════════════════════════════
# Phase 32 — Real Interactive Terminal (PTY via pywinpty)
# ══════════════════════════════════════════════════════════════════════════════
try:
    from terminal_backend import terminal_registry, _HAS_PTY
    _TERMINAL_AVAILABLE = _HAS_PTY
except ImportError as _te:
    terminal_registry = None
    _TERMINAL_AVAILABLE = False
    print(f"[Phase 32] terminal_backend unavailable: {_te}", file=sys.stderr)


def _pty_session_id(sid):
    return f"pty-{sid}"


@app.route("/api/pty/start", methods=["POST"])
def api_pty_start():
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable",
                        "hint": "pip install pywinpty"}), 503
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id") or "global"
    cwd  = session_workspace(sid) if sid != "global" else BASE_DIR
    tid  = _pty_session_id(sid)
    sess = terminal_registry.get_or_create(tid, cwd)
    return jsonify({"ok": True, "terminal_id": tid,
                    "cwd": cwd, "shell": sess.get_state()["shell"]})


@app.route("/api/pty/<tid>/input", methods=["POST"])
def api_pty_input(tid):
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable"}), 503
    data = request.get_json(silent=True) or {}
    text = data.get("data", "")
    sess = terminal_registry.get(tid)
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    sess.write(text)
    return jsonify({"ok": True})


@app.route("/api/pty/<tid>/run", methods=["POST"])
def api_pty_run(tid):
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable"}), 503
    data   = request.get_json(silent=True) or {}
    cmd    = (data.get("command") or "").strip()
    source = data.get("source", "user")
    if not cmd:
        return jsonify({"ok": False, "error": "empty_command"}), 400
    sess = terminal_registry.get(tid)
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    entry = sess.run_command(cmd, source=source)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/pty/<tid>/resize", methods=["POST"])
def api_pty_resize(tid):
    data = request.get_json(silent=True) or {}
    cols = int(data.get("cols", 220))
    rows = int(data.get("rows", 50))
    sess = terminal_registry.get(tid) if terminal_registry else None
    if sess:
        sess.resize(cols, rows)
    return jsonify({"ok": True})


@app.route("/api/pty/<tid>/stream")
def api_pty_stream(tid):
    """SSE — streams live PTY output to the xterm.js frontend."""
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable"}), 503
    sess = terminal_registry.get(tid)
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    client_id = str(uuid.uuid4())[:8]
    q = sess.subscribe(client_id)

    @stream_with_context
    def _gen():
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                except Exception:
                    yield "data: {\"type\":\"ping\"}\n\n"
                    continue
                if msg is None:
                    yield f"data: {json.dumps({'type':'exit'})}\n\n"
                    break
                yield f"data: {msg}\n\n"
        finally:
            sess.unsubscribe(client_id)

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/pty/<tid>/history")
def api_pty_history(tid):
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "history": []})
    limit = int(request.args.get("limit", 50))
    return jsonify({"ok": True, "history": sess.get_history(limit)})


@app.route("/api/pty/<tid>/state")
def api_pty_state(tid):
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    return jsonify({"ok": True, **sess.get_state()})


@app.route("/api/pty/<tid>/close", methods=["POST"])
def api_pty_close(tid):
    if terminal_registry:
        terminal_registry.close(tid)
    return jsonify({"ok": True})


@app.route("/api/pty/list")
def api_pty_list():
    if not terminal_registry:
        return jsonify({"ok": True, "sessions": []})
    return jsonify({"ok": True, "sessions": terminal_registry.list_sessions()})


@app.route("/api/pty/<tid>/agent-exec", methods=["POST"])
def api_pty_agent_exec(tid):
    """Mirror an agent-executed command into the real terminal."""
    data    = request.get_json(silent=True) or {}
    cmd     = (data.get("command") or "").strip()
    session = data.get("session_id")
    if not cmd:
        return jsonify({"ok": False, "error": "empty_command"}), 400
    if _TERMINAL_AVAILABLE and terminal_registry:
        sess = terminal_registry.get(tid)
        if sess and sess.is_alive():
            sess.run_command(cmd, source="agent")
    if session:
        db_insert_decision(session, time.time(), "agent_exec", cmd[:500])
    return jsonify({"ok": True})


# ── Phase 33: Venv status + Terminal Intelligence routes ─────────────────────

@app.route("/api/pty/<tid>/venv")
def api_pty_venv(tid):
    """Return venv info for this PTY session."""
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    state = sess.get_state()
    return jsonify({
        "ok":           True,
        "venv_name":    state.get("venv_name"),
        "venv_ready":   state.get("venv_ready"),
        "venv_activated": state.get("venv_activated"),
        "cwd":          state.get("cwd"),
    })


@app.route("/api/pty/<tid>/venv/ensure", methods=["POST"])
def api_pty_venv_ensure(tid):
    """Force-ensure (create if missing) the venv and send activation to terminal."""
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable"}), 503
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    try:
        from terminal_backend import ensure_venv, build_activate_command
        venv_info = ensure_venv(sess.cwd)
        if venv_info.get("ready"):
            cmd = build_activate_command(sess.cwd)
            sess.run_command(cmd, source="system")
        return jsonify({"ok": True, "venv": venv_info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pty/<tid>/last-error")
def api_pty_last_error(tid):
    """Return the most recently detected error in this terminal session."""
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    err = sess.get_last_error()
    return jsonify({"ok": True, "error": err})


@app.route("/api/pty/<tid>/clear-error", methods=["POST"])
def api_pty_clear_error(tid):
    """Clear the last error flag (called after user reads it)."""
    sess = terminal_registry.get(tid) if terminal_registry else None
    if sess:
        sess.clear_last_error()
    return jsonify({"ok": True})


@app.route("/api/pty/<tid>/fix", methods=["POST"])
def api_pty_fix(tid):
    """Apply an AI-generated fix for the last detected terminal error."""
    if not _TERMINAL_AVAILABLE:
        return jsonify({"ok": False, "error": "pty_unavailable"}), 503
    sess = terminal_registry.get(tid) if terminal_registry else None
    if not sess:
        return jsonify({"ok": False, "error": "no_session"}), 404
    err = sess.get_last_error()
    if not err:
        return jsonify({"ok": False, "error": "no_error_recorded"}), 400

    suggestion = err.get("suggestion", "")
    excerpt    = err.get("excerpt", "")

    # Build a Gemini fix prompt
    cfg = get_setting("config") or default_managed_config()
    env = env_for_session(cfg)
    gemini_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    fix_cmd = None

    if gemini_key:
        try:
            import urllib.request as _ur
            prompt = (
                f"The terminal produced this error:\n{excerpt}\n\n"
                f"Initial suggestion: {suggestion}\n\n"
                "Give me ONE shell command I can run to fix this. "
                "Reply ONLY with the raw command, no explanation, no backticks."
            )
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
            url = ("https://generativelanguage.googleapis.com/v1beta/"
                   f"models/gemini-1.5-flash:generateContent?key={gemini_key}")
            req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=20) as resp:
                rj = json.loads(resp.read())
            fix_cmd = (rj.get("candidates", [{}])[0]
                       .get("content", {}).get("parts", [{}])[0].get("text", "")).strip()
        except Exception:
            fix_cmd = None

    if fix_cmd:
        # Run the fix command in the terminal
        sess.run_command(fix_cmd, source="ai_fix")
        sess.clear_last_error()
        return jsonify({"ok": True, "fix_command": fix_cmd})
    else:
        # Fall back to the static suggestion
        return jsonify({"ok": True, "fix_command": None,
                        "suggestion": suggestion})


# ── Phase 33: Session Persistence routes ──────────────────────────────

_SESSION_PERSIST_DIR = os.path.join(BASE_DIR, "data", "sessions")
os.makedirs(_SESSION_PERSIST_DIR, exist_ok=True)


def _persist_path(tid: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", tid)
    return os.path.join(_SESSION_PERSIST_DIR, f"{safe}.json")


@app.route("/api/session/<sid>/save", methods=["POST"])
def api_session_save(sid):
    """Persist session terminal history + decisions to disk."""
    data = request.get_json(silent=True) or {}
    tid  = _pty_session_id(sid)
    sess = terminal_registry.get(tid) if terminal_registry else None
    terminal_history = sess.get_history(500) if sess else []

    # Pull agent decisions from DB
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, kind, decision FROM decisions WHERE session_id=? ORDER BY ts",
            (sid,)
        ).fetchall()
    decisions = [{"ts": r[0], "kind": r[1], "decision": r[2]} for r in rows]

    payload = {
        "sid":              sid,
        "saved_at":         time.time(),
        "terminal_history": terminal_history,
        "agent_decisions":  decisions,
        "extra":            data.get("extra") or {},
    }
    path = _persist_path(sid)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return jsonify({"ok": True, "path": path, "records": len(decisions)})


@app.route("/api/session/<sid>/restore", methods=["GET"])
def api_session_restore(sid):
    """Load a previously saved session snapshot."""
    path = _persist_path(sid)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return jsonify({"ok": True, "snapshot": payload})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sessions/saved")
def api_sessions_saved():
    """List all persisted session snapshots."""
    snapshots = []
    for fname in os.listdir(_SESSION_PERSIST_DIR):
        if fname.endswith(".json"):
            try:
                fpath = os.path.join(_SESSION_PERSIST_DIR, fname)
                with open(fpath, "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
                snapshots.append({
                    "sid":        meta.get("sid"),
                    "saved_at":   meta.get("saved_at"),
                    "decisions":  len(meta.get("agent_decisions", [])),
                    "commands":   len(meta.get("terminal_history", [])),
                })
            except Exception:
                pass
    snapshots.sort(key=lambda x: x.get("saved_at") or 0, reverse=True)
    return jsonify({"ok": True, "snapshots": snapshots})


# Also auto-save terminal history on PTY close
_orig_pty_close = api_pty_close  # reference to existing route fn


# ─ Phase 32: Step-level reasoning enrichment ────────────────────────────
_STEP_STORE = {}
_STEP_LOCK  = threading.Lock()


def record_step(session_id, step):
    with _STEP_LOCK:
        _STEP_STORE.setdefault(session_id, []).append({**step, "ts": time.time()})
        if len(_STEP_STORE[session_id]) > 200:
            _STEP_STORE[session_id] = _STEP_STORE[session_id][-200:]


@app.route("/api/session/<sid>/steps", methods=["GET"])
def api_session_steps_get(sid):
    with _STEP_LOCK:
        steps = list(_STEP_STORE.get(sid, []))
    return jsonify({"ok": True, "steps": steps})


@app.route("/api/session/<sid>/steps", methods=["POST"])
def api_session_steps_post(sid):
    data = request.get_json(silent=True) or {}
    if not {"phase", "status"}.issubset(data.keys()):
        return jsonify({"ok": False, "error": "missing fields"}), 400
    record_step(sid, data)
    return jsonify({"ok": True})


# ─ Phase 32: Multimodal Analysis ─────────────────────────────────────

def _call_gemini_text(prompt, gemini_key):
    """Simple Gemini text call — returns response text or raises."""
    import urllib.request as _ur
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/"
           f"models/gemini-1.5-flash:generateContent?key={gemini_key}")
    req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=30) as resp:
        rj = json.loads(resp.read())
    return (rj.get("candidates", [{}])[0]
            .get("content", {}).get("parts", [{}])[0].get("text", ""))


@app.route("/api/analyze-image", methods=["POST"])
def api_analyze_image():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f   = request.files["file"]
    raw = f.read(10 * 1024 * 1024)
    ext = os.path.splitext(f.filename or "image.png")[1].lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")
    b64  = base64.b64encode(raw).decode("ascii")
    prompt = (
        "Analyze this image as a senior software engineer. Respond in JSON: "
        '{"description": "...", "insights": [...], "suggested_task": "..."}'
    )
    cfg = get_setting("config") or default_managed_config()
    env = env_for_session(cfg)
    gemini_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    analysis = None
    if gemini_key:
        try:
            import urllib.request as _ur
            payload = json.dumps({"contents": [{"parts": [
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": prompt}
            ]}]}).encode()
            url = ("https://generativelanguage.googleapis.com/v1beta/"
                   f"models/gemini-1.5-flash:generateContent?key={gemini_key}")
            req = _ur.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=30) as resp:
                rj = json.loads(resp.read())
            text = (rj.get("candidates", [{}])[0]
                    .get("content", {}).get("parts", [{}])[0].get("text", ""))
            try:
                i = text.find("{")
                analysis = json.loads(text[i:text.rfind("}")+1])
            except Exception:
                analysis = {"description": text, "insights": [], "suggested_task": ""}
        except Exception as e:
            analysis = {"description": f"Vision error: {e}", "insights": [], "suggested_task": ""}
    if not analysis:
        analysis = {"description": "No vision model configured.",
                    "insights": ["Set GEMINI_API_KEY for image analysis."],
                    "suggested_task": ""}
    return jsonify({"ok": True, "filename": f.filename, "mime": mime, "analysis": analysis})


@app.route("/api/analyze-code", methods=["POST"])
def api_analyze_code():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no_file"}), 400
    f = request.files["file"]
    raw = f.read(512 * 1024)
    code = raw.decode("utf-8", errors="replace")
    filename = f.filename or "file.py"
    ext      = os.path.splitext(filename)[1].lower()
    prompt = (
        f"Review this {ext} file named {filename}.\n\n"
        f"```\n{code[:6000]}\n```\n\n"
        "Respond in JSON: {\"summary\": \"...\", \"issues\": "
        "[{\"line\": N, \"severity\": \"error|warning|info\", "
        "\"description\": \"...\", \"fix\": \"...\"}], "
        "\"quality_score\": 0-100, \"suggested_task\": \"...\"}"
    )
    cfg = get_setting("config") or default_managed_config()
    env = env_for_session(cfg)
    gemini_key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    analysis = None
    if gemini_key:
        try:
            text = _call_gemini_text(prompt, gemini_key)
            try:
                i = text.find("{")
                analysis = json.loads(text[i:text.rfind("}")+1])
            except Exception:
                analysis = {"summary": text, "issues": [], "quality_score": 0, "suggested_task": ""}
        except Exception as e:
            analysis = {"summary": f"Analysis error: {e}", "issues": [],
                        "quality_score": 0, "suggested_task": ""}
    if not analysis:
        analysis = {"summary": "No model available.", "issues": [],
                    "quality_score": 0, "suggested_task": f"Review {filename}"}
    return jsonify({"ok": True, "filename": filename,
                    "lines": code.count("\n") + 1, "analysis": analysis})


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 34 — Semi-AGI Routes
# ═══════════════════════════════════════════════════════════════════════════

def _get_ltm():
    """Get LongTermMemory singleton — best effort."""
    try:
        from long_term_memory import get_ltm
        return get_ltm()
    except Exception:
        return None


# ── Long-Term Memory routes ────────────────────────────────────────────────

@app.route("/api/ltm/stats")
def api_ltm_stats():
    """Return LTM statistics."""
    ltm = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "error": "ltm_unavailable"}), 503
    return jsonify({"ok": True, "stats": ltm.stats()})


@app.route("/api/ltm/search")
def api_ltm_search():
    """Semantic search across long-term memory."""
    q = request.args.get("q", "").strip()
    k = int(request.args.get("k", 5))
    if not q:
        return jsonify({"ok": False, "error": "missing_query"}), 400
    ltm = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "error": "ltm_unavailable"}), 503
    hits = ltm.search(q, k=min(k, 20))
    return jsonify({"ok": True, "query": q, "hits": hits})


@app.route("/api/ltm/remember", methods=["POST"])
def api_ltm_remember():
    """Manually store a memory."""
    data = request.get_json(silent=True) or {}
    ltm  = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "error": "ltm_unavailable"}), 503
    mid = ltm.remember(
        kind    = data.get("kind", "tasks"),
        task    = data.get("task", ""),
        content = data.get("content", ""),
        meta    = data.get("meta") or {},
        success = bool(data.get("success", False)),
    )
    return jsonify({"ok": True, "id": mid})


@app.route("/api/ltm/solved")
def api_ltm_solved():
    """Check if this task was solved before."""
    task = request.args.get("task", "").strip()
    if not task:
        return jsonify({"ok": False, "error": "missing_task"}), 400
    ltm = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "error": "ltm_unavailable"}), 503
    prior = ltm.have_we_solved(task)
    return jsonify({"ok": True, "found": prior is not None, "result": prior})


# ── Tool Learning routes ───────────────────────────────────────────────────

@app.route("/api/tools/learned")
def api_tools_learned():
    """List all learned tools from LTM."""
    ltm = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "tools": []}), 503
    try:
        with ltm._conn() as c:
            rows = c.execute(
                "SELECT name,description,trigger_pattern,usage_count,last_used,ts "
                "FROM tools_learned ORDER BY usage_count DESC LIMIT 50"
            ).fetchall()
        tools = [dict(r) for r in rows]
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tools": tools, "count": len(tools)})


@app.route("/api/tools/learned", methods=["POST"])
def api_tools_learn():
    """Register a new learned tool."""
    data = request.get_json(silent=True) or {}
    ltm  = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "error": "ltm_unavailable"}), 503
    ok = ltm.learn_tool(
        name            = data.get("name", ""),
        description     = data.get("description", ""),
        trigger_pattern = data.get("trigger_pattern", ""),
        code_template   = data.get("code_template", ""),
    )
    return jsonify({"ok": ok})


@app.route("/api/tools/find")
def api_tools_find():
    """Find a matching learned tool for a task."""
    task = request.args.get("task", "").strip()
    ltm  = _get_ltm()
    if not ltm or not task:
        return jsonify({"ok": False, "tool": None})
    tool = ltm.find_tool(task)
    return jsonify({"ok": True, "found": tool is not None, "tool": tool})


# ── Task Generalization routes ─────────────────────────────────────────────

@app.route("/api/patterns")
def api_patterns():
    """List stored task patterns."""
    ltm = _get_ltm()
    if not ltm:
        return jsonify({"ok": False, "patterns": []}), 503
    k = int(request.args.get("k", 20))
    pats = ltm.recall_patterns("", k=k)
    return jsonify({"ok": True, "patterns": pats})


@app.route("/api/patterns/match")
def api_patterns_match():
    """Match a task to its generalized pattern."""
    task = request.args.get("task", "").strip()
    ltm  = _get_ltm()
    if not ltm or not task:
        return jsonify({"ok": False, "pattern": None})
    try:
        from cognitive_agents import TaskGeneralizationEngine
        gen = TaskGeneralizationEngine(ltm, lambda k, p: None)
        result = gen.generalize(task, success=True)
        recalled = gen.recall_pattern(task, k=3)
        return jsonify({"ok": True, "generalization": result,
                        "recalled_patterns": recalled})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Agent message bus routes ───────────────────────────────────────────────

@app.route("/api/agents/messages")
def api_agents_messages():
    """Return recent inter-agent messages from the active cognitive team."""
    try:
        from orchestrator import Orchestrator
        # Peek at the active orchestrator's cognitive team if one is running
        orch = _get_active_orchestrator()
        if orch and orch.cognitive_team:
            msgs = orch.cognitive_team.bus.recent(n=50)
            return jsonify({"ok": True, "messages": msgs})
    except Exception:
        pass
    return jsonify({"ok": True, "messages": []})


@app.route("/api/agents/status")
def api_agents_status():
    """Return the status of all five cognitive agents."""
    try:
        from cognitive_agents import CognitiveAgentTeam
        ltm = _get_ltm()
        stats = ltm.stats() if ltm else {}
        return jsonify({
            "ok": True,
            "agents": [
                {"name": "planner",  "role": "Task decomposition",      "active": True},
                {"name": "executor", "role": "Command/code execution",  "active": True},
                {"name": "critic",   "role": "Output evaluation",       "active": True},
                {"name": "debugger", "role": "Failure analysis + patch","active": True},
                {"name": "memory",   "role": "Semantic recall + tools", "active": True},
            ],
            "ltm": stats,
            "phase34": True,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Parallel strategy routes ───────────────────────────────────────────────

@app.route("/api/parallel/race", methods=["POST"])
def api_parallel_race():
    """Run a task through all 3 strategies in parallel, return the winner."""
    data    = request.get_json(silent=True) or {}
    task    = (data.get("task") or "").strip()
    sid     = data.get("session_id") or "global"
    if not task:
        return jsonify({"ok": False, "error": "missing_task"}), 400

    try:
        from cognitive_agents import ParallelStrategyEngine
        from orchestrator import Orchestrator
        cfg   = get_setting("config") or default_managed_config()
        orch  = Orchestrator(config=cfg)
        engine = ParallelStrategyEngine(max_parallel=3, timeout=90.0)
        events: list[dict] = []

        winner = engine.race(
            subtask    = task,
            execute_fn = lambda sub, strat: orch.execute(sub, strat),
            verify_fn  = lambda sub, er: orch.verify(sub, er),
            emit_fn    = lambda k, p: events.append({"kind": k, **p}),
        )
        return jsonify({
            "ok":       True,
            "winner":   winner.strategy,
            "ok_result": winner.ok,
            "score":    winner.score,
            "elapsed":  winner.elapsed,
            "events":   events[-20:],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Intelligence Dashboard ─────────────────────────────────────────────────

@app.route("/api/intelligence/dashboard")
def api_intelligence_dashboard():
    """Aggregate view of the Semi-AGI layer health and activity."""
    ltm = _get_ltm()
    ltm_stats = ltm.stats() if ltm else {}

    tools_count = 0
    recent_patterns: list[dict] = []
    if ltm:
        try:
            with ltm._conn() as c:
                tools_count = c.execute(
                    "SELECT COUNT(*) FROM tools_learned").fetchone()[0]
            recent_patterns = ltm.recall_patterns("", k=5)
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "phase":  34,
        "agents": [
            {"name": "planner",  "icon": "🗺️",  "status": "ready"},
            {"name": "executor", "icon": "⚙️",  "status": "ready"},
            {"name": "critic",   "icon": "🔍",  "status": "ready"},
            {"name": "debugger", "icon": "🐛",  "status": "ready"},
            {"name": "memory",   "icon": "🧠",  "status": "ready"},
        ],
        "ltm":           ltm_stats,
        "tools_learned": tools_count,
        "patterns":      recent_patterns,
        "capabilities": [
            "Inter-agent MessageBus",
            "Long-Term Memory (SQLite + ChromaDB)",
            "Tool Learning + Auto-selection",
            "Task Generalization Engine",
            "Parallel Strategy Racing",
            "Debugger Agent + AI Patch",
            "Session Persistence",
            "Terminal Intelligence",
        ],
    })


def _get_active_orchestrator():
    """Attempt to get a cached or newly created orchestrator."""
    try:
        from orchestrator import Orchestrator
        cfg = get_setting("config") or default_managed_config()
        return Orchestrator(config=cfg)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 35 — Enterprise Autonomous System Routes
# ═══════════════════════════════════════════════════════════════════════════

# ── Phase 35 helper singletons ────────────────────────────────────────────────

def _get_sandbox():
    try:
        from sandbox_manager import get_sandbox
        return get_sandbox()
    except Exception:
        return None

def _get_task_queue():
    try:
        from task_queue import get_queue
        return get_queue(emit_fn=lambda k, p: None)
    except Exception:
        return None

def _get_resource_tracker():
    try:
        from resource_tracker import get_tracker
        return get_tracker()
    except Exception:
        return None

def _get_model_router():
    try:
        from model_router import get_router
        return get_router()
    except Exception:
        return None

def _get_workflow_engine():
    try:
        from workflow_engine import get_workflow_engine
        return get_workflow_engine()
    except Exception:
        return None


# ── Sandbox routes ────────────────────────────────────────────────────────────

@app.route("/api/sandbox/run", methods=["POST"])
def api_sandbox_run():
    """Execute code in isolated sandbox."""
    data     = request.get_json(silent=True) or {}
    code     = data.get("code", "").strip()
    language = data.get("language", "python")
    timeout  = int(data.get("timeout", 30))
    task_id  = data.get("task_id", "")
    if not code:
        return jsonify({"ok": False, "error": "missing_code"}), 400
    sb = _get_sandbox()
    if not sb:
        return jsonify({"ok": False, "error": "sandbox_unavailable"}), 503
    result = sb.run(code=code, language=language, timeout=min(timeout, 120),
                    task_id=task_id)
    return jsonify({"ok": result.ok, "result": result.to_dict()})


@app.route("/api/sandbox/stats")
def api_sandbox_stats():
    sb = _get_sandbox()
    if not sb:
        return jsonify({"ok": False, "error": "sandbox_unavailable"}), 503
    return jsonify({"ok": True, "stats": sb.stats(),
                    "history": sb.history(n=10)})


# ── Task queue routes ─────────────────────────────────────────────────────────

@app.route("/api/queue/snapshot")
def api_queue_snapshot():
    q = _get_task_queue()
    if not q:
        return jsonify({"ok": False, "error": "queue_unavailable"}), 503
    return jsonify({"ok": True, **q.queue_snapshot()})


@app.route("/api/queue/workers")
def api_queue_workers():
    q = _get_task_queue()
    if not q:
        return jsonify({"ok": False, "error": "queue_unavailable"}), 503
    return jsonify({"ok": True, "workers": q.worker_status()})


@app.route("/api/queue/submit", methods=["POST"])
def api_queue_submit():
    """Submit an orchestrator task to the distributed queue."""
    data    = request.get_json(silent=True) or {}
    task    = (data.get("task") or "").strip()
    priority = int(data.get("priority", 2))
    budget  = float(data.get("budget_usd", 0.0))
    if not task:
        return jsonify({"ok": False, "error": "missing_task"}), 400
    q = _get_task_queue()
    if not q:
        return jsonify({"ok": False, "error": "queue_unavailable"}), 503

    def _run_task():
        try:
            from orchestrator import Orchestrator
            cfg  = get_setting("config") or default_managed_config()
            orch = Orchestrator(config=cfg)
            return orch.run(task)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    tid = q.submit(_run_task, name=task[:60], priority=priority,
                   budget_usd=budget, timeout_s=300)
    return jsonify({"ok": True, "task_id": tid})


@app.route("/api/queue/task/<task_id>", endpoint="api_p35_queue_task")
def api_p35_queue_task(task_id):
    q = _get_task_queue()
    if not q:
        return jsonify({"ok": False, "error": "queue_unavailable"}), 503
    info = q.get(task_id)
    if not info:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "task": info})


@app.route("/api/queue/checkpoint/<task_id>", methods=["GET", "POST"])
def api_queue_checkpoint(task_id):
    q = _get_task_queue()
    if not q:
        return jsonify({"ok": False}), 503
    if request.method == "POST":
        state = request.get_json(silent=True) or {}
        q.checkpoint(task_id, state)
        return jsonify({"ok": True})
    cp = q.restore_checkpoint(task_id)
    return jsonify({"ok": True, "checkpoint": cp})


# ── Resource + cost routes ────────────────────────────────────────────────────

@app.route("/api/costs/totals")
def api_costs_totals():
    hours = float(request.args.get("hours", 24))
    tracker = _get_resource_tracker()
    if not tracker:
        return jsonify({"ok": False, "error": "tracker_unavailable"}), 503
    return jsonify({"ok": True, "totals": tracker.totals(since_hours=hours),
                    "active": tracker.active()})


@app.route("/api/costs/recent")
def api_costs_recent():
    n = int(request.args.get("n", 20))
    tracker = _get_resource_tracker()
    if not tracker:
        return jsonify({"ok": False, "error": "tracker_unavailable"}), 503
    return jsonify({"ok": True, "records": tracker.recent(n=n)})


@app.route("/api/costs/task/<task_id>")
def api_costs_task(task_id):
    tracker = _get_resource_tracker()
    if not tracker:
        return jsonify({"ok": False, "error": "tracker_unavailable"}), 503
    s = tracker.summary(task_id)
    return jsonify({"ok": s is not None, "summary": s})


# ── Model routing routes ──────────────────────────────────────────────────────

@app.route("/api/models/status")
def api_models_status():
    router = _get_model_router()
    if not router:
        return jsonify({"ok": False, "error": "router_unavailable"}), 503
    return jsonify({"ok": True, "providers": router.provider_status()})


@app.route("/api/models/call", methods=["POST"])
def api_models_call():
    data = request.get_json(silent=True) or {}
    prompt    = (data.get("prompt") or "").strip()
    task_type = data.get("task_type", "default")
    max_tok   = int(data.get("max_tokens", 512))
    if not prompt:
        return jsonify({"ok": False, "error": "missing_prompt"}), 400
    router = _get_model_router()
    if not router:
        return jsonify({"ok": False, "error": "router_unavailable"}), 503
    result = router.call(prompt, task_type=task_type, max_tokens=max_tok)
    return jsonify({"ok": True, "result": result})


# ── Workflow routes ───────────────────────────────────────────────────────────

@app.route("/api/workflows")
def api_workflows_list():
    engine = _get_workflow_engine()
    if not engine:
        return jsonify({"ok": False, "error": "engine_unavailable"}), 503
    return jsonify({"ok": True, "workflows": engine.list_workflows()})


@app.route("/api/workflows/run", methods=["POST"])
def api_workflows_run():
    data     = request.get_json(silent=True) or {}
    workflow = data.get("workflow", "generate_and_test")
    task     = (data.get("task") or "").strip()
    language = data.get("language", "python")
    code     = data.get("code", "")
    budget   = float(data.get("budget_usd", 0.10))
    if not task:
        return jsonify({"ok": False, "error": "missing_task"}), 400
    engine = _get_workflow_engine()
    if not engine:
        return jsonify({"ok": False, "error": "engine_unavailable"}), 503
    events: list = []
    result = engine.run(
        workflow_name=workflow, task=task,
        language=language, existing_code=code,
        emit_fn=lambda k, p: events.append({"kind": k, **p}),
        budget_usd=budget)
    result["events"] = events[-30:]
    return jsonify({"ok": True, "result": result})


# ── Enterprise system health ──────────────────────────────────────────────────

@app.route("/api/system/health")
def api_system_health():
    """Aggregate health check across all Phase 35 subsystems."""
    import psutil
    sb       = _get_sandbox()
    q        = _get_task_queue()
    tracker  = _get_resource_tracker()
    router   = _get_model_router()
    engine   = _get_workflow_engine()

    cpu_pct  = psutil.cpu_percent(interval=0.1)
    mem      = psutil.virtual_memory()
    disk     = psutil.disk_usage(".")

    queue_snap = q.queue_snapshot() if q else {}
    cost_24h   = tracker.totals(24) if tracker else {}
    providers  = [p for p in (router.provider_status() if router else [])
                  if p["available"]]

    return jsonify({
        "ok":    True,
        "phase": 35,
        "system": {
            "cpu_pct":  round(cpu_pct, 1),
            "mem_pct":  round(mem.percent, 1),
            "mem_gb":   round(mem.used / 1e9, 2),
            "disk_pct": round(disk.percent, 1),
        },
        "sandbox": sb.stats() if sb else {"available": False},
        "queue": {
            "by_status": queue_snap.get("by_status", {}),
            "n_workers": queue_snap.get("n_workers", 0),
            "redis":     queue_snap.get("redis", False),
            "running":   len(queue_snap.get("running", [])),
            "queued":    len(queue_snap.get("queued", [])),
        },
        "costs_24h":      cost_24h,
        "models_ready":   len(providers),
        "workflows":      engine.list_workflows() if engine else [],
        "capabilities": [
            "Secure Sandbox (Docker/Process)",
            "Distributed Task Queue + Retry",
            "Token Cost Tracker + Budget Limits",
            "Multi-LLM Router + Fallback",
            "End-to-End Workflow Engine",
            "Hardware-Aware Self-Optimization",
            "Low Resource Mode",
            "Graceful Shutdown Handler",
        ],
    })

@app.route("/api/hardware/status")
def api_hardware_status():
    """Returns Phase 36 hardware monitoring data."""
    try:
        from hardware_monitor import get_hardware_monitor
        return jsonify({"ok": True, "data": get_hardware_monitor().status()})
    except ImportError:
        return jsonify({"ok": False, "error": "Hardware monitor not available"}), 503

@app.route("/api/models/allow_local", methods=["POST"])
def api_models_allow_local():
    """Toggle allow_local_models override in the Model Router."""
    router = _get_model_router()
    if not router:
        return jsonify({"ok": False, "error": "router_unavailable"}), 503
    
    data = request.json or {}
    allow = bool(data.get("allow", False))
    router.allow_local_models = allow
    return jsonify({"ok": True, "allow_local_models": router.allow_local_models})

@app.route("/api/evolution/stats")
def api_evolution_stats():
    try:
        from evolution_engine import get_evolution_engine
        return jsonify({"ok": True, "data": get_evolution_engine().get_dashboard_stats()})
    except ImportError:
        return jsonify({"ok": False, "error": "not implemented"}), 501

@app.route("/api/governance/status")
def api_governance_status():
    try:
        from governance_layer import get_governance_layer
        return jsonify({"ok": True, "data": get_governance_layer().get_dashboard_data()})
    except ImportError:
        return jsonify({"ok": False, "error": "not implemented"}), 501

@app.route("/api/governance/patch/apply", methods=["POST"])
def api_governance_patch_apply():
    try:
        from governance_layer import get_governance_layer
        data = request.json or {}
        pid = data.get("id")
        if not pid: return jsonify({"ok": False, "error": "missing_id"}), 400
        ok = get_governance_layer().apply_patch(pid)
        return jsonify({"ok": ok})
    except ImportError:
        return jsonify({"ok": False, "error": "not implemented"}), 501

# ── Phase 39: Autonomous Worker API ──────────────────────────────────────────

@app.route("/api/worker/status")
def api_worker_status():
    from autonomous_worker import get_worker
    return jsonify({"ok": True, "data": get_worker().status()})

@app.route("/api/worker/start", methods=["POST"])
def api_worker_start():
    from autonomous_worker import get_worker
    return jsonify(get_worker().start())

@app.route("/api/worker/stop", methods=["POST"])
def api_worker_stop():
    from autonomous_worker import get_worker
    return jsonify(get_worker().stop())

@app.route("/api/worker/goals/add", methods=["POST"])
def api_worker_add_goal():
    from autonomous_worker import get_worker
    data = request.json or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    gid = get_worker().add_goal(
        title=title,
        description=data.get("description", ""),
        priority=int(data.get("priority", 5)),
        max_iterations=int(data.get("max_iterations", 20)),
    )
    return jsonify({"ok": True, "goal_id": gid})

@app.route("/api/worker/goals/pause", methods=["POST"])
def api_worker_pause_goal():
    from autonomous_worker import get_worker
    data = request.json or {}
    gid = data.get("id")
    if not gid: return jsonify({"ok": False, "error": "id required"}), 400
    get_worker().pause_goal(gid)
    return jsonify({"ok": True})

@app.route("/api/worker/goals/resume", methods=["POST"])
def api_worker_resume_goal():
    from autonomous_worker import get_worker
    data = request.json or {}
    gid = data.get("id")
    if not gid: return jsonify({"ok": False, "error": "id required"}), 400
    get_worker().resume_goal(gid)
    return jsonify({"ok": True})

@app.route("/api/worker/goals/delete", methods=["POST"])
def api_worker_delete_goal():
    from autonomous_worker import get_worker
    data = request.json or {}
    gid = data.get("id")
    if not gid: return jsonify({"ok": False, "error": "id required"}), 400
    get_worker().delete_goal(gid)
    return jsonify({"ok": True})

@app.route("/api/worker/tools")
def api_worker_tools():
    from tool_integrations import get_tool_registry
    return jsonify({"ok": True, "data": get_tool_registry().list_tools()})

# ── Phase 40: AI Team / Multi-Worker API ──────────────────────────────────────

@app.route("/api/team/status")
def api_team_status():
    from worker_team import get_team
    return jsonify({"ok": True, "data": get_team().team_status()})

@app.route("/api/team/start", methods=["POST"])
def api_team_start():
    from worker_team import get_team
    return jsonify(get_team().start_all())

@app.route("/api/team/stop", methods=["POST"])
def api_team_stop():
    from worker_team import get_team
    return jsonify(get_team().stop_all())

@app.route("/api/team/worker/start", methods=["POST"])
def api_team_worker_start():
    from worker_team import get_team
    role = (request.json or {}).get("role", "")
    if not role: return jsonify({"ok": False, "error": "role required"}), 400
    return jsonify(get_team().start_worker(role))

@app.route("/api/team/worker/stop", methods=["POST"])
def api_team_worker_stop():
    from worker_team import get_team
    role = (request.json or {}).get("role", "")
    if not role: return jsonify({"ok": False, "error": "role required"}), 400
    return jsonify(get_team().stop_worker(role))

@app.route("/api/team/goals/global", methods=["POST"])
def api_team_add_global_goal():
    from worker_team import get_team
    data = request.json or {}
    title = data.get("title", "").strip()
    if not title: return jsonify({"ok": False, "error": "title required"}), 400
    gid = get_team().add_global_goal(title, data.get("description", ""))
    return jsonify({"ok": True, "global_goal_id": gid})

@app.route("/api/team/goals/assign", methods=["POST"])
def api_team_assign_goal():
    from worker_team import get_team
    data = request.json or {}
    role = data.get("role", "").strip()
    title = data.get("title", "").strip()
    if not role or not title: return jsonify({"ok": False, "error": "role and title required"}), 400
    result = get_team().add_worker_goal(role, title, data.get("description", ""),
                                        int(data.get("priority", 5)))
    return jsonify(result)

@app.route("/api/team/messages/clear", methods=["POST"])
def api_team_clear_messages():
    from worker_team import get_team
    get_team().clear_messages()
    return jsonify({"ok": True})

# ── Phase 41: Projects, Artifacts & Deployments ───────────────────────────────

@app.route("/api/projects/list")
def api_projects_list():
    from project_runner import get_project_runner
    return jsonify({"ok": True, "data": get_project_runner().list_projects()})

@app.route("/api/projects/stats")
def api_projects_stats():
    from project_runner import get_project_runner
    from artifact_registry import get_artifact_registry
    return jsonify({"ok": True,
                    "projects": get_project_runner().dashboard_stats(),
                    "artifacts": get_artifact_registry().dashboard_stats()})

@app.route("/api/projects/submit", methods=["POST"])
def api_projects_submit():
    from project_runner import get_project_runner
    data = request.json or {}
    name  = data.get("name", "").strip()
    goal  = data.get("goal", "").strip()
    if not name or not goal:
        return jsonify({"ok": False, "error": "name and goal required"}), 400
    pid = get_project_runner().submit_project(
        name=name, goal=goal,
        project_type=data.get("type", "website"),
        platform=data.get("platform", "local"),
        max_retries=int(data.get("max_retries", 2)),
    )
    return jsonify({"ok": True, "project_id": pid})

@app.route("/api/projects/<pid>")
def api_project_get(pid):
    from project_runner import get_project_runner
    p = get_project_runner().get_project(pid)
    if not p: return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "data": p})

@app.route("/api/artifacts/list")
def api_artifacts_list():
    from artifact_registry import get_artifact_registry
    status = request.args.get("status")
    atype  = request.args.get("type")
    return jsonify({"ok": True, "data": get_artifact_registry().list_artifacts(status, atype)})

@app.route("/api/artifacts/<aid>/versions")
def api_artifact_versions(aid):
    from artifact_registry import get_artifact_registry
    return jsonify({"ok": True, "data": get_artifact_registry().get_versions(aid)})

@app.route("/api/artifacts/<aid>/files")
def api_artifact_files(aid):
    from artifact_registry import get_artifact_registry
    return jsonify({"ok": True, "data": get_artifact_registry().get_files(aid)})

@app.route("/api/deploy/platforms")
def api_deploy_platforms():
    from deployment_engine import get_deployment_engine
    return jsonify({"ok": True, "data": get_deployment_engine().platforms()})

@app.route("/api/deploy/trigger", methods=["POST"])
def api_deploy_trigger():
    from deployment_engine import get_deployment_engine
    from artifact_registry import get_artifact_registry
    data = request.json or {}
    platform = data.get("platform", "").strip()
    artifact_id = data.get("artifact_id", "").strip()
    if not platform or not artifact_id:
        return jsonify({"ok": False, "error": "platform and artifact_id required"}), 400

    files = get_artifact_registry().get_files(artifact_id)
    art_list = get_artifact_registry().list_artifacts(limit=200)
    art = next((a for a in art_list if a["id"] == artifact_id), None)
    if not art: return jsonify({"ok": False, "error": "artifact not found"}), 404

    result = get_deployment_engine().deploy(platform, art["name"], files)
    if result.ok:
        get_artifact_registry().mark_deployed(artifact_id, platform, result.live_url,
                                               result.deployment_id, result.project_id)
    return jsonify({"ok": result.ok, "data": result.to_dict()})

# ── Phase 42: Browser Automation ──────────────────────────────────────────────
@app.route("/api/browser/run", methods=["POST"])
def api_browser_run():
    from browser_automation import get_browser_automation
    data = request.json or {}
    url = data.get("url")
    action = data.get("action", "scrape_text")
    
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
        
    browser = get_browser_automation()
    
    # We execute it in a thread to avoid blocking the event loop or if async is running
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser.run, action, url, **data)
        try:
            result = future.result(timeout=60)
            return jsonify({"ok": result.get("success", False), "data": result})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

# ── Phase 42: File/Project Analysis ───────────────────────────────────────────
@app.route("/api/analyze/file", methods=["POST"])
def api_analyze_large_file():
    from file_analyzer import get_file_analyzer
    data = request.json or {}
    file_path = data.get("file_path")
    if not file_path:
        return jsonify({"ok": False, "error": "file_path is required"}), 400
    
    analyzer = get_file_analyzer()
    result = analyzer.analyze_large_file(file_path)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})

@app.route("/api/analyze/project", methods=["POST"])
def api_analyze_project():
    from file_analyzer import get_file_analyzer
    data = request.json or {}
    project_dir = data.get("project_dir", "./workspace")
    
    analyzer = get_file_analyzer()
    result = analyzer.detect_incomplete_project(project_dir)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})

# ── Phase 42: GitHub Integration ──────────────────────────────────────────────
@app.route("/api/github/import", methods=["POST"])
def api_github_import():
    from github_analyzer import get_github_analyzer
    data = request.json or {}
    repo_url = data.get("repo_url")
    if not repo_url:
        return jsonify({"ok": False, "error": "repo_url is required"}), 400
        
    analyzer = get_github_analyzer()
    local_path = analyzer.clone_repo(repo_url)
    
    if not local_path:
        return jsonify({"ok": False, "error": "Failed to clone repository"}), 500
        
    structure = analyzer.analyze_structure(local_path)
    plan = analyzer.generate_completion_plan(structure)
    
    return jsonify({
        "ok": True, 
        "data": {
            "local_path": local_path,
            "structure": structure,
            "completion_plan": plan
        }
    })

# ── Phase 43: System Consolidation & Hardening ────────────────────────────────

@app.route("/api/trace/<task_id>", methods=["GET"])
def api_get_trace(task_id):
    from observability import get_observability
    trace = get_observability().get_trace(task_id)
    if trace:
        return jsonify({"ok": True, "data": trace})
    return jsonify({"ok": False, "error": "Trace not found"}), 404

@app.route("/api/models/decision", methods=["GET"])
def api_model_decision():
    task = request.args.get("task", "default")
    max_cost = float(request.args.get("max_cost", "999.0"))
    from model_router import get_router
    decision = get_router().get_decision(task_type=task, max_cost=max_cost)
    return jsonify({"ok": True, "data": decision})

@app.route("/api/system/stress-test", methods=["POST"])
def api_stress_test():
    import concurrent.futures
    from workflow_engine import get_workflow_engine
    
    tasks = [
        ("Write a hello world in Python", "python"),
        ("Write a JS function to sum an array", "javascript"),
        ("Write a python script that prints 'test'", "python"),
        ("Create a python dict with numbers 1 to 5", "python"),
        ("Write a js console.log('hello')", "javascript")
    ]
    
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_workflow_engine().run, t[0], t[1]): t for t in tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                results.append(res)
            except Exception as e:
                results.append({"ok": False, "error": str(e)})
                
    success_count = sum(1 for r in results if r.get("ok"))
    return jsonify({"ok": True, "data": {"total": 5, "success": success_count, "results": results}})

import time
import queue

from auth_system import token_required, auth_signup, auth_login, get_dashboard, init_db
from flask import g

workflow_queues = {}
RATE_LIMIT_STORE = {"requests": [], "active_workflows": 0}
CHAOS_FLAGS = {"tool_failure": False, "timeout": False, "model_failure": False}

@app.route("/api/auth/signup", methods=["POST"])
def api_auth_signup():
    data = request.json or {}
    success, msg = auth_signup(data.get("username"), data.get("password"))
    return jsonify({"ok": success, "message": msg}), 200 if success else 400

@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.json or {}
    success, token_or_msg = auth_login(data.get("username"), data.get("password"))
    if success:
        return jsonify({"ok": True, "token": token_or_msg})
    return jsonify({"ok": False, "error": token_or_msg}), 401

@app.route("/api/user/dashboard", methods=["GET"])
@token_required
def api_user_dashboard():
    data = get_dashboard(g.user_id)
    return jsonify({"ok": True, "data": data})

@app.route("/api/run_workflow", methods=["POST"])
@token_required
def api_workflow_run():
    # Rate limit check (Phase 49)
    now = time.time()
    RATE_LIMIT_STORE["requests"] = [t for t in RATE_LIMIT_STORE["requests"] if now - t < 60]
    if len(RATE_LIMIT_STORE["requests"]) >= 20: # max 20 per minute
        return jsonify({"ok": False, "error": "Rate limit exceeded. Max requests per minute reached."}), 429
    if RATE_LIMIT_STORE["active_workflows"] >= 3: # max 3 concurrent
        return jsonify({"ok": False, "error": "Too many concurrent workflows."}), 429

    data = request.json or {}
    task = data.get("task", "")
    if not task:
        return jsonify({"ok": False, "error": "task required"}), 400
        
    from safety_layer import check_prompt_safety, check_system_resources
    is_safe, reason = check_prompt_safety(task)
    if not is_safe:
        return jsonify({"ok": False, "error": f"Security block: {reason}"}), 403
        
    res_ok, res_reason = check_system_resources()
    if not res_ok:
        return jsonify({"ok": False, "error": f"Resource throttling: {res_reason}"}), 503
        
    RATE_LIMIT_STORE["requests"].append(now)
    RATE_LIMIT_STORE["active_workflows"] += 1
    
    from workflow_engine import get_workflow_engine
    import threading
    engine = get_workflow_engine()
    import uuid
    wid = uuid.uuid4().hex[:12]
    
    # Initialize queue for SSE
    workflow_queues[wid] = queue.Queue()
    
    def emit_fn(event, payload):
        if wid in workflow_queues:
            workflow_queues[wid].put({"event": event, "payload": payload})
            
    def workflow_thread(user_id):
        try:
            engine.run(task, "python", emit_fn, force_id=wid, chaos_flags=CHAOS_FLAGS, user_id=user_id)
        finally:
            RATE_LIMIT_STORE["active_workflows"] = max(0, RATE_LIMIT_STORE["active_workflows"] - 1)
            
    threading.Thread(target=workflow_thread, args=(g.user_id,), daemon=True).start()
    return jsonify({"ok": True, "workflow_id": wid})

@app.route("/api/system/chaos-test", methods=["POST"])
def api_chaos_test():
    data = request.json or {}
    mode = data.get("mode", "tool_failure")
    if mode in CHAOS_FLAGS:
        CHAOS_FLAGS[mode] = not CHAOS_FLAGS[mode]
    return jsonify({"ok": True, "chaos_mode": mode, "active": CHAOS_FLAGS[mode]})



@app.route("/api/tests/run", methods=["GET"])
def api_tests_run():
    from test_runner import run_tests
    try:
        results = run_tests()
        return jsonify({"ok": True, "data": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/artifacts/download", methods=["GET"])
def api_artifacts_download():
    import shutil, os, tempfile
    path = request.args.get("path")
    if not path or not os.path.exists(path):
        return "Artifact path not found", 404
        
    tmp_zip = tempfile.mktemp(suffix=".zip")
    if os.path.isdir(path):
        shutil.make_archive(tmp_zip.replace(".zip", ""), 'zip', path)
        return send_file(tmp_zip, as_attachment=True, download_name=f"{os.path.basename(path)}.zip")
    else:
        return send_file(path, as_attachment=True)

@app.route("/api/trace/stream/<wid>", methods=["GET"])
def api_trace_stream(wid):
    def generate():
        q = workflow_queues.get(wid)
        if not q:
            # If reconnecting after it's done, fallback to polling observability?
            # For simplicity, if queue missing, just close.
            yield "data: {\"event\": \"error\", \"payload\": {\"message\": \"Stream not found or expired\"}}\n\n"
            return
            
        try:
            while True:
                try:
                    msg = q.get(timeout=15) # Shorter timeout for more frequent pings
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg["event"] == "workflow_done" or msg["event"] == "error":
                        break
                except queue.Empty:
                    yield "data: {\"event\": \"ping\", \"payload\": {}}\n\n"
        finally:
            if wid in workflow_queues:
                del workflow_queues[wid]

    from flask import Response
    return Response(generate(), mimetype="text/event-stream")

@app.route("/api/workflow/<wid>", methods=["GET"])
def api_workflow_get(wid):
    from workflow_engine import get_workflow_engine
    res = get_workflow_engine().get_result(wid)
    if not res:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404
    return jsonify({"ok": True, "data": res})


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 52 — EXTENDED CAPABILITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/ollama/status", methods=["GET"])
def api_ollama_status():
    """Real-time Ollama diagnostics."""
    import urllib.request as ureq
    result = {"reachable": False, "models": [], "recommended": None, "error": None}
    from model_router import get_ollama_base_url
    ollama_base = get_ollama_base_url()
    try:
        r = ureq.urlopen(f"{ollama_base}/api/tags", timeout=3)
        data = json.loads(r.read())
        models = data.get("models", [])
        result["reachable"] = True
        result["models"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 2)} for m in models]
        local_models = [m for m in models if not any(x in m["name"] for x in [":cloud", "gpt-oss", "qwen3-coder:480b"])]
        result["local_models"] = [m["name"] for m in local_models]
        try:
            import psutil
            mem = psutil.virtual_memory()
            result["available_memory_gb"] = round(mem.available / 1e9, 2)
            for m in local_models:
                if m.get("size", 0) / 1e9 <= mem.available / 1e9 * 0.9:
                    result["recommended"] = m["name"]
                    break
        except ImportError:
            pass
        result["ollama_enabled"] = (
            os.environ.get("OLLAMA_ENABLED", "").lower() == "true" or
            os.environ.get("ALLOW_LOCAL_MODELS", "").lower() == "true"
        )
        result["current_model"] = os.environ.get("OLLAMA_LOCAL_MODEL", "phi3:mini")
    except Exception as e:
        result["error"] = str(e)
    return jsonify(result)


@app.route("/api/ollama/set-model", methods=["POST"])
def api_ollama_set_model():
    """Switch Ollama model at runtime."""
    data = request.json or {}
    model = data.get("model", "").strip()
    if not model:
        return jsonify({"ok": False, "error": "model is required"}), 400
    os.environ["OLLAMA_LOCAL_MODEL"] = model
    os.environ["OLLAMA_ENABLED"] = "true"
    import model_router as mr
    mr._router_instance = None
    return jsonify({"ok": True, "model": model})


@app.route("/api/github/analyze", methods=["POST"])
def api_github_analyze():
    """Clone and analyze a GitHub repository."""
    data = request.json or {}
    repo_url = data.get("url", "").strip()
    if not repo_url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    from github_analyzer import get_github_analyzer
    analyzer = get_github_analyzer()
    local_path = analyzer.clone_repo(repo_url)
    if not local_path:
        return jsonify({"ok": False, "error": "Failed to clone repository"}), 500
    structure = analyzer.analyze_structure(local_path)
    completion_plan = analyzer.generate_completion_plan(structure)
    has_readme = os.path.exists(os.path.join(local_path, "README.md"))
    try:
        top_files = [f for f in os.listdir(local_path) if not f.startswith(".")][:30]
    except Exception:
        top_files = []
    return jsonify({"ok": True, "repo_url": repo_url, "local_path": local_path,
                    "structure": structure, "completion_plan": completion_plan,
                    "has_readme": has_readme, "top_files": top_files})


@app.route("/api/file/analyze", methods=["POST"])
def api_file_analyze():
    """Analyze workspace file (large file chunking support)."""
    data = request.json or {}
    file_path = data.get("path", "").strip()
    max_chars = int(data.get("max_chars", 50000))
    if not file_path:
        return jsonify({"ok": False, "error": "path is required"}), 400
    safe_base = os.path.abspath("./workspace")
    full_path = os.path.abspath(file_path)
    if not full_path.startswith(safe_base):
        return jsonify({"ok": False, "error": "Access denied"}), 403
    if not os.path.exists(full_path):
        return jsonify({"ok": False, "error": "File not found"}), 404
    file_size = os.path.getsize(full_path)
    is_large = file_size > 10 * 1024 * 1024
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_chars) if is_large else f.read()
        ext = os.path.splitext(full_path)[1].lower()
        lang = {".py": "python", ".js": "javascript", ".ts": "typescript",
                ".html": "html", ".css": "css", ".json": "json",
                ".md": "markdown", ".sh": "bash"}.get(ext, "text")
        return jsonify({"ok": True, "path": file_path, "size_bytes": file_size,
                        "size_mb": round(file_size / 1024 / 1024, 2),
                        "is_large": is_large, "truncated": is_large,
                        "language": lang, "content": content,
                        "line_count": content.count("\n")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/readme/generate", methods=["POST"])
def api_readme_generate():
    """Auto-generate README.md for a workspace project."""
    import datetime
    data = request.json or {}
    project_path = data.get("path", "./workspace").strip()
    task_desc = data.get("task", "").strip()
    safe_base = os.path.abspath("./workspace")
    full_path = os.path.abspath(project_path)
    if not full_path.startswith(safe_base) and full_path != safe_base:
        return jsonify({"ok": False, "error": "Access denied"}), 403
    try:
        files = []
        ext_counts = {}
        for root, dirs, fnames in os.walk(full_path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in fnames[:50]:
                rel = os.path.relpath(os.path.join(root, fname), full_path)
                files.append(rel.replace("\\", "/"))
                ext = os.path.splitext(fname)[1].lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
        ptype = "Python" if ".py" in ext_counts else ("JavaScript" if ".js" in ext_counts else "Web" if ".html" in ext_counts else "General")
        file_list = "\n".join(files[:20]) + ("\n..." if len(files) > 20 else "")
        lang_list = "\n".join(f"- `{e}`: {c} files" for e, c in sorted(ext_counts.items(), key=lambda x: -x[1])[:8])
        run_cmd = "python main.py" if ".py" in ext_counts else ("npm start" if ".js" in ext_counts else "open index.html")
        install_cmd = "pip install -r requirements.txt" if ".py" in ext_counts else ("npm install" if ".js" in ext_counts else "")
        readme = f"# {task_desc or f'{ptype} Project'}\n\n> Generated by OpenHand AI\n\n## Overview\n\n{task_desc or f'A {ptype} project with {len(files)} files.'}\n\n## Structure\n\n```\n{file_list}\n```\n\n## Languages\n\n{lang_list}\n\n## Getting Started\n\n```bash\n{install_cmd}\n{run_cmd}\n```\n\n---\n*Generated: {datetime.datetime.now().strftime('%Y-%m-%d')}*\n"
        readme_path = os.path.join(full_path, "README.md")
        try:
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme)
            saved = True
        except Exception:
            saved = False
        return jsonify({"ok": True, "readme": readme, "saved": saved, "path": readme_path if saved else None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/providers/status", methods=["GET"])
def api_providers_status():
    """Real-time status of all LLM providers."""
    from model_router import get_router
    router = get_router()
    try:
        return jsonify({"ok": True, "providers": router.provider_status()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 7 — STRUCTURED AGENT SYSTEM
# 5 specialist agents: Code Reviewer, Debugger, Test Generator,
# Security Auditor, Performance Optimizer
# Pipeline runs automatically based on plan mode; toggleable per-agent.
# ═══════════════════════════════════════════════════════════════════════════════

_P7_AGENTS = {
    "reviewer": {
        "id":          "reviewer",
        "name":        "Code Reviewer",
        "icon":        "🔍",
        "description": "Checks code quality, readability, and best practices",
        "plans":       ["pro", "elite"],
        "auto_trigger": ["always"],
        "color":       "#6c8ebf",
    },
    "debugger": {
        "id":          "debugger",
        "name":        "Debugger",
        "icon":        "🐛",
        "description": "Detects runtime errors and suggests targeted fixes",
        "plans":       ["pro", "elite"],
        "auto_trigger": ["on_error"],
        "color":       "#d6a94a",
    },
    "tester": {
        "id":          "tester",
        "name":        "Test Generator",
        "icon":        "🧪",
        "description": "Generates unit tests and test coverage suggestions",
        "plans":       ["elite"],
        "auto_trigger": ["on_code"],
        "color":       "#6bbf6c",
    },
    "security": {
        "id":          "security",
        "name":        "Security Auditor",
        "icon":        "🛡",
        "description": "Scans for vulnerabilities and security anti-patterns",
        "plans":       ["elite"],
        "auto_trigger": ["on_backend"],
        "color":       "#bf6c6c",
    },
    "optimizer": {
        "id":          "optimizer",
        "name":        "Performance Optimizer",
        "icon":        "⚡",
        "description": "Identifies bottlenecks and efficiency improvements",
        "plans":       ["elite"],
        "auto_trigger": ["on_heavy"],
        "color":       "#a56cbf",
    },
}

# Per-session pipeline state: { sid: { stage, agents: [{id, status, result}] } }
_P7_PIPELINES: dict = {}
_p7_lock = threading.Lock()

_P7_SETTINGS_KEY = "p7_agent_config"

_P7_PIPELINE_ORDER = ["debugger", "reviewer", "security", "optimizer", "tester"]


def p7_get_user_config() -> dict:
    """Return user's agent toggle config."""
    return get_setting(_P7_SETTINGS_KEY, {
        "enabled": True,
        "toggles": {aid: True for aid in _P7_AGENTS},
    })


def p7_should_trigger(agent_id: str, task: str, log_text: str, plan_mode: str) -> bool:
    """Smart triggering: decide if this agent should run given context."""
    agent = _P7_AGENTS[agent_id]
    # Plan gate
    if plan_mode not in agent["plans"]:
        return False
    triggers = agent["auto_trigger"]
    task_l = task.lower()
    log_l  = log_text.lower()
    if "always" in triggers:
        return True
    if "on_error" in triggers:
        error_hints = ["error", "traceback", "exception", "failed", "fail:", "[error]", "❌"]
        return any(h in log_l for h in error_hints)
    if "on_code" in triggers:
        code_hints = ["def ", "class ", "function", "import ", "require(", "```python",
                      "```js", "```javascript", "#!/usr", ".py", ".js", ".ts"]
        return any(h in log_l or h in task_l for h in code_hints)
    if "on_backend" in triggers:
        backend_hints = ["api", "auth", "database", "db", "sql", "server", "backend",
                         "password", "token", "secret", "key", "flask", "django",
                         "fastapi", "express", "login", "endpoint", "http"]
        return any(h in task_l or h in log_l for h in backend_hints)
    if "on_heavy" in triggers:
        heavy_hints = ["performance", "speed", "optimiz", "slow", "loop", "algorithm",
                       "process", "compute", "scale", "load", "cache", "async", "concurrent"]
        return any(h in task_l or h in log_l for h in heavy_hints)
    return False


def p7_run_reviewer(task: str, log_text: str, code_snippets: list) -> dict:
    """Code quality review via pattern analysis."""
    findings = []
    score = 100
    text = "\n".join(code_snippets) + "\n" + log_text

    checks = [
        (["TODO", "FIXME", "HACK", "XXX"],           "Contains TODO/FIXME markers — resolve before production",    5),
        (["print(", "console.log("],                  "Debug print statements detected — use proper logging",       5),
        (["except:", "except Exception:"],            "Bare except clauses catch all errors — be more specific",    8),
        (["global "],                                 "Global variable usage detected — consider refactoring",      6),
        (["password", "secret", "api_key"],           "Sensitive variable names detected in code",                 10),
        (["time.sleep", "Thread.sleep"],              "Blocking sleep calls found — consider async alternatives",   5),
        (["eval(", "exec("],                          "Dynamic code execution (eval/exec) detected — risky",       10),
    ]
    for keywords, msg, penalty in checks:
        if any(k.lower() in text.lower() for k in keywords):
            findings.append({"severity": "warn", "message": msg})
            score = max(0, score - penalty)

    # Positive signals
    positive = []
    if '"""' in text or "'''" in text:
        positive.append("Docstrings present — good documentation habit")
    if "import logging" in text or "logger" in text:
        positive.append("Proper logging used")
    if "try:" in text and "except" in text:
        positive.append("Error handling implemented")

    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    return {
        "score":     score,
        "grade":     grade,
        "findings":  findings,
        "positive":  positive,
        "summary":   f"Code quality score: {score}/100 (Grade {grade}). "
                     f"{len(findings)} issue(s) found.",
    }


def p7_run_debugger(task: str, log_text: str) -> dict:
    """Extract and explain errors from session logs."""
    errors = []
    lines = log_text.split("\n")
    error_patterns = [
        ("Traceback", "Python exception — check stack trace above for root cause"),
        ("NameError",  "Variable or name not defined — check spelling and scope"),
        ("TypeError",  "Wrong type passed to function — verify argument types"),
        ("ImportError", "Module import failed — check package is installed"),
        ("ModuleNotFoundError", "Missing dependency — run pip install for the package"),
        ("AttributeError", "Object missing attribute — verify object type and API"),
        ("SyntaxError",    "Syntax error in code — check for missing colons, brackets"),
        ("IndentationError","Indentation error — check consistent spaces/tabs"),
        ("KeyError",       "Dictionary key missing — use .get() or check key exists"),
        ("IndexError",     "List index out of range — check list length before access"),
        ("FileNotFoundError","File not found — verify path and working directory"),
        ("ConnectionError","Network connection failed — check URL and connectivity"),
        ("TimeoutError",   "Operation timed out — add retry logic or increase timeout"),
        ("[ERROR]",        "Agent reported an error — see logs for details"),
        ("❌",             "Task step failed — see adjacent log lines for context"),
    ]
    seen = set()
    for line in lines:
        for pattern, explanation in error_patterns:
            if pattern.lower() in line.lower() and pattern not in seen:
                errors.append({"error": pattern, "explanation": explanation, "line": line.strip()[:120]})
                seen.add(pattern)

    fixes = []
    if not errors:
        fixes.append("No errors detected in session logs — task completed cleanly")
    else:
        fixes.append(f"Found {len(errors)} error pattern(s) — address the highest-severity first")
        if any("Import" in e["error"] or "Module" in e["error"] for e in errors):
            fixes.append("Run: pip install -r requirements.txt to resolve missing packages")
        if any("Syntax" in e["error"] or "Indentation" in e["error"] for e in errors):
            fixes.append("Use a linter (flake8/pylint) to catch syntax issues before running")

    return {
        "errors_found": len(errors),
        "errors":        errors[:8],
        "fixes":         fixes,
        "summary":       f"{'No errors detected.' if not errors else f'{len(errors)} error type(s) identified with suggested fixes.'}",
    }


def p7_run_tester(task: str, log_text: str, code_snippets: list) -> dict:
    """Generate test case suggestions based on code found."""
    text = "\n".join(code_snippets) + "\n" + task
    tests = []

    import re
    func_matches = re.findall(r"def\s+(\w+)\s*\(", text)
    class_matches = re.findall(r"class\s+(\w+)\s*[\(:]", text)

    for fn in func_matches[:5]:
        if fn.startswith("_"):
            continue
        tests.append({
            "type": "unit",
            "target": fn,
            "suggestion": f"def test_{fn}(): — verify expected return value and edge cases (empty input, None, boundary values)",
        })
    for cls in class_matches[:3]:
        tests.append({
            "type": "class",
            "target": cls,
            "suggestion": f"test_{cls.lower()}_init() — verify object creation; test_{cls.lower()}_methods() — verify key method outputs",
        })

    if not tests:
        tests.append({
            "type": "integration",
            "target": "main flow",
            "suggestion": "Write an integration test covering the full task flow end-to-end",
        })

    coverage_target = min(80 + len(tests) * 2, 95)
    return {
        "tests_suggested": len(tests),
        "tests":           tests,
        "coverage_target": coverage_target,
        "summary":         f"{len(tests)} test case(s) suggested. Target ≥{coverage_target}% coverage.",
    }


def p7_run_security(task: str, log_text: str, code_snippets: list) -> dict:
    """Scan for common security anti-patterns."""
    text = "\n".join(code_snippets) + "\n" + log_text + "\n" + task
    vulns = []

    checks = [
        (["password =", 'password="', "password='"],
         "HIGH", "Hardcoded password detected — use environment variables"),
        (["api_key =", 'api_key="', "SECRET_KEY ="],
         "HIGH", "Hardcoded API key/secret — move to .env file"),
        (["eval(", "exec("],
         "HIGH", "Dynamic code execution — potential code injection vector"),
        (["subprocess.call", "os.system", "shell=True"],
         "MEDIUM", "Shell execution with user data — risk of command injection"),
        (["SQL", "SELECT", "INSERT", "UPDATE", "DELETE", "WHERE"],
         "MEDIUM", "SQL operations detected — ensure parameterized queries, avoid string concat"),
        (["pickle.load", "pickle.loads"],
         "MEDIUM", "Pickle deserialization — unsafe with untrusted data"),
        (["http://", "verify=False"],
         "LOW", "Insecure HTTP or disabled TLS verification detected"),
        (["debug=True", "DEBUG = True"],
         "LOW", "Debug mode enabled — disable in production"),
        (["cors", "Access-Control-Allow-Origin: *"],
         "LOW", "Permissive CORS policy — restrict allowed origins in production"),
    ]

    for keywords, severity, message in checks:
        if any(k.lower() in text.lower() for k in keywords):
            vulns.append({"severity": severity, "message": message})

    high   = sum(1 for v in vulns if v["severity"] == "HIGH")
    medium = sum(1 for v in vulns if v["severity"] == "MEDIUM")
    low    = sum(1 for v in vulns if v["severity"] == "LOW")
    risk   = "Critical" if high >= 2 else "High" if high else "Medium" if medium >= 2 else "Low" if medium else "Clean"

    return {
        "risk_level": risk,
        "total":      len(vulns),
        "high":       high,
        "medium":     medium,
        "low":        low,
        "findings":   vulns[:8],
        "summary":    f"Risk level: {risk}. {high} high, {medium} medium, {low} low severity issue(s).",
    }


def p7_run_optimizer(task: str, log_text: str, code_snippets: list) -> dict:
    """Identify performance bottlenecks and suggest improvements."""
    text = "\n".join(code_snippets) + "\n" + task
    suggestions = []

    patterns = [
        (["for ", "while "],
         "Loop optimization",
         "Consider list comprehensions or vectorized operations (numpy) for heavy loops"),
        (["time.sleep", "sleep("],
         "Blocking sleep",
         "Replace blocking sleeps with asyncio.sleep() in async contexts"),
        (["requests.get", "requests.post"],
         "Sync HTTP calls",
         "Use async HTTP (httpx/aiohttp) for concurrent API calls"),
        (["open(", "read(", "write("],
         "File I/O",
         "Buffer file reads, use context managers, consider async I/O for large files"),
        (["SELECT *"],
         "Database query",
         "Avoid SELECT * — fetch only needed columns to reduce data transfer"),
        (["json.loads", "json.dumps"],
         "JSON parsing",
         "For high-frequency JSON ops, consider ujson or orjson for 3-10x speedup"),
        (["import "],
         "Import overhead",
         "Move heavy imports inside functions if used rarely to reduce startup time"),
        (["print("],
         "Logging overhead",
         "Replace print() with logging module — disable debug logs in production"),
    ]

    for keywords, category, suggestion in patterns:
        if any(k.lower() in text.lower() for k in keywords):
            suggestions.append({"category": category, "suggestion": suggestion})

    if not suggestions:
        suggestions.append({
            "category": "General",
            "suggestion": "No major bottlenecks detected. Profile with cProfile for deeper analysis.",
        })

    return {
        "opportunities": len(suggestions),
        "suggestions":   suggestions[:6],
        "estimated_gain": f"{min(len(suggestions) * 15, 60)}% potential improvement",
        "summary":        f"{len(suggestions)} optimization opportunity(-ies) identified.",
    }


def p7_execute_pipeline(sid: str, task: str, log_text: str,
                         plan_mode: str, enabled_agents: dict) -> None:
    """Run the multi-agent pipeline in a background thread."""
    with _p7_lock:
        pipeline = _P7_PIPELINES.get(sid)
        if not pipeline:
            return
        pipeline["stage"] = "running"
        pipeline["started_at"] = time.time()

    # Extract code snippets from log (lines that look like code)
    code_snippets = [ln for ln in log_text.split("\n")
                     if any(h in ln for h in ["def ", "class ", "import ", "function", "const ", "var "])]

    for agent_id in _P7_PIPELINE_ORDER:
        if agent_id not in _P7_AGENTS:
            continue
        # Check user toggle
        if not enabled_agents.get(agent_id, True):
            with _p7_lock:
                for ag in _P7_PIPELINES[sid]["agents"]:
                    if ag["id"] == agent_id:
                        ag["status"] = "skipped"
                        ag["skipped_reason"] = "disabled"
            continue
        # Check smart triggering
        should_run = p7_should_trigger(agent_id, task, log_text, plan_mode)
        if not should_run:
            with _p7_lock:
                for ag in _P7_PIPELINES[sid]["agents"]:
                    if ag["id"] == agent_id:
                        ag["status"] = "skipped"
                        ag["skipped_reason"] = "not triggered"
            continue
        # Mark as running
        with _p7_lock:
            for ag in _P7_PIPELINES[sid]["agents"]:
                if ag["id"] == agent_id:
                    ag["status"] = "running"
                    ag["started_at"] = time.time()
        # Run the agent
        try:
            t0 = time.time()
            if agent_id == "reviewer":
                result = p7_run_reviewer(task, log_text, code_snippets)
            elif agent_id == "debugger":
                result = p7_run_debugger(task, log_text)
            elif agent_id == "tester":
                result = p7_run_tester(task, log_text, code_snippets)
            elif agent_id == "security":
                result = p7_run_security(task, log_text, code_snippets)
            elif agent_id == "optimizer":
                result = p7_run_optimizer(task, log_text, code_snippets)
            else:
                result = {}
            elapsed = round(time.time() - t0, 2)
            with _p7_lock:
                for ag in _P7_PIPELINES[sid]["agents"]:
                    if ag["id"] == agent_id:
                        ag["status"]   = "done"
                        ag["result"]   = result
                        ag["elapsed"]  = elapsed
        except Exception as exc:
            with _p7_lock:
                for ag in _P7_PIPELINES[sid]["agents"]:
                    if ag["id"] == agent_id:
                        ag["status"] = "error"
                        ag["error"]  = str(exc)[:200]
        # Small pause between agents so the UI can show sequential progress
        time.sleep(0.4)

    with _p7_lock:
        if sid in _P7_PIPELINES:
            _P7_PIPELINES[sid]["stage"] = "done"
            _P7_PIPELINES[sid]["finished_at"] = time.time()


def p7_init_pipeline(sid: str, plan_mode: str, task: str, enabled_agents: dict) -> dict:
    """Initialise (or reset) the pipeline state for a session."""
    agents_state = []
    for agent_id in _P7_PIPELINE_ORDER:
        meta = _P7_AGENTS[agent_id]
        agents_state.append({
            "id":     agent_id,
            "name":   meta["name"],
            "icon":   meta["icon"],
            "color":  meta["color"],
            "status": "pending",
            "result": None,
        })
    state = {
        "sid":        sid,
        "plan_mode":  plan_mode,
        "task":       task[:200],
        "stage":      "pending",
        "agents":     agents_state,
        "created_at": time.time(),
    }
    with _p7_lock:
        _P7_PIPELINES[sid] = state
    return state


@app.route("/api/p7/agents")
def api_p7_agents():
    """Return the 5 Phase 7 agent definitions."""
    return jsonify({"ok": True, "agents": list(_P7_AGENTS.values())})


@app.route("/api/p7/config", methods=["GET", "POST"])
def api_p7_config():
    """Get or save user's agent toggle configuration."""
    if request.method == "POST":
        d = request.get_json() or {}
        cfg = p7_get_user_config()
        if "enabled" in d:
            cfg["enabled"] = bool(d["enabled"])
        if "toggles" in d and isinstance(d["toggles"], dict):
            for aid, val in d["toggles"].items():
                if aid in _P7_AGENTS:
                    cfg["toggles"][aid] = bool(val)
        set_setting(_P7_SETTINGS_KEY, cfg)
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": True, "config": p7_get_user_config()})


@app.route("/api/p7/pipeline/run", methods=["POST"])
def api_p7_pipeline_run():
    """Trigger the multi-agent pipeline for a completed session."""
    d         = request.get_json() or {}
    sid       = (d.get("sid") or "").strip()
    task      = (d.get("task") or "").strip()
    plan_mode = (d.get("plan_mode") or "elite").lower()
    log_text  = (d.get("log_text") or "").strip()

    if not sid:
        return jsonify({"ok": False, "error": "sid required"}), 400
    if plan_mode == "lite":
        return jsonify({"ok": False, "error": "Agents not available in Lite mode"}), 400

    user_cfg      = p7_get_user_config()
    system_enabled = user_cfg.get("enabled", True)
    if not system_enabled:
        return jsonify({"ok": False, "error": "Agent system is disabled"}), 400

    enabled_agents = user_cfg.get("toggles", {agent_id: True for agent_id in _P7_AGENTS})
    state = p7_init_pipeline(sid, plan_mode, task, enabled_agents)

    # Launch pipeline in background thread
    t = threading.Thread(
        target=p7_execute_pipeline,
        args=(sid, task, log_text, plan_mode, enabled_agents),
        daemon=True,
        name=f"p7-pipeline-{sid[:8]}",
    )
    t.start()

    return jsonify({"ok": True, "sid": sid, "state": state})


@app.route("/api/p7/pipeline/status/<sid>")
def api_p7_pipeline_status(sid):
    """Return current pipeline state for a session."""
    with _p7_lock:
        state = _P7_PIPELINES.get(sid)
    if not state:
        return jsonify({"ok": False, "error": "No pipeline for this session"}), 404
    return jsonify({"ok": True, "state": state})


@app.route("/api/p7/pipeline/clear/<sid>", methods=["POST"])
def api_p7_pipeline_clear(sid):
    """Clear pipeline state for a session."""
    with _p7_lock:
        _P7_PIPELINES.pop(sid, None)
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 8 — MONETIZATION & ACCESS CONTROL LAYER
# Subscription plans, usage tracking, feature gating, coupon system.
# NO real payment processing — lightweight SaaS scaffolding.
# ═══════════════════════════════════════════════════════════════════════════════

import datetime as _dt

_P8_PLANS = {
    "free": {
        "name":        "Free",
        "price":       "$0 / month",
        "icon":        "🆓",
        "color":       "#4caf50",
        "limits": {
            "lite_daily":    None,   # unlimited
            "pro_daily":     10,     # 10 Pro runs / day
            "elite_monthly": 0,      # blocked
        },
        "features": [
            "Unlimited Lite runs",
            "10 Pro runs per day",
            "Elite mode: blocked",
            "BYOK (bring your own keys)",
            "Session history",
        ],
    },
    "pro": {
        "name":        "Pro",
        "price":       "$20 / month",
        "icon":        "⭐",
        "color":       "#388bfd",
        "limits": {
            "lite_daily":    None,
            "pro_daily":     None,   # unlimited
            "elite_monthly": 20,     # 20 Elite runs / month
        },
        "features": [
            "Unlimited Lite runs",
            "Unlimited Pro runs",
            "20 Elite runs per month",
            "Agent pipeline (Reviewer + Debugger)",
            "Priority routing",
            "BYOK Priority Mode",
        ],
    },
    "elite": {
        "name":        "Elite",
        "price":       "$50 / month",
        "icon":        "👑",
        "color":       "#bc8cff",
        "limits": {
            "lite_daily":    None,
            "pro_daily":     None,
            "elite_monthly": None,   # unlimited
        },
        "features": [
            "Unlimited Lite runs",
            "Unlimited Pro runs",
            "Unlimited Elite runs",
            "Full 5-agent pipeline",
            "Priority routing + failover",
            "All future features",
            "BYOK Priority Mode",
        ],
    },
}

# Valid coupon codes: { code: { plan, days, note } }
_P8_COUPONS = {
    "HAMMAD30":  {"plan": "pro",   "days": 30,  "note": "30 days of Pro"},
    "ELITE7":    {"plan": "elite", "days": 7,   "note": "7-day Elite trial"},
    "PROLIFE":   {"plan": "pro",   "days": 3650,"note": "Lifetime Pro"},
    "ELITE30":   {"plan": "elite", "days": 30,  "note": "30 days Elite"},
    "OPENHAND":  {"plan": "pro",   "days": 90,  "note": "90-day Pro"},
    "TRYAGENT":  {"plan": "pro",   "days": 14,  "note": "2-week Pro trial"},
    "AGENTELITE":{"plan": "elite", "days": 14,  "note": "2-week Elite trial"},
}

_P8_KEY = "p8_subscription"   # settings key for subscription state


def p8_get_state() -> dict:
    """Return current subscription state, defaulting to Free."""
    default = {
        "plan":          "free",
        "expires":       None,    # ISO date string or None = permanent
        "coupon_used":   None,
        "byok_priority": False,
        "usage": {
            "pro_daily":     {"date": "",  "count": 0},
            "elite_monthly": {"month": "", "count": 0},
        },
    }
    state = get_setting(_P8_KEY, default)
    # Merge missing keys from default (forward-compat)
    for k, v in default.items():
        if k not in state:
            state[k] = v
    if "usage" not in state:
        state["usage"] = default["usage"]
    for k, v in default["usage"].items():
        if k not in state["usage"]:
            state["usage"][k] = v
    return state


def p8_save_state(state: dict) -> None:
    set_setting(_P8_KEY, state)


def p8_effective_plan(state: dict) -> str:
    """Return the active plan, checking expiry."""
    plan = state.get("plan", "free")
    expires = state.get("expires")
    if expires and plan != "free":
        try:
            exp_dt = _dt.date.fromisoformat(expires)
            if _dt.date.today() > exp_dt:
                # Expired — downgrade to free
                state["plan"]    = "free"
                state["expires"] = None
                p8_save_state(state)
                return "free"
        except (ValueError, TypeError):
            pass
    return plan


def p8_get_usage(state: dict) -> dict:
    """Return normalised usage counters, resetting stale windows."""
    today  = _dt.date.today().isoformat()
    month  = _dt.date.today().strftime("%Y-%m")
    usage  = state.get("usage", {})

    pd = usage.get("pro_daily", {})
    if pd.get("date") != today:
        pd = {"date": today, "count": 0}
        usage["pro_daily"] = pd

    em = usage.get("elite_monthly", {})
    if em.get("month") != month:
        em = {"month": month, "count": 0}
        usage["elite_monthly"] = em

    state["usage"] = usage
    return usage


def p8_check_and_increment(exec_mode: str, plan: str, state: dict) -> tuple[bool, str]:
    """
    Check if the requested exec_mode is allowed under `plan`.
    Returns (allowed: bool, reason: str).
    If allowed, increments the relevant counter and saves state.
    """
    limits    = _P8_PLANS[plan]["limits"]
    usage     = p8_get_usage(state)
    exec_mode = exec_mode.lower()

    if exec_mode == "lite":
        # Always allowed
        return True, ""

    if exec_mode == "pro":
        cap = limits["pro_daily"]
        if cap is None:
            return True, ""
        pd = usage["pro_daily"]
        if pd["count"] >= cap:
            return False, f"Pro limit reached ({cap}/day). Upgrade to Pro plan for unlimited Pro runs."
        pd["count"] += 1
        p8_save_state(state)
        return True, ""

    if exec_mode == "elite":
        cap = limits["elite_monthly"]
        if cap == 0:
            return False, "Elite mode requires a Pro or Elite plan. Upgrade to unlock."
        if cap is None:
            return True, ""
        em = usage["elite_monthly"]
        if em["count"] >= cap:
            return False, f"Elite limit reached ({cap}/month). Upgrade to Elite plan for unlimited Elite runs."
        em["count"] += 1
        p8_save_state(state)
        return True, ""

    return True, ""


@app.route("/api/plan/info")
def api_plan_info():
    """Return subscription state, plan details, and usage counters."""
    state  = p8_get_state()
    plan   = p8_effective_plan(state)
    usage  = p8_get_usage(state)
    limits = _P8_PLANS[plan]["limits"]
    today  = _dt.date.today().isoformat()
    month  = _dt.date.today().strftime("%Y-%m")

    pd_count = usage.get("pro_daily",     {}).get("count", 0)
    em_count = usage.get("elite_monthly", {}).get("count", 0)

    return jsonify({
        "ok":     True,
        "plan":   plan,
        "meta":   _P8_PLANS[plan],
        "expires": state.get("expires"),
        "coupon":  state.get("coupon_used"),
        "byok_priority": state.get("byok_priority", False),
        "usage": {
            "pro_daily":     {"count": pd_count, "limit": limits["pro_daily"],     "date": today},
            "elite_monthly": {"count": em_count, "limit": limits["elite_monthly"], "month": month},
        },
        "all_plans": {k: {"name": v["name"], "price": v["price"], "icon": v["icon"],
                          "color": v["color"], "features": v["features"]}
                      for k, v in _P8_PLANS.items()},
    })


@app.route("/api/plan/set", methods=["POST"])
def api_plan_set():
    """Directly set plan (admin / demo use — no payment check)."""
    d    = request.get_json() or {}
    plan = d.get("plan", "free").lower()
    if plan not in _P8_PLANS:
        return jsonify({"ok": False, "error": "Unknown plan"}), 400
    state = p8_get_state()
    state["plan"]        = plan
    state["expires"]     = d.get("expires")   # optional ISO date
    state["coupon_used"] = None
    p8_save_state(state)
    return jsonify({"ok": True, "plan": plan, "meta": _P8_PLANS[plan]})


@app.route("/api/plan/apply-coupon", methods=["POST"])
def api_plan_apply_coupon():
    """Apply a coupon code to unlock a plan."""
    d    = request.get_json() or {}
    code = (d.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "Coupon code required"}), 400
    coupon = _P8_COUPONS.get(code)
    if not coupon:
        return jsonify({"ok": False, "error": "Invalid coupon code. Check spelling and try again."}), 400

    plan    = coupon["plan"]
    days    = coupon["days"]
    expires = (_dt.date.today() + _dt.timedelta(days=days)).isoformat()
    state   = p8_get_state()
    # Only upgrade, never downgrade via coupon
    plan_rank = {"free": 0, "pro": 1, "elite": 2}
    if plan_rank.get(plan, 0) <= plan_rank.get(p8_effective_plan(state), 0) and p8_effective_plan(state) != "free":
        return jsonify({"ok": False, "error": "You already have an equal or better plan."}), 400

    state["plan"]        = plan
    state["expires"]     = expires if days < 3650 else None
    state["coupon_used"] = code
    p8_save_state(state)
    return jsonify({
        "ok":      True,
        "plan":    plan,
        "expires": state["expires"],
        "note":    coupon["note"],
        "meta":    _P8_PLANS[plan],
    })


@app.route("/api/plan/usage")
def api_plan_usage():
    """Return just the usage counters (lightweight poll)."""
    state  = p8_get_state()
    plan   = p8_effective_plan(state)
    usage  = p8_get_usage(state)
    limits = _P8_PLANS[plan]["limits"]
    return jsonify({
        "ok":   True,
        "plan": plan,
        "usage": {
            "pro_daily":     {
                "count": usage["pro_daily"]["count"],
                "limit": limits["pro_daily"],
            },
            "elite_monthly": {
                "count": usage["elite_monthly"]["count"],
                "limit": limits["elite_monthly"],
            },
        },
    })


@app.route("/api/plan/byok-priority", methods=["POST"])
def api_plan_byok_priority():
    """Toggle BYOK Priority Mode (Pro/Elite only)."""
    state = p8_get_state()
    plan  = p8_effective_plan(state)
    if plan == "free":
        return jsonify({"ok": False, "error": "BYOK Priority Mode requires Pro or Elite plan"}), 403
    d       = request.get_json() or {}
    enabled = bool(d.get("enabled", not state.get("byok_priority", False)))
    state["byok_priority"] = enabled
    p8_save_state(state)
    return jsonify({"ok": True, "byok_priority": enabled})


# ── Patch /api/queue-task to enforce plan limits ──────────────────────────────
# We wrap the existing queue_task view with plan-gate logic by replacing it.

_p8_orig_queue_task = api_queue_task  # already defined above

@app.route("/api/plan/check", methods=["POST"])
def api_plan_check():
    """Check if an exec_mode is allowed for current plan (pre-flight)."""
    d         = request.get_json() or {}
    exec_mode = (d.get("exec_mode") or d.get("plan_mode") or "lite").lower()
    state     = p8_get_state()
    plan      = p8_effective_plan(state)
    allowed, reason = p8_check_and_increment.__wrapped__ \
        if hasattr(p8_check_and_increment, '__wrapped__') else (True, "")
    # Dry-run check (no increment)
    limits    = _P8_PLANS[plan]["limits"]
    usage     = p8_get_usage(state)
    if exec_mode == "lite":
        allowed, reason = True, ""
    elif exec_mode == "pro":
        cap = limits["pro_daily"]
        if cap is None:
            allowed, reason = True, ""
        elif usage["pro_daily"]["count"] >= cap:
            allowed, reason = False, f"Pro limit: {cap}/day. Upgrade for unlimited."
        else:
            allowed, reason = True, ""
    elif exec_mode == "elite":
        cap = limits["elite_monthly"]
        if cap == 0:
            allowed, reason = False, "Elite requires Pro or Elite subscription."
        elif cap is None:
            allowed, reason = True, ""
        elif usage["elite_monthly"]["count"] >= cap:
            allowed, reason = False, f"Elite limit: {cap}/month. Upgrade for unlimited."
        else:
            allowed, reason = True, ""
    else:
        allowed, reason = True, ""
    return jsonify({"ok": True, "allowed": allowed, "reason": reason, "plan": plan})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
