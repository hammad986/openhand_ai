"""
resource_tracker.py — Phase 35: Resource + Cost Intelligence
=============================================================
Tracks per-task token usage, API cost, CPU/memory, and enforces budgets.

Features:
  • Token counting (tiktoken with fallback word-count heuristic)
  • Per-provider cost estimation ($/1K tokens from .env or hardcoded defaults)
  • CPU + memory snapshot via psutil
  • Budget enforcement: auto-raise BudgetExceededError when cost > cap
  • Thread-safe; persists run ledger to SQLite

Usage:
    tracker = get_tracker()
    with tracker.track("task-123", budget_usd=0.05) as ctx:
        tokens = ctx.count_and_charge("gemini", prompt, completion)
        ctx.check_budget()   # raises BudgetExceededError if over
    summary = tracker.summary("task-123")
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional

import psutil

logger = logging.getLogger(__name__)

# ── Token counter (tiktoken preferred) ───────────────────────────────────────
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def _count_tokens(text: str) -> int:
        try:
            return len(_ENC.encode(text[:50_000]))
        except Exception:
            return len(text.split())
except Exception:
    _ENC = None
    def _count_tokens(text: str) -> int:
        return max(1, len(text.split()))


# ── Cost table (USD per 1K tokens, input/output) ─────────────────────────────
# Updated April 2025 list prices. Override via env vars: COST_<PROVIDER>_IN/OUT
_DEFAULT_COSTS: Dict[str, Dict[str, float]] = {
    "gemini":       {"in": 0.00025,  "out": 0.0005},
    "gemini-pro":   {"in": 0.00125,  "out": 0.005},
    "gpt-4o":       {"in": 0.005,    "out": 0.015},
    "gpt-4o-mini":  {"in": 0.00015,  "out": 0.0006},
    "claude-3-5":   {"in": 0.003,    "out": 0.015},
    "groq":         {"in": 0.00005,  "out": 0.00008},
    "openrouter":   {"in": 0.0005,   "out": 0.0015},
    "ollama":       {"in": 0.0,      "out": 0.0},
    "local":        {"in": 0.0,      "out": 0.0},
    "default":      {"in": 0.001,    "out": 0.002},
}

def _cost_per_1k(provider: str, direction: str) -> float:
    key = provider.lower().split("/")[0]
    env_key = f"COST_{key.upper()}_{direction.upper()}"
    env_val = os.environ.get(env_key, "")
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    table = _DEFAULT_COSTS.get(key, _DEFAULT_COSTS["default"])
    return table.get(direction, 0.001)


# ── Exceptions ────────────────────────────────────────────────────────────────
class BudgetExceededError(Exception):
    """Raised when a task's accumulated cost exceeds its budget cap."""
    def __init__(self, task_id: str, spent: float, budget: float):
        self.task_id = task_id
        self.spent   = spent
        self.budget  = budget
        super().__init__(
            f"Task {task_id}: spent ${spent:.5f} > budget ${budget:.5f}")


# ── Per-task context ──────────────────────────────────────────────────────────
@dataclass
class TaskBudgetContext:
    task_id:    str
    budget_usd: float               # 0 = unlimited
    _tracker:   "ResourceTracker"
    tokens_in:  int   = 0
    tokens_out: int   = 0
    cost_usd:   float = 0.0
    calls:      int   = 0
    cpu_pct:    float = 0.0
    mem_mb:     float = 0.0
    _t0:        float = field(default_factory=time.time)

    def count_and_charge(self, provider: str,
                         prompt: str, completion: str = "") -> int:
        """Count tokens for one LLM call, accumulate cost, return total tokens."""
        ti = _count_tokens(prompt)
        to = _count_tokens(completion) if completion else 0
        cost = (ti * _cost_per_1k(provider, "in") +
                to * _cost_per_1k(provider, "out")) / 1000.0
        self.tokens_in  += ti
        self.tokens_out += to
        self.cost_usd   += cost
        self.calls      += 1
        return ti + to

    def check_budget(self) -> None:
        """Raise BudgetExceededError if over cap."""
        if self.budget_usd > 0 and self.cost_usd > self.budget_usd:
            raise BudgetExceededError(self.task_id, self.cost_usd, self.budget_usd)

    def snapshot_system(self) -> None:
        """Take a CPU + memory snapshot via psutil."""
        try:
            self.cpu_pct = psutil.cpu_percent(interval=0.1)
            self.mem_mb  = psutil.Process().memory_info().rss / 1_048_576
        except Exception:
            pass

    def elapsed(self) -> float:
        return time.time() - self._t0

    def to_dict(self) -> dict:
        return {
            "task_id":    self.task_id,
            "tokens_in":  self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens":     self.tokens_in + self.tokens_out,
            "cost_usd":   round(self.cost_usd, 6),
            "budget_usd": self.budget_usd,
            "over_budget": (self.budget_usd > 0 and
                            self.cost_usd > self.budget_usd),
            "calls":      self.calls,
            "cpu_pct":    round(self.cpu_pct, 1),
            "mem_mb":     round(self.mem_mb, 1),
            "elapsed_s":  round(self.elapsed(), 2),
        }


# ── SQLite schema ─────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS resource_ledger (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    cost_usd    REAL    DEFAULT 0,
    budget_usd  REAL    DEFAULT 0,
    calls       INTEGER DEFAULT 0,
    cpu_pct     REAL    DEFAULT 0,
    mem_mb      REAL    DEFAULT 0,
    elapsed_s   REAL    DEFAULT 0,
    over_budget INTEGER DEFAULT 0,
    ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rl_task ON resource_ledger(task_id);
CREATE INDEX IF NOT EXISTS ix_rl_ts   ON resource_ledger(ts DESC);
"""


class ResourceTracker:
    """Thread-safe resource + cost tracker with SQLite persistence."""

    def __init__(self, db_path: str = "./data/resource_ledger.db") -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db   = db_path
        self._lock = threading.Lock()
        self._active: Dict[str, TaskBudgetContext] = {}
        self._init_db()

    def _init_db(self) -> None:
        try:
            conn = sqlite3.connect(self._db, check_same_thread=False)
            conn.executescript(_DDL)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("[ResourceTracker] DB init failed: %s", e)

    @contextmanager
    def track(self, task_id: str = "",
              budget_usd: float = 0.0):
        """Context manager that creates a TaskBudgetContext for one task run."""
        task_id = task_id or uuid.uuid4().hex[:12]
        ctx = TaskBudgetContext(
            task_id=task_id, budget_usd=budget_usd, _tracker=self)
        with self._lock:
            self._active[task_id] = ctx
        try:
            yield ctx
        finally:
            ctx.snapshot_system()
            with self._lock:
                self._active.pop(task_id, None)
            self._persist(ctx)

    def _persist(self, ctx: TaskBudgetContext) -> None:
        try:
            conn = sqlite3.connect(self._db, timeout=5,
                                   check_same_thread=False)
            conn.execute(
                "INSERT OR REPLACE INTO resource_ledger "
                "(id,task_id,tokens_in,tokens_out,cost_usd,budget_usd,"
                " calls,cpu_pct,mem_mb,elapsed_s,over_budget,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], ctx.task_id,
                 ctx.tokens_in, ctx.tokens_out, ctx.cost_usd, ctx.budget_usd,
                 ctx.calls, ctx.cpu_pct, ctx.mem_mb, ctx.elapsed(),
                 int(ctx.budget_usd > 0 and ctx.cost_usd > ctx.budget_usd),
                 time.time()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("[ResourceTracker] persist failed: %s", e)

    def active(self) -> list[dict]:
        """Return current in-flight task contexts."""
        with self._lock:
            return [c.to_dict() for c in self._active.values()]

    def summary(self, task_id: str) -> Optional[dict]:
        """Return the most recent ledger row for task_id."""
        try:
            conn = sqlite3.connect(self._db, timeout=5,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM resource_ledger WHERE task_id=? "
                "ORDER BY ts DESC LIMIT 1", (task_id,)).fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception:
            return None

    def totals(self, since_hours: float = 24.0) -> dict:
        """Aggregate cost + token totals for the last N hours."""
        since = time.time() - since_hours * 3600
        try:
            conn = sqlite3.connect(self._db, timeout=5,
                                   check_same_thread=False)
            row = conn.execute(
                "SELECT SUM(tokens_in+tokens_out) AS tokens, "
                "SUM(cost_usd) AS cost, COUNT(*) AS runs, "
                "SUM(over_budget) AS over_budget_count "
                "FROM resource_ledger WHERE ts>=?", (since,)).fetchone()
            conn.close()
            return {
                "window_hours": since_hours,
                "tokens":       int(row[0] or 0),
                "cost_usd":     round(float(row[1] or 0), 4),
                "runs":         int(row[2] or 0),
                "over_budget":  int(row[3] or 0),
            }
        except Exception:
            return {}

    def recent(self, n: int = 20) -> list[dict]:
        try:
            conn = sqlite3.connect(self._db, timeout=5,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM resource_ledger ORDER BY ts DESC LIMIT ?",
                (n,)).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []


# Module-level singleton
_tracker_instance: Optional[ResourceTracker] = None
_tracker_lock = threading.Lock()

def get_tracker() -> ResourceTracker:
    global _tracker_instance
    with _tracker_lock:
        if _tracker_instance is None:
            _tracker_instance = ResourceTracker()
    return _tracker_instance
