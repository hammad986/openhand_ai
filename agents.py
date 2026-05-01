"""
agents.py - Phase 16: Multi-Agent Collaboration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Role-based facade over the existing orchestrator. The four roles map
1:1 onto methods that already live on `Orchestrator`:

  ┌──────────────┬─────────────────────────────────────┐
  │   Role       │  Underlying orchestrator method     │
  ├──────────────┼─────────────────────────────────────┤
  │ PlannerAgent │  Orchestrator.plan()                │
  │ ExecutorAgent│  Orchestrator.execute()             │
  │ CriticAgent  │  Orchestrator.verify() + suggestion │
  │ MemoryAgent  │  Memory.semantic_recall +           │
  │              │  Memory.recall_tool_stats           │
  └──────────────┴─────────────────────────────────────┘

The agents do NOT re-implement any logic. They are thin facades that
exist to (a) give the system a formal role vocabulary, (b) emit
distinct `agent_*` events for observability, and (c) exchange
structured JSON messages through a SharedContext object so future
work has a clean seam to wedge cross-role coordination into without
having to modify the orchestrator's run() loop again.

Constraint honoured: agent.py / router.py / tools.py / main.py are
NOT touched. This module is additive.
"""

from __future__ import annotations
import time, uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ──────────────────────────────────────────────────────────────────────
# 1. Structured-message envelope
# ──────────────────────────────────────────────────────────────────────
def make_message(role: str,
                 kind: str,
                 payload: dict,
                 *,
                 to: str = "*",
                 correlation_id: Optional[str] = None) -> dict:
    """Build a JSON-serialisable envelope for inter-agent comms.

    The shape is intentionally minimal so it can be logged, replayed
    or persisted as-is. `correlation_id` lets request/response pairs
    be reconstructed when a sub-task fans out.
    """
    return {
        "id":             uuid.uuid4().hex[:12],
        "ts":             round(time.time(), 3),
        "from":           role,
        "to":             to,
        "kind":           kind,
        "correlation_id": correlation_id,
        "payload":        payload,
    }


# ──────────────────────────────────────────────────────────────────────
# 2. SharedContext — passed by reference through one run()
# ──────────────────────────────────────────────────────────────────────
@dataclass
class SharedContext:
    """Per-task scratchpad shared across all four roles for one run().

    Fields are deliberately plain dicts/lists so the whole context is
    JSON-serialisable for tests, snapshots and future remote agents.
    """
    task:        str
    complexity:  str            = "moderate"
    plan:        list[str]      = field(default_factory=list)
    history:     list[dict]     = field(default_factory=list)
    messages:    list[dict]     = field(default_factory=list)
    recall:      dict           = field(default_factory=dict)
    tool_stats:  dict           = field(default_factory=dict)
    critiques:   list[dict]     = field(default_factory=list)

    # Caps applied to growing collections so a runaway/very long run
    # can never blow memory. Both numbers are intentionally generous
    # (a typical run is 4–8 sub-tasks; the worst pathological case
    # we've seen is ~32 sub-tasks × ~4 retries × ~3 msgs ≈ 384).
    _MAX_MESSAGES:  int = 256
    _MAX_CRITIQUES: int = 128

    def post(self, msg: dict) -> None:
        """Append a structured message to the shared message log.

        Capped at `_MAX_MESSAGES` to keep memory bounded on long runs;
        the oldest message is dropped first (FIFO).
        """
        self.messages.append(msg)
        cap = self._MAX_MESSAGES
        if len(self.messages) > cap:
            del self.messages[: len(self.messages) - cap]

    def add_critique(self, critique: dict) -> None:
        """Append a critique to the per-run critique log, FIFO-capped
        at `_MAX_CRITIQUES`. Without the cap, a very long run with
        many failed sub-tasks could grow `critiques` unboundedly."""
        self.critiques.append(critique)
        cap = self._MAX_CRITIQUES
        if len(self.critiques) > cap:
            del self.critiques[: len(self.critiques) - cap]


# ──────────────────────────────────────────────────────────────────────
# 3. Agent base + four concrete roles
# ──────────────────────────────────────────────────────────────────────
class _BaseAgent:
    """Common plumbing: a back-reference to the orchestrator and an
    emit function. Sub-classes override `role` and add behaviour."""
    role: str = "base"

    def __init__(self,
                 orchestrator,
                 emit_fn: Callable[[str, dict], None]) -> None:
        self.o     = orchestrator
        self._emit = emit_fn

    def _announce(self,
                  ctx: SharedContext,
                  kind: str,
                  payload: dict) -> dict:
        """Emit an `agent_*` event AND mirror it onto ctx.messages so
        the message log is the single source of truth for what the
        roles told each other."""
        msg = make_message(self.role, kind, payload)
        ctx.post(msg)
        # Build the UI event from the envelope so role+kind always
        # travel together — keeps the Reasoning tab consistent.
        self._emit(f"agent_{self.role}", {
            "kind":    kind,
            "role":    self.role,
            "payload": payload,
        })
        return msg


class PlannerAgent(_BaseAgent):
    """Wraps Orchestrator.plan(). The actual planning LLM call still
    lives on the orchestrator — this role exists so that the *fact* of
    planning is reported under a consistent role banner."""
    role = "planner"

    def announce_plan(self,
                      ctx: SharedContext,
                      steps: list[str],
                      source: str = "llm") -> dict:
        """Record + emit the plan that was just produced."""
        ctx.plan = list(steps)
        return self._announce(ctx, "plan_ready", {
            "task":    ctx.task[:200],
            "n_steps": len(steps),
            "source":  source,
            "preview": [s[:120] for s in steps[:5]],
        })


class ExecutorAgent(_BaseAgent):
    """Wraps Orchestrator.execute(). Reports each sub-task attempt's
    headline outcome under `agent_executor` so the Reasoning tab can
    show a clean per-step trail without the noise of every internal
    `role`/`execute` event."""
    role = "executor"

    def announce_attempt(self,
                         ctx: SharedContext,
                         subtask: str,
                         strategy: str,
                         attempt: int,
                         exec_result: dict) -> dict:
        ok = bool(exec_result.get("success"))
        return self._announce(ctx, "attempt_done", {
            "subtask":  subtask[:160],
            "strategy": strategy,
            "attempt":  attempt,
            "ok":       ok,
            "elapsed":  exec_result.get("elapsed_seconds", 0.0),
        })


class CriticAgent(_BaseAgent):
    """Reads the verifier's verdict and produces a structured critique
    with a `suggestion` field that the reflection loop folds into its
    learnings. The critic is deliberately cheap by default — it does
    NOT spend an extra LLM call when the verdict already gave a useful
    reason. Only when the reason is empty/uninformative do we ask the
    router for a one-sentence next-step suggestion."""
    role = "critic"

    # Reasons shorter than this are treated as uninformative and
    # trigger the (optional) one-sentence LLM follow-up.
    _MIN_REASON_CHARS = 20

    def evaluate(self,
                 ctx:         SharedContext,
                 subtask:     str,
                 exec_result: dict,
                 verdict:     dict) -> dict:
        ok          = bool(verdict.get("ok"))
        reason      = (verdict.get("reason") or "").strip()
        suggestion  = self._suggest(subtask, exec_result, verdict, ok, reason)
        # Score is intentionally bimodal — finer-grained quality
        # scoring belongs to a future phase. 1.0 / 0.2 mirrors the
        # usefulness encoding the tool-learning layer already uses,
        # so down-stream consumers can treat both signals uniformly.
        critique = {
            "subtask":     subtask[:160],
            "ok":          ok,
            "reason":      reason[:200],
            "suggestion":  suggestion[:240],
            "score":       1.0 if ok else 0.2,
        }
        ctx.add_critique(critique)
        self._announce(ctx, "critique", critique)
        return critique

    def _suggest(self,
                 subtask:     str,
                 exec_result: dict,
                 verdict:     dict,
                 ok:          bool,
                 reason:      str) -> str:
        if ok:
            return "Result accepted; no change needed."
        if reason and len(reason) >= self._MIN_REASON_CHARS:
            # Verifier already explained — turn it into an actionable
            # next-step phrasing without a second LLM call.
            return f"Address the failure: {reason}"
        # Verdict reason is empty or too terse to act on. Ask the
        # router for ONE sentence — strict token cap, best-effort.
        try:
            out = (str(exec_result.get("output", "")) or "")[:300]
            prompt = (
                "A sub-task failed verification. In ONE concise sentence, "
                "suggest the most likely next concrete step. Be specific.\n"
                f"Sub-task: {subtask[:240]}\n"
                f"Output (truncated): {out}\n"
                f"Verifier said: {reason or '(no reason given)'}"
            )
            txt = self.o.router.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=80)
            return (txt or "Retry with a different strategy.").strip()
        except Exception as e:
            # Never let critic suggestions break the run.
            return f"Retry with a different strategy. ({type(e).__name__})"


class MemoryAgent(_BaseAgent):
    """Owns recall + tool-history surfacing. Storage stays on the
    underlying Memory object — this role is a read-side facade. Both
    methods are silent on failure so a disabled vector store never
    blocks the run."""
    role = "memory"

    def recall(self, ctx: SharedContext) -> dict:
        recall: dict = {}
        try:
            recall = self.o.memory.semantic_recall(ctx.task, k=3) or {}
        except Exception as e:
            recall = {"warn": f"semantic_recall failed: {type(e).__name__}: {e}"}
        stats: dict = {}
        try:
            stats = self.o.memory.recall_tool_stats(ctx.task, k=8) or {}
        except Exception as e:
            stats = {"warn": f"recall_tool_stats failed: {type(e).__name__}: {e}"}

        ctx.recall     = recall if isinstance(recall, dict) else {}
        ctx.tool_stats = stats  if isinstance(stats,  dict) else {}

        # Build a small, structured summary for observability — never
        # dump full payloads (they can be large and noisy).
        recall_summary = {}
        if isinstance(recall, dict):
            for k, v in recall.items():
                if isinstance(v, list):
                    recall_summary[k] = len(v)
        tool_summary = []
        if isinstance(stats, dict):
            for tool, st in list(stats.items())[:8]:
                if isinstance(st, dict):
                    tool_summary.append({
                        "tool":     tool,
                        "n":        st.get("n", 0),
                        "ok":       st.get("ok", 0),
                        "fail":     st.get("fail", 0),
                        "useful":   st.get("useful_avg", 0.0),
                    })

        self._announce(ctx, "memory_loaded", {
            "task":          ctx.task[:160],
            "recall_hits":   recall_summary,
            "tool_history":  tool_summary,
        })
        return {"recall": ctx.recall, "tool_stats": ctx.tool_stats}


# ──────────────────────────────────────────────────────────────────────
# 4. AgentTeam — bundle of all four roles
# ──────────────────────────────────────────────────────────────────────
class AgentTeam:
    """Convenience holder. The orchestrator builds one of these in
    __init__ and reaches into it from `run()` and `_attempt_subtask`."""
    def __init__(self,
                 orchestrator,
                 emit_fn: Callable[[str, dict], None]) -> None:
        self.planner  = PlannerAgent(orchestrator,  emit_fn)
        self.executor = ExecutorAgent(orchestrator, emit_fn)
        self.critic   = CriticAgent(orchestrator,   emit_fn)
        self.memory   = MemoryAgent(orchestrator,   emit_fn)

    def all(self):
        return (self.planner, self.executor, self.critic, self.memory)
