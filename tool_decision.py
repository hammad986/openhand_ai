"""
Phase 14 — Controlled Tool Routing (Tool Decision Layer)

A thin, opt-in wrapper that sits BETWEEN the orchestrator's planner
and executor. Before handing a sub-task to `agent.run`, it asks the
LLM exactly one question — "would calling a single read-only tool
*right now* materially help the executor?" — and, if so, calls that
tool via the existing `Tools.execute` surface, then injects the
result into the framed prompt as PRE-FETCHED CONTEXT.

Design constraints (intentional, do not relax without a discussion):

  • UNTOUCHED FILES: agent.py, router.py, tools.py, main.py.
    This layer reaches into `agent.tools.execute(...)` ONLY through
    its existing public method — it does not patch, monkey-patch, or
    re-implement any tool.

  • READ-ONLY WHITELIST. The decision layer is a *reconnaissance*
    step, not an action step. The actual write/exec tools
    (`write_file`, `run_python`, `run_shell`, `git_commit`, …) are
    still chosen and invoked by the inner Agent's normal LLM loop
    inside `agent.run`. Restricting the layer to read-only tools
    means:
      (a) it can't accidentally double-write or corrupt state,
      (b) it can't trigger side effects that would surprise the
          executor,
      (c) loop guards are trivially safe — re-reading a file twice
          is wasteful but never destructive.

  • HARD CAPS. Per-task call cap (default 3) and per-subtask cap
    (default 1) prevent runaway tool use. A signature dedupe ((tool,
    sorted-args)) blocks back-to-back identical calls.

  • SILENT FALLBACK. Any failure — LLM error, JSON parse error, tool
    crash, timeout, whitelist rejection — returns `None` and the
    orchestrator's normal flow proceeds untouched. This layer is
    *strictly additive*: it can only improve a run, never break one.

  • OBSERVABILITY. Two event kinds are emitted via the orchestrator's
    `_emit` channel:
      [ORCHESTRATOR] {"kind":"tool_decision",  ...}
      [ORCHESTRATOR] {"kind":"tool_execution", ...}
    The frontend (Phase 13A) routes both into the Decisions panel.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional


# ── Whitelist ────────────────────────────────────────────────────────
# Read-only / inspection-only tools that have NO network egress and
# NO state mutation. server_test was *considered* but rejected: it
# accepts an arbitrary URL + method + body and would let a confused
# (or adversarial) LLM probe internal services or trigger remote
# state changes — i.e. an SSRF risk that doesn't belong in a
# "controlled" reconnaissance layer. The executor agent can still
# call server_test through its normal tool loop where the user has
# already opted into those side effects.
WHITELIST: dict[str, dict[str, Any]] = {
    "read_file": {
        "args":          {"path": "str"},
        "allowed_args":  {"path"},
        "desc":          "Read the full contents of one file from the workspace.",
    },
    "list_files": {
        "args":          {"subdir": "str (optional, default '')"},
        "allowed_args":  {"subdir"},
        "desc":          "List files recursively under the workspace or a sub-directory.",
    },
    "git_status": {
        "args":          {},
        "allowed_args":  set(),
        "desc":          "Show `git status --short` in the workspace.",
    },
}


def _clamp_int(env_name: str, default: int, lo: int, hi: int) -> int:
    """Read an env var as int, clamp to [lo, hi]. Falls back to
    `default` on any parse failure so a typoed env var can never
    disable safety."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# Tunables — every one env-overridable, every one clamped.
MAX_CALLS_PER_TASK     = _clamp_int("TOOL_MAX_CALLS_PER_TASK",     3, 0, 10)
MAX_CALLS_PER_SUBTASK  = _clamp_int("TOOL_MAX_CALLS_PER_SUBTASK",  1, 0, 5)
TOOL_TIMEOUT_SECS      = _clamp_int("TOOL_DECISION_TIMEOUT_SECS", 20, 1, 120)
DECISION_LLM_MAX_TOKENS = _clamp_int("TOOL_DECISION_MAX_TOKENS",  300, 50, 1000)
# Hard cap on how much pre-fetched output to inject — prompt budget.
PREFETCH_INJECTION_CAP = _clamp_int("TOOL_PREFETCH_CAP_CHARS", 1500, 200, 8000)

# Globally enable/disable without removing the layer.
ENABLED = os.environ.get("TOOL_DECISION_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")


_DECISION_PROMPT = """You decide whether ONE read-only tool should be \
called BEFORE the executor agent starts working on a sub-task.

Sub-task:
{subtask}

Strategy chosen by the orchestrator: {strategy}
Planned steps (best-effort hint): {plan_hint}

Available tools (READ-ONLY ONLY — write/exec tools are NOT in scope \
here; the executor will choose those itself):
{tool_menu}

Rules:
  - Pick a tool ONLY when the executor would clearly benefit from \
seeing the result (e.g. inspecting an existing file before editing \
it, listing files to understand the project, checking server health).
  - Otherwise return tool=null. "When in doubt, don't."
  - Do NOT pick a tool just to be helpful — it costs latency and \
prompt budget.

Return STRICT JSON, no commentary:
{{"tool": "<tool_name or null>", "args": {{...}}, \
"reason": "<one short sentence>"}}
"""


class ToolDecisionLayer:
    """One instance per orchestrator. Owns per-task counters and the
    signature dedupe set so the layer can enforce loop guards across
    every sub-task in a single run."""

    def __init__(self, agent, emit_fn, memory=None) -> None:
        # `agent` is the existing Agent instance owned by the
        # orchestrator. We only ever read from `agent.tools` and
        # `agent.router` — never patch them.
        self.agent = agent
        self._emit = emit_fn
        # Phase 15 — optional Memory handle. When present, the
        # decision LLM is biased by `recall_tool_stats(subtask)`
        # so prior outcomes inform future picks. Absent / disabled
        # memory degrades silently to the original Phase 14 path
        # (no bias block, no [AVOID] markers).
        self.memory = memory
        # Per-task state. `reset_for_task` clears these at the top of
        # each `Orchestrator.run`.
        self._task_calls: int = 0
        self._task_signatures: set[tuple[str, str]] = set()
        # Per-subtask state. Keyed by the truncated subtask string so
        # the orchestrator does NOT need to thread an explicit subtask
        # id through; the same subtask invoked twice in one task
        # shares the same counter.
        self._subtask_calls: dict[str, int] = {}
        self._enabled: bool = ENABLED and self._has_required_attrs()

    # ── Lifecycle hooks ─────────────────────────────────────────────
    def _has_required_attrs(self) -> bool:
        return (self.agent is not None
                and hasattr(self.agent, "tools")
                and hasattr(self.agent.tools, "execute")
                and hasattr(self.agent, "router")
                and hasattr(self.agent.router, "chat"))

    def reset_for_task(self) -> None:
        """Called at the START of every Orchestrator.run() — wipes the
        per-task budget so a fresh task gets a fresh tool allowance."""
        self._task_calls = 0
        self._task_signatures.clear()
        self._subtask_calls.clear()

    # ── Main entry point ────────────────────────────────────────────
    def maybe_use_tool(self, subtask: str, strategy: str,
                       plan_hint: list[str] | None = None
                       ) -> Optional[dict]:
        """Returns a `{tool, args, output, elapsed_ms}` dict on a
        successful pre-fetch, or None when the layer chose not to act
        OR any guard / failure tripped. NEVER raises."""
        if not self._enabled:
            return None
        # Wall-clock for the whole maybe_use_tool call (decision LLM
        # + tool execute). If we're not done within TOOL_TIMEOUT_SECS
        # we still return None below — the budget here only protects
        # against an in-flight router/tool that's about to start, not
        # a synchronous call that's already running. router.chat does
        # its own client-level timeout; Tools.execute does its own
        # per-tool timeout. This wall clock is a final belt-and-
        # braces check.
        _t_start = time.time()
        # Per-task cap — a global circuit breaker for the whole run.
        if self._task_calls >= MAX_CALLS_PER_TASK:
            self._emit("tool_decision", {
                "subtask":  subtask[:160],
                "tool":     None,
                "reason":   ("max-calls-per-task reached "
                             f"({self._task_calls}/{MAX_CALLS_PER_TASK})"),
                "skipped":  "budget_exceeded",
            })
            return None

        # Per-subtask cap — enforced HERE rather than at the call site
        # so a future caller that fans out (e.g. retries the same
        # sub-task) can't accidentally chain extra tool calls. Keyed
        # by the truncated subtask so identical retries collapse.
        sub_key = subtask[:160]
        if self._subtask_calls.get(sub_key, 0) >= MAX_CALLS_PER_SUBTASK:
            self._emit("tool_decision", {
                "subtask": sub_key,
                "tool":    None,
                "reason":  ("max-calls-per-subtask reached "
                            f"({self._subtask_calls[sub_key]}/"
                            f"{MAX_CALLS_PER_SUBTASK})"),
                "skipped": "subtask_budget_exceeded",
            })
            return None

        # ── 1. Ask the LLM ──────────────────────────────────────────
        decision = self._ask_llm(subtask, strategy, plan_hint or [])
        # Wall-clock check after the LLM call. If the decision LLM
        # itself ran long enough to blow the budget, we abandon
        # before issuing any tool side effect.
        if (time.time() - _t_start) > TOOL_TIMEOUT_SECS:
            self._emit("tool_decision", {
                "subtask": sub_key,
                "tool":    None,
                "reason":  f"decision LLM exceeded {TOOL_TIMEOUT_SECS}s budget",
                "skipped": "wall_clock_exceeded",
            })
            return None
        if decision is None:
            # LLM failed or returned unparseable JSON — log and bail.
            self._emit("tool_decision", {
                "subtask":  subtask[:160],
                "tool":     None,
                "reason":   "llm decision unavailable",
                "skipped":  "llm_error",
            })
            return None

        tool = decision.get("tool")
        args = decision.get("args") or {}
        why  = (decision.get("reason") or "")[:200]

        # Decision = "no tool". Emit, return.
        if not tool or tool == "null":
            self._emit("tool_decision", {
                "subtask": subtask[:160],
                "tool":    None,
                "reason":  why or "model declined to use a tool",
            })
            return None

        # ── 2. Whitelist enforcement ────────────────────────────────
        if tool not in WHITELIST:
            self._emit("tool_decision", {
                "subtask": subtask[:160],
                "tool":    tool,
                "args":    self._safe_args(args),
                "reason":  why,
                "skipped": "not_in_whitelist",
            })
            return None

        # Coerce arg names/types defensively. Anything that looks
        # wrong → bail; we don't try to be clever and rewrite.
        if not isinstance(args, dict):
            self._emit("tool_decision", {
                "subtask": subtask[:160],
                "tool":    tool,
                "reason":  why,
                "skipped": "args_not_object",
            })
            return None

        # Per-tool arg whitelist — strip ANY key not declared in
        # WHITELIST[tool]['allowed_args']. This stops the LLM from
        # smuggling extra parameters (e.g. an absolute `path` to a
        # tool that should only accept relative subdirs, or a `data`
        # body to a tool that should only do GETs). String values are
        # bounded so a runaway pattern can't blow up the prompt.
        allowed = WHITELIST[tool].get("allowed_args") or set()
        clean_args: dict[str, Any] = {}
        for k, v in args.items():
            if k not in allowed:
                continue
            if isinstance(v, str):
                clean_args[k] = v[:1000]
            elif isinstance(v, (int, float, bool)) or v is None:
                clean_args[k] = v
            # silently drop anything else — tools expect scalar args
        args = clean_args

        # ── 3. Loop guard via signature dedupe ─────────────────────
        sig = (tool, json.dumps(args, sort_keys=True, default=str)[:300])
        if sig in self._task_signatures:
            self._emit("tool_decision", {
                "subtask": subtask[:160],
                "tool":    tool,
                "args":    self._safe_args(args),
                "reason":  why,
                "skipped": "duplicate_call_signature",
            })
            return None

        # We are committing to a call — record signature & emit
        # the *positive* decision before execution so the timeline
        # shows decision → execution in order.
        self._task_signatures.add(sig)
        self._emit("tool_decision", {
            "subtask":  subtask[:160],
            "tool":     tool,
            "args":     self._safe_args(args),
            "reason":   why,
            "approved": True,
            "calls_so_far": self._task_calls,
        })

        # ── 4. Execute via existing Tools.execute(...) ──────────────
        t0 = time.time()
        result: dict[str, Any]
        try:
            # Note: we deliberately don't add our own thread+timeout
            # wrapper here — `Tools.execute` already enforces its own
            # per-tool timeouts (e.g. run_python timeout=30,
            # run_shell timeout=60), and our whitelist contains only
            # quick read-only tools. If a future whitelist entry
            # needs an external wall clock, add it here.
            raw = self.agent.tools.execute(tool, **args)
            if not isinstance(raw, dict):
                raw = {"success": False, "output": "", "error":
                       f"tool returned non-dict: {type(raw).__name__}"}
            result = raw
        except Exception as e:
            result = {"success": False, "output": "",
                      "error": f"{type(e).__name__}: {e}"}
        elapsed_ms = int((time.time() - t0) * 1000)

        self._task_calls += 1
        self._subtask_calls[sub_key] = self._subtask_calls.get(sub_key, 0) + 1

        ok = bool(result.get("success"))
        out = (result.get("output") or "")
        err = (result.get("error") or "")

        self._emit("tool_execution", {
            "subtask":    subtask[:160],
            "tool":       tool,
            "args":       self._safe_args(args),
            "ok":         ok,
            "elapsed_ms": elapsed_ms,
            "out_chars":  len(out),
            "err":        err[:200] if not ok else "",
            "calls_so_far": self._task_calls,
        })

        if not ok:
            # Tool itself failed — fall back to standard execution.
            # Do NOT inject error text into the prompt; the executor
            # has its own error-handling channel.
            return None

        return {
            "tool":       tool,
            "args":       args,
            "output":     out[:PREFETCH_INJECTION_CAP],
            "truncated":  len(out) > PREFETCH_INJECTION_CAP,
            "elapsed_ms": elapsed_ms,
        }

    # ── Helpers ─────────────────────────────────────────────────────
    # Phase 15 — minimum sample size before a tool's history can flip
    # it into [AVOID] in the prompt menu. Two failures with zero
    # successes is a credible "this didn't help" signal; one failure
    # is noise.
    AVOID_MIN_FAILS = 2

    def _bias_stats(self, subtask: str) -> dict[str, dict]:
        """Best-effort: ask Memory for per-tool stats on similar
        sub-tasks. Returns {} when memory missing or recall fails."""
        if self.memory is None:
            return {}
        try:
            return self.memory.recall_tool_stats(subtask, k=8) or {}
        except Exception:
            return {}

    @staticmethod
    def _render_bias_block(stats: dict[str, dict]) -> str:
        """Format the per-tool stats into a prompt block. Tools sorted
        by usefulness desc, then by ok-ratio desc, so the highest-
        signal tool floats to the top."""
        if not stats:
            return ""
        ranked = sorted(
            stats.items(),
            key=lambda kv: (-(kv[1].get("useful_avg") or 0.0),
                            -((kv[1].get("ok") or 0) /
                              max(1, kv[1].get("n") or 1))))
        lines = []
        for name, s in ranked[:4]:
            n  = s.get("n", 0)
            ok = s.get("ok", 0)
            ua = s.get("useful_avg", 0.0)
            lines.append(
                f"  - {name}: {ok}/{n} succeeded, "
                f"avg usefulness {ua:.2f}")
        return ("PAST EXPERIENCE on similar sub-tasks (use as a "
                "soft hint, not a rule):\n" + "\n".join(lines) + "\n\n")

    def _menu_with_avoid(self, stats: dict[str, dict]) -> str:
        """Render the tool menu, marking entries with `AVOID_MIN_FAILS+`
        failures and zero successes as `[AVOID — past failures]`."""
        out = []
        for name, spec in WHITELIST.items():
            sig = (', '.join(spec['args'].keys()) or '')
            avoid = ""
            s = stats.get(name)
            if s and s.get("ok", 0) == 0 and s.get("fail", 0) >= self.AVOID_MIN_FAILS:
                avoid = "  [AVOID — repeatedly unhelpful in similar contexts]"
            out.append(f"  - {name}({sig})  → {spec['desc']}{avoid}")
        return "\n".join(out)

    def _ask_llm(self, subtask: str, strategy: str,
                 plan_hint: list[str]) -> Optional[dict]:
        # Phase 15 — bias the LLM with prior outcomes when available.
        # This is purely additive: when memory is absent / empty, the
        # bias block + avoid markers vanish and the prompt is byte-
        # identical to the Phase 14 version.
        stats = self._bias_stats(subtask)
        bias_block = self._render_bias_block(stats)
        menu = self._menu_with_avoid(stats)
        plan_str = ("; ".join(s[:80] for s in plan_hint[:4])
                    if plan_hint else "(none)")
        prompt = bias_block + _DECISION_PROMPT.format(
            subtask=subtask[:1200],
            strategy=strategy or "(none)",
            plan_hint=plan_str,
            tool_menu=menu,
        )
        try:
            raw = self.agent.router.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=DECISION_LLM_MAX_TOKENS)
        except Exception:
            return None
        if not isinstance(raw, str) or not raw.strip():
            return None
        # Be liberal in what we accept — pull the first {...} block.
        i, j = raw.find("{"), raw.rfind("}")
        if i < 0 or j < i:
            return None
        try:
            obj = json.loads(raw[i:j + 1])
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    @staticmethod
    def _safe_args(args: Any) -> dict:
        """Truncate arg values to keep emitted events small."""
        if not isinstance(args, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in list(args.items())[:6]:
            try:
                if isinstance(v, str):
                    out[str(k)[:40]] = v[:200]
                elif isinstance(v, (int, float, bool)) or v is None:
                    out[str(k)[:40]] = v
                else:
                    out[str(k)[:40]] = str(v)[:200]
            except Exception:
                continue
        return out


def format_prefetch_block(prefetch: dict) -> str:
    """Render a successful pre-fetch result as a prompt block to be
    prepended to the executor's framed prompt. Empty string when
    `prefetch` is falsy."""
    if not prefetch:
        return ""
    args_repr = json.dumps(prefetch.get("args") or {},
                           sort_keys=True, default=str)[:200]
    note = " (truncated)" if prefetch.get("truncated") else ""
    return (
        "PRE-FETCHED CONTEXT — the orchestrator already ran this "
        f"read-only tool on your behalf.\n"
        f"  tool: {prefetch.get('tool')}\n"
        f"  args: {args_repr}\n"
        f"  output{note}:\n"
        "----- BEGIN OUTPUT -----\n"
        f"{prefetch.get('output', '')}\n"
        "----- END OUTPUT -----\n"
        "Use this directly — do NOT re-run the same tool.\n\n"
    )
