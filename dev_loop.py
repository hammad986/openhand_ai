"""dev_loop.py — Phase 20.5: Autonomous Dev Loop.

Connects Phases 20.1–20.4 into a continuous Plan → Code → Run → Test →
Critic → Fix → Repeat cycle.

Pipeline per iteration
----------------------
    1. PLANNER   — call the LLM with task + (optional) past failure
                   summary + past fixes from CodeLearning.  Returns
                   raw text from which the EXECUTOR extracts code.
    2. EXECUTOR  — strip markdown fences from the LLM response and
                   stage the code as an in-memory string.  No file is
                   written to the project tree; the runner stages its
                   own sandboxed copy.
    3. RUNNER    — code_runner.run_code(...) — smoke-execute the code
                   so syntax / import / startup errors are caught
                   before we generate tests.
    4. TESTER    — code_testing.generate_and_run(...) on the resolved
                   target function.  Function name is auto-detected
                   via code_intel.analyze_file (with regex fallback)
                   or supplied by the caller.
    5. CRITIC    — code_testing.summarize_for_critic plus a structured
                   "critic signature" we use to detect no-progress.
    6. LEARNING  — code_testing.record_failures_to_learning persists
                   each failed test as a code_lessons row;
                   CodeLearning.lookup_for_prompt fetches similar
                   past fixes for the next planner pass.

Loop termination (in priority order)
------------------------------------
    * Wall-clock budget exceeded (default 120 s, hard ceiling 600 s)
      → ``stop_reason="timeout"``.  Checked both at iteration start
      AND at iteration end so a phase that overshoots inside an
      iteration still surfaces the timeout reason.
    * Test pass with run status == "success" → ``stop_reason="success"``.
    * Two consecutive iterations produced the same critic signature
      → ``stop_reason="no_progress"`` (disable via
      ``stop_on_no_progress=False``).
    * Hit ``max_iterations`` (default 5, hard ceiling 50)
      → ``stop_reason="max_iterations"``.
    * No LLM configured at all (and no initial code) → ``stop_reason
      ="no_planner"``.
    * LLM was called but returned unusable / empty output during a
      replan → ``stop_reason="planner_empty"`` (distinct from
      ``no_planner`` so callers can tell apart "never had a planner"
      from "planner is broken").

Safety
------
    * All subprocess execution inherits code_runner's RLIMIT/setsid/
      stream-cap protections; this module adds **no** new exec paths.
    * max_iterations and total_timeout_sec are clamped to hard
      ceilings — passing 9999 silently caps to 50.
    * Every phase is wrapped in try/except so a single phase crash
      degrades to a bad iteration record, never an unhandled
      exception.  ``autonomous_dev_loop`` itself is contracted to
      always return a structured dict.

Modularity
----------
    * **No existing module is modified.**  All integration is by
      duck-typed import of public surfaces only.
    * No new dependencies; pure stdlib + the 20.1–20.4 modules.
    * The Memory + LLMRouter handles are optional and lazily used —
      the loop runs end-to-end (sans LLM) given an ``initial_code``
      argument, which makes it easy to unit-test without API keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

# ── 20.1–20.4 surface imports ─────────────────────────────────────────
# All imported at module load.  Keeping them top-level (rather than
# lazy) makes ImportError surface immediately at app start instead of
# mid-loop, and matches how the rest of the project wires Phase 20.
from code_runner import run_code, RunResult
from code_testing import (
    generate_and_run,
    summarize_for_critic,
    record_failures_to_learning,
)

logger = logging.getLogger("dev_loop")

# ── Tunable defaults (all caller-overridable) ─────────────────────────
_DEFAULT_MAX_ITER = 5
_HARD_MAX_ITER = 50            # paranoid ceiling — defends against
                                # accidental thousand-iteration loops.
_DEFAULT_TOTAL_TIMEOUT = 120.0  # seconds, full loop budget
_HARD_TOTAL_TIMEOUT = 600.0
_DEFAULT_RUN_TIMEOUT = 5.0      # per code_runner call
_DEFAULT_TEST_TIMEOUT = 5.0     # per code_testing call
_DEFAULT_MAX_TESTS = 8

# ── Markdown / code extraction ────────────────────────────────────────
# Matches ```python … ``` or ``` … ```; non-greedy so we get the first
# fenced block when the LLM emits commentary + multiple snippets.
_FENCE_RE = re.compile(
    r"```(?:python|py|py3|python3)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# Matches "function foo", "def foo", "function called foo", "named foo"
# inside a free-form task spec — used as a hint for function-name
# auto-detection so we don't always pick the first def in the file.
_HINT_NAME_RE = re.compile(
    r"\b(?:function|def|method|named|called)\s+(?:`)?([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)

_TOPLEVEL_DEF_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(",
    re.MULTILINE,
)


# ─────────────────────────────────────────────────────────────────────
# Structured records
# ─────────────────────────────────────────────────────────────────────


@dataclass
class IterRecord:
    """One iteration of the dev loop, fully serialisable."""

    iter: int
    phase: str = "plan"          # last phase reached this iteration
    code: str = ""               # code under test this iteration
    run: Optional[Dict[str, Any]] = None      # RunResult.to_dict()
    test: Optional[Dict[str, Any]] = None     # TestRunResult dict
    critic: str = ""             # human-readable summary
    signature: str = ""          # stable hash for no-progress check
    lessons_used: int = 0        # how many past lessons fed the planner
    lessons_recorded: int = 0    # how many failures persisted this iter
    function_name: str = ""
    notes: List[str] = field(default_factory=list)
    duration_sec: float = 0.0
    # Phase 25 — runtime environment healing.  Populated when
    # ``auto_fix_env=True`` and the loop pip-installed a missing module
    # detected from the iteration's stderr/error.  Shape:
    #   {"installed": "<pkg>", "ok": bool, "exit": int, "skipped": str?}
    env_fix: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iter": self.iter,
            "phase": self.phase,
            "code": self.code,
            "run": self.run,
            "test": self.test,
            "critic": self.critic,
            "signature": self.signature,
            "lessons_used": self.lessons_used,
            "lessons_recorded": self.lessons_recorded,
            "function_name": self.function_name,
            "notes": list(self.notes),
            "duration_sec": round(self.duration_sec, 4),
            "env_fix": self.env_fix,
        }


@dataclass
class DevLoopResult:
    """Top-level loop result — always returned, never raised from."""

    success: bool = False
    iterations: int = 0
    final_code: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    task: str = ""
    function_name: str = ""
    duration_sec: float = 0.0
    llm_used: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "iterations": self.iterations,
            "final_code": self.final_code,
            "history": list(self.history),
            "stop_reason": self.stop_reason,
            "task": self.task,
            "function_name": self.function_name,
            "duration_sec": round(self.duration_sec, 4),
            "llm_used": self.llm_used,
        }


# ─────────────────────────────────────────────────────────────────────
# Helpers — code extraction, naming, signatures
# ─────────────────────────────────────────────────────────────────────


def _extract_python_code(text: str) -> str:
    """Pull Python source out of an LLM response.

    Strategy:
        1. If a fenced block is present, return its content.
        2. Else return the text as-is so callers that pass raw source
           still work.

    Always returns a stripped string; never None.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _detect_function_name(code: str, hint: str = "") -> Optional[str]:
    """Resolve the function the tester should target.

    Order:
        1. Name explicitly mentioned in ``hint`` (the task spec) and
           also defined in ``code`` — strongest signal.
        2. ``code_intel.analyze_file`` against a tempfile copy — picks
           up the project's canonical AST view.
        3. Regex fallback (first top-level ``def`` in the source).
    """
    if not isinstance(code, str) or "def " not in code:
        return None

    # 1. Hint-driven match
    if hint:
        for m in _HINT_NAME_RE.finditer(hint):
            name = m.group(1)
            if re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(",
                         code, re.MULTILINE):
                return name

    # 2. code_intel.analyze_file via tempfile (Phase 20.1 integration)
    try:
        from code_intel import analyze_file  # local import → no top-level coupling
        fd, path = tempfile.mkstemp(prefix="devloop_", suffix=".py", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(code)
            info = analyze_file(path)
            funcs = info.get("functions") or []
            if funcs:
                return funcs[0].get("name") or None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception as e:
        logger.debug(f"[dev_loop] code_intel detect failed: {e}")

    # 3. Regex fallback
    m = _TOPLEVEL_DEF_RE.search(code)
    return m.group(1) if m else None


def _critic_signature(run: Optional[Dict[str, Any]],
                      test: Optional[Dict[str, Any]]) -> str:
    """Stable 12-hex digest of a (run, test) outcome.

    Two iterations with the same signature are "the same failure" for
    the purposes of the no-progress detector.  Designed to be tolerant
    of jitter in tracebacks (line numbers, addresses) — only the
    structured shape matters.
    """
    bits = {
        "run_status": (run or {}).get("status"),
        "run_exit": (run or {}).get("exit_code"),
        "run_truncated": bool((run or {}).get("truncated_stdout")
                              or (run or {}).get("truncated_stderr")),
        "test_timed_out": bool((test or {}).get("timed_out")),
        "test_total": int((test or {}).get("total_tests") or 0),
        "test_failed": int((test or {}).get("failed_tests") or 0),
        "errs": sorted(
            (str(e.get("error_type") or ""), str(e.get("name") or ""))
            for e in ((test or {}).get("errors") or [])
        ),
    }
    blob = json.dumps(bits, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────
# LLM adapter — supports LLMRouter, plain callable, or None
# ─────────────────────────────────────────────────────────────────────

# Public type alias for callers; not enforced (we duck-type).
LLMLike = Union[Callable[[str], str], Any, None]


def _call_llm(llm: LLMLike, prompt: str,
              *, system: Optional[str] = None,
              max_tokens: int = 2048,
              role: Optional[str] = None) -> Optional[str]:
    """Call the LLM with a uniform interface.  Returns None on any
    failure (including "no LLM configured") so the loop's fallback
    paths can branch cleanly on falsy.

    Accepted shapes:
        * Object with ``.chat_role(role, messages, system=...,
          max_tokens=...)`` -- Phase 23 unified router; preferred
          when ``role`` is supplied.
        * Object with ``.chat(messages, system=..., max_tokens=...)``
          method -- matches router.LLMRouter.
        * Plain callable taking a single ``prompt`` string.
        * ``None`` -- returns None.
    """
    if llm is None:
        return None
    # Phase 23: prefer role-aware dispatch when available + role given.
    if role and hasattr(llm, "chat_role") and callable(getattr(llm, "chat_role")):
        try:
            return llm.chat_role(
                role,
                [{"role": "user", "content": prompt}],
                system=system,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.warning(f"[dev_loop] llm.chat_role({role}) failed: "
                           f"{type(e).__name__}: {e}; falling back to chat()")
            # fall through to plain chat below
    # Router-style next (preferred -- gives us system prompt + retry).
    if hasattr(llm, "chat") and callable(getattr(llm, "chat")):
        try:
            return llm.chat(
                [{"role": "user", "content": prompt}],
                system=system,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.warning(f"[dev_loop] llm.chat failed: "
                           f"{type(e).__name__}: {e}")
            return None
    # Bare callable
    if callable(llm):
        try:
            return llm(prompt)
        except Exception as e:
            logger.warning(f"[dev_loop] llm callable failed: "
                           f"{type(e).__name__}: {e}")
            return None
    logger.warning(f"[dev_loop] unrecognised llm type: "
                   f"{type(llm).__name__}; treating as None")
    return None


# ─────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = (
    "You are a Python coding assistant inside an autonomous "
    "test-driven loop.  You receive a task spec, optional past-fix "
    "lessons, and (after the first iteration) the previous attempt "
    "plus the test critic's summary.  Your job is to emit Python "
    "source code that satisfies the spec.  Output exactly ONE fenced "
    "```python``` code block and no commentary."
)


def _build_planner_prompt(
    task: str,
    *,
    past_fixes: str = "",
    current_code: str = "",
    critic_summary: str = "",
    run_error: str = "",
    function_name_hint: str = "",
) -> str:
    """Assemble the user-facing planner prompt.

    Sections are emitted only when they have content so the prompt
    stays short on the very first iteration.
    """
    parts: List[str] = [f"### TASK\n{task.strip()}\n"]

    if function_name_hint:
        parts.append(
            f"### TARGET FUNCTION\n"
            f"Define a top-level function named `{function_name_hint}`.\n"
        )

    if past_fixes:
        # Already pre-formatted by CodeLearning.format_past_fixes.
        parts.append(past_fixes.rstrip() + "\n")

    if current_code:
        parts.append(
            "### PREVIOUS ATTEMPT (failed)\n"
            f"```python\n{current_code.rstrip()}\n```\n"
        )

    if run_error:
        # Trim long tracebacks aggressively; the critic summary carries
        # the high-signal bits.
        snippet = run_error.strip()
        if len(snippet) > 1500:
            snippet = snippet[:1500] + "\n... (truncated)"
        parts.append(f"### RUNTIME ERROR\n```\n{snippet}\n```\n")

    if critic_summary:
        parts.append(f"### TEST CRITIC\n{critic_summary.strip()}\n")

    if current_code or critic_summary or run_error:
        parts.append(
            "### INSTRUCTIONS\n"
            "Diagnose what went wrong and emit a corrected, complete "
            "implementation.  Output only one fenced ```python``` "
            "block — no prose, no extra blocks.\n"
        )
    else:
        parts.append(
            "### INSTRUCTIONS\n"
            "Write the implementation now.  Output only one fenced "
            "```python``` block — no prose.\n"
        )

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────
# Phase wrappers — every one returns a structured value, never raises
# ─────────────────────────────────────────────────────────────────────


def _safe_run_code(code: str, *, timeout: float,
                   memory_limit_mb: Optional[int],
                   max_output_bytes: int) -> Dict[str, Any]:
    """Smoke-execute the candidate code via Phase 20.4.

    Returns RunResult.to_dict().  Even on internal exceptions we
    return a synthetic dict so the loop can continue.
    """
    try:
        res: RunResult = run_code(
            code,
            timeout=timeout,
            memory_limit_mb=memory_limit_mb,
            max_output_bytes=max_output_bytes,
        )
        return res.to_dict()
    except Exception as e:  # pragma: no cover — run_code is itself defensive
        logger.warning(f"[dev_loop] run_code raised: "
                       f"{type(e).__name__}: {e}")
        return {
            "status": "error",
            "output": "",
            "error": f"runner exception: {type(e).__name__}: {e}",
            "exit_code": -1,
            "duration_sec": 0.0,
            "sandbox_dir": "",
            "truncated_stdout": False,
            "truncated_stderr": False,
            "extra": {},
        }


def _safe_run_tests(code: str, function_name: str, *,
                    timeout: float,
                    max_tests: int) -> Dict[str, Any]:
    """Run Phase 20.3 on ``code``.  Returns the TestRunResult dict.
    Synthesises an "all failed" result on internal exception."""
    try:
        return generate_and_run(
            code,
            function_name,
            timeout=timeout,
            max_tests=max_tests,
        )
    except Exception as e:
        logger.warning(f"[dev_loop] generate_and_run raised: "
                       f"{type(e).__name__}: {e}")
        return {
            "passed": False,
            "total_tests": 0,
            "failed_tests": 0,
            "duration_sec": 0.0,
            "timed_out": False,
            "function": function_name,
            "errors": [{
                "name": "<dev_loop>",
                "error_type": type(e).__name__,
                "message": str(e),
                "traceback": "",
            }],
            "generated_test_names": [],
            "generated_tests": [],
        }


# ─────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────


_MAX_ENV_FIXES_PER_LOOP = 3   # Phase 25 — hard cap on auto pip installs.


def _attempt_env_fix(run_dict: Optional[Dict[str, Any]],
                     test_dict: Optional[Dict[str, Any]],
                     *, attempted_so_far: int,
                     already_installed: set) -> Optional[Dict[str, Any]]:
    """Phase 25 — try to pip-install a single missing module surfaced in
    the iteration's stderr/error.  Returns ``None`` if nothing actionable
    was found OR the loop-wide cap was hit; otherwise returns a dict
    suitable for ``IterRecord.env_fix`` and (when ok) the next iteration
    will see the freshly-installed module.

    The dispatch goes through :mod:`command_layer` so the global AI
    toggle, install consent, hard blocklist, and audit log all apply
    uniformly.  We pass ``allow_install=True`` because the dev-loop
    caller has already opted in by setting ``auto_fix_env=True``.
    """
    if attempted_so_far >= _MAX_ENV_FIXES_PER_LOOP:
        return {"skipped": "loop_cap_reached", "ok": False}
    haystack_parts: List[str] = []
    if isinstance(run_dict, dict):
        haystack_parts.append(str(run_dict.get("error") or ""))
        haystack_parts.append(str(run_dict.get("stderr") or ""))
    if isinstance(test_dict, dict):
        for err in (test_dict.get("errors") or []):
            if isinstance(err, dict):
                haystack_parts.append(str(err.get("message") or ""))
                haystack_parts.append(str(err.get("traceback") or ""))
    haystack = "\n".join(p for p in haystack_parts if p)
    try:
        from command_layer import parse_missing_modules, execute_command
    except Exception as e:                         # noqa: BLE001
        logger.warning(f"[dev_loop] command_layer import failed: "
                       f"{type(e).__name__}: {e}")
        return None
    mods = parse_missing_modules(haystack, limit=5)
    # Skip modules we already tried this loop — avoids ping-ponging on
    # a module pip can't actually install.
    target = next((m for m in mods if m not in already_installed), None)
    if not target:
        return None
    already_installed.add(target)
    envelope = execute_command(
        f"pip install {target}",
        allow_install=True,
        source="dev_loop",
        timeout=120,
    )
    return {
        "installed": target,
        "ok":        bool(envelope.get("ok")),
        "exit":      int(envelope.get("exit", -1)),
        "category":  envelope.get("category", ""),
        "error":     envelope.get("error", "") or "",
    }


def autonomous_dev_loop(
    task: str,
    *,
    function_name: Optional[str] = None,
    initial_code: Optional[str] = None,
    llm: LLMLike = None,
    memory: Any = None,           # duck-typed Memory instance
    max_iterations: int = _DEFAULT_MAX_ITER,
    total_timeout_sec: float = _DEFAULT_TOTAL_TIMEOUT,
    run_timeout: float = _DEFAULT_RUN_TIMEOUT,
    test_timeout: float = _DEFAULT_TEST_TIMEOUT,
    max_tests: int = _DEFAULT_MAX_TESTS,
    memory_limit_mb: Optional[int] = 256,
    max_output_bytes: int = 1024 * 1024,
    stop_on_no_progress: bool = True,
    on_iteration: Optional[Callable[[Dict[str, Any]], None]] = None,
    auto_fix_env: bool = False,   # Phase 25 — pip-install missing modules
) -> Dict[str, Any]:
    """Run the autonomous Plan → Code → Test → Fix loop.

    Parameters
    ----------
    task:
        Free-form spec the planner will satisfy.  May mention the
        target function name (e.g. ``"write a function add(a, b)
        that ..."``) — auto-detected via the ``_HINT_NAME_RE`` hint
        regex.
    function_name:
        Explicit override for the tester's target.  When None we
        auto-detect from the generated code (Phase 20.1 → regex
        fallback).
    initial_code:
        Skip the very first planner call and start the loop with this
        code.  Lets callers run the loop deterministically without an
        LLM (useful for tests and offline sandboxes).
    llm:
        Anything supporting ``.chat(messages, system=..., max_tokens=
        ...)`` (e.g. LLMRouter) OR a plain ``Callable[[str], str]`` OR
        None.  When None and ``initial_code`` is also None the loop
        cannot proceed and returns ``stop_reason="no_planner"``.
    memory:
        A Memory instance.  When provided we instantiate CodeLearning
        on top of it for past-fix lookup and failure recording.  When
        None those steps are skipped silently.
    max_iterations:
        Hard cap on iterations (clamped to ``[1, 50]``).
    total_timeout_sec:
        Wall-clock budget for the whole loop (clamped to
        ``[1.0, 600.0]``).
    stop_on_no_progress:
        If True (default), break out when two consecutive iterations
        produced the same critic signature.  Set False to force the
        loop to spend its full iteration budget.
    on_iteration:
        Optional callback invoked once per finished iteration with
        the dict-form ``IterRecord``.  Exceptions inside the callback
        are caught and logged so they can't break the loop.

    Returns
    -------
    dict
        Always.  Shape: ``DevLoopResult.to_dict()``.
    """
    t_start = time.monotonic()

    # ── Phase 30 Safety Integration ──
    try:
        from safety_layer import safety_controller, tracker
        safety_controller.reset()
        safety_controller.max_iterations = 3
        safety_controller.max_commands = 10
        safety_controller.max_runtime = 60
    except ImportError:
        safety_controller = None
        tracker = None

    # ── Argument clamping (safety) ────────────────────────────────────
    max_iterations = 3 if safety_controller else max(1, min(int(max_iterations or 1), _HARD_MAX_ITER))
    total_timeout_sec = 60.0 if safety_controller else max(1.0,
                            min(float(total_timeout_sec or _DEFAULT_TOTAL_TIMEOUT),
                                _HARD_TOTAL_TIMEOUT))
    run_timeout = max(0.5, min(float(run_timeout or _DEFAULT_RUN_TIMEOUT), 60.0))
    test_timeout = max(0.5, min(float(test_timeout or _DEFAULT_TEST_TIMEOUT), 60.0))
    max_tests = max(1, min(int(max_tests or 1), 32))

    result = DevLoopResult(task=task, llm_used=False)

    # ── Lazy CodeLearning façade ─────────────────────────────────────
    learning = None
    if memory is not None:
        try:
            from code_learning import CodeLearning
            learning = CodeLearning(memory)
        except Exception as e:
            logger.warning(f"[dev_loop] CodeLearning init failed: "
                           f"{type(e).__name__}: {e}")
            learning = None

    # ── Hint extraction (used both for naming and the planner prompt)
    hint_name = ""
    m = _HINT_NAME_RE.search(task or "")
    if m:
        hint_name = m.group(1)

    # ── Bootstrap code: either caller-supplied or first planner call.
    # Distinguish "no LLM at all" (no_planner) from "LLM responded
    # with empty/unusable text" (planner_empty) so callers can
    # diagnose which case they're in.
    code = ""
    bootstrap_planner_called = False
    if initial_code is not None and initial_code.strip():
        code = _extract_python_code(initial_code)
    elif llm is not None:
        bootstrap_planner_called = True
        prompt0 = _build_planner_prompt(
            task,
            function_name_hint=function_name or hint_name,
        )
        # Phase 23: route the bootstrap planner call through the
        # `planner` role so a user-pinned cheap/local model handles it.
        raw = _call_llm(llm, prompt0, system=_PLANNER_SYSTEM, role="planner")
        if raw:
            code = _extract_python_code(raw)
            if code:
                result.llm_used = True
    if not code:
        # If we never had a planner: "no_planner".
        # If we asked the planner and got nothing back: "planner_empty".
        result.stop_reason = "planner_empty" if bootstrap_planner_called \
            else "no_planner"
        result.duration_sec = time.monotonic() - t_start
        return result.to_dict()

    # ── Resolve function name once we have code in hand
    resolved_name = function_name or _detect_function_name(code, hint=task)
    result.function_name = resolved_name or ""

    # ── Loop state
    prev_signature: Optional[str] = None
    last_run: Optional[Dict[str, Any]] = None
    last_test: Optional[Dict[str, Any]] = None
    last_critic = ""
    # Single tracker lesson row allocated on the FIRST failure of the
    # whole loop and updated with the final fix on success.  Avoids
    # the previous behaviour of inserting one new "open lesson" row
    # per iteration (which leaked unresolved rows into the DB on
    # convergence and was flagged by the architect).
    tracker_lesson_id: Optional[int] = None
    # Phase 25 — auto pip-install bookkeeping (only used when
    # ``auto_fix_env=True`` is set by the caller).
    env_fix_attempts = 0
    env_fix_installed: set = set()

    for i in range(1, max_iterations + 1):
        iter_t0 = time.monotonic()
        rec = IterRecord(iter=i, code=code, function_name=resolved_name or "")

        if safety_controller:
            safety_controller.current_iterations = i
            limit_check = safety_controller.check_limits()
            if limit_check:
                rec.notes.append(f"terminated: {limit_check['reason']} - {limit_check['detail']}")
                rec.duration_sec = time.monotonic() - iter_t0
                result.history.append(rec.to_dict())
                result.iterations = i - 1
                result.stop_reason = limit_check["reason"]
                break

        # ── Wall-clock budget check
        elapsed = time.monotonic() - t_start
        if elapsed >= total_timeout_sec:
            rec.notes.append(f"budget exhausted before iter ({elapsed:.1f}s)")
            rec.duration_sec = time.monotonic() - iter_t0
            result.history.append(rec.to_dict())
            result.iterations = i - 1
            result.stop_reason = "timeout"
            break

        # ── Phase: RUNNER (smoke-execute candidate code)
        rec.phase = "run"
        rec.run = _safe_run_code(
            code,
            timeout=run_timeout,
            memory_limit_mb=memory_limit_mb,
            max_output_bytes=max_output_bytes,
        )
        last_run = rec.run

        # ── Phase: TESTER (only meaningful if we have a target name)
        if not resolved_name:
            # Re-attempt detection (planner may have produced a def
            # only on this iteration).
            resolved_name = _detect_function_name(code, hint=task)
            rec.function_name = resolved_name or ""
            result.function_name = result.function_name or rec.function_name

        if resolved_name:
            rec.phase = "test"
            rec.test = _safe_run_tests(
                code, resolved_name,
                timeout=test_timeout,
                max_tests=max_tests,
            )
        else:
            rec.test = {
                "passed": False,
                "total_tests": 0,
                "failed_tests": 0,
                "errors": [{
                    "name": "<dev_loop>",
                    "error_type": "NoTarget",
                    "message": "Could not resolve a function name to test.",
                    "traceback": "",
                }],
                "timed_out": False,
                "duration_sec": 0.0,
                "function": "",
                "generated_test_names": [],
                "generated_tests": [],
            }
        last_test = rec.test

        # ── Phase: CRITIC (summary + signature)
        rec.phase = "critic"
        rec.critic = summarize_for_critic(rec.test)
        rec.signature = _critic_signature(rec.run, rec.test)
        last_critic = rec.critic

        # Compose a richer summary for the planner — include runner
        # error when the test phase saw nothing to chew on.
        run_err_for_prompt = ""
        run_status = rec.run.get("status")
        if run_status not in ("success",):
            run_err_for_prompt = (rec.run.get("error") or "").strip()

        # ── Success criterion: tests passed AND smoke-run was clean.
        run_ok = run_status == "success"
        test_ok = bool(rec.test.get("passed")) and \
            int(rec.test.get("total_tests") or 0) > 0
        if run_ok and test_ok:
            rec.phase = "success"
            rec.duration_sec = time.monotonic() - iter_t0
            result.history.append(rec.to_dict())
            result.iterations = i
            result.success = True
            result.final_code = code
            result.stop_reason = "success"
            # Bonus: if we have an open failure lesson from any prior
            # iteration of THIS loop, attach the winning fix to it.
            # Tracker is allocated once at first failure and reused
            # across iterations, so this leaves at most one open row
            # behind on non-success terminations and zero on success.
            if learning is not None and tracker_lesson_id is not None:
                try:
                    learning.record_fix(tracker_lesson_id, code, success=True)
                except Exception as e:
                    logger.debug(f"[dev_loop] record_fix skipped: {e}")
            _safe_callback(on_iteration, rec.to_dict())
            break

        # ── Phase 25: ENV-FIX (opt-in pip install of missing modules)
        # Runs only on a failed iteration AND only when the caller
        # explicitly opted in via ``auto_fix_env=True``.  If we install
        # something the next iteration will retry the same code; the
        # planner is NOT invoked just for a missing-module failure, so
        # we don't burn LLM tokens chasing a packaging bug.
        if auto_fix_env:
            try:
                fix = _attempt_env_fix(
                    rec.run, rec.test,
                    attempted_so_far=env_fix_attempts,
                    already_installed=env_fix_installed,
                )
            except Exception as e:                 # noqa: BLE001
                logger.warning(f"[dev_loop] env-fix raised: "
                               f"{type(e).__name__}: {e}")
                fix = None
            if fix is not None:
                rec.env_fix = fix
                # Only count *real* attempts (loop_cap_reached/skipped
                # don't burn an attempt slot).
                if fix.get("installed"):
                    env_fix_attempts += 1
                    rec.notes.append(
                        f"env_fix: pip install {fix['installed']} -> "
                        f"ok={fix.get('ok')}")

        # ── Phase: LEARNING (record failures, fetch suggestions)
        rec.phase = "learning"
        past_fixes_block = ""
        if learning is not None:
            # Persist failures as code_lessons rows.
            try:
                rec.lessons_recorded = record_failures_to_learning(
                    rec.test, learning,
                    file=resolved_name or "",
                    context=(rec.run or {}).get("error", "")[:1000],
                )
            except Exception as e:
                logger.warning(f"[dev_loop] record_failures_to_learning "
                               f"raised: {type(e).__name__}: {e}")

            # Pick a representative failure to seed similarity lookup.
            errs = rec.test.get("errors") or []
            seed_err = errs[0] if errs else {}
            try:
                past_fixes_block = learning.lookup_for_prompt(
                    error_message=str(seed_err.get("message") or run_err_for_prompt),
                    error_type=str(seed_err.get("error_type") or "")
                              or None,
                )
            except Exception as e:
                logger.warning(f"[dev_loop] lookup_for_prompt raised: "
                               f"{type(e).__name__}: {e}")
                past_fixes_block = ""
            rec.lessons_used = past_fixes_block.count("[") if past_fixes_block else 0

            # Allocate ONE dedicated tracker lesson row on the first
            # failing iteration of this loop so we have something to
            # attach the eventual fix to via record_fix().  On
            # subsequent iterations we reuse the same id — no row
            # leakage on convergence.
            if tracker_lesson_id is None:
                try:
                    tracker_lesson_id = learning.record_failure(
                        error_type=str(seed_err.get("error_type")
                                       or "DevLoopFailure"),
                        error_message=str(seed_err.get("message")
                                          or last_critic),
                        file=resolved_name or "",
                        context=(rec.run or {}).get("error", "")[:1000],
                    )
                except Exception:
                    tracker_lesson_id = None

        # ── Phase 30 Safety: Infinite Loop Detection ──
        if safety_controller:
            err_msg = str(seed_err.get("message") or run_err_for_prompt) if 'seed_err' in locals() else run_err_for_prompt
            loop_err = safety_controller.record_error(err_msg)
            if loop_err:
                rec.notes.append(f"terminated: {loop_err['reason']} - {loop_err['detail']}")
                rec.duration_sec = time.monotonic() - iter_t0
                result.history.append(rec.to_dict())
                result.iterations = i
                result.stop_reason = loop_err["reason"]
                _safe_callback(on_iteration, rec.to_dict())
                break

        # ── No-progress detector
        if stop_on_no_progress and prev_signature is not None \
                and rec.signature == prev_signature:
            
            loop_np = None
            if safety_controller:
                loop_np = safety_controller.record_no_progress()
                
            if loop_np or not safety_controller:
                rec.notes.append("identical critic signature → no progress")
                rec.duration_sec = time.monotonic() - iter_t0
                result.history.append(rec.to_dict())
                result.iterations = i
                result.stop_reason = "loop_detected" if loop_np else "no_progress"
                _safe_callback(on_iteration, rec.to_dict())
                break
        prev_signature = rec.signature

        # ── Final iteration?  Don't replan — just record and stop.
        if i >= max_iterations:
            rec.duration_sec = time.monotonic() - iter_t0
            result.history.append(rec.to_dict())
            result.iterations = i
            result.stop_reason = "max_iterations"
            _safe_callback(on_iteration, rec.to_dict())
            break

        # ── Phase: PLANNER (fix attempt for next iteration)
        rec.phase = "fix"
        if llm is None:
            # Without an LLM we can't replan.  This is the same code
            # path as "stuck on initial_code" — bail cleanly.
            rec.notes.append("no llm available for replanning")
            rec.duration_sec = time.monotonic() - iter_t0
            result.history.append(rec.to_dict())
            result.iterations = i
            result.stop_reason = "no_planner"
            _safe_callback(on_iteration, rec.to_dict())
            break

        prompt = _build_planner_prompt(
            task,
            past_fixes=past_fixes_block,
            current_code=code,
            critic_summary=last_critic,
            run_error=run_err_for_prompt,
            function_name_hint=resolved_name or hint_name,
        )
        # Phase 23: main loop iteration writes code, so use the
        # `coding` role rather than the (cheaper) planner role.
        raw = _call_llm(llm, prompt, system=_PLANNER_SYSTEM, role="coding")
        new_code = _extract_python_code(raw or "")
        if new_code:
            result.llm_used = True
            code = new_code
            # Function name might have changed — re-detect lazily next
            # iteration only if the explicit override is unset.
            if function_name is None:
                detected = _detect_function_name(code, hint=task)
                if detected:
                    resolved_name = detected
        else:
            # LLM was called but returned empty / unusable text.
            # Stop now with a distinct reason so callers can tell this
            # apart from "no LLM at all" (no_planner).
            rec.notes.append("planner returned empty code; stopping")
            rec.duration_sec = time.monotonic() - iter_t0
            result.history.append(rec.to_dict())
            result.iterations = i
            result.stop_reason = "planner_empty"
            _safe_callback(on_iteration, rec.to_dict())
            break

        rec.duration_sec = time.monotonic() - iter_t0
        result.history.append(rec.to_dict())
        _safe_callback(on_iteration, rec.to_dict())

        # ── Wall-clock budget check (END of iteration).  A phase that
        # overshoots the budget mid-iteration must surface as
        # "timeout", not as "max_iterations" or "no_progress" on the
        # next pass.  This is the architect's priority-1 fix.
        if (time.monotonic() - t_start) >= total_timeout_sec:
            result.iterations = i
            result.stop_reason = "timeout"
            break

    # ── Loop exited.  Set fallback fields if a break didn't.
    if result.iterations == 0:
        # Edge case: max_iterations clamped to 1 and we broke very
        # early via timeout before the first record was appended.
        result.iterations = len(result.history)
    if not result.stop_reason:
        # Should be unreachable, but guarantee non-empty for callers.
        result.stop_reason = "completed"
    if not result.final_code:
        result.final_code = code
    result.duration_sec = time.monotonic() - t_start
    # Final guard: if the wall-clock has been blown by ANY path that
    # didn't already set stop_reason="success", override to "timeout"
    # so the budget contract is honoured even on edge cases.
    if result.duration_sec >= total_timeout_sec \
            and result.stop_reason not in ("success", "timeout"):
        result.stop_reason = "timeout"
        
    if tracker:
        tracker.record_task(
            success=result.success,
            exec_time=result.duration_sec,
            provider="dev_loop_llm" if result.llm_used else "none",
            error_type=result.stop_reason if not result.success else None
        )
        summary = {
            "task": task[:50] + "...",
            "status": "success" if result.success else "failure",
            "iterations": result.iterations,
            "commands": safety_controller.current_commands if safety_controller else 0,
            "provider_used": "dev_loop_llm" if result.llm_used else "none",
            "fallback_used": False,
            "termination_reason": result.stop_reason if not result.success else None
        }
        print(f"[Phase 30 Observability] {json.dumps(summary)}")

    return result.to_dict()


def _safe_callback(cb: Optional[Callable[[Dict[str, Any]], None]],
                   payload: Dict[str, Any]) -> None:
    """Invoke an optional progress callback, swallowing any exception
    so callback bugs can never break the loop."""
    if cb is None:
        return
    try:
        cb(payload)
    except Exception as e:
        logger.warning(f"[dev_loop] on_iteration callback raised: "
                       f"{type(e).__name__}: {e}")


# Public alias matching the spec's verb form.
run_dev_loop = autonomous_dev_loop


# ─────────────────────────────────────────────────────────────────────
# CLI — manual smoke / offline testing
# ─────────────────────────────────────────────────────────────────────


def _cli(argv: List[str]) -> int:  # pragma: no cover — manual use only
    """``python3 dev_loop.py --task "..." [--initial-code FILE]
    [--function-name NAME] [--max-iter N]``

    Without an LLM key wired in, requires ``--initial-code`` so the
    runner / tester / critic chain can be exercised offline.
    """
    import argparse
    p = argparse.ArgumentParser(description="Phase 20.5 dev-loop CLI")
    p.add_argument("--task", required=True, help="Task spec for the planner")
    p.add_argument("--initial-code", help="Path to seed-code file (skips planner)")
    p.add_argument("--function-name", help="Override target function name")
    p.add_argument("--max-iter", type=int, default=_DEFAULT_MAX_ITER)
    p.add_argument("--budget-sec", type=float, default=_DEFAULT_TOTAL_TIMEOUT)
    p.add_argument("--no-progress-stop", action="store_true",
                   default=True, help="(default on) bail on identical signature")
    p.add_argument("--allow-no-progress", action="store_true",
                   help="Disable the no-progress short-circuit")
    args = p.parse_args(argv)

    initial = None
    if args.initial_code:
        with open(args.initial_code, "r", encoding="utf-8") as fh:
            initial = fh.read()

    out = autonomous_dev_loop(
        args.task,
        function_name=args.function_name,
        initial_code=initial,
        max_iterations=args.max_iter,
        total_timeout_sec=args.budget_sec,
        stop_on_no_progress=not args.allow_no_progress,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("success") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli(sys.argv[1:]))


__all__ = [
    "autonomous_dev_loop",
    "run_dev_loop",
    "DevLoopResult",
    "IterRecord",
    "LLMLike",
]
