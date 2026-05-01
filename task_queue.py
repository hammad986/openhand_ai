"""
task_queue.py — Phase 35: Distributed Worker System
=====================================================
Priority queue + N worker threads, retry/backoff, watchdog, checkpoint.
"""
from __future__ import annotations
import heapq, json, logging, os, sqlite3, threading, time, traceback, uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import redis as _redis_mod
    _REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    _rc = _redis_mod.from_url(_REDIS_URL, socket_connect_timeout=2,
                               socket_timeout=2, decode_responses=True)
    _rc.ping()
    _REDIS_OK = True
except Exception:
    _rc = None
    _REDIS_OK = False

DEFAULT_WORKERS       = int(os.environ.get("WORKER_COUNT", "4"))
STUCK_TASK_TIMEOUT    = int(os.environ.get("STUCK_TIMEOUT_SECS", "120"))
HEARTBEAT_INTERVAL    = 5
RETRY_BASE_DELAY      = 2
MAX_QUEUE_SIZE        = 500

STATUS_QUEUED   = "queued"
STATUS_RUNNING  = "running"
STATUS_DONE     = "done"
STATUS_FAILED   = "failed"
STATUS_RETRYING = "retrying"
STATUS_KILLED   = "killed"

PRIORITY_CRITICAL = 0
PRIORITY_HIGH     = 1
PRIORITY_NORMAL   = 2
PRIORITY_LOW      = 3


@dataclass
class Task:
    id:          str
    name:        str
    fn:          Callable
    args:        tuple = field(default_factory=tuple)
    kwargs:      dict  = field(default_factory=dict)
    priority:    int   = PRIORITY_NORMAL
    max_retries: int   = 3
    budget_usd:  float = 0.0
    timeout_s:   int   = 300
    status:      str   = STATUS_QUEUED
    attempts:    int   = 0
    result:      Any   = None
    error:       str   = ""
    queued_at:   float = field(default_factory=time.time)
    started_at:  float = 0.0
    finished_at: float = 0.0
    heartbeat:   float = 0.0
    worker_id:   str   = ""
    checkpoint:  dict  = field(default_factory=dict)
    _seq:        int   = field(default=0)

    def __lt__(self, other: "Task") -> bool:
        return (self.priority, self._seq) < (other.priority, other._seq)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "priority":    self.priority,
            "status":      self.status,
            "attempts":    self.attempts,
            "error":       (self.error or "")[:300],
            "queued_at":   self.queued_at,
            "started_at":  self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s":   round(self.finished_at - self.started_at, 2)
                           if self.finished_at > self.started_at else
                           round(time.time() - self.started_at, 2)
                           if self.started_at else 0,
            "worker_id":   self.worker_id,
            "budget_usd":  self.budget_usd,
            "checkpoint":  self.checkpoint,
        }


_DDL = """
CREATE TABLE IF NOT EXISTS task_log (
    id TEXT PRIMARY KEY, name TEXT, priority INTEGER, status TEXT,
    attempts INTEGER, error TEXT, result_summary TEXT,
    queued_at REAL, started_at REAL, finished_at REAL,
    budget_usd REAL, worker_id TEXT, checkpoint TEXT
);
CREATE INDEX IF NOT EXISTS ix_tl_status ON task_log(status);
CREATE INDEX IF NOT EXISTS ix_tl_ts ON task_log(queued_at DESC);
"""


class TaskQueue:
    def __init__(self, n_workers: int = DEFAULT_WORKERS,
                 emit_fn: Optional[Callable] = None,
                 db_path: str = "./data/task_queue.db") -> None:
        self._heap:    list = []
        self._seq_ctr  = 0
        self._lock     = threading.Lock()
        self._has_work = threading.Event()
        self._stop     = threading.Event()
        self._tasks:   Dict[str, Task] = {}
        self._emit     = emit_fn or (lambda k, p: None)
        self._workers: List[threading.Thread] = []
        self._n_workers = n_workers
        self._db_path   = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()
        self._start_workers()
        self._start_watchdog()

    def _init_db(self):
        try:
            c = sqlite3.connect(self._db_path, check_same_thread=False)
            c.executescript(_DDL); c.commit(); c.close()
        except Exception: pass

    def _persist(self, task: Task):
        try:
            c = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            c.execute("INSERT OR REPLACE INTO task_log "
                "(id,name,priority,status,attempts,error,result_summary,"
                " queued_at,started_at,finished_at,budget_usd,worker_id,checkpoint)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, task.name, task.priority, task.status,
                 task.attempts, (task.error or "")[:300],
                 json.dumps(task.result, default=str)[:500]
                 if task.result is not None else "",
                 task.queued_at, task.started_at, task.finished_at,
                 task.budget_usd, task.worker_id,
                 json.dumps(task.checkpoint, default=str)[:1000]))
            c.commit(); c.close()
        except Exception: pass

    def submit(self, fn: Callable, *args, name: str = "",
               priority: int = PRIORITY_NORMAL, max_retries: int = 3,
               budget_usd: float = 0.0, timeout_s: int = 300,
               task_id: str = "", **kwargs) -> str:
        if len(self._heap) >= MAX_QUEUE_SIZE:
            raise RuntimeError(f"Queue full ({MAX_QUEUE_SIZE})")
        tid = task_id or uuid.uuid4().hex[:16]
        with self._lock:
            self._seq_ctr += 1
            task = Task(id=tid, name=name or fn.__name__,
                        fn=fn, args=args, kwargs=kwargs,
                        priority=priority, max_retries=max_retries,
                        budget_usd=budget_usd, timeout_s=timeout_s,
                        _seq=self._seq_ctr)
            self._tasks[tid] = task
            heapq.heappush(self._heap, task)
        self._has_work.set()
        self._persist(task)
        self._emit("task_queued", {"id": tid, "name": task.name, "priority": priority})
        if _REDIS_OK and _rc:
            try: _rc.lpush("openhand:task_queue",
                           json.dumps({"id": tid, "priority": priority}))
            except Exception: pass
        return tid

    def _start_workers(self):
        try:
            from hardware_monitor import get_hardware_monitor
            get_hardware_monitor().register_shutdown_hook(self.shutdown)
        except ImportError:
            pass

        for i in range(self._n_workers):
            t = threading.Thread(target=self._worker_loop, args=(i, f"worker-{i}"), daemon=True)
            t.start(); self._workers.append(t)

    def _worker_loop(self, worker_index: int, worker_id: str):
        while not self._stop.is_set():
            try:
                from hardware_monitor import get_hardware_monitor
                if get_hardware_monitor().status()["low_resource_mode"]:
                    if worker_index > 0:
                        # Yield if not the primary worker in low resource mode
                        time.sleep(2.0)
                        continue
            except ImportError:
                pass

            self._has_work.wait(timeout=2.0)
            task = self._dequeue()
            if task is None: self._has_work.clear(); continue
            self._run_task(task, worker_id)

    def _dequeue(self) -> Optional[Task]:
        with self._lock:
            return heapq.heappop(self._heap) if self._heap else None

    def _run_task(self, task: Task, worker_id: str):
        task.status = STATUS_RUNNING; task.started_at = time.time()
        task.heartbeat = time.time(); task.worker_id = worker_id
        task.attempts += 1
        self._persist(task)
        self._emit("task_started", {"id": task.id, "name": task.name,
                                    "worker": worker_id, "attempt": task.attempts})
        def _hb():
            while task.status == STATUS_RUNNING:
                task.heartbeat = time.time(); time.sleep(HEARTBEAT_INTERVAL)
        threading.Thread(target=_hb, daemon=True).start()
        try:
            task.result = task.fn(*task.args, **task.kwargs)
            task.status = STATUS_DONE; task.finished_at = time.time()
            self._emit("task_done", {"id": task.id, "name": task.name,
                "elapsed_s": round(task.finished_at - task.started_at, 2),
                "attempts": task.attempts})
        except Exception as e:
            task.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}"
            eff_retries = task.max_retries
            try:
                from hardware_monitor import get_hardware_monitor
                if get_hardware_monitor().status()["low_resource_mode"]:
                    eff_retries = min(1, eff_retries)
            except ImportError:
                pass

            if task.attempts < eff_retries:
                delay = RETRY_BASE_DELAY * (2 ** (task.attempts - 1))
                task.status = STATUS_RETRYING; task.finished_at = time.time()
                self._persist(task)
                self._emit("task_retry", {"id": task.id, "attempt": task.attempts,
                                          "delay_s": delay})
                time.sleep(delay)
                with self._lock: heapq.heappush(self._heap, task)
                self._has_work.set()
            else:
                task.status = STATUS_FAILED; task.finished_at = time.time()
                self._emit("task_failed", {"id": task.id, "name": task.name,
                    "error": task.error[:300], "attempts": task.attempts})
        finally:
            if task.status not in (STATUS_RETRYING,): self._persist(task)

    def _start_watchdog(self):
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def _watchdog_loop(self):
        while not self._stop.is_set():
            time.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            with self._lock:
                running = [t for t in self._tasks.values() if t.status == STATUS_RUNNING]
            for task in running:
                if (now - task.heartbeat) > STUCK_TASK_TIMEOUT:
                    task.status = STATUS_KILLED
                    task.error = f"Stuck: no heartbeat for {STUCK_TASK_TIMEOUT}s"
                    task.finished_at = now
                    self._persist(task)
                    self._emit("task_killed", {"id": task.id, "reason": "stuck"})

    def checkpoint(self, task_id: str, state: dict):
        with self._lock:
            t = self._tasks.get(task_id)
            if t: t.checkpoint = state
        self._emit("task_checkpoint", {"id": task_id})

    def restore_checkpoint(self, task_id: str) -> Optional[dict]:
        with self._lock:
            t = self._tasks.get(task_id)
            if t: return dict(t.checkpoint)
        try:
            c = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
            row = c.execute("SELECT checkpoint FROM task_log WHERE id=?",
                            (task_id,)).fetchone()
            c.close()
            return json.loads(row[0]) if row and row[0] else None
        except Exception: return None

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock: t = self._tasks.get(task_id)
        return t.to_dict() if t else None

    def queue_snapshot(self) -> dict:
        with self._lock: tasks = list(self._tasks.values())
        by_status: Dict[str, int] = {}
        for t in tasks: by_status[t.status] = by_status.get(t.status, 0) + 1
        queued  = sorted([t for t in tasks if t.status == STATUS_QUEUED],
                         key=lambda t: (t.priority, t._seq))
        running = [t for t in tasks if t.status == STATUS_RUNNING]
        recent  = sorted([t for t in tasks if t.status in
                          (STATUS_DONE, STATUS_FAILED, STATUS_KILLED)],
                         key=lambda t: t.finished_at, reverse=True)[:20]
        return {"by_status": by_status, "n_workers": self._n_workers,
                "redis": _REDIS_OK,
                "queued":  [t.to_dict() for t in queued[:20]],
                "running": [t.to_dict() for t in running],
                "recent":  [t.to_dict() for t in recent]}

    def worker_status(self) -> list:
        return [{"id": f"worker-{i}", "alive": w.is_alive()}
                for i, w in enumerate(self._workers)]

    def shutdown(self): self._stop.set(); self._has_work.set()


_queue_instance: Optional[TaskQueue] = None
_queue_lock = threading.Lock()

def get_queue(emit_fn: Optional[Callable] = None) -> TaskQueue:
    global _queue_instance
    with _queue_lock:
        if _queue_instance is None:
            _queue_instance = TaskQueue(emit_fn=emit_fn)
    return _queue_instance
