"""
Phase 20.4 — Sandboxed Code Runner
==================================

Pure-Python sandboxed subprocess executor for the Multi-Agent Dev System.
The job of this module is to **run untrusted Python code as safely as
possible** without depending on container/root tools (firejail, nsjail,
bubblewrap, chroot) which are not available in the Replit environment.

What we provide
---------------
1. **Per-run sandbox directory** (``tempfile.mkdtemp``, mode 0700).
   Cleaned up unless the caller passes ``keep_sandbox=True``.

2. **Subprocess execution** — never ``exec()``.  The user's code is
   written to ``<sandbox>/main.py`` and run via
   ``python -E -s -B -u main.py``:

     * ``-E`` ignores ``PYTHON*`` env vars (defence in depth — we also
       scrub the env explicitly)
     * ``-s`` skips user site-packages
     * ``-B`` suppresses ``.pyc`` writes
     * ``-u`` forces unbuffered I/O

   We deliberately *avoid* ``-I`` (isolated mode) because it implies
   ``-P``, which drops the script's directory from ``sys.path`` and
   would prevent staged ``extra_files`` from being importable.

3. **POSIX resource limits** applied in a ``preexec_fn``:
   ``RLIMIT_CPU``, ``RLIMIT_AS`` (memory), ``RLIMIT_FSIZE``,
   ``RLIMIT_CORE`` (always 0 — no core dumps), and optionally
   ``RLIMIT_NOFILE`` / ``RLIMIT_NPROC``.

4. **Process-group kill on timeout** — the child is started with
   ``os.setsid()`` so the entire descendant tree can be killed via
   ``os.killpg(pgid, SIGKILL)`` after a ``TimeoutExpired``.  This
   closes the architect's Phase 20.3 grandchild-process gap.

5. **Output capping** — stdout / stderr each capped to
   ``max_output_bytes`` (default 1 MB).  Truncation is reported via
   ``truncated_stdout`` / ``truncated_stderr`` flags.

6. **Optional result-file channel** — if ``result_file_name`` is set,
   the runner allocates ``<sandbox>/<name>``, passes the relative
   path as the first extra argv to the child, then reads it back
   (capped at ``max_result_bytes``, default 5 MB) into
   ``RunResult.extra["result_file"]``.  This is the integration hook
   for the Phase 20.3 self-testing system, whose harness writes its
   completion envelope to ``sys.argv[1]``.

7. **Scrubbed environment** — the child sees a minimal env (PATH,
   HOME, TMPDIR, PYTHON*-safety flags, locale).  Caller-supplied
   ``env`` overrides extend, never the parent's full environment.

8. **Optional language-level import gate** — when
   ``restrict_imports=True``, a small preamble is prepended that
   monkeypatches ``__builtins__.__import__`` to reject a configurable
   block-list (default: ``subprocess``, ``ctypes``, ``socket``,
   ``urllib``, etc.).  This is *defense-in-depth*, not a true
   sandbox; the OS-level limits above are the primary defense.

9. **Path-traversal-safe extra files** — caller can stage
   ``extra_files={'helper.py': '...'}`` into the sandbox; absolute
   paths and ``..`` segments are rejected.

Honest limitations
------------------
* Same-uid file access: without root or container tools we cannot
  prevent the child from reading absolute paths it knows
  (``/home/runner/...``).  ``cwd``+scrubbed-env reduces the surface
  but does not eliminate it.
* ``RLIMIT_AS`` is best-effort: Linux overcommit may delay enforcement
  until first-touch.  For typical test workloads this is fine.
* The import gate is bypassable by sufficiently advanced code (e.g.
  ``importlib._bootstrap``).  It catches naïve misuse only.

Public API
----------
``run_code(code, **opts) -> RunResult``
``run_script(script_path, **opts) -> RunResult``

Compatible with Phase 20.3's harness via ``result_file_name="result.json"``.

No new dependencies, no UI, no main-loop wiring.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
try:
    import resource
except ImportError:
    resource = None

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────

# Hard ceiling for ``timeout`` — keeps the runner from being abused
# as an indefinite background launcher.  Callers asking for >60 s are
# rejected up-front rather than silently honoured.
_MAX_TIMEOUT = 60.0

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_MEM_MB = 256
_DEFAULT_OUTPUT_CAP = 1 * 1024 * 1024            # 1 MB stdout / stderr cap
_DEFAULT_RESULT_FILE_CAP = 5 * 1024 * 1024       # 5 MB result-file cap
_DEFAULT_FSIZE_CAP = 5 * 1024 * 1024             # RLIMIT_FSIZE per-file
_DEFAULT_NOFILE: Optional[int] = None            # leave alone — Python opens many fds
_DEFAULT_NPROC: Optional[int] = None             # leave alone — shared user

# Module-level allow-list of env vars that are inherited *into* the
# scrubbed child env.  Anything outside this list is dropped.
_INHERITED_ENV_KEYS = ("LANG", "LC_ALL")


def _resolve_real_python() -> str:
    """Return path to a Python binary safe to exec under RLIMIT_AS.

    On Replit (and other Nix-wrapped environments), ``sys.executable``
    points at a thin Go wrapper that reserves ~1 GB of virtual address
    space at startup for its own runtime — which is fatally truncated
    by the modest ``RLIMIT_AS`` we apply to sandboxed children.  We
    therefore prefer the unwrapped CPython interpreter found at
    ``<sys.exec_prefix>/bin/python3``, falling back to
    ``sys.executable`` only when no separate underlying binary exists.
    Caller can always override via the ``python_executable`` parameter.
    """
    try:
        candidate = os.path.join(sys.exec_prefix, "bin", "python3")
        if (os.path.isfile(candidate)
                and os.access(candidate, os.X_OK)
                and os.path.realpath(candidate)
                != os.path.realpath(sys.executable)):
            return candidate
    except (OSError, AttributeError):
        pass
    return sys.executable


# Cached at import time; cheap to compute, no side effects.
_REAL_PYTHON = _resolve_real_python()

# Default block-list for the optional import gate.  We deliberately
# do NOT block ``os`` or ``sys`` — too many normal things break — but
# we do block obvious escape hatches like ``subprocess`` and ``socket``.
DEFAULT_BLOCKED_MODULES: tuple[str, ...] = (
    "subprocess",
    "multiprocessing",
    "concurrent.futures.process",
    "ctypes",
    "_ctypes",
    "socket",
    "ssl",
    "urllib",
    "urllib.request",
    "http",
    "http.client",
    "http.server",
    "ftplib",
    "smtplib",
    "telnetlib",
    "shutil",
)


# ─────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    """Structured response from :func:`run_code` / :func:`run_script`.

    ``status`` values:

    ``"success"``
        Subprocess exited 0.
    ``"error"``
        Subprocess exited non-zero (e.g. uncaught exception → SystemExit
        with traceback on stderr).
    ``"timeout"``
        Wall-clock ``timeout`` exceeded; entire process group SIGKILL'd.
    ``"killed"``
        Subprocess was killed by a signal (negative returncode) — most
        likely an OOM kill or ``RLIMIT_FSIZE`` violation (SIGXFSZ).
    ``"rejected"``
        The runner refused to launch (bad args / unsafe ``extra_files``
        path / etc.).  ``error`` carries the human-readable reason.
    """

    status: str
    output: str
    error: str
    exit_code: int
    duration_sec: float
    sandbox_dir: str
    truncated_stdout: bool
    truncated_stderr: bool
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────
# Internal: import-gate preamble
# ─────────────────────────────────────────────────────────────────

# Prepended to user code when ``restrict_imports=True``.  We capture
# the original __import__ as a closure local *before* installing the
# gate, so the gate cannot be defeated by re-importing __builtins__.
# This is best-effort: code that goes through ``importlib._bootstrap``
# can still side-step it.  See module docstring "Honest limitations".
_IMPORT_GATE_TEMPLATE = '''\
import builtins as _ig_builtins
_IG_BLOCKED = frozenset({blocked_repr})
_ig_orig_import = _ig_builtins.__import__

def _ig_gate(name, _g=None, _l=None, fromlist=(), level=0,
             _orig=_ig_orig_import, _blocked=_IG_BLOCKED):
    base = name.split(".", 1)[0]
    if base in _blocked or name in _blocked:
        raise ImportError(
            "import of " + name + " blocked by code_runner sandbox"
        )
    return _orig(name, _g, _l, fromlist, level)

_ig_builtins.__import__ = _ig_gate
del _ig_gate, _ig_orig_import, _ig_builtins
'''


def _build_import_gate(blocked: List[str]) -> str:
    """Return the import-gate preamble configured to block ``blocked``."""
    return _IMPORT_GATE_TEMPLATE.format(blocked_repr=repr(tuple(blocked)))


# ─────────────────────────────────────────────────────────────────
# Internal: streaming output reader with cap-and-kill
# ─────────────────────────────────────────────────────────────────

# Chunk size for the streaming reader.  Small enough that a flooding
# child can't push much past the cap before we notice and kill, large
# enough that ordinary output is read in 1-2 syscalls.
_STREAM_CHUNK = 64 * 1024


def _stream_capped(
    proc: "subprocess.Popen[bytes]",
    cap_bytes: int,
    wall_timeout: float,
) -> tuple[bytes, bytes, bool, bool, bool]:
    """Stream ``proc.stdout`` / ``proc.stderr`` with hard byte caps.

    The architect-flagged DoS in the original implementation came from
    using ``proc.communicate(timeout=…)``, which buffers the **entire**
    output stream in parent memory before any cap is applied.  A
    flooding child could exhaust host RAM long before the wall-clock
    timeout fired.

    This helper runs two daemon threads — one per pipe — that read in
    ``_STREAM_CHUNK``-sized batches, append to a per-stream buffer up
    to ``cap_bytes``, and SIGKILL the entire process group the moment
    either stream exceeds its cap.  After the kill the readers drain
    until EOF (which arrives quickly because the kernel closes the
    pipes when the child dies), then exit.

    Returns ``(stdout_bytes, stderr_bytes, truncated_out,
    truncated_err, timed_out)``.
    """
    import threading

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    truncated = {"out": False, "err": False}
    cap_killed = {"flag": False}

    def _kill_group() -> None:
        if cap_killed["flag"]:
            return
        cap_killed["flag"] = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _reader(stream, buf: bytearray, key: str) -> None:
        try:
            while True:
                chunk = stream.read(_STREAM_CHUNK)
                if not chunk:
                    break
                room = cap_bytes - len(buf)
                if room <= 0:
                    # Already capped; just drain & discard, kill once.
                    truncated[key] = True
                    _kill_group()
                    continue
                if len(chunk) > room:
                    buf.extend(chunk[:room])
                    truncated[key] = True
                    _kill_group()
                else:
                    buf.extend(chunk)
        except (OSError, ValueError):
            # Pipe closed under us / stream torn down — fine, exit.
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t_out = threading.Thread(
        target=_reader, args=(proc.stdout, stdout_buf, "out"), daemon=True)
    t_err = threading.Thread(
        target=_reader, args=(proc.stderr, stderr_buf, "err"), daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=wall_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Wall-clock timeout: kill the process group (closes 20.3
        # grandchild gap).
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass

    # Reader threads exit shortly after the child's pipes close.
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    return (bytes(stdout_buf), bytes(stderr_buf),
            truncated["out"], truncated["err"], timed_out)


# ─────────────────────────────────────────────────────────────────
# Internal: preexec_fn factory
# ─────────────────────────────────────────────────────────────────

def _make_preexec(
    cpu_seconds: Optional[int],
    memory_bytes: Optional[int],
    fsize_bytes: Optional[int],
    nofile: Optional[int],
    nproc: Optional[int],
) -> Callable[[], None]:
    """Build a ``preexec_fn`` that runs in the child after ``fork()``
    and before ``exec()``.  Each ``setrlimit`` call is wrapped in a
    permissive try/except so a single failed limit doesn't kill the
    entire run — we degrade gracefully on platforms / environments
    where a particular limit isn't enforceable.

    The first thing we do is ``os.setsid()`` so the parent can later
    ``killpg`` the entire descendant process group on timeout.  This
    is the architect-flagged grandchild-process fix from Phase 20.3.
    """
    def _preexec() -> None:
        try:
            os.setsid()
        except OSError:
            pass

        def _try_set(limit_name: str, value: Optional[int]) -> None:
            if value is None:
                return
            if limit is None or resource is None:
                return
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError) as exc:
                # Don't crash the child — just log via stderr.  The
                # parent will still enforce wall-clock timeout and
                # output caps even if a particular RLIMIT failed.
                try:
                    os.write(2, f"[code_runner] setrlimit({limit_name}, "
                                f"{value}) failed: {exc}\n".encode())
                except OSError:
                    pass

        _try_set("RLIMIT_CPU",   cpu_seconds)
        _try_set("RLIMIT_AS",    memory_bytes)
        _try_set("RLIMIT_FSIZE", fsize_bytes)
        _try_set("RLIMIT_NOFILE", nofile)
        _try_set("RLIMIT_NPROC", nproc)
        _try_set("RLIMIT_CORE",  0)

    return _preexec


# ─────────────────────────────────────────────────────────────────
# Internal: env scrubbing
# ─────────────────────────────────────────────────────────────────

def _build_env(sandbox_dir: str,
               extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Build the env dict the child will see.

    Starts from a minimal allow-list (PATH, HOME, TMPDIR, locale +
    Python-safety flags), inherits a few harmless keys from the parent
    if present, then applies caller-supplied overrides last.
    """
    env: Dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "HOME": sandbox_dir,
        "TMPDIR": sandbox_dir,
        "PWD": sandbox_dir,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in _INHERITED_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]
    if extra_env:
        env.update(extra_env)
    return env


# ─────────────────────────────────────────────────────────────────
# Internal: extra-file staging
# ─────────────────────────────────────────────────────────────────

def _is_safe_relative(path: str) -> bool:
    """Reject absolute paths and any segment equal to ``..``."""
    if not path or os.path.isabs(path):
        return False
    parts = path.replace("\\", "/").split("/")
    return all(p not in ("", "..") for p in parts)


def _stage_extra_files(sandbox_dir: str,
                       files: Dict[str, str]) -> Optional[str]:
    """Write each ``files`` entry into the sandbox, with path
    traversal protection.  Returns ``None`` on success, or a
    human-readable rejection reason."""
    for relname, content in files.items():
        if not _is_safe_relative(relname):
            return f"extra_files path '{relname}' is not safe"
        full = os.path.join(sandbox_dir, relname)
        # Defence-in-depth: confirm the resolved path stays inside.
        full_real = os.path.realpath(full)
        sandbox_real = os.path.realpath(sandbox_dir)
        if not (full_real == sandbox_real
                or full_real.startswith(sandbox_real + os.sep)):
            return f"extra_files path '{relname}' escapes sandbox"
        os.makedirs(os.path.dirname(full) or sandbox_dir, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        try:
            os.chmod(full, 0o600)
        except OSError:
            pass
    return None


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

def run_code(
    code: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_output_bytes: int = _DEFAULT_OUTPUT_CAP,
    memory_limit_mb: Optional[int] = _DEFAULT_MEM_MB,
    cpu_seconds: Optional[int] = None,
    fsize_limit_bytes: Optional[int] = _DEFAULT_FSIZE_CAP,
    nofile: Optional[int] = _DEFAULT_NOFILE,
    nproc: Optional[int] = _DEFAULT_NPROC,
    extra_files: Optional[Dict[str, str]] = None,
    extra_argv: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    restrict_imports: bool = False,
    blocked_modules: Optional[List[str]] = None,
    result_file_name: Optional[str] = None,
    max_result_bytes: int = _DEFAULT_RESULT_FILE_CAP,
    keep_sandbox: bool = False,
    python_executable: Optional[str] = None,
) -> RunResult:
    """Execute Python ``code`` in a sandboxed subprocess.

    Most arguments are documented at the top of the module; the few
    that need extra context here:

    Parameters
    ----------
    cpu_seconds:
        ``RLIMIT_CPU`` value.  When ``None``, defaults to
        ``ceil(timeout) + 1`` so the kernel-level CPU limit kicks in
        at roughly the same time as the parent wall-clock timeout.
    result_file_name:
        Relative filename inside the sandbox.  When set, the runner
        passes the path as the first extra argv to the child and
        reads the file back into ``RunResult.extra["result_file"]``
        (bytes, capped to ``max_result_bytes``).  This is how the
        Phase 20.3 testing harness round-trips its completion
        envelope.
    keep_sandbox:
        Skip cleanup so the caller can inspect ``RunResult.sandbox_dir``.
        Use sparingly — leaks a tempdir per call.
    """
    # ── Argument validation (rejected up-front, no subprocess spawn) ─
    if not isinstance(code, str):
        return _rejected("code must be a string")
    if timeout <= 0 or timeout > _MAX_TIMEOUT:
        return _rejected(f"timeout {timeout!r} out of range (0, {_MAX_TIMEOUT}]")
    if max_output_bytes <= 0:
        return _rejected("max_output_bytes must be positive")
    if max_result_bytes <= 0:
        return _rejected("max_result_bytes must be positive")
    if result_file_name is not None and not _is_safe_relative(result_file_name):
        return _rejected(f"result_file_name '{result_file_name}' is not safe")

    sandbox_dir = tempfile.mkdtemp(prefix="coderun_")
    cleanup_dir = sandbox_dir if not keep_sandbox else None
    try:
        try:
            os.chmod(sandbox_dir, 0o700)
        except OSError:
            pass

        # ── Stage extra files first so import-gate diagnostics that
        # mention them are accurate.
        if extra_files:
            err = _stage_extra_files(sandbox_dir, extra_files)
            if err:
                return _rejected(err, sandbox_dir=sandbox_dir)

        # ── Compose code (optional import-gate preamble + user source)
        if restrict_imports:
            blocked = list(blocked_modules) if blocked_modules \
                else list(DEFAULT_BLOCKED_MODULES)
            code_to_run = _build_import_gate(blocked) + "\n" + code
        else:
            code_to_run = code

        main_path = os.path.join(sandbox_dir, "main.py")
        with open(main_path, "w", encoding="utf-8") as fh:
            fh.write(code_to_run)
        try:
            os.chmod(main_path, 0o600)
        except OSError:
            pass

        # ── Build argv:
        #   python -E -s -B -u main.py [result_file_name] [extra_argv...]
        # See module docstring for why we use these flags instead of -I.
        # Default to the unwrapped interpreter so RLIMIT_AS doesn't
        # break the child startup; caller may override.
        py = python_executable or _REAL_PYTHON
        argv: List[str] = [py, "-E", "-s", "-B", "-u", main_path]
        if result_file_name:
            argv.append(result_file_name)
        if extra_argv:
            argv.extend(extra_argv)

        run_env = _build_env(sandbox_dir, env)

        cpu = cpu_seconds if cpu_seconds is not None \
            else max(1, int(timeout) + 1)
        mem_bytes = (memory_limit_mb * 1024 * 1024) \
            if memory_limit_mb else None

        preexec = _make_preexec(
            cpu_seconds=cpu,
            memory_bytes=mem_bytes,
            fsize_bytes=fsize_limit_bytes,
            nofile=nofile,
            nproc=nproc,
        )

        # ── Spawn ───────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=sandbox_dir,
                env=run_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=preexec,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            return _rejected(f"failed to spawn subprocess: {exc}",
                             sandbox_dir=sandbox_dir)

        # Streaming read with cap-and-kill — protects host from
        # output-flooding children (architect Phase 20.4 review).  The
        # cap is enforced *during* read, not after, and the process
        # group is SIGKILL'd as soon as either stream exceeds it.
        (stdout_bytes, stderr_bytes,
         truncated_out, truncated_err,
         timed_out) = _stream_capped(proc, max_output_bytes, timeout)

        duration = time.monotonic() - t0

        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        # ── Status mapping ─────────────────────────────────────────
        rc = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            status = "timeout"
        elif rc == 0:
            status = "success"
        elif rc < 0:
            status = "killed"
        else:
            status = "error"

        result = RunResult(
            status=status,
            output=stdout_str,
            error=stderr_str,
            exit_code=rc,
            duration_sec=round(duration, 4),
            sandbox_dir=sandbox_dir,
            truncated_stdout=truncated_out,
            truncated_stderr=truncated_err,
        )

        # ── Result-file readback ───────────────────────────────────
        # Hardened against architect-flagged symlink exfiltration:
        #   * O_NOFOLLOW so a child-planted symlink raises ELOOP
        #     instead of being silently followed to /etc/shadow,
        #     ~/.replit, etc.
        #   * fstat S_ISREG so directories / sockets / FIFOs / device
        #     nodes are rejected.
        #   * Defence-in-depth realpath check: even if O_NOFOLLOW were
        #     somehow defeated, the resolved path must still live
        #     inside the sandbox.
        if result_file_name:
            full = os.path.join(sandbox_dir, result_file_name)
            sandbox_real = os.path.realpath(sandbox_dir)
            result.extra["result_file"] = None
            fd = -1
            try:
                fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                result.extra["result_file_status"] = "missing"
            except OSError as exc:
                # ELOOP (symlink+NOFOLLOW), ENOTDIR, EACCES, etc.
                result.extra["result_file_status"] = (
                    f"open failed: {exc}")
            else:
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        result.extra["result_file_status"] = (
                            "not a regular file")
                    elif st.st_size > max_result_bytes:
                        result.extra["result_file_status"] = (
                            f"oversize: {st.st_size} > {max_result_bytes}")
                    else:
                        full_real = os.path.realpath(full)
                        if not (full_real == sandbox_real
                                or full_real.startswith(
                                    sandbox_real + os.sep)):
                            result.extra["result_file_status"] = (
                                "outside sandbox")
                        else:
                            data = b""
                            remaining = st.st_size
                            while remaining > 0:
                                chunk = os.read(
                                    fd, min(remaining, _STREAM_CHUNK))
                                if not chunk:
                                    break
                                data += chunk
                                remaining -= len(chunk)
                            result.extra["result_file"] = data
                            result.extra["result_file_status"] = "ok"
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        return result

    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def run_script(
    script_path: str,
    *,
    extra_argv: Optional[List[str]] = None,
    **kwargs: Any,
) -> RunResult:
    """Run an *existing* ``.py`` file in the sandbox.

    The script is read from disk and re-emitted into the sandbox dir
    as ``main.py``.  All other keyword arguments are forwarded to
    :func:`run_code`.  Useful for the Phase 20.3 self-testing system,
    which generates a harness script via ``code_testing._build_harness``
    and previously ran it via its own un-sandboxed subprocess wrapper.
    """
    if not isinstance(script_path, str) or not os.path.isfile(script_path):
        return _rejected(f"script not found: {script_path!r}")
    try:
        with open(script_path, "r", encoding="utf-8") as fh:
            code = fh.read()
    except OSError as exc:
        return _rejected(f"could not read script: {exc}")
    return run_code(code, extra_argv=extra_argv, **kwargs)


# ─────────────────────────────────────────────────────────────────
# Internal helper: rejected-result factory
# ─────────────────────────────────────────────────────────────────

def _rejected(reason: str, *, sandbox_dir: str = "") -> RunResult:
    """Build a uniform 'rejected' RunResult.  When the rejection
    happens after the sandbox dir was created, the caller passes its
    path so the dir can still be reported / cleaned up."""
    return RunResult(
        status="rejected",
        output="",
        error=reason,
        exit_code=-1,
        duration_sec=0.0,
        sandbox_dir=sandbox_dir,
        truncated_stdout=False,
        truncated_stderr=False,
    )


# ─────────────────────────────────────────────────────────────────
# CLI — quick smoke entry point.  Not used by the dev loop; useful
# for ad-hoc verification:  python3 code_runner.py 'print(1+1)'
# ─────────────────────────────────────────────────────────────────

def _cli(argv: List[str]) -> int:  # pragma: no cover — manual use only
    if len(argv) < 2:
        print("usage: python3 code_runner.py '<code>'", file=sys.stderr)
        return 2
    res = run_code(argv[1])
    print(json.dumps({k: v for k, v in res.to_dict().items()
                      if k != "extra"}, indent=2))
    return 0 if res.status == "success" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv))
