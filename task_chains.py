"""
task_chains.py — Phase 17: Autonomous Task Chains
═══════════════════════════════════════════════════════════════════════

A "chain" is a long-running, persistent goal that one orchestrator.run()
isn't expected to finish in a single shot. The chain is split into
ordered "tasks" (sub-goals), each of which IS one orchestrator.run()
invocation. Tasks live in SQLite (via Memory), so a chain naturally
survives an orchestrator process restart — call resume(chain_id) and
the runner picks up at the next pending task.

Status flow (per task):
    pending → running → (completed | failed)

The runner never mutates state outside Memory: it just orchestrates
transitions and forwards observability events to the SSE pipeline via
the caller-supplied `emit_fn` (which Phase 16 plumbed through as
`orch._emit`). Because of that the runner is safe to instantiate fresh
per request — there is no shared in-memory queue to keep coherent.

Design notes (why it's structured this way):
• Decomposition reuses orchestrator.plan() — the SAME planner the rest
  of the system already uses, so chains and inline runs decompose the
  same way and the Phase 16 PlannerAgent role is inherited for free.
• Each task is run with `decompose=False` so the orchestrator treats
  the sub-goal as atomic (it has its OWN inner planner if needed). This
  prevents quadratic blow-up where a chain task spawns another full
  decomposition cascade.
• Two safety caps guard against infinite loops:
    - `max_attempts_per_task`: a task that keeps failing won't be
      retried forever — after N attempts it's marked failed terminally.
    - `max_tasks_per_chain`: a chain whose Planner keeps spawning
      new tasks won't grow without bound.
• All exceptions from orch.run() are swallowed into a structured
  failure record so a single crashing task can never tear down the
  whole chain — only that task is marked failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Any

# Status constants — must match memory.py's stored values exactly.
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"   # Phase 18 — adaptive-execution terminal status.

# Defaults are conservative; web_app can override per request.
DEFAULT_MAX_TASKS_PER_CHAIN = 32
DEFAULT_MAX_ATTEMPTS_PER_TASK = 2
DEFAULT_MAX_STEPS_PER_DRIVE = 16
# Phase 18 — Goal Intelligence defaults.
DEFAULT_MAX_REPLANS_PER_CHAIN = 2     # cap on dynamic replanning per chain
DEFAULT_SKIP_PRIORITY_THRESHOLD = 0.15
# Priority weights — must sum to 1.0. Tuned to favour explicit user
# importance (humans say "this matters") over historical signal.
PRIORITY_WEIGHT_IMPORTANCE = 0.50
PRIORITY_WEIGHT_HISTORY    = 0.30
PRIORITY_WEIGHT_DEPS_READY = 0.20


@dataclass
class Task:
    """Lightweight in-memory mirror of a `chain_tasks` row. The runner
    only constructs one of these to pass to callers — Memory itself
    returns plain dicts (shape-compatible with this dataclass).

    Phase 18 — adds priority / importance / depends_on / skipped_reason
    so the runner can reason about scheduling without re-querying."""
    id: int
    chain_id: int
    goal: str
    status: str = PENDING
    parent_id: Optional[int] = None
    attempts: int = 0
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    priority: float = 0.0
    importance: int = 5
    depends_on: list = field(default_factory=list)
    skipped_reason: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Task":
        # depends_on is stored as a CSV string in the DB; explode it.
        deps_raw = row.get("depends_on", "") or ""
        deps: list[int] = []
        for tok in deps_raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                deps.append(int(tok))
            except ValueError:
                continue
        return cls(
            id=row["id"],
            chain_id=row["chain_id"],
            goal=row["goal"],
            status=row.get("status", PENDING),
            parent_id=row.get("parent_id"),
            attempts=row.get("attempts", 0),
            last_error=row.get("last_error", "") or "",
            created_at=row.get("created_at", "") or "",
            updated_at=row.get("updated_at", "") or "",
            priority=float(row.get("priority", 0.0) or 0.0),
            importance=int(row.get("importance", 5) or 5),
            depends_on=deps,
            skipped_reason=row.get("skipped_reason", "") or "",
        )


class TaskChainRunner:
    """Drives one chain forward: decomposes the goal once at creation,
    then on each `run_next` pops the oldest pending task, runs it,
    persists the outcome, and emits observability events. Stateless
    between calls — all state lives in Memory."""

    def __init__(
        self,
        orchestrator: Any,
        memory: Any,
        emit_fn: Optional[Callable[[str, dict], None]] = None,
        max_tasks_per_chain: int = DEFAULT_MAX_TASKS_PER_CHAIN,
        max_attempts_per_task: int = DEFAULT_MAX_ATTEMPTS_PER_TASK,
        max_replans_per_chain: int = DEFAULT_MAX_REPLANS_PER_CHAIN,
        skip_priority_threshold: float = DEFAULT_SKIP_PRIORITY_THRESHOLD,
    ):
        self.orch = orchestrator
        self.memory = memory
        # If no emit_fn was passed, default to a no-op so internal calls
        # never need to None-check. Tests rely on this swappable hook.
        self._emit = emit_fn or (lambda kind, payload: None)
        self.max_tasks_per_chain = int(max_tasks_per_chain)
        self.max_attempts_per_task = int(max_attempts_per_task)
        # Phase 18 — Goal Intelligence knobs.
        self.max_replans_per_chain = int(max_replans_per_chain)
        self.skip_priority_threshold = float(skip_priority_threshold)

    # ────────────────────────────────────────────────────────────
    # CREATE
    # ────────────────────────────────────────────────────────────
    def create_chain(self, goal: str, importance: int = 5,
                     system_generated: bool = False,
                     confidence: float = 1.0,
                     auto_source: str = "") -> dict:
        """Decompose `goal` via the existing orchestrator planner and
        persist the chain + initial pending tasks. Returns
        {chain_id, tasks: [{id, goal, priority}, ...]}.

        Phase 18 — `importance` (1-10) is applied to every sub-task
        and folded into each task's priority score. The runner then
        re-scores on each pop, so the initial seeding here is just a
        starting point. On planner failure we degrade to a single-task
        chain — the chain still works, it just isn't pre-split.

        Phase 19 — `system_generated` / `confidence` / `auto_source`
        are forwarded to memory.create_chain so goal-engine-produced
        chains are persistently distinguishable. User-created chains
        keep the existing defaults; the runner does not change
        scheduling behaviour for system chains beyond the lower
        `importance` the caller passes (default 2)."""
        sub_goals: list[str] = []
        try:
            # orchestrator.plan(task, complexity) is the public API the
            # PlannerAgent (Phase 16) wraps. "complex" tells it to use
            # the LLM rather than a heuristic split.
            raw = self.orch.plan(goal, "complex")
            # plan() can return either a list[str] (preferred) or a
            # list of dicts depending on how the planner formatted; be
            # tolerant on both shapes.
            for item in (raw or []):
                if isinstance(item, str) and item.strip():
                    sub_goals.append(item.strip())
                elif isinstance(item, dict):
                    text = (item.get("task") or item.get("goal") or
                            item.get("description") or "").strip()
                    if text:
                        sub_goals.append(text)
        except Exception:
            # Planner crashed — fall through to single-task fallback.
            sub_goals = []

        if not sub_goals:
            sub_goals = [goal]

        # Cap right at creation time so a runaway planner never blows
        # past the per-chain ceiling later.
        if len(sub_goals) > self.max_tasks_per_chain:
            sub_goals = sub_goals[: self.max_tasks_per_chain]

        chain_id = self.memory.create_chain(
            goal,
            system_generated=system_generated,
            confidence=confidence,
            auto_source=auto_source,
        )
        if chain_id is None:
            return {"error": "create_chain failed", "chain_id": None,
                    "tasks": []}

        tasks_out = []
        # Seed priorities upfront so the very first pop respects the
        # ORDER BY priority DESC. We don't know historical success per
        # individual sub-goal yet, so this is just (importance + neutral
        # history + deps_ready=1.0). Recomputed on each pop anyway.
        seed_history = 0.5
        seed_priority = (
            PRIORITY_WEIGHT_IMPORTANCE * (importance / 10.0)
            + PRIORITY_WEIGHT_HISTORY  * seed_history
            + PRIORITY_WEIGHT_DEPS_READY * 1.0
        )
        for sg in sub_goals:
            tid = self.memory.enqueue_task(
                chain_id, sg,
                importance=importance,
                priority=seed_priority,
            )
            if tid is None:
                continue
            tasks_out.append({
                "id": tid, "goal": sg,
                "priority": round(seed_priority, 4),
                "importance": importance,
            })
            self._emit("task_created", {
                "chain_id": chain_id,
                "task_id": tid,
                "goal": sg[:200],
                "priority": round(seed_priority, 4),
                "importance": importance,
            })
        # Initial bulk priority emission so the UI gets one summary
        # event rather than N separate ones.
        self._emit("task_priority", {
            "chain_id": chain_id,
            "scored": [{"task_id": t["id"], "priority": t["priority"]}
                       for t in tasks_out],
            "reason": "initial seed",
        })
        return {"chain_id": chain_id, "tasks": tasks_out}

    # ────────────────────────────────────────────────────────────
    # PRIORITY / SCORING
    # ────────────────────────────────────────────────────────────
    def _historical_success_rate(self, goal: str) -> float:
        """Best-effort look-up of how often similar tasks have
        succeeded. Returns a value in [0, 1]; defaults to 0.5
        ("neutral, unknown") whenever the history layer is
        unavailable or yields no data — that's the right fallback
        for a brand-new goal we've never seen before."""
        try:
            # strategy_stats is the public surface for execution
            # history aggregation; it returns a per-strategy score
            # for the given task pattern. We collapse it into a
            # single weighted-mean success rate.
            stats = self.memory.strategy_stats(task=goal) or {}
            if not stats:
                return 0.5
            total_attempts = sum(s.get("attempts", 0)
                                 for s in stats.values())
            if total_attempts <= 0:
                return 0.5
            total_success = sum(s.get("successes", 0)
                                for s in stats.values())
            return max(0.0, min(1.0,
                                total_success / float(total_attempts)))
        except Exception:
            return 0.5

    def _compute_priority(self, task: Task,
                          terminal_ids: set[int]) -> float:
        """Weighted blend of importance, historical success rate, and
        dependency-readiness. Returns a value in [0, 1].

        Why these three? Importance is the human/planner signal of
        "this matters". History is the empirical signal of "we tend
        to succeed at this kind of work". Deps-ready is the gating
        signal — a task with unmet deps gets a 0 contribution there,
        pushing it well below the skip threshold (preventing it from
        being skipped purely for being temporarily blocked, since the
        dependency check inside next_pending_task gates it anyway —
        but also discouraging it relative to ready peers)."""
        importance_score = max(0.0, min(1.0, task.importance / 10.0))
        history_score    = self._historical_success_rate(task.goal)
        deps_ready_score = (1.0 if all(d in terminal_ids
                                       for d in task.depends_on)
                            else 0.0)
        return (
            PRIORITY_WEIGHT_IMPORTANCE * importance_score
            + PRIORITY_WEIGHT_HISTORY  * history_score
            + PRIORITY_WEIGHT_DEPS_READY * deps_ready_score
        )

    def recompute_priorities(self, chain_id: int) -> dict:
        """Re-score every PENDING task on a chain and persist. Useful
        when (a) a dependency just completed, or (b) the user wants
        to nudge a stale chain. Returns a {task_id: priority} map."""
        detail = self.memory.get_chain(chain_id)
        if not detail:
            return {}
        terminal_ids = {t["id"] for t in detail["tasks"]
                        if t["status"] in (COMPLETED, SKIPPED)}
        scored: dict[int, float] = {}
        for row in detail["tasks"]:
            if row["status"] != PENDING:
                continue
            task = Task.from_row(row)
            new_p = self._compute_priority(task, terminal_ids)
            self.memory.set_task_priority(task.id, new_p)
            scored[task.id] = round(new_p, 4)
        if scored:
            self._emit("task_priority", {
                "chain_id": chain_id,
                "scored": [{"task_id": tid, "priority": p}
                           for tid, p in scored.items()],
                "reason": "recompute",
            })
        return scored

    # ────────────────────────────────────────────────────────────
    # DRIVE
    # ────────────────────────────────────────────────────────────
    def run_next(self, chain_id: int) -> Optional[dict]:
        """Pop the next eligible task and run it. Returns
        {task_id, ok, reason, ...} on a real attempt, or None if the
        chain has nothing eligible (no pending tasks, or all pending
        tasks gated by unmet deps).

        Phase 18 self-management:
        • Priority recompute happens lazily on the first row before
          we commit to running it (deps may have changed).
        • If recomputed priority falls below `skip_priority_threshold`
          AND there are higher-priority alternatives, the task is
          skipped (terminal status='skipped', task_priority emitted).
        • If this is a retry (attempts > 0), the goal text is
          augmented with last_error so the agent learns from the
          previous attempt; task_retry is emitted.
        • On terminal failure with attempts == max_attempts the runner
          calls maybe_replan_after_failure to spawn alternative
          children (capped per chain)."""
        # next_pending_task already orders by priority DESC and
        # filters out tasks with unmet deps.
        row = self.memory.next_pending_task(chain_id)
        if row is None:
            return None
        task = Task.from_row(row)

        # ── Safety: chain-level fan-out cap. ──
        progress = self.memory.chain_progress(chain_id)
        if progress.get("total", 0) > self.max_tasks_per_chain:
            self.memory.mark_task_done(
                task.id, ok=False,
                reason=f"chain exceeded max_tasks="
                       f"{self.max_tasks_per_chain}")
            self._emit("task_completed", {
                "chain_id": chain_id, "task_id": task.id,
                "ok": False, "reason": "chain cap exceeded",
            })
            return {"task_id": task.id, "ok": False,
                    "reason": "chain cap exceeded"}

        # ── Phase 18: lazy priority recompute. ──
        # We re-score before deciding to run, so a task whose deps
        # just satisfied (and whose history numbers shifted under
        # other runs) gets the right score now.
        detail = self.memory.get_chain(chain_id) or {"tasks": []}
        terminal_ids = {t["id"] for t in detail["tasks"]
                        if t["status"] in (COMPLETED, SKIPPED)}
        new_pri = self._compute_priority(task, terminal_ids)
        if abs(new_pri - task.priority) > 1e-6:
            self.memory.set_task_priority(task.id, new_pri)
            task.priority = new_pri
        self._emit("task_priority", {
            "chain_id": chain_id, "task_id": task.id,
            "priority": round(new_pri, 4),
            "importance": task.importance,
            "reason": "pre-run recompute",
        })

        # ── Phase 18: adaptive skip. ──
        # Drop low-value tasks ONLY if there's a higher-priority
        # pending alternative — never skip the last remaining task,
        # even if it's low-value, because the user explicitly asked
        # for it.
        if new_pri < self.skip_priority_threshold:
            other_pending = [t for t in detail["tasks"]
                             if t["status"] == PENDING
                             and t["id"] != task.id
                             and float(t.get("priority", 0.0))
                                 >= self.skip_priority_threshold]
            if other_pending:
                skip_reason = (f"priority {new_pri:.3f} < threshold "
                               f"{self.skip_priority_threshold} with "
                               f"{len(other_pending)} better alternatives")
                self.memory.set_task_skipped(task.id, skip_reason)
                self._emit("task_priority", {
                    "chain_id": chain_id, "task_id": task.id,
                    "skipped": True, "priority": round(new_pri, 4),
                    "reason": skip_reason,
                })
                return {"task_id": task.id, "ok": False,
                        "skipped": True, "reason": skip_reason}

        # ── Per-task attempt cap. ──
        # Checked BEFORE the running-bump so an exhausted task is
        # marked failed terminally (and may trigger a replan below).
        if task.attempts >= self.max_attempts_per_task:
            self.memory.mark_task_done(
                task.id, ok=False,
                reason=f"attempts exhausted "
                       f"({self.max_attempts_per_task})")
            self._emit("task_completed", {
                "chain_id": chain_id, "task_id": task.id,
                "ok": False, "reason": "attempts exhausted",
            })
            # An exhausted task is a hard failure — try to replan.
            self.maybe_replan_after_failure(chain_id, task.id)
            return {"task_id": task.id, "ok": False,
                    "reason": "attempts exhausted"}

        # ── Race-safe transition pending → running. ──
        if not self.memory.mark_task_running(task.id):
            return {"task_id": task.id, "ok": False,
                    "reason": "lost race for task"}
        attempt_no = task.attempts + 1   # post-bump value

        # ── Phase 18: retry with reflection + critic feedback. ──
        # If this is a retry, prepend the failure context to the goal
        # so the agent learns from its previous attempt rather than
        # re-running the exact same prompt.
        run_goal = task.goal
        if task.attempts > 0 and task.last_error:
            run_goal = self._augment_goal_for_retry(
                task.goal, task.last_error)
            self._emit("task_retry", {
                "chain_id": chain_id, "task_id": task.id,
                "attempt": attempt_no,
                "previous_error": task.last_error[:200],
                "augmented_goal_preview": run_goal[:200],
            })

        self._emit("task_progress", {
            "chain_id": chain_id, "task_id": task.id,
            "status": RUNNING, "attempt": attempt_no,
            "priority": round(new_pri, 4),
            "goal": task.goal[:200],
        })

        # ── Execute (decompose=False — see class docstring). ──
        ok = False
        reason = ""
        try:
            result = self.orch.run(run_goal, decompose=False) or {}
            ok = bool(result.get("ok"))
            reason = (result.get("reason")
                      or result.get("summary")
                      or "").strip()
        except Exception as e:
            ok = False
            reason = f"crashed: {type(e).__name__}: {e}"

        # ── Phase 18: retry-aware terminal handling. ──
        # On success, mark done normally.
        # On failure with attempts left, reset to PENDING (preserving
        # last_error) so the next pop re-runs with augmented goal.
        # On failure with attempts exhausted, mark done as failed and
        # try to replan.
        if ok:
            self.memory.mark_task_done(task.id, ok=True,
                                       reason=reason[:500])
            self._emit("task_completed", {
                "chain_id": chain_id, "task_id": task.id,
                "ok": True, "reason": reason[:200],
                "attempt": attempt_no,
            })
        elif attempt_no < self.max_attempts_per_task:
            # Retryable failure — flip back to pending so the augmented
            # retry path actually runs on the next pop. We do NOT emit
            # task_completed here because the task isn't terminal yet;
            # we emit task_progress(status="pending_retry") so the UI
            # can render the bounce-back.
            #
            # If reset_task_for_retry returns False, another writer
            # already moved this row off `running` — most likely the
            # subprocess terminal hook handled it. Don't try to force
            # a status change; just emit a progress event so the UI
            # knows we noticed and let whoever owns the row finish.
            reset_ok = self.memory.reset_task_for_retry(
                task.id, last_error=reason[:500])
            self._emit("task_progress", {
                "chain_id": chain_id, "task_id": task.id,
                "status": ("pending_retry" if reset_ok
                           else "race_lost"),
                "attempt": attempt_no,
                "last_error": reason[:200],
            })
        else:
            # Exhausted retries — terminal failure + replan attempt.
            self.memory.mark_task_done(task.id, ok=False,
                                       reason=reason[:500])
            self._emit("task_completed", {
                "chain_id": chain_id, "task_id": task.id,
                "ok": False, "reason": reason[:200],
                "attempt": attempt_no,
            })
            self.maybe_replan_after_failure(chain_id, task.id)

        return {"task_id": task.id, "ok": ok, "reason": reason,
                "attempt": attempt_no,
                "priority": round(new_pri, 4)}

    # ────────────────────────────────────────────────────────────
    # RETRY / REPLAN HELPERS
    # ────────────────────────────────────────────────────────────
    @staticmethod
    def _augment_goal_for_retry(goal: str, last_error: str) -> str:
        """Prepend the previous-failure context to a goal so the agent
        treats this as a corrective attempt rather than a fresh run.
        Kept short to avoid blowing the agent's prompt budget."""
        err = (last_error or "").strip()[:300]
        if not err:
            return goal
        return (f"[Retry — the previous attempt failed with: {err}. "
                f"Try a different approach.]\n{goal}")

    def maybe_replan_after_failure(self, chain_id: int,
                                   task_id: int) -> Optional[dict]:
        """If this chain still has replan budget AND the failed task
        is genuinely terminal (status=failed), ask the orchestrator's
        replanner for alternative children and enqueue them as new
        tasks in the chain. Idempotent on a (chain, task) pair as
        long as the chain's replan_count budget hasn't moved past
        max_replans_per_chain."""
        # ── Phase 18 atomic idempotency + budget reservation ──
        # try_reserve_replan does the existing-children check, the
        # budget cap check, and the replan_count bump inside a single
        # SQLite IMMEDIATE transaction. This is the ONLY gate — two
        # concurrent callers (e.g. in-process runner + terminal hook)
        # cannot both pass and both spawn children.
        row = self.memory.get_task(task_id)
        if not row or row.get("status") != FAILED:
            # Task isn't in a terminally-failed state — nothing to do.
            return None
        ok_reserve, why, new_used = self.memory.try_reserve_replan(
            chain_id, task_id, self.max_replans_per_chain)
        if not ok_reserve:
            self._emit("task_replan", {
                "chain_id": chain_id, "task_id": task_id,
                "skipped": True, "reason": why,
            })
            return None
        failed_goal = row["goal"]
        last_error = row.get("last_error") or ""

        children: list[str] = []
        try:
            raw = self.orch.replan(failed_goal, last_error)
            for item in (raw or []):
                if isinstance(item, str) and item.strip():
                    children.append(item.strip())
                elif isinstance(item, dict):
                    text = (item.get("task") or item.get("goal")
                            or "").strip()
                    if text:
                        children.append(text)
        except Exception as e:
            self._emit("task_replan", {
                "chain_id": chain_id, "task_id": task_id,
                "skipped": True,
                "reason": f"replan crashed: {type(e).__name__}: {e}",
            })
            return None
        if not children:
            self._emit("task_replan", {
                "chain_id": chain_id, "task_id": task_id,
                "skipped": True,
                "reason": "replanner returned no alternatives",
            })
            return None

        # NOTE: budget was already bumped atomically inside
        # try_reserve_replan() above — do NOT bump again here, that
        # would double-consume the budget per replan.
        new_used = self.memory.chain_replan_count(chain_id)

        # Replan children inherit the failed task's importance + 1
        # (clamped to 10) — they're the "must succeed" recovery path.
        boosted = min(10, int(row.get("importance", 5) or 5) + 1)
        # Respect the chain-level fan-out cap when enqueueing.
        progress = self.memory.chain_progress(chain_id)
        budget = max(0, self.max_tasks_per_chain
                     - progress.get("total", 0))
        children = children[:max(0, min(3, budget))]

        enqueued: list[dict] = []
        for child_goal in children:
            tid = self.memory.enqueue_task(
                chain_id, child_goal,
                parent_id=task_id,
                importance=boosted,
            )
            if tid is None:
                continue
            enqueued.append({"id": tid, "goal": child_goal})
            self._emit("task_created", {
                "chain_id": chain_id, "task_id": tid,
                "goal": child_goal[:200],
                "importance": boosted,
                "via": "replan", "parent_id": task_id,
            })
        # Re-score the whole chain so the new children take their
        # place in the priority order alongside any other pending.
        self.recompute_priorities(chain_id)
        self._emit("task_replan", {
            "chain_id": chain_id, "task_id": task_id,
            "replan_count": new_used,
            "children_enqueued": len(enqueued),
            "children": [c["id"] for c in enqueued],
            "reason": f"failed task replanned into "
                      f"{len(enqueued)} alternative(s)",
        })
        return {"replan_count": new_used,
                "children": enqueued}

    def run_chain(self, chain_id: int,
                  max_steps: int = DEFAULT_MAX_STEPS_PER_DRIVE) -> dict:
        """Drive the chain forward up to `max_steps` task attempts (a
        belt-and-braces ceiling on top of the per-task / per-chain
        caps). Returns the final progress histogram."""
        steps = 0
        cap = max(1, min(int(max_steps), self.max_tasks_per_chain))
        while steps < cap:
            r = self.run_next(chain_id)
            if r is None:
                break
            steps += 1
        return self.memory.chain_progress(chain_id)

    def resume(self, chain_id: int,
               max_steps: int = DEFAULT_MAX_STEPS_PER_DRIVE) -> dict:
        """Pick up a chain after a crash / restart. Demotes any task
        stuck in 'running' (because the process died mid-task) back to
        'pending' so it gets re-attempted, then drives the chain."""
        n_reset = self.memory.reset_stuck_running(chain_id)
        if n_reset:
            self._emit("task_progress", {
                "chain_id": chain_id, "status": "resumed",
                "reset_running": n_reset,
            })
        return self.run_chain(chain_id, max_steps=max_steps)
