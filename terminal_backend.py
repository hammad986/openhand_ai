"""
terminal_backend.py — Phase 33 Cross-Platform Real Interactive Terminal Engine
=================================================================================
Unified PTY abstraction over:
  Windows  →  pywinpty  (PtyProcess)
  Linux    →  Python stdlib pty module
  macOS    →  Python stdlib pty module

Per-session features:
  • Real shell (PowerShell/cmd/bash/zsh) with full ANSI color
  • Live stdout/stderr streamed to SSE clients (ring-buffer replay)
  • Keystrokes forwarded UI → PTY stdin
  • Strict venv isolation: auto-creates / activates `my_env`
  • Command history persisted to <cwd>/.pty_history.json
  • Terminal Intelligence: pattern-matching error detection + fix suggestions
  • Idle-session reaper (IDLE_TIMEOUT_SECS)
  • Memory-bounded output buffer (OUTPUT_BUF_LINES)

Thread-safety: all mutable state guarded by _lock.
"""


from __future__ import annotations

import io
import json
import os
import platform
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Unix-only modules — conditionally imported
_IS_WINDOWS = platform.system() == "Windows"

if not _IS_WINDOWS:
    import fcntl
    import select
    import termios
else:
    fcntl = None    # type: ignore
    select = None   # type: ignore  # noqa: F841
    termios = None  # type: ignore



# ── OS detection ──────────────────────────────────────────────────────────────
_OS = platform.system()  # "Windows" | "Linux" | "Darwin"

# ── PTY backend selection ─────────────────────────────────────────────────────
_HAS_WINPTY = False
_HAS_POSIX_PTY = False

if _OS == "Windows":
    try:
        import winpty as _winpty_mod
        _HAS_WINPTY = True
    except ImportError:
        pass
else:
    try:
        import pty as _posix_pty
        _HAS_POSIX_PTY = True
    except ImportError:
        pass

_HAS_PTY = _HAS_WINPTY or _HAS_POSIX_PTY

# ── Shell detection ───────────────────────────────────────────────────────────
def _detect_shell() -> str:
    if _OS == "Windows":
        for candidate in ("pwsh", "powershell", "cmd"):
            found = shutil.which(candidate)
            if found:
                return found
        return os.environ.get("COMSPEC", "cmd.exe")
    else:
        # Prefer user's $SHELL, fall back to bash
        shell = os.environ.get("SHELL", "")
        if shell and shutil.which(shell):
            return shell
        for candidate in ("/bin/bash", "/bin/zsh", "/bin/sh"):
            if os.path.exists(candidate):
                return candidate
        return "/bin/sh"

_DEFAULT_SHELL = _detect_shell()

# ── Constants ──────────────────────────────────────────────────────────────────
IDLE_TIMEOUT_SECS = 600
MAX_HISTORY       = 500
OUTPUT_BUF_LINES  = 3000
PTY_COLS          = 220
PTY_ROWS          = 50
VENV_NAME         = "my_env"          # User's required venv name

# ── Error pattern library (Terminal Intelligence) ──────────────────────────────
_ERROR_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (regex, short_label, fix_suggestion)
    (re.compile(r"ModuleNotFoundError|No module named", re.I),
     "Missing module",
     "Run: pip install <module_name>"),
    (re.compile(r"pip.*not recognized|'pip'.*not found|pip.*not found", re.I),
     "pip not found",
     "Ensure venv is active: my_env\\Scripts\\activate (Win) or source my_env/bin/activate (Unix)"),
    (re.compile(r"SyntaxError", re.I),
     "Syntax error",
     "Check indentation and brackets on the line reported above"),
    (re.compile(r"PermissionError|Access is denied|Permission denied", re.I),
     "Permission denied",
     "Try running as admin, or check file ownership"),
    (re.compile(r"FileNotFoundError|No such file or directory", re.I),
     "File not found",
     "Check the path — use ls/dir to confirm the file exists"),
    (re.compile(r"ConnectionRefusedError|Connection refused", re.I),
     "Connection refused",
     "The server may not be running. Start it first, then retry"),
    (re.compile(r"Port \d+ already in use|Address already in use", re.I),
     "Port already in use",
     "Kill the process using the port: netstat -ano | findstr <PORT>  (Win) or lsof -i :<PORT> (Unix)"),
    (re.compile(r"npm ERR!|npm error", re.I),
     "npm error",
     "Run: npm install  then retry"),
    (re.compile(r"command not found|not recognized as an internal or external", re.I),
     "Command not found",
     "Install the tool or add it to your PATH"),
    (re.compile(r"AssertionError|assertion failed", re.I),
     "Assertion failed",
     "A test or sanity check failed — inspect the asserted expression"),
    (re.compile(r"KeyboardInterrupt|Ctrl\+C", re.I),
     "Interrupted",
     "Process was cancelled by the user"),
    (re.compile(r"out of memory|MemoryError|Killed", re.I),
     "Out of memory",
     "Reduce batch size or close other programs to free RAM"),
    (re.compile(r"git.*not a git repository|fatal:.*not a git repo", re.I),
     "Not a git repo",
     "Run: git init  in the project root first"),
    (re.compile(r"[Ee]rror.*line\s+\d+|Traceback|Error:", re.I),
     "Runtime error",
     "Read the traceback — check the last line for the root cause"),
]


def _detect_error(output: str) -> Optional[dict]:
    """Scan a chunk of terminal output for known error patterns.
    Returns {label, suggestion, excerpt} or None."""
    for rx, label, suggestion in _ERROR_PATTERNS:
        m = rx.search(output)
        if m:
            start = max(0, m.start() - 60)
            end   = min(len(output), m.end() + 120)
            excerpt = output[start:end].strip().replace("\r", "").replace("\n", " | ")[:300]
            return {"label": label, "suggestion": suggestion, "excerpt": excerpt}
    return None


# ── Venv helpers ──────────────────────────────────────────────────────────────

def _venv_paths(cwd: str) -> dict:
    """Return resolved absolute paths for the venv, python, pip, and activate."""
    venv_root = Path(cwd) / VENV_NAME
    if _OS == "Windows":
        scripts   = venv_root / "Scripts"
        python    = scripts / "python.exe"
        pip       = scripts / "pip.exe"
        activate  = str(scripts / "activate.bat")
        activate_cmd = str(scripts / "Activate.ps1")  # PS1 for pwsh/powershell
    else:
        bin_dir   = venv_root / "bin"
        python    = bin_dir / "python"
        pip       = bin_dir / "pip"
        activate  = str(bin_dir / "activate")
        activate_cmd = f"source {activate}"
    return {
        "root":         str(venv_root),
        "python":       str(python),
        "pip":          str(pip),
        "activate":     activate,
        "activate_cmd": activate_cmd,
        "exists":       venv_root.exists(),
        "ready":        python.exists(),
    }


def ensure_venv(cwd: str) -> dict:
    """Ensure `my_env` venv exists in cwd. Creates it if missing.
    Returns venv path info dict with extra key `created`."""
    vp    = _venv_paths(cwd)
    created = False
    if not vp["exists"]:
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", vp["root"]],
                cwd=cwd, check=True,
                capture_output=True, timeout=120,
            )
            created = True
        except Exception as e:
            return {**vp, "created": False, "error": str(e)}
    vp2 = _venv_paths(cwd)
    return {**vp2, "created": created}


def build_activate_command(cwd: str) -> str:
    """Return the shell command to activate my_env in cwd."""
    vp = _venv_paths(cwd)
    if _OS == "Windows":
        shell = _DEFAULT_SHELL.lower()
        if "pwsh" in shell or "powershell" in shell:
            # PowerShell: need to set ExecutionPolicy first (user scope)
            ps1 = vp["activate_cmd"]
            return f'Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force 2>$null; & "{ps1}"'
        else:
            # cmd.exe
            return f'"{vp["activate"]}"'
    else:
        return f'source "{vp["activate"]}"'


# ── POSIX PTY wrapper ──────────────────────────────────────────────────────────

class _PosixPty:
    """Thin wrapper around Python's `pty` module for Linux/macOS."""

    def __init__(self, shell: str, cwd: str, env: dict, cols: int, rows: int):
        self._master_fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._alive = False

        # Set terminal size via TIOCSWINSZ before fork
        master, slave = _posix_pty.openpty()
        self._master_fd = master
        self._slave_fd  = slave
        self._set_winsize(rows, cols)

        pid = os.fork()
        if pid == 0:
            # Child
            os.setsid()
            import fcntl as _fcntl
            _fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(master)
            os.close(slave)
            os.chdir(cwd)
            os.execvpe(shell, [shell], env)
            os._exit(1)
        else:
            # Parent
            os.close(slave)
            self._pid   = pid
            self._alive = True

    def write(self, data: str) -> None:
        if self._master_fd is None:
            return
        try:
            os.write(self._master_fd, data.encode("utf-8", errors="replace"))
        except OSError:
            self._alive = False

    def read(self, size: int = 4096, timeout: float = 0.05) -> Optional[str]:
        if self._master_fd is None:
            return None
        r, _, _ = select.select([self._master_fd], [], [], timeout)
        if not r:
            return None
        try:
            data = os.read(self._master_fd, size)
            return data.decode("utf-8", errors="replace")
        except OSError:
            self._alive = False
            return None

    def setwinsize(self, rows: int, cols: int) -> None:
        self._set_winsize(rows, cols)

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self._master_fd is None:
            return
        try:
            ws = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, ws)
        except Exception:
            pass

    def terminate(self, force: bool = False) -> None:
        if self._pid:
            try:
                os.kill(self._pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._alive = False

    @property
    def alive(self) -> bool:
        if not self._alive:
            return False
        if self._pid:
            try:
                result = os.waitpid(self._pid, os.WNOHANG)
                if result[0] != 0:
                    self._alive = False
            except ChildProcessError:
                self._alive = False
        return self._alive


# ── PtySession ────────────────────────────────────────────────────────────────

class PtySession:
    """One cross-platform PTY session with venv enforcement + terminal intelligence."""

    def __init__(self, sid: str, cwd: str):
        self.sid         = sid
        self.cwd         = cwd
        self._lock       = threading.Lock()
        self._pty        = None          # winpty.PtyProcess | _PosixPty
        self._alive      = False
        self._last_used  = time.time()

        # Output ring-buffer for SSE replay
        self._output_buf: list[str] = []
        # SSE subscribers {client_id: queue}
        self._subscribers: dict[str, queue.Queue] = {}
        # Command history
        self._history: list[dict] = []
        # Last detected error for Intelligence layer
        self._last_error: Optional[dict] = None
        # Venv state
        self._venv_info: Optional[dict] = None
        self._venv_activated = False
        # Reader thread
        self._reader_thread: Optional[threading.Thread] = None
        # History persistence path
        self._history_path = os.path.join(cwd, ".pty_history.json")

        # Load persisted history
        self._load_history()

    # ── Start / Stop ──────────────────────────────────────────────────

    def start(self):
        if self._alive:
            return
        if not _HAS_PTY:
            raise RuntimeError(
                "No PTY backend: install pywinpty (Windows) or ensure pty stdlib (Linux/Mac)"
            )

        # Ensure venv exists
        venv_info = ensure_venv(self.cwd)
        self._venv_info = venv_info
        if venv_info.get("error"):
            # Non-fatal: log and continue without venv
            self._broadcast_system(
                f"\x1b[33m[venv] WARNING: could not create {VENV_NAME}: "
                f"{venv_info['error']}\x1b[0m\r\n"
            )

        env = os.environ.copy()
        env["TERM"]      = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["PYTHONUNBUFFERED"] = "1"

        # Pre-inject venv to PATH so all commands use it automatically
        if venv_info.get("ready"):
            if _OS == "Windows":
                scripts_dir = str(Path(venv_info["root"]) / "Scripts")
                env["PATH"] = scripts_dir + ";" + env.get("PATH", "")
                env["VIRTUAL_ENV"] = venv_info["root"]
            else:
                bin_dir = str(Path(venv_info["root"]) / "bin")
                env["PATH"] = bin_dir + ":" + env.get("PATH", "")
                env["VIRTUAL_ENV"] = venv_info["root"]

        if _OS == "Windows":
            self._pty = _winpty_mod.PtyProcess.spawn(
                _DEFAULT_SHELL,
                cwd=self.cwd,
                env=env,
                dimensions=(PTY_ROWS, PTY_COLS),
            )
        else:
            self._pty = _PosixPty(
                shell=_DEFAULT_SHELL,
                cwd=self.cwd,
                env=env,
                cols=PTY_COLS,
                rows=PTY_ROWS,
            )

        self._alive = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"pty-reader-{self.sid[:8]}",
        )
        self._reader_thread.start()

        # Send welcome + venv activation
        self._send_activation_banner(venv_info)

    def _send_activation_banner(self, venv_info: dict):
        """Write activation command into shell right after start."""
        import time as _t; _t.sleep(0.3)  # Let shell finish init

        if venv_info.get("ready"):
            status = "created" if venv_info.get("created") else "found"
            banner = (
                f"\x1b[32m[Nexora] venv '{VENV_NAME}' {status} — activating…\x1b[0m\r\n"
            )
            activate_cmd = build_activate_command(self.cwd)
            self.write(banner)
            self.write(activate_cmd + ("\r\n" if _OS == "Windows" else "\n"))
            self._venv_activated = True
        else:
            warn = (
                f"\x1b[33m[Nexora] venv '{VENV_NAME}' not ready — running without isolation.\x1b[0m\r\n"
                f"\x1b[33m[Nexora] Run: python -m venv {VENV_NAME}  to create it.\x1b[0m\r\n"
            )
            self.write(warn)

    def stop(self):
        self._alive = False
        try:
            if self._pty:
                if _OS == "Windows":
                    self._pty.terminate(force=True)
                else:
                    self._pty.terminate(force=True)
        except Exception:
            pass
        self._pty = None
        self._save_history()
        with self._lock:
            for q in self._subscribers.values():
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass

    def is_alive(self) -> bool:
        if not self._alive:
            return False
        if _OS != "Windows" and self._pty:
            try:
                return bool(self._pty.alive)
            except Exception:
                pass
        return self._alive and self._pty is not None

    # ── Input ─────────────────────────────────────────────────────────

    def write(self, data: str):
        self._last_used = time.time()
        if not self.is_alive():
            return
        try:
            if _OS == "Windows":
                self._pty.write(data)
            else:
                self._pty.write(data)
        except Exception:
            self._alive = False

    def run_command(self, cmd: str, source: str = "user") -> dict:
        self._last_used = time.time()
        entry = {
            "cmd":    cmd,
            "source": source,
            "ts":     time.time(),
            "status": "sent",
        }
        with self._lock:
            self._history.append(entry)
            if len(self._history) > MAX_HISTORY:
                self._history.pop(0)
        nl = "\r\n" if _OS == "Windows" else "\n"
        self.write(cmd + nl)
        return entry

    def resize(self, cols: int, rows: int):
        if self.is_alive():
            try:
                if _OS == "Windows":
                    self._pty.setwinsize(rows, cols)
                else:
                    self._pty.setwinsize(rows, cols)
            except Exception:
                pass

    # ── SSE Subscription ──────────────────────────────────────────────

    def subscribe(self, client_id: str) -> "queue.Queue[Optional[str]]":
        q: queue.Queue = queue.Queue(maxsize=8192)
        with self._lock:
            self._subscribers[client_id] = q
            replay = "".join(self._output_buf[-150:])
        if replay:
            try:
                q.put_nowait(json.dumps({"type": "output", "data": replay,
                                         "replay": True}))
            except queue.Full:
                pass
        return q

    def unsubscribe(self, client_id: str):
        with self._lock:
            self._subscribers.pop(client_id, None)

    def _broadcast_system(self, text: str):
        """Inject a system message into the output buffer and all subscribers."""
        msg = json.dumps({"type": "output", "data": text, "replay": False})
        with self._lock:
            self._output_buf.append(text)
            subs = list(self._subscribers.values())
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

    # ── Reader loop ───────────────────────────────────────────────────

    def _reader_loop(self):
        accumulated = ""
        while self._alive:
            try:
                if _OS == "Windows":
                    chunk = self._pty.read(4096, timeout=0.05)
                else:
                    chunk = self._pty.read(4096, timeout=0.05)

                if chunk is None:
                    time.sleep(0.01)
                    continue
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                if not chunk:
                    time.sleep(0.01)
                    continue
            except (EOFError, OSError):
                break
            except Exception:
                time.sleep(0.05)
                continue

            # Terminal Intelligence: accumulate and scan
            accumulated += chunk
            if len(accumulated) > 8192:
                accumulated = accumulated[-4096:]
            error_info = _detect_error(chunk)
            if error_info:
                self._last_error = error_info
                # Emit as a special event too
                err_msg = json.dumps({
                    "type":  "error_detected",
                    "label": error_info["label"],
                    "suggestion": error_info["suggestion"],
                    "excerpt": error_info["excerpt"],
                })
                with self._lock:
                    subs = list(self._subscribers.values())
                for sq in subs:
                    try:
                        sq.put_nowait(err_msg)
                    except queue.Full:
                        pass

            # Buffer + broadcast output
            with self._lock:
                self._output_buf.append(chunk)
                if len(self._output_buf) > OUTPUT_BUF_LINES:
                    self._output_buf.pop(0)
                subs = list(self._subscribers.values())

            msg = json.dumps({"type": "output", "data": chunk, "replay": False})
            for q in subs:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

        # Session ended
        exit_msg = json.dumps({"type": "exit", "data": "[terminal closed]"})
        with self._lock:
            subs = list(self._subscribers.values())
        for q in subs:
            try:
                q.put_nowait(exit_msg)
                q.put_nowait(None)
            except queue.Full:
                pass
        self._alive = False
        self._save_history()

    # ── History persistence ───────────────────────────────────────────

    def _load_history(self):
        try:
            with open(self._history_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
                if isinstance(loaded, list):
                    self._history = loaded[-MAX_HISTORY:]
        except (FileNotFoundError, json.JSONDecodeError):
            self._history = []

    def _save_history(self):
        try:
            with self._lock:
                data = list(self._history[-MAX_HISTORY:])
            with open(self._history_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, default=str)
        except Exception:
            pass

    # ── State & history ───────────────────────────────────────────────

    def get_history(self, limit: int = 50) -> list:
        with self._lock:
            return list(self._history[-limit:])

    def get_last_error(self) -> Optional[dict]:
        return self._last_error

    def clear_last_error(self):
        self._last_error = None

    def get_state(self) -> dict:
        venv = self._venv_info or {}
        return {
            "sid":             self.sid,
            "alive":           self.is_alive(),
            "cwd":             self.cwd,
            "shell":           _DEFAULT_SHELL,
            "os":              _OS,
            "idle_s":          round(time.time() - self._last_used, 1),
            "history_count":   len(self._history),
            "venv_name":       VENV_NAME,
            "venv_ready":      venv.get("ready", False),
            "venv_activated":  self._venv_activated,
            "last_error":      self._last_error,
        }


# ── Session Registry ──────────────────────────────────────────────────────────

class TerminalRegistry:
    """Global registry of PtySession instances — one per web-terminal session."""

    def __init__(self):
        self._sessions: dict[str, PtySession] = {}
        self._lock     = threading.Lock()
        self._reaper   = threading.Thread(
            target=self._reap_idle, daemon=True, name="pty-reaper"
        )
        self._reaper.start()

    def get_or_create(self, tid: str, cwd: str) -> PtySession:
        with self._lock:
            sess = self._sessions.get(tid)
            if sess and sess.is_alive():
                return sess
            sess = PtySession(tid, cwd)
            self._sessions[tid] = sess
        sess.start()
        return sess

    def get(self, tid: str) -> Optional[PtySession]:
        with self._lock:
            return self._sessions.get(tid)

    def close(self, tid: str):
        with self._lock:
            sess = self._sessions.pop(tid, None)
        if sess:
            sess.stop()

    def close_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                s.stop()
            except Exception:
                pass

    def _reap_idle(self):
        while True:
            time.sleep(60)
            with self._lock:
                to_reap = [
                    tid for tid, s in self._sessions.items()
                    if not s.is_alive()
                    or (time.time() - s._last_used) > IDLE_TIMEOUT_SECS
                ]
            for tid in to_reap:
                self.close(tid)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [s.get_state() for s in self._sessions.values()]


# ── Singleton ──────────────────────────────────────────────────────────────────
terminal_registry = TerminalRegistry()
