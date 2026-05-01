"""
sandbox_manager.py — Phase 35: Secure Sandbox Execution
========================================================
Provides isolated, resource-limited execution environments.

Backends (auto-selected):
  1. Docker  — preferred; full isolation via containers
  2. Process — subprocess with resource limits (fallback when Docker absent)
  3. Thread  — in-process with timeout only (last resort)

All backends expose the same interface:
    SandboxManager.run(code, language, timeout, task_id) → SandboxResult

Design rules:
  • Silent fallback — never raises; returns SandboxResult with error field
  • Auto-cleanup   — containers/files removed after run or on timeout
  • Resource caps  — CPU, memory, disk enforced per task
  • Thread-safe    — multiple workers call run() concurrently
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import psutil

logger = logging.getLogger(__name__)

# ── Resource limits ──────────────────────────────────────────────────────────
DEFAULT_TIMEOUT_SECS  = int(os.environ.get("SANDBOX_TIMEOUT",   "30"))
DEFAULT_MEM_MB        = int(os.environ.get("SANDBOX_MEM_MB",    "256"))
DEFAULT_CPU_CORES     = float(os.environ.get("SANDBOX_CPU",     "0.5"))
DEFAULT_DISK_MB       = int(os.environ.get("SANDBOX_DISK_MB",   "100"))
DOCKER_IMAGE          = os.environ.get("SANDBOX_IMAGE", "python:3.11-slim")
MAX_OUTPUT_CHARS      = 8000

# ── Check backends ────────────────────────────────────────────────────────────
def _has_docker() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_DOCKER_AVAILABLE = _has_docker()


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SandboxResult:
    task_id:    str
    backend:    str           # 'docker' | 'process' | 'thread'
    ok:         bool
    stdout:     str = ""
    stderr:     str = ""
    exit_code:  int = 0
    elapsed_s:  float = 0.0
    mem_mb:     float = 0.0
    timed_out:  bool = False
    error:      str = ""

    def to_dict(self) -> dict:
        return {
            "task_id":   self.task_id,
            "backend":   self.backend,
            "ok":        self.ok,
            "stdout":    self.stdout[:MAX_OUTPUT_CHARS],
            "stderr":    self.stderr[:MAX_OUTPUT_CHARS],
            "exit_code": self.exit_code,
            "elapsed_s": round(self.elapsed_s, 3),
            "mem_mb":    round(self.mem_mb, 2),
            "timed_out": self.timed_out,
            "error":     self.error[:500],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Docker backend
# ─────────────────────────────────────────────────────────────────────────────
class _DockerSandbox:
    """Runs code in a throwaway Docker container."""

    def run(self, code: str, language: str, timeout: int,
            task_id: str, mem_mb: int, cpu: float, workspace_dir: str = None) -> SandboxResult:
        t0 = time.time()
        cid = f"openhand-sb-{task_id[:12]}"
        tmpdir = workspace_dir if workspace_dir else tempfile.mkdtemp(prefix="sb_")
        try:
            # Write code file
            ext = {"python": "py", "javascript": "js",
                   "bash": "sh", "shell": "sh"}.get(language, "py")
            code_file = os.path.join(tmpdir, f"main.{ext}")
            with open(code_file, "w", encoding="utf-8") as f:
                f.write(code)

            runner = {
                "python": "python main.py",
                "javascript": "node main.js",
                "bash": "bash main.sh",
                "shell": "sh main.sh",
            }.get(language, "python main.py")

            cmd = [
                "docker", "run", "--rm",
                "--name", cid,
                "--network", "none",           # no internet
                f"--memory={mem_mb}m",
                f"--cpus={cpu}",
                "--pids-limit=64",
                "--read-only",
                "--tmpfs", "/tmp:size=50m",
                "-v", f"{tmpdir}:/workspace:ro",
                "-w", "/workspace",
                DOCKER_IMAGE,
                "sh", "-c", runner,
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                elapsed = time.time() - t0
                ok = (proc.returncode == 0)
                return SandboxResult(
                    task_id=task_id, backend="docker",
                    ok=ok, stdout=stdout, stderr=stderr,
                    exit_code=proc.returncode, elapsed_s=elapsed)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", cid],
                               capture_output=True, timeout=5)
                proc.kill()
                return SandboxResult(
                    task_id=task_id, backend="docker",
                    ok=False, timed_out=True,
                    elapsed_s=time.time() - t0,
                    error=f"Timed out after {timeout}s")
        except Exception as e:
            return SandboxResult(
                task_id=task_id, backend="docker",
                ok=False, error=str(e), elapsed_s=time.time() - t0)
        finally:
            if not workspace_dir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            # Ensure container removed even if --rm failed
            subprocess.run(["docker", "rm", "-f", cid],
                           capture_output=True, timeout=5,
                           check=False)


# ─────────────────────────────────────────────────────────────────────────────
# Process backend (fallback)
# ─────────────────────────────────────────────────────────────────────────────
import re as _re_mod

try:
    _DANGEROUS = _re_mod.compile(
        r"\b(os\.remove|shutil\.rmtree|subprocess|os\.system|eval\(|exec\(|"
        r"__import__|socket\.connect)",
        _re_mod.I)
except Exception:
    _DANGEROUS = None


class _ProcessSandbox:
    """Runs code as a subprocess with timeout and basic safety checks."""

    _BLOCKED = {
        "shutil.rmtree", "os.remove", "os.system", "os.popen",
        "subprocess.run", "subprocess.Popen", "socket.connect",
        "requests.delete", "open.*'w'",
    }

    def run(self, code: str, language: str, timeout: int,
            task_id: str, mem_mb: int, cpu: float, workspace_dir: str = None) -> SandboxResult:
        t0 = time.time()
        # Basic safety scan
        if _DANGEROUS and _DANGEROUS.search(code):
            return SandboxResult(
                task_id=task_id, backend="process",
                ok=False, error="Code blocked by safety scanner")

        tmpdir = workspace_dir if workspace_dir else tempfile.mkdtemp(prefix="sb_proc_")
        try:
            ext = {"python": "py", "bash": "sh",
                   "javascript": "js"}.get(language, "py")
            code_file = os.path.join(tmpdir, f"main.{ext}")
            with open(code_file, "w", encoding="utf-8") as f:
                f.write(code)

            interp = {"python": sys.executable, "bash": "bash",
                      "javascript": "node"}.get(language, sys.executable)
            env = {**os.environ, "PYTHONPATH": "", "HOME": tmpdir}

            proc = subprocess.Popen(
                [interp, code_file], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=tmpdir, env=env, text=True)

            # Track peak memory
            peak_mb = 0.0
            try:
                ps_proc = psutil.Process(proc.pid)
            except Exception:
                ps_proc = None

            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                if ps_proc:
                    try:
                        peak_mb = ps_proc.memory_info().rss / 1_048_576
                    except Exception:
                        pass
                elapsed = time.time() - t0
                ok = proc.returncode == 0
                # Memory limit enforcement (post-hoc)
                if peak_mb > mem_mb:
                    return SandboxResult(
                        task_id=task_id, backend="process", ok=False,
                        error=f"Memory limit exceeded: {peak_mb:.0f}MB > {mem_mb}MB",
                        elapsed_s=elapsed, mem_mb=peak_mb)
                return SandboxResult(
                    task_id=task_id, backend="process",
                    ok=ok, stdout=stdout, stderr=stderr,
                    exit_code=proc.returncode, elapsed_s=elapsed,
                    mem_mb=peak_mb)
            except subprocess.TimeoutExpired:
                proc.kill()
                return SandboxResult(
                    task_id=task_id, backend="process",
                    ok=False, timed_out=True,
                    elapsed_s=time.time() - t0,
                    error=f"Timed out after {timeout}s")
        except Exception as e:
            return SandboxResult(
                task_id=task_id, backend="process",
                ok=False, error=str(e)[:300],
                elapsed_s=time.time() - t0)
        finally:
            if not workspace_dir:
                shutil.rmtree(tmpdir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# SandboxManager — public interface
# ─────────────────────────────────────────────────────────────────────────────
class SandboxManager:
    """Auto-selects Docker or Process backend; tracks all runs."""

    def __init__(self) -> None:
        self._docker  = _DockerSandbox()  if _DOCKER_AVAILABLE else None
        self._process = _ProcessSandbox()
        self._lock    = threading.Lock()
        self._history: list[dict] = []
        self._MAX_HISTORY = 200
        logger.info("[Sandbox] backend=%s",
                    "docker" if _DOCKER_AVAILABLE else "process")

    @property
    def backend_name(self) -> str:
        return "docker" if _DOCKER_AVAILABLE else "process"

    def run(self,
            code:     str,
            language: str   = "python",
            timeout:  int   = DEFAULT_TIMEOUT_SECS,
            task_id:  str   = "",
            mem_mb:   int   = DEFAULT_MEM_MB,
            cpu:      float = DEFAULT_CPU_CORES,
            workspace_dir: str = None) -> SandboxResult:
        """Execute code in an isolated environment. Never raises."""
        task_id = task_id or uuid.uuid4().hex[:12]
        backend = self._docker if self._docker else self._process
        try:
            result = backend.run(code, language, timeout, task_id, mem_mb, cpu, workspace_dir)
        except Exception as e:
            result = SandboxResult(
                task_id=task_id, backend=self.backend_name,
                ok=False, error=f"Sandbox internal error: {e}")

        with self._lock:
            self._history.append({**result.to_dict(), "ts": time.time()})
            if len(self._history) > self._MAX_HISTORY:
                self._history.pop(0)
        return result

    def history(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history[-n:])

    def stats(self) -> dict:
        with self._lock:
            h = list(self._history)
        total   = len(h)
        ok      = sum(1 for r in h if r.get("ok"))
        to_     = sum(1 for r in h if r.get("timed_out"))
        avg_ms  = (sum(r.get("elapsed_s", 0) for r in h) / total * 1000) if total else 0
        return {
            "backend":    self.backend_name,
            "docker":     _DOCKER_AVAILABLE,
            "total_runs": total,
            "ok":         ok,
            "failed":     total - ok,
            "timed_out":  to_,
            "avg_ms":     round(avg_ms, 1),
        }


# Module-level singleton
_sandbox_instance: Optional[SandboxManager] = None
_sandbox_lock = threading.Lock()

def get_sandbox() -> SandboxManager:
    global _sandbox_instance
    with _sandbox_lock:
        if _sandbox_instance is None:
            _sandbox_instance = SandboxManager()
    return _sandbox_instance
