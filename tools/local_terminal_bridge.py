#!/usr/bin/env python3
"""
Phase 24 — Local Terminal Bridge
================================

Run this script ON YOUR OWN MACHINE to give the Multi-Agent AI Dev System
real OS access (pip install, system commands, anything your shell can do).

The web app, when set to "Local Mode", will POST commands to this bridge
and stream the results back to the in-app terminal.

============================================================================
                       !! SECURITY WARNING !!
This bridge executes ARBITRARY shell commands as the user that runs it.
Anyone who can reach this port can execute commands on this machine.

By default the bridge binds to 127.0.0.1 (loopback only) so only programs
running on this machine can reach it. If you change --bind to expose it on
a network interface, you MUST set LOCAL_BRIDGE_TOKEN to a strong secret.
============================================================================

USAGE
-----
    # 1) Generate a token (or pick one yourself):
    python -c "import secrets; print(secrets.token_urlsafe(24))"

    # 2) Set the token and start the bridge:
    #    Linux/Mac:
    export LOCAL_BRIDGE_TOKEN="<paste-the-token>"
    python3 tools/local_terminal_bridge.py
    #    Windows (PowerShell):
    $env:LOCAL_BRIDGE_TOKEN="<paste-the-token>"
    python tools\\local_terminal_bridge.py

    # 3) In the web app:
    #    Settings -> Terminal Mode -> Local
    #    URL:   http://127.0.0.1:5002
    #    Token: <paste-the-token>
    #    Click "Validate Connection".

OPTIONAL FLAGS
--------------
    --port 5002         (or LOCAL_BRIDGE_PORT)
    --bind 127.0.0.1    (or LOCAL_BRIDGE_BIND)
    --shell auto|bash|sh|cmd|powershell|pwsh
    --max-timeout 600   (hard ceiling per request, in seconds)

ENDPOINTS
---------
    GET  /health            -> {"ok": true, "platform": "...", ...}
    POST /terminal/run      -> {"ok": ..., "stdout": ..., "stderr": ...,
                                 "exit_code": int, "duration_sec": float}
    POST /terminal/stream   -> Server-Sent Events:
                                  event: stdout / stderr  data: <line>
                                  event: done             data: {summary}

This script depends ONLY on the Python standard library — no pip install
required.  Tested on CPython 3.8+ on Linux, macOS, and Windows.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_PORT = int(os.environ.get("LOCAL_BRIDGE_PORT", "5002"))
DEFAULT_BIND = os.environ.get("LOCAL_BRIDGE_BIND", "127.0.0.1")
DEFAULT_TIMEOUT_SEC = 30
HARD_TIMEOUT_CEIL_SEC = 600
MAX_REQUEST_BYTES = 64_000
MAX_OUTPUT_BYTES = 1_000_000          # /terminal/run buffered cap
STREAM_LINE_CAP = 4096                # cap per streamed line
STREAM_TOTAL_CAP = 4_000_000          # hard total cap on streamed bytes
TOKEN = os.environ.get("LOCAL_BRIDGE_TOKEN", "").strip()

# ── CORS / origin allowlist ────────────────────────────────────────────────
# Architect-flagged HIGH security gap (Phase 24): with `Access-Control-Allow-
# Origin: *` and optional auth, any malicious website the user visits could
# POST to http://127.0.0.1:5002/terminal/run via `fetch()` and execute
# arbitrary commands — drive-by local RCE inside the browser context. The
# fix is two-fold:
#   1. Default-deny CORS. Only echo back an explicit allowlist of origins
#      from `LOCAL_BRIDGE_ALLOWED_ORIGINS` (comma-separated).
#   2. Require `Content-Type: application/json` on POSTs. This forces a
#      CORS preflight on every cross-origin browser request (a "simple"
#      `text/plain` POST would skip preflight entirely), so the strict
#      allowlist actually gets enforced before the command runs.
# Server-to-server callers (curl, urllib, our own Flask `_bridge_call`)
# don't send an Origin header by default, so the legitimate flow is
# unaffected.
def _parse_origins(raw: str) -> set[str]:
    out: set[str] = set()
    for part in (raw or "").split(","):
        p = part.strip().rstrip("/")
        if p:
            out.add(p)
    return out

ALLOWED_ORIGINS: set[str] = _parse_origins(
    os.environ.get("LOCAL_BRIDGE_ALLOWED_ORIGINS", ""))

VERSION = 1


# ── Shell selection (cross-platform) ────────────────────────────────────────
def detect_shell(forced: str | None = None) -> tuple[list[str], str]:
    """Return ([argv-prefix], pretty-name).  argv-prefix is what we prepend
    to the user's command before subprocess.Popen."""
    sys_name = platform.system().lower()
    pick = (forced or "auto").lower()

    def _have(binary: str) -> bool:
        return bool(shutil.which(binary))

    if pick == "auto":
        if sys_name == "windows":
            if _have("pwsh"):
                pick = "pwsh"
            elif _have("powershell"):
                pick = "powershell"
            else:
                pick = "cmd"
        else:
            if _have("bash"):
                pick = "bash"
            else:
                pick = "sh"

    if pick == "bash":
        return (["bash", "-lc"], "bash")
    if pick == "sh":
        return (["sh", "-c"], "sh")
    if pick == "cmd":
        return (["cmd.exe", "/c"], "cmd")
    if pick == "powershell":
        return (["powershell.exe", "-NoProfile", "-Command"], "powershell")
    if pick == "pwsh":
        return (["pwsh", "-NoProfile", "-Command"], "pwsh")
    # Fallback
    return (["sh", "-c"], "sh")


SHELL_ARGV: list[str] = []
SHELL_NAME: str = ""
MAX_TIMEOUT_SEC: int = HARD_TIMEOUT_CEIL_SEC


# ── HTTP handler ────────────────────────────────────────────────────────────
class BridgeHandler(BaseHTTPRequestHandler):
    server_version = f"LocalTerminalBridge/{VERSION}"

    # ----- helpers -------------------------------------------------------
    def _origin_allowed(self) -> str | None:
        """Return the request's Origin if it's in the allowlist, else None.

        A missing Origin header is treated as None and means "not a
        cross-origin browser request" — server-to-server callers (curl,
        urllib, our own Flask `_bridge_call`) don't send Origin and are
        evaluated by `_check_origin` instead, which permits them.
        """
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and origin in ALLOWED_ORIGINS:
            return origin
        return None

    def _cors_headers(self) -> None:
        """Emit CORS response headers ONLY for explicitly allowlisted
        origins. No wildcard, ever.  Requests with no Origin header
        (curl/urllib) get no CORS headers — they don't need them."""
        origin = self._origin_allowed()
        if not origin:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Bridge-Token",
        )
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, OPTIONS"
        )

    def _check_origin(self) -> bool:
        """Reject browser-originated cross-origin POSTs that aren't in the
        allowlist.  Requests with NO Origin header (server-to-server) pass
        through — they aren't a drive-by RCE vector.  Requests WITH an
        Origin must be in `ALLOWED_ORIGINS`.  Returns True if the request
        may proceed; otherwise sends 403 and returns False.
        """
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return True              # curl / urllib / native client
        if origin in ALLOWED_ORIGINS:
            return True
        self._json(403, {"ok": False, "error": "origin_forbidden",
                         "detail": "Origin not in LOCAL_BRIDGE_ALLOWED_ORIGINS"})
        return False

    def _check_content_type(self) -> bool:
        """Force `application/json` on POSTs.  This is what makes the
        browser send a CORS preflight on cross-origin attempts (a
        text/plain POST would skip preflight and bypass the allowlist).
        """
        ct = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ct != "application/json":
            self._json(415, {"ok": False, "error": "unsupported_media_type",
                             "detail": "POST body must be application/json"})
            return False
        return True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            length = 0
        if length > MAX_REQUEST_BYTES:
            self._json(413, {"ok": False, "error": "payload_too_large"})
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except Exception as e:
            self._json(400, {"ok": False, "error": "bad_json",
                             "detail": str(e)[:200]})
            return None

    def _check_auth(self) -> bool:
        if not TOKEN:
            return True
        sent = self.headers.get("X-Bridge-Token", "")
        if not sent or not secrets.compare_digest(sent, TOKEN):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    # ----- HTTP verbs ----------------------------------------------------
    def do_OPTIONS(self) -> None:                  # noqa: N802
        # Preflight: only succeed when the Origin is allowlisted, so a
        # malicious site's `fetch()` preflight fails before the actual
        # POST is sent.  Browsers refuse the request when ACAO is missing.
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:                      # noqa: N802
        if self.path == "/health":
            # /health is intentionally CORS-friendly *only* when an origin
            # is allowlisted (handled by `_cors_headers`); the response
            # body itself is non-sensitive (platform/shell name + flags),
            # so we don't gate the GET behind `_check_origin`.
            self._json(200, {
                "ok": True,
                "service": "local_terminal_bridge",
                "version": VERSION,
                "platform": platform.system(),
                "release": platform.release(),
                "shell": SHELL_NAME,
                "auth_required": bool(TOKEN),
                "max_timeout_sec": MAX_TIMEOUT_SEC,
            })
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:                     # noqa: N802
        # Order matters: origin check first (cheapest reject), then
        # content-type (forces preflight on cross-origin), then auth.
        if self.path in ("/terminal/run", "/terminal/stream"):
            if not self._check_origin():
                return
            if not self._check_content_type():
                return
            if not self._check_auth():
                return
            if self.path == "/terminal/run":
                self._handle_run()
            else:
                self._handle_stream()
            return
        self._json(404, {"ok": False, "error": "not_found"})

    # ----- /terminal/run (buffered) --------------------------------------
    def _handle_run(self) -> None:
        req = self._read_json()
        if req is None:
            return
        cmd = (req.get("cmd") or "").strip()
        cwd = req.get("cwd") or None
        try:
            timeout = int(req.get("timeout") or DEFAULT_TIMEOUT_SEC)
        except Exception:
            timeout = DEFAULT_TIMEOUT_SEC
        timeout = max(1, min(timeout, MAX_TIMEOUT_SEC))
        if not cmd:
            self._json(400, {"ok": False, "error": "cmd_required"})
            return
        if cwd and not os.path.isdir(cwd):
            self._json(400, {"ok": False, "error": "bad_cwd"})
            return
        argv = list(SHELL_ARGV) + [cmd]
        started = time.time()
        try:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=True,
                text=True, timeout=timeout,
            )
            duration = time.time() - started
            out = proc.stdout or ""
            err = proc.stderr or ""
            t_out = len(out.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES
            t_err = len(err.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES
            if t_out:
                out = out.encode("utf-8", "replace")[:MAX_OUTPUT_BYTES]\
                    .decode("utf-8", "replace") + "\n[truncated]"
            if t_err:
                err = err.encode("utf-8", "replace")[:MAX_OUTPUT_BYTES]\
                    .decode("utf-8", "replace") + "\n[truncated]"
            self._json(200, {
                "ok": (proc.returncode == 0),
                "exit_code": proc.returncode,
                "stdout": out, "stderr": err,
                "duration_sec": round(duration, 3),
                "platform": platform.system(),
                "shell": SHELL_NAME,
                "truncated": (t_out or t_err),
                "timed_out": False,
            })
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (
                e.stdout.decode("utf-8", "replace") if e.stdout else "")
            err = e.stderr if isinstance(e.stderr, str) else (
                e.stderr.decode("utf-8", "replace") if e.stderr else "")
            err = (err or "") + f"\n[timeout after {timeout}s]"
            self._json(200, {
                "ok": False, "exit_code": -1,
                "stdout": out[-MAX_OUTPUT_BYTES:],
                "stderr": err[-MAX_OUTPUT_BYTES:],
                "duration_sec": round(time.time() - started, 3),
                "platform": platform.system(),
                "shell": SHELL_NAME,
                "truncated": False,
                "timed_out": True,
            })
        except FileNotFoundError as e:
            self._json(500, {
                "ok": False,
                "error": "shell_not_found",
                "detail": str(e)[:300],
                "shell": SHELL_NAME,
            })
        except Exception as e:
            self._json(500, {
                "ok": False, "error": "exec_failed",
                "detail": str(e)[:300],
            })

    # ----- /terminal/stream (SSE) ----------------------------------------
    def _handle_stream(self) -> None:
        req = self._read_json()
        if req is None:
            return
        cmd = (req.get("cmd") or "").strip()
        cwd = req.get("cwd") or None
        try:
            timeout = int(req.get("timeout") or DEFAULT_TIMEOUT_SEC)
        except Exception:
            timeout = DEFAULT_TIMEOUT_SEC
        timeout = max(1, min(timeout, MAX_TIMEOUT_SEC))
        if not cmd:
            self._json(400, {"ok": False, "error": "cmd_required"})
            return
        if cwd and not os.path.isdir(cwd):
            self._json(400, {"ok": False, "error": "bad_cwd"})
            return

        argv = list(SHELL_ARGV) + [cmd]
        # Open SSE stream
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._cors_headers()
            self.end_headers()
        except Exception:
            return

        def _emit(event: str, data: str) -> bool:
            """Write an SSE message; return False if the socket is dead."""
            chunk = (
                f"event: {event}\n"
                f"data: {data}\n\n"
            ).encode("utf-8", "replace")
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
                return True
            except Exception:
                return False

        # Spawn the child
        started = time.time()
        try:
            popen_kw: dict = dict(
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1,
                text=True,
            )
            if platform.system() != "Windows":
                # Put child in its own session so we can kill the whole
                # tree on timeout.
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(argv, **popen_kw)
        except FileNotFoundError as e:
            _emit("error", json.dumps({"error": "shell_not_found",
                                       "detail": str(e)[:300]}))
            _emit("done", json.dumps({"exit_code": -1,
                                       "duration_sec": 0.0,
                                       "timed_out": False}))
            return
        except Exception as e:
            _emit("error", json.dumps({"error": "exec_failed",
                                       "detail": str(e)[:300]}))
            _emit("done", json.dumps({"exit_code": -1,
                                       "duration_sec": 0.0,
                                       "timed_out": False}))
            return

        total_sent = [0]
        client_dead = threading.Event()
        capped = threading.Event()

        def _pump(stream, event_name: str) -> None:
            try:
                for raw in iter(stream.readline, ""):
                    if client_dead.is_set() or capped.is_set():
                        break
                    line = (raw or "").rstrip("\n")
                    if len(line) > STREAM_LINE_CAP:
                        line = line[:STREAM_LINE_CAP] + "…[line-cap]"
                    encoded = json.dumps(line)
                    if total_sent[0] + len(encoded) > STREAM_TOTAL_CAP:
                        capped.set()
                        _emit("error", json.dumps(
                            {"error": "output_cap_exceeded"}))
                        break
                    total_sent[0] += len(encoded)
                    if not _emit(event_name, encoded):
                        client_dead.set()
                        break
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(
            target=_pump, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(
            target=_pump, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if platform.system() == "Windows":
                    proc.kill()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Drain pumps briefly
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        duration = time.time() - started
        exit_code = proc.returncode if proc.returncode is not None else -1
        _emit("done", json.dumps({
            "exit_code": exit_code,
            "duration_sec": round(duration, 3),
            "timed_out": timed_out,
            "capped": capped.is_set(),
            "platform": platform.system(),
            "shell": SHELL_NAME,
        }))

    # Quiet logging — write to stderr, one line.
    def log_message(self, fmt: str, *args) -> None:                # noqa: D401
        sys.stderr.write(
            f"[bridge {time.strftime('%H:%M:%S')}] "
            f"{self.address_string()} - {fmt % args}\n"
        )


# ── Entrypoint ──────────────────────────────────────────────────────────────
def main() -> int:
    global SHELL_ARGV, SHELL_NAME, MAX_TIMEOUT_SEC

    ap = argparse.ArgumentParser(description=(
        "Local Terminal Bridge — give the Multi-Agent AI Dev System "
        "real OS access on this machine."
    ))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--bind", default=DEFAULT_BIND)
    ap.add_argument("--shell", default="auto",
                    choices=["auto", "bash", "sh", "cmd",
                             "powershell", "pwsh"])
    ap.add_argument("--max-timeout", type=int,
                    default=HARD_TIMEOUT_CEIL_SEC,
                    help="Hard ceiling on per-request timeout (sec).")
    args = ap.parse_args()

    SHELL_ARGV, SHELL_NAME = detect_shell(args.shell)
    MAX_TIMEOUT_SEC = max(1, min(args.max_timeout, HARD_TIMEOUT_CEIL_SEC))

    if not TOKEN:
        sys.stderr.write(
            "[bridge] WARNING: LOCAL_BRIDGE_TOKEN is not set — anyone who "
            "can reach this port can run commands on this machine. "
            "Recommended only when --bind is 127.0.0.1.\n"
        )
    if args.bind != "127.0.0.1" and not TOKEN:
        sys.stderr.write(
            "[bridge] REFUSING to start on a non-loopback bind without "
            "LOCAL_BRIDGE_TOKEN.  Set the env var or use --bind 127.0.0.1.\n"
        )
        return 2

    sys.stderr.write(
        f"[bridge] starting on http://{args.bind}:{args.port}  "
        f"shell={SHELL_NAME}  platform={platform.system()}  "
        f"auth={'on' if TOKEN else 'off'}\n"
    )
    httpd = ThreadingHTTPServer((args.bind, args.port), BridgeHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[bridge] shutting down.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
