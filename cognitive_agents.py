"""
cognitive_agents.py — Phase 34 Semi-AGI Layer
==============================================
Extends agents.py with:
  • DebuggerAgent      — autonomous failure analysis + patch suggestion
  • MessageBus         — typed pub/sub inter-agent communication
  • TaskGeneralizationEngine — converts specific tasks → reusable patterns
  • CognitiveAgentTeam — full 5-agent squad with pattern memory
  • ParallelStrategyEngine — runs multiple strategies, picks winner

Thread-safe. All agents communicate exclusively through MessageBus.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents import (AgentTeam, SharedContext, _BaseAgent,
                    make_message, PlannerAgent, ExecutorAgent,
                    CriticAgent, MemoryAgent)

logger = logging.getLogger(__name__)


# ── 1. MessageBus ─────────────────────────────────────────────────────────────

class MessageBus:
    """Typed pub/sub bus for inter-agent communication.

    Agents subscribe to topics (role names or '*').
    Messages are delivered synchronously to all subscribers in the
    order they subscribed.  All operations are thread-safe.
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # topic → list of (subscriber_id, callback)
        self._subs: Dict[str, list] = defaultdict(list)
        self._history: list[dict] = []
        self._MAX_HISTORY = 512

    def subscribe(self, topic: str, cb: Callable[[dict], None],
                  subscriber_id: str = "") -> str:
        sid = subscriber_id or uuid.uuid4().hex[:8]
        with self._lock:
            self._subs[topic].append((sid, cb))
        return sid

    def publish(self, topic: str, msg: dict) -> None:
        self._store(topic, msg)
        with self._lock:
            cbs = list(self._subs.get(topic, []))
            wildcards = list(self._subs.get("*", []))
        for _, cb in cbs + wildcards:
            try:
                cb(msg)
            except Exception as e:
                logger.debug("[MessageBus] callback error: %s", e)

    def _store(self, topic: str, msg: dict) -> None:
        entry = {"topic": topic, "ts": time.time(), **msg}
        with self._lock:
            self._history.append(entry)
            if len(self._history) > self._MAX_HISTORY:
                self._history.pop(0)

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._history[-n:])

    def drain(self) -> list[dict]:
        with self._lock:
            msgs = list(self._history)
            self._history.clear()
        return msgs


# ── 2. DebuggerAgent ─────────────────────────────────────────────────────────

class DebuggerAgent(_BaseAgent):
    """Phase 34 — autonomous failure analysis + patch suggestions.

    Triggered by CriticAgent when verdict.ok == False.
    Steps:
      1. Classify error category (matches terminal_backend patterns)
      2. Query memory for past fixes in this category
      3. Ask LLM for a concrete 1-step remediation patch
      4. Emit 'debug_patch' event and post patch to MessageBus
    """
    role = "debugger"

    _ERROR_CATS = {
        "import":     r"ModuleNotFoundError|No module named|ImportError",
        "syntax":     r"SyntaxError|IndentationError",
        "permission": r"PermissionError|Access is denied|Permission denied",
        "file":       r"FileNotFoundError|No such file",
        "connection": r"ConnectionRefusedError|Connection refused",
        "port":       r"Port \d+ already in use|Address already in use",
        "assertion":  r"AssertionError",
        "memory":     r"MemoryError|Killed",
        "timeout":    r"TimeoutError|timed out|Timeout",
        "runtime":    r"Traceback|Error:",
    }

    def diagnose(self,
                 ctx: SharedContext,
                 subtask: str,
                 exec_result: dict,
                 verdict: dict,
                 bus: Optional["MessageBus"] = None) -> dict:
        """Analyse a failure and produce a structured debug patch."""
        error_text = (
            (exec_result.get("crashed") or "") + " " +
            (exec_result.get("result_str") or "") + " " +
            verdict.get("reason", "")
        )
        category = self._classify(error_text)

        # Check memory for past fixes
        known_fix = ""
        try:
            known_fix = self.o.memory.find_fix_for_error(category) or ""
        except Exception:
            pass

        # Ask LLM for a concrete patch (cheap — strict token cap)
        patch = self._llm_patch(subtask, error_text, category, known_fix)

        result = {
            "subtask":   subtask[:160],
            "category":  category,
            "known_fix": known_fix[:200] if known_fix else None,
            "patch":     patch[:400],
            "error_excerpt": error_text[:300],
        }
        self._announce(ctx, "debug_patch", result)
        if bus:
            bus.publish("debugger", make_message(
                self.role, "debug_patch", result,
                to="executor", correlation_id=ctx.task[:40]))
        return result

    def _classify(self, text: str) -> str:
        for cat, pattern in self._ERROR_CATS.items():
            if re.search(pattern, text, re.I):
                return cat
        return "runtime"

    def _llm_patch(self, subtask: str, error: str,
                   category: str, known_fix: str) -> str:
        try:
            hints = f"\nKnown fix from memory: {known_fix}" if known_fix else ""
            prompt = (
                f"Sub-task: {subtask[:300]}\n"
                f"Error category: {category}\n"
                f"Error output: {error[:400]}{hints}\n\n"
                "Give ONE concrete shell command or code line to fix this. "
                "Reply with ONLY the fix, no explanation."
            )
            raw = self.o.router.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=120)
            return (raw or "").strip()
        except Exception as e:
            return f"# Debugger LLM unavailable: {e}"


# ── 3. TaskGeneralizationEngine ──────────────────────────────────────────────

# Pattern library: (regex → pattern_name, description_template)
_TASK_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"index.*error|out of bounds|list.*index",  re.I),
     "array_boundary_bug",
     "Array/list index boundary error — add bounds check before access"),
    (re.compile(r"import|module not found|install",          re.I),
     "missing_dependency",
     "Missing Python dependency — pip install required package"),
    (re.compile(r"connect|socket|port|refused",              re.I),
     "network_connectivity",
     "Network connectivity issue — check server is running and port is correct"),
    (re.compile(r"permission|access denied|chmod",           re.I),
     "permission_error",
     "File/OS permission error — check ownership or run with elevated rights"),
    (re.compile(r"login|auth|token|credential|password",     re.I),
     "authentication_flow",
     "Authentication/authorization — implement or validate credential flow"),
    (re.compile(r"database|sql|query|table|row",             re.I),
     "database_operation",
     "Database read/write/schema operation"),
    (re.compile(r"api|endpoint|rest|http|request|response",  re.I),
     "api_integration",
     "REST API integration — define endpoint, auth, and payload schema"),
    (re.compile(r"test|assert|pytest|unittest",              re.I),
     "test_authoring",
     "Write or fix automated tests for a module"),
    (re.compile(r"docker|container|image|deploy",            re.I),
     "containerization",
     "Container/deployment operation — Dockerfile or docker-compose"),
    (re.compile(r"build|compile|webpack|npm|pip install",    re.I),
     "build_system",
     "Build/package system operation"),
    (re.compile(r"refactor|rename|restructure|clean",        re.I),
     "code_refactor",
     "Code quality / refactoring task"),
    (re.compile(r"parse|json|xml|yaml|csv|format",           re.I),
     "data_parsing",
     "Data format parsing/serialization"),
    (re.compile(r"ui|frontend|css|html|react|vue",           re.I),
     "frontend_dev",
     "Frontend/UI implementation task"),
    (re.compile(r"async|await|concurrent|thread|queue",      re.I),
     "concurrency",
     "Async/concurrent programming task"),
    (re.compile(r"memory|leak|gc|garbage|oom",               re.I),
     "memory_management",
     "Memory management or leak investigation"),
]


class TaskGeneralizationEngine:
    """Convert specific tasks → named generalized patterns for future reuse.

    Patterns are stored in the VectorStore `reflections` collection
    so the MemoryAgent can surface them on semantically similar tasks.
    """

    def __init__(self, vector_store, emit_fn: Callable) -> None:
        self._vs = vector_store
        self._emit = emit_fn
        self._cache: Dict[str, str] = {}   # task_hash → pattern_name

    def generalize(self, task: str, success: bool,
                   strategy: str = "") -> dict:
        """Match task to a pattern and persist it."""
        pattern = self._match(task)
        key = f"gen_{uuid.uuid4().hex[:8]}"

        doc = (
            f"PATTERN: {pattern['name']} | "
            f"TASK: {task[:200]} | "
            f"SUCCESS: {success} | "
            f"STRATEGY: {strategy}"
        )
        meta = {
            "pattern":  pattern["name"],
            "success":  success,
            "strategy": strategy,
            "ts":       time.time(),
        }
        if self._vs:
            try:
                self._vs.add("reflections", key, doc, meta)
            except Exception:
                pass

        result = {**pattern, "task_snippet": task[:120],
                  "success": success, "strategy": strategy}
        self._emit("generalize", result)
        return result

    def recall_pattern(self, task: str, k: int = 3) -> list[dict]:
        """Retrieve the most relevant past patterns for this task."""
        if not self._vs:
            return []
        try:
            hits = self._vs.query("reflections", task, k=k)
            return [{"doc": h["document"], "dist": h["distance"],
                     "pattern": (h.get("metadata") or {}).get("pattern", "?")}
                    for h in hits]
        except Exception:
            return []

    @staticmethod
    def _match(task: str) -> dict:
        for rx, name, desc in _TASK_PATTERNS:
            if rx.search(task):
                return {"name": name, "description": desc}
        return {"name": "general_coding", "description": "General software development task"}


# ── 4. ParallelStrategyEngine ────────────────────────────────────────────────

@dataclass
class StrategyResult:
    strategy: str
    ok: bool
    exec_result: dict
    verdict: dict
    elapsed: float
    score: float = 0.0


class ParallelStrategyEngine:
    """Run up to `max_parallel` strategies concurrently; pick the winner.

    Winner = first successful result, or highest-scored failure.
    Scoring: success=1.0, partial ok_steps bonus, speed bonus.
    """
    STRATEGIES = ["direct", "step_by_step", "alternate"]

    def __init__(self, max_parallel: int = 3,
                 timeout: float = 90.0) -> None:
        self.max_parallel = max_parallel
        self.timeout = timeout

    def race(self,
             subtask: str,
             execute_fn: Callable[[str, str], dict],
             verify_fn:  Callable[[str, dict], dict],
             emit_fn:    Callable[[str, dict], None]) -> StrategyResult:
        """Run all strategies in parallel and return the best result.

        `execute_fn(subtask, strategy) → exec_result`
        `verify_fn(subtask, exec_result) → verdict`
        """
        try:
            from evolution_engine import get_evolution_engine
            strategies = get_evolution_engine().get_ranked_strategies(self.max_parallel)
        except ImportError:
            strategies = self.STRATEGIES[:self.max_parallel]
        try:
            from hardware_monitor import get_hardware_monitor
            if get_hardware_monitor().status()["low_resource_mode"]:
                strategies = ["direct"]  # Force single strategy to save RAM/CPU
        except ImportError:
            pass

        emit_fn("parallel_start", {
            "subtask": subtask[:160],
            "strategies": strategies,
        })

        results: list[StrategyResult] = []
        lock = threading.Lock()

        def _run(strat: str) -> StrategyResult:
            t0 = time.time()
            try:
                er = execute_fn(subtask, strat)
                vd = verify_fn(subtask, er)
            except Exception as exc:
                er = {"crashed": str(exc)}
                vd = {"ok": False, "reason": str(exc)}
            elapsed = time.time() - t0
            sr = StrategyResult(
                strategy=strat, ok=bool(vd.get("ok")),
                exec_result=er, verdict=vd, elapsed=elapsed)
            sr.score = self._score(sr)
            try:
                from evolution_engine import get_evolution_engine
                get_evolution_engine().record_strategy_result(strat, sr.ok, sr.score)
            except ImportError:
                pass
            return sr

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            futures = {pool.submit(_run, s): s for s in strategies}
            for fut in as_completed(futures, timeout=self.timeout):
                try:
                    sr = fut.result()
                    with lock:
                        results.append(sr)
                        emit_fn("parallel_result", {
                            "strategy": sr.strategy,
                            "ok": sr.ok,
                            "score": round(sr.score, 3),
                            "elapsed": round(sr.elapsed, 2),
                        })
                        # Short-circuit on first success
                        if sr.ok:
                            for f2 in futures:
                                f2.cancel()
                except Exception:
                    pass

        if not results:
            return StrategyResult("direct", False, {}, {"ok": False,
                                  "reason": "all strategies failed"}, 0.0)

        winner = max(results, key=lambda r: (r.ok, r.score))
        emit_fn("parallel_winner", {
            "subtask":  subtask[:160],
            "winner":   winner.strategy,
            "ok":       winner.ok,
            "score":    round(winner.score, 3),
            "n_ran":    len(results),
            "compared": [{"s": r.strategy, "ok": r.ok,
                          "score": round(r.score, 3)} for r in results],
        })
        return winner

    @staticmethod
    def _score(sr: StrategyResult) -> float:
        base = 1.0 if sr.ok else 0.0
        steps = sr.exec_result.get("step_records") or []
        ok_s  = sum(1 for s in steps
                    if s.get("kind") == "step_result" and s.get("success"))
        tot   = max(len([s for s in steps if s.get("kind") == "step_result"]), 1)
        step_bonus = (ok_s / tot) * 0.3
        speed_bonus = max(0.0, 0.1 - sr.elapsed / 1000.0)
        return round(min(1.0, base + step_bonus + speed_bonus), 3)


# ── 5. CognitiveAgentTeam ────────────────────────────────────────────────────

class CognitiveAgentTeam:
    """Phase 34 full 5-agent squad with MessageBus and generalizer.

    Extends the existing AgentTeam with:
      • DebuggerAgent
      • MessageBus
      • TaskGeneralizationEngine
      • ParallelStrategyEngine
    """

    def __init__(self,
                 orchestrator,
                 emit_fn: Callable[[str, dict], None],
                 vector_store=None) -> None:
        # Original 4-agent team
        self._base = AgentTeam(orchestrator, emit_fn)

        # Convenience re-exports
        self.planner  = self._base.planner
        self.executor = self._base.executor
        self.critic   = self._base.critic
        self.memory   = self._base.memory

        # Phase 34 additions
        self.debugger = DebuggerAgent(orchestrator, emit_fn)
        self.bus      = MessageBus()
        self.generalizer = TaskGeneralizationEngine(vector_store, emit_fn)
        self.parallel = ParallelStrategyEngine()

        # Wire message logging to emit_fn
        self.bus.subscribe("*", lambda m: emit_fn("bus_message", m), "logger")

    # ── convenience passthroughs ──────────────────────────────────────────────
    def all_agents(self):
        return (self.planner, self.executor, self.critic,
                self.memory, self.debugger)

    def broadcast(self, from_role: str, to: str, kind: str,
                  payload: dict, ctx: SharedContext) -> None:
        """Post a message to the bus AND the shared context."""
        msg = make_message(from_role, kind, payload, to=to)
        ctx.post(msg)
        self.bus.publish(to if to != "*" else from_role, msg)
