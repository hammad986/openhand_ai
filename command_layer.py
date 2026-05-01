"""command_layer.py — Phase 25.

Single chokepoint for "an AI agent or a UI button wants to run a real
shell command".  Sits *above* the Phase 22 sandbox runner and the Phase
24 Local Bridge proxy, NOT in place of them — `/api/terminal/run` keeps
working exactly as before.

Design goals
------------
* **Default-deny**.  Until the user flips an explicit toggle in the UI
  (persisted at ``data/ai_terminal_settings.json``) NOTHING here will
  actually exec.  We return a structured ``ai_disabled`` envelope so the
  caller can surface a confirmation prompt.
* **Whitelist-first categorisation**.  Every command is classified into
  one of ``safe``, ``run``, ``install`` or ``blocked`` *before* it ever
  reaches a subprocess.  This is regex-free string parsing on
  ``shlex.split`` tokens; we never trust the raw command string.
* **One audit trail**.  Every attempt — including blocks and disabled
  short-circuits — is appended to ``data/ai_exec_log.jsonl`` with a
  rotating cap so the log can't grow unbounded.
* **Injectable dispatcher**.  ``_DISPATCHER`` is a module-level callable
  the test suite monkey-patches.  The default reads
  ``data/terminal_mode.json`` and picks sandbox vs. bridge.

This module is intentionally importable *without* importing ``web_app``
to keep the dependency graph one-way.  The default dispatcher uses
``urllib`` for the bridge and ``subprocess`` for the sandbox — same
primitives Phase 22/24 already use.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from config import Config

logger = logging.getLogger("command_layer")

# ── Persistence locations ────────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_SETTINGS_PATH = os.path.join(_DATA_DIR, "ai_terminal_settings.json")
_EXEC_LOG_PATH = os.path.join(_DATA_DIR, "ai_exec_log.jsonl")
_TERMINAL_MODE_PATH = os.path.join(_DATA_DIR, "terminal_mode.json")  # Phase 24

# Hard caps — defensive, not user-configurable from the UI.
_EXEC_LOG_MAX_LINES = 500
_DEFAULT_TIMEOUT_SEC = 60
_MAX_TIMEOUT_SEC = 300  # 5 min hard ceiling regardless of caller request
_MAX_CMD_LEN = 4096

_DEFAULT_SETTINGS = {
    "ai_enabled":          False,
    "allow_install_auto":  False,
    "allow_run_auto":      True,
}

_settings_lock = threading.Lock()
_log_lock = threading.Lock()

# ── Whitelist categorisation ─────────────────────────────────────────────
# The *first* token of the command (after shlex.split) is the executable
# basename.  We strip any leading directory and `.exe` suffix so paths
# like ``/usr/bin/python3`` or ``python3.exe`` still resolve.

_SAFE_BINS = frozenset({
    "ls", "pwd", "echo", "cat", "head", "tail", "grep", "egrep", "fgrep",
    "find", "wc", "file", "which", "whoami", "uname", "date", "env",
    "printenv", "stat", "tree", "du", "df", "true", "false",
})
# Read-only language probes: only allowed with `--version`/`-V` flags.
_VERSION_BINS = frozenset({"python", "python3", "node", "npm", "pip", "pip3"})
_VERSION_FLAGS = frozenset({"--version", "-V", "-v"})

# Run-category binaries — followed by a *path* argument we accept; we
# DO NOT accept `python -c "..."` here (would let an agent escape every
# guard by literally running arbitrary code as an argument).
_RUN_BINS = frozenset({"python", "python3", "node"})
_RUN_FLAGS_BLOCKED = frozenset({
    "-c", "-e", "--eval", "--exec", "-S", "-m",  # `-m` allows pip etc — handled separately
})
# `npm run <script>` and `npm test` are explicitly allowed; other npm
# subcommands fall through to the install / blocked branches.
_NPM_RUN_SUBS = frozenset({"run", "run-script", "test", "start"})

# Install-category recognisers.
_PIP_BINS    = frozenset({"pip", "pip3"})
_PIP_INSTALL = frozenset({"install"})
# Reject pip flags that can rewrite the world.
_PIP_FLAGS_BLOCKED = frozenset({
    "--target", "--prefix", "--root", "--user",  # destination override
    "--editable", "-e",                         # editable installs
    "--no-deps",                                # weird dep graphs
    "--index-url", "--extra-index-url",         # alt indexes
    "--find-links", "-f",                       # arbitrary URL fetches
    "--no-binary", "--only-binary",             # power-user knobs
})
_NPM_INSTALL_SUBS = frozenset({"install", "i", "add", "ci"})

# Final hard-block list — wins over everything above. Mirrors the Phase
# 22 sandbox blocklist for parity, plus a few obviously-system-level
# binaries we never want the AI to invoke even in Local Bridge mode.
_HARD_BLOCKED_BINS = frozenset({
    "rm", "rmdir", "shutdown", "reboot", "halt", "poweroff", "init",
    "mkfs", "fdisk", "dd", "mount", "umount", "useradd", "userdel",
    "passwd", "su", "sudo", "doas",
    "chown", "chmod", "chgrp", "setfacl",
    "kill", "killall", "pkill", "iptables", "ufw", "nft",
    "nc", "ncat", "telnet", "ssh", "scp", "rsync",
    "wget", "curl",
    "bash", "sh", "zsh", "fish", "ksh", "csh", "dash",
    "awk", "gawk", "sed",
    "xargs",
})
# Tokens whose presence ANYWHERE in the raw command string triggers a
# hard block.  Stops shell-escape via ``cmd; rm -rf /``-style payloads
# getting in BEFORE tokenisation, AND ``python script.py && curl evil``.
#
# IMPORTANT (Phase-25 architect-round-1 fix): ``&&`` is a *substring* of
# its own check but a single ``&`` is NOT, and likewise ``\n`` was missing
# entirely, so payloads like ``echo ok & curl evil`` or ``echo ok\nrm -rf
# /`` would classify as ``safe`` and reach ``bash -lc`` where they DO act
# as command separators.  We now block:
#   - every classic separator (`;`, `|`, `||`, `&&`, single `&`)
#   - command-substitution markers (`` ` ``, `$(`, `$\{`, bare `$`)
#   - all redirection operators (`>`, `>>`, `<`, `<<`)
#   - in-band line breaks (`\n`, `\r`) and NUL (`\0`)
#   - line-continuation backslash at EOL (handled below in classify_command)
_HARD_BLOCKED_TOKENS = frozenset({
    ";", "&&", "||", "|", "&",
    "`", "$(", "${", "$",
    ">", ">>", "<", "<<",
    "\n", "\r", "\0",
})

# Acceptable Python package name characters for auto-fix-env.
_PKG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._\-]{0,99}$")


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    ok: bool
    category: str           # safe|run|install|blocked|disabled|error
    mode: str               # sandbox|local|n/a
    cmd: str
    exit: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    blocked_reason: str = ""
    error: str = ""
    timed_out: bool = False
    truncated: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Settings ─────────────────────────────────────────────────────────────

def _normalize_settings(raw: Any) -> dict:
    out = dict(_DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for k in _DEFAULT_SETTINGS:
            v = raw.get(k)
            if isinstance(v, bool):
                out[k] = v
    return out


def load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as fh:
            return _normalize_settings(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


def save_settings(raw: Any) -> tuple[dict, list[str]]:
    """Validate + persist.  Returns (settings, errors[])."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return dict(_DEFAULT_SETTINGS), ["invalid_payload"]
    for k, v in raw.items():
        if k not in _DEFAULT_SETTINGS:
            errors.append(f"unknown_field:{k}")
        elif not isinstance(v, bool):
            errors.append(f"not_bool:{k}")
    if errors:
        return load_settings(), errors
    merged = dict(load_settings())
    merged.update(raw)
    norm = _normalize_settings(merged)
    with _settings_lock:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(norm, fh, indent=2, sort_keys=True)
        os.replace(tmp, _SETTINGS_PATH)
    return norm, []


# ── Whitelist classifier ─────────────────────────────────────────────────

def _basename(token: str) -> str:
    base = os.path.basename(token).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def classify_command(cmd: str) -> tuple[str, str, list[str]]:
    """Returns ``(category, blocked_reason, tokens)``.

    ``category`` is one of ``safe|run|install|blocked``.  ``blocked_reason``
    is a short machine-readable code (``empty``, ``too_long``,
    ``shell_meta:;``, ``hard_blocked:rm``, ``unknown_binary:cowsay``,
    ``python_eval_flag:-c``, etc.).  ``tokens`` is the shlex-split
    result so callers don't have to redo the parse.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return "blocked", "empty", []
    if len(cmd) > _MAX_CMD_LEN:
        return "blocked", "too_long", []

    # Stage 1 — raw-string scan for shell metacharacters.  We do this
    # BEFORE shlex.split so a payload like ``echo hi; rm -rf /`` is
    # rejected even though shlex would happily eat the semicolon.
    for tok in _HARD_BLOCKED_TOKENS:
        if tok in cmd:
            return "blocked", f"shell_meta:{tok}", []

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError as e:
        return "blocked", f"parse_error:{e}", []
    if not tokens:
        return "blocked", "empty", []

    head = _basename(tokens[0])

    # Stage 2 — hard-block argv[0] regardless of context.
    if head in _HARD_BLOCKED_BINS:
        return "blocked", f"hard_blocked:{head}", tokens

    # Stage 3 — version probes (``python --version`` etc.).
    if head in _VERSION_BINS and len(tokens) >= 2 \
            and tokens[1] in _VERSION_FLAGS and len(tokens) == 2:
        return "safe", "", tokens

    # Stage 4 — pip / pip3 install <pkg…>.
    if head in _PIP_BINS:
        if len(tokens) >= 2 and tokens[1] in _PIP_INSTALL:
            # Special-case ``pip install -r <path>`` for ``run_setup`` —
            # path must be a SAFE basename or absolute path with no
            # `..` segments, and end in ``requirements*.txt`` so we can't
            # be coerced into installing arbitrary spec files smuggled
            # via `auto_fix_env` text.
            if len(tokens) == 4 and tokens[2] in {"-r", "--requirement"}:
                req_path = tokens[3]
                rp_basename = os.path.basename(req_path)
                if (".." in req_path.split(os.sep)
                        or not rp_basename.startswith("requirements")
                        or not rp_basename.endswith(".txt")):
                    return "blocked", "pip_requirements_path_invalid", tokens
                return "install", "", tokens
            for t in tokens[2:]:
                if t in _PIP_FLAGS_BLOCKED:
                    return "blocked", f"pip_flag_blocked:{t}", tokens
                if t.startswith("-"):
                    # Allow plain ``--upgrade``, ``-U``, ``--quiet`` etc.
                    if t not in {"--upgrade", "-U", "--quiet", "-q",
                                 "--no-cache-dir"}:
                        return "blocked", f"pip_flag_unknown:{t}", tokens
                else:
                    if not _PKG_NAME_RE.match(t.split("==")[0].split(">")[0]
                                              .split("<")[0]):
                        return "blocked", f"pip_pkg_invalid:{t[:32]}", tokens
            return "install", "", tokens
        if len(tokens) == 2 and tokens[1] in {"list", "freeze",
                                              "--version", "-V"}:
            return "safe", "", tokens
        return "blocked", f"pip_subcmd_blocked:{tokens[1] if len(tokens)>1 else ''}", tokens

    # Stage 5 — npm install / npm run / npm test.
    if head == "npm":
        if len(tokens) >= 2 and tokens[1] in _NPM_INSTALL_SUBS:
            return "install", "", tokens
        if len(tokens) >= 2 and tokens[1] in _NPM_RUN_SUBS:
            return "run", "", tokens
        if len(tokens) == 2 and tokens[1] in {"--version", "-v"}:
            return "safe", "", tokens
        return "blocked", f"npm_subcmd_blocked:{tokens[1] if len(tokens)>1 else ''}", tokens

    # Stage 6 — `python script.py` / `node script.js`.
    if head in _RUN_BINS:
        for t in tokens[1:]:
            if t in _RUN_FLAGS_BLOCKED:
                return "blocked", f"python_eval_flag:{t}", tokens
        if len(tokens) >= 2 and not tokens[1].startswith("-"):
            return "run", "", tokens
        return "blocked", "python_no_script", tokens

    # Stage 7 — pure-safe binaries (ls, cat, …).
    if head in _SAFE_BINS:
        return "safe", "", tokens

    return "blocked", f"unknown_binary:{head}", tokens


# ── Audit log ────────────────────────────────────────────────────────────

def _append_log(entry: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with _log_lock:
            with open(_EXEC_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Cheap rotation — read tail if file got too big.
            try:
                with open(_EXEC_LOG_PATH, "r", encoding="utf-8") as fh:
                    lines = fh.readlines()
                if len(lines) > _EXEC_LOG_MAX_LINES:
                    keep = lines[-_EXEC_LOG_MAX_LINES:]
                    tmp = _EXEC_LOG_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.writelines(keep)
                    os.replace(tmp, _EXEC_LOG_PATH)
            except OSError:
                pass
    except OSError as e:
        logger.warning("failed to append exec log: %s", e)


def read_log(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), _EXEC_LOG_MAX_LINES))
    try:
        with open(_EXEC_LOG_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ── Dispatcher seam ──────────────────────────────────────────────────────
# Default dispatcher reads the persisted Phase 24 mode and runs either
# the in-process sandbox or a bridge HTTP call.  Tests monkey-patch
# ``command_layer._DISPATCHER`` to stub.

def _load_terminal_mode() -> dict:
    try:
        with open(_TERMINAL_MODE_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (OSError, json.JSONDecodeError):
        data = {}
    mode = (data.get("mode") or Config.TERMINAL_MODE_DEFAULT
            ).strip().lower()
    if mode not in Config.TERMINAL_MODES:
        mode = "sandbox"
    return {
        "mode":        mode,
        "local_url":   (data.get("local_url") or "").strip(),
        "local_token": (data.get("local_token") or "").strip(),
    }


def _sandbox_dispatch(cmd: str, *, timeout: int) -> dict:
    """In-process sandbox runner.  Mirrors the Phase 22 dispatch in
    ``web_app.py`` but stays import-free of Flask."""
    started = time.time()
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
            "USER", "LOGNAME", "TERM", "TZ", "PWD", "SHELL"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    workspace = (os.getenv("WORKSPACE_DIR")
                 or Config.WORKSPACE_DIR or "./workspace")
    os.makedirs(workspace, exist_ok=True)
    # Detect OS for shell dispatch
    is_windows = os.name == "nt"
    shell_cmd = ["bash", "-lc", cmd] if not is_windows else ["cmd", "/c", cmd]
    
    try:
        proc = subprocess.run(
            shell_cmd,
            cwd=workspace, env=env, timeout=timeout,
            capture_output=True, text=True,
        )
        return {
            "ok":           proc.returncode == 0,
            "exit":         proc.returncode,
            "stdout":       proc.stdout[:Config.TERMINAL_MAX_OUTPUT_BYTES],
            "stderr":       proc.stderr[:Config.TERMINAL_MAX_OUTPUT_BYTES],
            "duration_sec": round(time.time() - started, 3),
            "timed_out":    False,
            "truncated":    (len(proc.stdout) >
                             Config.TERMINAL_MAX_OUTPUT_BYTES),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False, "exit": -1,
            "stdout": (e.stdout or "")[:Config.TERMINAL_MAX_OUTPUT_BYTES]
                      if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "")[:Config.TERMINAL_MAX_OUTPUT_BYTES]
                      if isinstance(e.stderr, str) else "",
            "duration_sec": round(time.time() - started, 3),
            "timed_out": True, "truncated": False,
            "error": "timeout",
        }
    except Exception as e:                        # noqa: BLE001
        return {
            "ok": False, "exit": -1, "stdout": "", "stderr": str(e),
            "duration_sec": round(time.time() - started, 3),
            "timed_out": False, "truncated": False, "error": "exec_error",
        }


def _bridge_dispatch(cmd: str, *, mode_cfg: dict, timeout: int) -> dict:
    started = time.time()
    base = (mode_cfg.get("local_url") or "").rstrip("/")
    if not base:
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "",
                "duration_sec": 0.0, "timed_out": False, "truncated": False,
                "error": "bridge_unconfigured"}
    body = json.dumps({"cmd": cmd, "timeout": timeout}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if mode_cfg.get("local_token"):
        headers["X-Bridge-Token"] = mode_cfg["local_token"]
    req = urllib.request.Request(base + "/terminal/run", data=body,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout + 5) as r:
            raw = r.read(Config.TERMINAL_BRIDGE_MAX_RESPONSE_BYTES + 1)
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:                         # noqa: BLE001
            data = {"error": f"http_{e.code}"}
        return {"ok": False, "exit": -1, "stdout": "",
                "stderr": data.get("error", f"http_{e.code}"),
                "duration_sec": round(time.time() - started, 3),
                "timed_out": False, "truncated": False,
                "error": data.get("error", f"http_{e.code}")}
    except Exception as e:                         # noqa: BLE001
        return {"ok": False, "exit": -1, "stdout": "", "stderr": str(e),
                "duration_sec": round(time.time() - started, 3),
                "timed_out": False, "truncated": False,
                "error": "bridge_unreachable"}
    return {
        "ok":           bool(data.get("ok")),
        "exit":         int(data.get("exit_code", -1)),
        "stdout":       (data.get("stdout") or "")[:Config.TERMINAL_MAX_OUTPUT_BYTES],
        "stderr":       (data.get("stderr") or "")[:Config.TERMINAL_MAX_OUTPUT_BYTES],
        "duration_sec": float(data.get("duration_sec", 0.0)),
        "timed_out":    bool(data.get("timed_out")),
        "truncated":    bool(data.get("truncated")),
    }


def _default_dispatcher(cmd: str, *, mode: Optional[str],
                        timeout: int) -> tuple[str, dict]:
    """Returns ``(resolved_mode, raw_dispatch_dict)``."""
    cfg = _load_terminal_mode()
    resolved = (mode or cfg["mode"]).lower()
    if resolved == "local":
        return resolved, _bridge_dispatch(cmd, mode_cfg=cfg, timeout=timeout)
    return "sandbox", _sandbox_dispatch(cmd, timeout=timeout)


_DISPATCHER: Callable[[str], tuple[str, dict]] = _default_dispatcher


# ── Public API ───────────────────────────────────────────────────────────

def execute_command(
    cmd: str,
    *,
    mode: Optional[str] = None,
    allow_install: bool = False,
    sid: Optional[str] = None,
    source: str = "api",
    timeout: Optional[int] = None,
) -> dict:
    """Run ``cmd`` on behalf of an AI agent or UI button.

    Returns a ``CommandResult.to_dict()`` envelope.  Never raises.
    """
    settings = load_settings()
    timeout_eff = max(1, min(int(timeout or _DEFAULT_TIMEOUT_SEC),
                             _MAX_TIMEOUT_SEC))

    # 1) Hard gate — global toggle.
    if not settings["ai_enabled"]:
        result = CommandResult(
            ok=False, category="disabled", mode="n/a", cmd=cmd or "",
            error="ai_disabled",
            blocked_reason="ai_terminal_disabled",
        )
        _append_log({
            "ts": time.time(), "source": source, "sid": sid,
            "category": "disabled", "mode": "n/a", "cmd": cmd or "",
            "ok": False, "exit": -1, "duration_sec": 0.0,
            "error": "ai_disabled",
        })
        return result.to_dict()

    # 2) Whitelist classification.
    category, reason, _tokens = classify_command(cmd or "")
    
    # Safety Layer Check (Phase 30)
    from safety_layer import TerminalSafety, safety_controller
    
    if not TerminalSafety.is_safe(cmd, os.getcwd()):
        category = "blocked"
        reason = "safety_layer_blocked"
        
    safety_controller.current_commands += 1
    
    limit_check = safety_controller.check_limits()
    if limit_check:
        result = CommandResult(
            ok=False, category="blocked", mode="n/a", cmd=cmd or "",
            blocked_reason=limit_check["reason"], error=limit_check["detail"],
        )
        _append_log({
            "ts": time.time(), "source": source, "sid": sid,
            "category": "blocked", "mode": "n/a", "cmd": cmd or "",
            "ok": False, "exit": -1, "duration_sec": 0.0,
            "blocked_reason": limit_check["reason"], "error": limit_check["detail"],
        })
        return result.to_dict()

    if category == "blocked":
        result = CommandResult(
            ok=False, category="blocked", mode="n/a", cmd=cmd or "",
            blocked_reason=reason, error="command_blocked",
        )
        _append_log({
            "ts": time.time(), "source": source, "sid": sid,
            "category": "blocked", "mode": "n/a", "cmd": cmd or "",
            "ok": False, "exit": -1, "duration_sec": 0.0,
            "blocked_reason": reason, "error": "command_blocked",
        })
        return result.to_dict()

    # 3) Per-category consent.
    if category == "install":
        if not (allow_install or settings["allow_install_auto"]):
            result = CommandResult(
                ok=False, category="install", mode="n/a", cmd=cmd,
                error="confirmation_required",
                blocked_reason="install_requires_consent",
            )
            _append_log({
                "ts": time.time(), "source": source, "sid": sid,
                "category": "install", "mode": "n/a", "cmd": cmd,
                "ok": False, "exit": -1, "duration_sec": 0.0,
                "error": "confirmation_required",
            })
            return result.to_dict()
    elif category == "run":
        if not settings["allow_run_auto"]:
            result = CommandResult(
                ok=False, category="run", mode="n/a", cmd=cmd,
                error="confirmation_required",
                blocked_reason="run_requires_consent",
            )
            _append_log({
                "ts": time.time(), "source": source, "sid": sid,
                "category": "run", "mode": "n/a", "cmd": cmd,
                "ok": False, "exit": -1, "duration_sec": 0.0,
                "error": "confirmation_required",
            })
            return result.to_dict()

    # 4) Dispatch.
    try:
        resolved_mode, raw = _DISPATCHER(cmd, mode=mode, timeout=timeout_eff)
    except Exception as e:                         # noqa: BLE001
        result = CommandResult(
            ok=False, category=category, mode=str(mode or "?"), cmd=cmd,
            error="dispatcher_crash", stderr=str(e),
        )
        _append_log({
            "ts": time.time(), "source": source, "sid": sid,
            "category": category, "mode": "?", "cmd": cmd,
            "ok": False, "exit": -1, "duration_sec": 0.0,
            "error": "dispatcher_crash",
        })
        return result.to_dict()

    result = CommandResult(
        ok=bool(raw.get("ok")),
        category=category,
        mode=resolved_mode,
        cmd=cmd,
        exit=int(raw.get("exit", -1)),
        stdout=raw.get("stdout") or "",
        stderr=raw.get("stderr") or "",
        duration_sec=float(raw.get("duration_sec", 0.0)),
        timed_out=bool(raw.get("timed_out")),
        truncated=bool(raw.get("truncated")),
        error=raw.get("error", "") or "",
    )
    _append_log({
        "ts":           time.time(),
        "source":       source,
        "sid":          sid,
        "category":     category,
        "mode":         resolved_mode,
        "cmd":          cmd,
        "ok":           result.ok,
        "exit":         result.exit,
        "duration_sec": result.duration_sec,
        "error":        result.error,
    })
    return result.to_dict()


# ── Helpers used by /api/ai/run-setup + /api/ai/auto-fix-env ────────────

_MISSING_MOD_RE = re.compile(
    r"ModuleNotFoundError: No module named ['\"]([A-Za-z][\w.\-]{0,99})['\"]")
_IMPORT_FROM_RE = re.compile(
    r"ImportError: cannot import name .* from ['\"]([A-Za-z][\w.\-]{0,99})['\"]")


def parse_missing_modules(text: str, *, limit: int = 5) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    seen: list[str] = []
    for m in _MISSING_MOD_RE.finditer(text):
        name = m.group(1).split(".")[0]
        if _PKG_NAME_RE.match(name) and name not in seen:
            seen.append(name)
            if len(seen) >= limit:
                return seen
    for m in _IMPORT_FROM_RE.finditer(text):
        name = m.group(1).split(".")[0]
        if _PKG_NAME_RE.match(name) and name not in seen:
            seen.append(name)
            if len(seen) >= limit:
                return seen
    return seen


def auto_fix_env(text: str, *, source: str = "api",
                 limit: int = 5) -> dict:
    """Pip-install every missing module mentioned in ``text``.

    The caller is expected to be the human pressing the UI button OR the
    dev-loop — both have already proven consent (the dev-loop only opts
    in when ``auto_fix_env=True``; the UI button runs only when the AI
    toggle is on)."""
    mods = parse_missing_modules(text, limit=limit)
    attempts: list[dict] = []
    for m in mods:
        cmd = f"pip install {m}"
        envelope = execute_command(cmd, allow_install=True, source=source)
        attempts.append({"module": m, "result": envelope})
    return {"ok": True, "modules": mods, "attempts": attempts}


def run_setup(*, source: str = "api") -> dict:
    """Run ``pip install -r requirements.txt`` if the file exists in the
    project root.  No-op (still ok) otherwise.

    Phase-25 architect-round-1 fix: this used to short-circuit the
    classifier and call ``_DISPATCHER`` directly, which created policy
    drift (``run_setup`` could in principle dispatch even when the
    classifier would have rejected the requirements path).  We now
    funnel through :func:`execute_command` — the classifier whitelists
    the ``pip install -r requirements*.txt`` form via Stage-4, and the
    install-consent gate still applies — keeping the layer as a single
    enforcement point.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    req_path = os.path.join(project_root, "requirements.txt")
    if not os.path.isfile(req_path):
        return {"ok": True, "ran": False, "reason": "no_requirements_file"}
    # Always pass the basename to the classifier — the working directory
    # of the dispatcher is the project root, so this resolves correctly
    # AND avoids leaking absolute paths into the audit log.
    envelope = execute_command(
        "pip install -r requirements.txt",
        allow_install=False,        # respect ``allow_install_auto`` only
        source=source,
    )
    if envelope.get("error") in ("ai_disabled", "command_blocked",
                                  "confirmation_required"):
        return {"ok": False, "ran": False,
                "error": envelope.get("error"),
                "blocked_reason": envelope.get("blocked_reason", "")}
    return {
        "ok":           bool(envelope.get("ok")),
        "ran":          True,
        "mode":         envelope.get("mode", "?"),
        "exit":         int(envelope.get("exit", -1)),
        "stdout":       envelope.get("stdout", ""),
        "stderr":       envelope.get("stderr", ""),
        "duration_sec": float(envelope.get("duration_sec", 0.0)),
    }


__all__ = [
    "CommandResult",
    "classify_command",
    "execute_command",
    "load_settings",
    "save_settings",
    "read_log",
    "parse_missing_modules",
    "auto_fix_env",
    "run_setup",
]
