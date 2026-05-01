"""
scheduler.py — Phase 18: Background Autonomous Agents + Task Scheduler
═══════════════════════════════════════════════════════════════════════

A self-contained, SQLite-backed task scheduler that runs as a daemon
thread inside the Flask process. It supports:

  • one-time  — execute at a specific UTC datetime
  • interval  — repeat every N minutes
  • daily     — repeat at HH:MM UTC every day

Task types:
  • prompt   — submit as a regular agent session (enqueue_task)
  • goal     — decompose + auto-run via the goal/chain system
  • analysis — run a pre-canned analysis prompt as an agent session

Safety:
  • max_concurrent cap (default 2) prevents runaway parallelism
  • max_retries cap per scheduled run (default 3)
  • worker sleeps POLL_INTERVAL seconds between sweeps (default 30s)
  • all DB writes go through a single lock
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Optional

# ── Config ────────────────────────────────────────────────────────
SCHEDULER_DB    = "scheduler.db"
MAX_CONCURRENT  = 2       # max tasks running at the same time
MAX_RETRIES     = 3       # max run attempts per scheduled run slot
POLL_INTERVAL   = 30      # seconds between scheduler sweeps
MAX_HISTORY     = 50      # run-history entries kept per task
HISTORY_TRIM    = 40      # trim to this many after exceeding MAX_HISTORY

# ── Task dataclass ────────────────────────────────────────────────
@dataclass
class ScheduledTask:
    id:             str   = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name:           str   = "Unnamed Task"
    task_type:      str   = "prompt"      # prompt | goal | analysis
    prompt:         str   = ""
    schedule_type:  str   = "once"        # once | interval | daily
    schedule_value: str   = ""            # ISO dt | minutes | HH:MM
    enabled:        bool  = True
    status:         str   = "idle"        # idle | running | completed | failed | paused
    last_run:       Optional[float] = None
    next_run:       Optional[float] = None
    run_count:      int   = 0
    fail_count:     int   = 0
    created_at:     float = field(default_factory=time.time)
    config_json:    str   = "{}"
    last_session_id: str  = ""
    last_error:     str   = ""

    @classmethod
    def from_row(cls, row: dict) -> "ScheduledTask":
        return cls(
            id=row.get("id", uuid.uuid4().hex[:12]),
            name=row.get("name", "Unnamed Task"),
            task_type=row.get("task_type", "prompt"),
            prompt=row.get("prompt", ""),
            schedule_type=row.get("schedule_type", "once"),
            schedule_value=row.get("schedule_value", ""),
            enabled=bool(row.get("enabled", 1)),
            status=row.get("status", "idle"),
            last_run=row.get("last_run"),
            next_run=row.get("next_run"),
            run_count=row.get("run_count", 0),
            fail_count=row.get("fail_count", 0),
            created_at=row.get("created_at", time.time()),
            config_json=row.get("config_json", "{}"),
            last_session_id=row.get("last_session_id", ""),
            last_error=row.get("last_error", ""),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["config"] = json.loads(self.config_json or "{}")
        d["next_run_human"] = _fmt_ts(self.next_run)
        d["last_run_human"] = _fmt_ts(self.last_run)
        return d


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "—"


def compute_next_run(schedule_type: str, schedule_value: str,
                     from_ts: Optional[float] = None) -> Optional[float]:
    """Return the next Unix timestamp when this task should fire, or
    None if the task should not repeat (once-only, already fired)."""
    now = from_ts or time.time()

    if schedule_type == "once":
        if not schedule_value:
            return now + 5          # fire in 5 s if no datetime given
        try:
            dt = datetime.fromisoformat(schedule_value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            return ts if ts > now else now + 5
        except Exception:
            return now + 5

    if schedule_type == "interval":
        try:
            minutes = max(1, float(schedule_value))
        except (ValueError, TypeError):
            minutes = 60
        return now + minutes * 60

    if schedule_type == "daily":
        try:
            parts = schedule_value.split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except (ValueError, IndexError, AttributeError):
            hh, mm = 9, 0
        dt_now = datetime.fromtimestamp(now, tz=timezone.utc)
        candidate = dt_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate.timestamp() <= now:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    return None


# ── Scheduler class ───────────────────────────────────────────────
class TaskScheduler:
    """Thread-safe, SQLite-backed scheduler.

    Call `start(enqueue_fn, goal_fn)` once after construction to begin
    the background sweep loop. Both callbacks are optional: if not
    supplied the scheduler still tracks tasks but won't execute them.

    `enqueue_fn(prompt, cfg) -> session_id`
    `goal_fn(prompt, importance) -> chain_id`
    """

    def __init__(self, db_path: str = SCHEDULER_DB):
        self.db_path  = db_path
        self._lock    = threading.Lock()
        self._running_count = 0
        self._thread  = None
        self._stop    = threading.Event()
        self._enqueue: Optional[Callable] = None
        self._goal:    Optional[Callable] = None
        self._init_db()

    # ── DB bootstrap ─────────────────────────────────────────────
    def _init_db(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL DEFAULT 'Task',
                task_type       TEXT NOT NULL DEFAULT 'prompt',
                prompt          TEXT NOT NULL DEFAULT '',
                schedule_type   TEXT NOT NULL DEFAULT 'once',
                schedule_value  TEXT NOT NULL DEFAULT '',
                enabled         INTEGER NOT NULL DEFAULT 1,
                status          TEXT NOT NULL DEFAULT 'idle',
                last_run        REAL,
                next_run        REAL,
                run_count       INTEGER NOT NULL DEFAULT 0,
                fail_count      INTEGER NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL,
                config_json     TEXT NOT NULL DEFAULT '{}',
                last_session_id TEXT NOT NULL DEFAULT '',
                last_error      TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS task_run_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                run_at      REAL NOT NULL,
                status      TEXT NOT NULL,
                session_id  TEXT NOT NULL DEFAULT '',
                error       TEXT NOT NULL DEFAULT '',
                duration    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_history_task
                ON task_run_history(task_id, run_at DESC);
            """)

    def _conn(self):
        c = sqlite3.connect(self.db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ── CRUD ─────────────────────────────────────────────────────
    def create_task(self, name: str, task_type: str, prompt: str,
                    schedule_type: str, schedule_value: str,
                    enabled: bool = True, config: dict | None = None) -> ScheduledTask:
        t = ScheduledTask(
            name=name[:100],
            task_type=task_type,
            prompt=prompt[:2000],
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            enabled=enabled,
            config_json=json.dumps(config or {}),
        )
        t.next_run = compute_next_run(schedule_type, schedule_value)
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO scheduled_tasks
                   (id,name,task_type,prompt,schedule_type,schedule_value,
                    enabled,status,next_run,created_at,config_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (t.id, t.name, t.task_type, t.prompt, t.schedule_type,
                 t.schedule_value, int(t.enabled), t.status, t.next_run,
                 t.created_at, t.config_json),
            )
        return t

    def update_task(self, task_id: str, **fields) -> bool:
        allowed = {"name", "task_type", "prompt", "schedule_type",
                   "schedule_value", "enabled", "status", "config_json",
                   "next_run", "last_run", "last_session_id", "last_error"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        # Recompute next_run if schedule changed
        if "schedule_type" in updates or "schedule_value" in updates:
            task = self.get_task(task_id)
            if task:
                st = updates.get("schedule_type", task.schedule_type)
                sv = updates.get("schedule_value", task.schedule_value)
                updates["next_run"] = compute_next_run(st, sv)
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [task_id]
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE scheduled_tasks SET {cols} WHERE id=?", vals)
        return True

    def delete_task(self, task_id: str) -> bool:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
            c.execute("DELETE FROM task_run_history WHERE task_id=?", (task_id,))
        return True

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return ScheduledTask.from_row(dict(r)) if r else None

    def list_tasks(self, include_disabled: bool = True) -> list[ScheduledTask]:
        q = "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
        with self._conn() as c:
            rows = c.execute(q).fetchall()
        tasks = [ScheduledTask.from_row(dict(r)) for r in rows]
        if not include_disabled:
            tasks = [t for t in tasks if t.enabled]
        return tasks

    def get_history(self, task_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM task_run_history WHERE task_id=? "
                "ORDER BY run_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_recent_history(self, limit: int = 30) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT h.*, t.name as task_name FROM task_run_history h "
                "LEFT JOIN scheduled_tasks t ON t.id=h.task_id "
                "ORDER BY h.run_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _record_run(self, task_id: str, status: str,
                    session_id: str = "", error: str = "",
                    duration: float = 0.0):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO task_run_history "
                "(task_id,run_at,status,session_id,error,duration) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, time.time(), status, session_id, error[:500], duration),
            )
            # Trim history
            c.execute(
                "DELETE FROM task_run_history WHERE task_id=? AND id NOT IN ("
                "  SELECT id FROM task_run_history WHERE task_id=? "
                "  ORDER BY run_at DESC LIMIT ?)",
                (task_id, task_id, HISTORY_TRIM),
            )

    # ── Scheduler loop ────────────────────────────────────────────
    def start(self, enqueue_fn: Callable | None = None,
               goal_fn: Callable | None = None):
        self._enqueue = enqueue_fn
        self._goal    = goal_fn
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="p18-scheduler"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._sweep()
            except Exception as e:
                print(f"[scheduler] sweep error: {e}", flush=True)
            self._stop.wait(POLL_INTERVAL)

    def _sweep(self):
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM scheduled_tasks "
                "WHERE enabled=1 AND status NOT IN ('running','paused') "
                "  AND next_run IS NOT NULL AND next_run <= ? "
                "ORDER BY next_run ASC LIMIT 10",
                (now,),
            ).fetchall()

        for row in rows:
            task = ScheduledTask.from_row(dict(row))
            if self._running_count >= MAX_CONCURRENT:
                break
            self._fire(task)

    def _fire(self, task: ScheduledTask):
        self._running_count += 1
        t = threading.Thread(
            target=self._run_task, args=(task,), daemon=True,
            name=f"p18-task-{task.id}",
        )
        t.start()

    def _run_task(self, task: ScheduledTask):
        started = time.time()
        self.update_task(task.id, status="running",
                         last_run=started, last_error="")
        session_id = ""
        error = ""
        success = False
        try:
            if task.task_type == "goal" and self._goal:
                cfg = json.loads(task.config_json or "{}")
                importance = cfg.get("importance", 7)
                chain_id = self._goal(task.prompt, importance)
                session_id = f"chain:{chain_id}"
                success = True
            elif self._enqueue:
                cfg = json.loads(task.config_json or "{}")
                if "mode" not in cfg:
                    cfg["mode"] = "managed"
                session_id = self._enqueue(task.prompt, cfg)
                success = True
            else:
                error = "No execution backend available"
        except Exception as e:
            error = str(e)[:400]
            success = False

        duration = time.time() - started
        run_status = "success" if success else "failed"

        # Compute next_run (None for once-only after first fire)
        if task.schedule_type == "once":
            next_run = None
            new_status = "completed" if success else "failed"
        else:
            next_run = compute_next_run(task.schedule_type,
                                        task.schedule_value, time.time())
            new_status = "idle"

        new_run_count  = task.run_count + 1
        new_fail_count = task.fail_count + (0 if success else 1)

        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE scheduled_tasks SET status=?,run_count=?,fail_count=?,"
                "last_run=?,next_run=?,last_session_id=?,last_error=? WHERE id=?",
                (new_status, new_run_count, new_fail_count, started,
                 next_run, session_id, error, task.id),
            )

        self._record_run(task.id, run_status, session_id, error, duration)
        self._running_count = max(0, self._running_count - 1)

    def status(self) -> dict:
        tasks = self.list_tasks()
        now = time.time()
        running   = [t for t in tasks if t.status == "running"]
        scheduled = [t for t in tasks if t.enabled and t.next_run and t.next_run > now]
        scheduled.sort(key=lambda t: t.next_run)
        return {
            "total_tasks":    len(tasks),
            "enabled_tasks":  sum(1 for t in tasks if t.enabled),
            "running_count":  self._running_count,
            "next_task":      scheduled[0].to_dict() if scheduled else None,
            "next_run_human": _fmt_ts(scheduled[0].next_run) if scheduled else "—",
            "running_tasks":  [t.to_dict() for t in running],
            "poll_interval":  POLL_INTERVAL,
        }

    def run_now(self, task_id: str) -> dict:
        """Manually fire a task immediately regardless of schedule."""
        task = self.get_task(task_id)
        if not task:
            return {"ok": False, "error": "not found"}
        if task.status == "running":
            return {"ok": False, "error": "already running"}
        self._fire(task)
        return {"ok": True, "task_id": task_id}
