"""Phase 20.3 — Self-Testing System.

Lightweight, dependency-free harness that:

  1. **Generates** test cases for a Python function from its source —
     edge cases, typical inputs, and failure scenarios — by AST-
     introspecting the signature (parameter names, defaults, type
     annotations).
  2. **Executes** them safely in a *separate Python subprocess* with
     a timeout, so a runaway loop or `sys.exit()` in the function
     under test cannot corrupt or hang the host process.
  3. **Returns** a structured result dict that the critic and
     :pymod:`code_learning` can consume directly:

         {
             "passed":       bool,   # True iff every test passed
             "total_tests":  int,
             "failed_tests": int,
             "errors":       list[dict],   # one entry per failure
             "duration_sec": float,
             "timed_out":    bool,
         }

Design notes
------------
* **Why subprocess, not exec()?** The user explicitly forbade
  ``exec``: process isolation gives us a free hard timeout, a clean
  stdout boundary, and the ability to swap in a sandboxed runner in
  Phase 20.4 by replacing one helper (:pyfunc:`_run_subprocess`).

* **Why generate harness code instead of using pytest?** Zero new
  dependencies, deterministic output (a sentinel-tagged JSON line on
  stdout), and we can shape the result schema for downstream consumers
  without parsing pytest's reporter.

* **What "failure scenario" tests assert.** Because we do not have an
  oracle for the *correct* output, generated tests fall into two
  categories:

    - ``expected_exception is None`` → smoke test: passes iff the call
      raises nothing.
    - ``expected_exception == "any"`` → failure test: passes iff *any*
      exception is raised (the function rejected the bad input).

  Callers wanting strict equality assertions can pass their own
  :class:`GeneratedTest` objects with ``call="assert split('a,b') == ['a','b']"``
  and the harness will execute them verbatim — see
  :pyfunc:`run_tests`.

* **Critic / dev-loop integration.** Two helpers,
  :pyfunc:`summarize_for_critic` and
  :pyfunc:`record_failures_to_learning`, are shaped to plug straight
  into the Phase 20.4 autonomous-dev loop without further glue.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────


@dataclass
class GeneratedTest:
    """A single executable test case.

    ``call`` is the raw Python expression/statement that exercises the
    function under test, e.g. ``"split_csv('a,b')"`` or
    ``"assert split_csv('a,b') == ['a','b']"``.

    ``expected_exception`` controls pass/fail semantics:
        * ``None`` — passes iff the call raises nothing (smoke test).
        * ``"any"`` — passes iff *any* exception is raised
          (failure-rejection test).
        * any other string (e.g. ``"TypeError"``) — passes only if an
          exception whose class name matches is raised.
    """
    name: str
    call: str
    expected_exception: Optional[str] = None
    description: str = ""


@dataclass
class TestRunResult:
    """Mutable accumulator used during ``run_tests``; converted to a
    plain dict before returning so callers don't depend on the class."""
    passed: bool = True
    total_tests: int = 0
    failed_tests: int = 0
    errors: list[dict] = field(default_factory=list)
    duration_sec: float = 0.0
    timed_out: bool = False

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total_tests": self.total_tests,
            "failed_tests": self.failed_tests,
            "errors": list(self.errors),
            "duration_sec": round(self.duration_sec, 4),
            "timed_out": self.timed_out,
        }


# ─────────────────────────────────────────────────────────────────────
# Test generation — AST-driven signature introspection
# ─────────────────────────────────────────────────────────────────────


# Edge-value catalogue per annotated type.  Order matters: the most
# "interesting" / failure-prone values come first so a small ``max_tests``
# budget still hits the high-signal cases.  Values are stored as the
# *Python source* that will appear inside the harness call string —
# never as live objects — so the harness file stays self-contained
# and the generator never needs to import the function under test.
_EDGE_VALUES_BY_TYPE: dict[str, tuple[str, ...]] = {
    "int":   ("0", "-1", "1"),
    "float": ("0.0", "-1.0", "1.5"),
    "str":   ("''", "'abc'", "' '"),
    "bytes": ("b''", "b'abc'"),
    "bool":  ("True", "False"),
    "list":  ("[]", "[1, 2, 3]"),
    "tuple": ("()", "(1, 2)"),
    "dict":  ("{}", "{'k': 'v'}"),
    "set":   ("set()", "{1, 2}"),
    "none":  ("None",),
}

# When a parameter has *no* annotation, try a small, polymorphic set.
# Keeps total test count bounded even on un-typed code.
_UNTYPED_FALLBACK: tuple[str, ...] = ("None", "0", "''", "[]")

# Bad-input values to use for failure tests, keyed by the *expected*
# parameter type.  Idea: pick something that's clearly the wrong type.
_FAILURE_VALUES_BY_TYPE: dict[str, str] = {
    "int":   "'not_an_int'",
    "float": "'not_a_float'",
    "str":   "12345",
    "bytes": "12345",
    "bool":  "object()",
    "list":  "12345",
    "tuple": "12345",
    "dict":  "12345",
    "set":   "12345",
}


def _annotation_to_typename(node: Optional[ast.expr]) -> str:
    """Best-effort flatten of an AST annotation to a lowercase typename
    we recognise in :pydata:`_EDGE_VALUES_BY_TYPE`. Returns ``""`` when
    the annotation is missing / too complex to interpret reliably —
    callers then fall back to the polymorphic value set."""
    if node is None:
        return ""
    # Bare names: ``int``, ``str``, ``MyClass``...
    if isinstance(node, ast.Name):
        return node.id.lower()
    # Subscripted generics: ``Optional[int]`` / ``list[str]`` / ``Dict[str, int]``.
    # Strategy: descend into ``Optional[X]`` and ``Union[X, None]`` to
    # surface ``X``; for ``list[X]`` / ``dict[X, Y]`` etc., return the
    # *outer* container name (which is what we have edge values for).
    if isinstance(node, ast.Subscript):
        outer = ""
        if isinstance(node.value, ast.Name):
            outer = node.value.id.lower()
        elif isinstance(node.value, ast.Attribute):
            outer = node.value.attr.lower()
        if outer in {"optional", "union"}:
            # Look inside the brackets for the first non-None type.
            inner = node.slice
            if isinstance(inner, ast.Tuple):
                for elt in inner.elts:
                    name = _annotation_to_typename(elt)
                    if name and name != "none":
                        return name
            else:
                return _annotation_to_typename(inner)
            return ""
        return outer
    # ``str | None`` PEP 604 unions.
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            name = _annotation_to_typename(side)
            if name and name != "none":
                return name
        return ""
    # ``None`` literal annotation (rare but valid in unions).
    if isinstance(node, ast.Constant) and node.value is None:
        return "none"
    return ""


def _extract_target_function(
    source: str,
    function_name: str,
) -> Optional[ast.FunctionDef]:
    """Locate the named function in ``source``.  Walks the whole tree
    so methods inside classes are findable too — useful when callers
    pass an entire module file instead of an isolated function."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        logger.warning(f"_extract_target_function: bad source — {e}")
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                return node  # type: ignore[return-value]
    return None


@dataclass
class _Param:
    """Internal: one parameter of the function under test, including
    enough context to render correct call syntax (kwonly args MUST
    be passed as ``name=value`` or the call raises ``TypeError``)."""
    name: str
    typename: str
    has_default: bool
    kind: str  # "positional" or "kwonly"


def _all_params(fn: ast.FunctionDef) -> list[_Param]:
    """Return every parameter of ``fn`` we want to exercise — both
    positional-or-keyword and posonly *and* kwonly.  ``self``/``cls``
    are skipped; ``*args`` / ``**kwargs`` are ignored (we have no way
    to invent meaningful variadic inputs without an oracle).

    Architect-flagged before: the previous helper only looked at
    ``fn.args.args`` and missed ``posonlyargs`` + ``kwonlyargs``,
    causing generated tests for ``def f(*, x: int)`` to emit ``f()``
    and mark a *correct* function as failing.  Fixed here."""
    params: list[_Param] = []

    # Positional-only + positional-or-keyword share the ``defaults``
    # tuple, which aligns with the *trailing* args.
    pos = list(fn.args.posonlyargs) + list(fn.args.args)
    if pos and pos[0].arg in {"self", "cls"}:
        pos = pos[1:]
    n_defaults = len(fn.args.defaults)
    n_pos = len(pos)
    for i, a in enumerate(pos):
        has_default = (n_pos - i) <= n_defaults
        params.append(_Param(
            name=a.arg,
            typename=_annotation_to_typename(a.annotation),
            has_default=has_default,
            kind="positional",
        ))

    # Keyword-only args use a parallel ``kw_defaults`` list where each
    # slot is either an AST node or ``None`` (no default).
    for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        params.append(_Param(
            name=a.arg,
            typename=_annotation_to_typename(a.annotation),
            has_default=(d is not None),
            kind="kwonly",
        ))
    return params


def _render_call(
    funcname: str,
    params: list[_Param],
    vary_index: int,
    vary_value: str,
    is_async: bool,
) -> str:
    """Render the Python source for a single call, varying parameter
    ``vary_index`` to ``vary_value`` and filling everything else with
    a typical value of its annotated type.

    Kwonly params are always emitted as ``name=value`` so the call is
    syntactically legal.  Async functions are wrapped in
    ``asyncio.run(...)`` so the coroutine actually executes (and any
    runtime exception in the body propagates) instead of being
    silently discarded as an unawaited coroutine."""
    pos_args: list[str] = []
    kw_args: list[str] = []
    for j, p in enumerate(params):
        value = vary_value if j == vary_index else _typical_value(p.typename)
        if p.kind == "kwonly":
            kw_args.append(f"{p.name}={value}")
        else:
            pos_args.append(value)
    inner = f"{funcname}({', '.join(pos_args + kw_args)})"
    # Async wrapping uses the trusted local helper '__async_run__'
    # (a closure-captured reference to asyncio.run) instead of looking
    # up the asyncio module by name.  This is immune to user code
    # monkeypatching asyncio.run from inside their own namespace —
    # see _build_harness for the full trust-isolation design.
    return f"__async_run__({inner})" if is_async else inner


def _candidate_values(typename: str) -> tuple[str, ...]:
    """Edge-value source strings for a given typename, with the
    polymorphic fallback when the type is unknown."""
    if typename in _EDGE_VALUES_BY_TYPE:
        return _EDGE_VALUES_BY_TYPE[typename]
    return _UNTYPED_FALLBACK


def _typical_value(typename: str) -> str:
    """A *single* "looks normal" value for a type — used as filler for
    other parameters when we're varying one parameter at a time, so
    each test isolates one edge case."""
    candidates = _candidate_values(typename)
    # Heuristic: pick the second value if available (the first is
    # usually the most pathological — empty/zero/None — which we want
    # to vary intentionally rather than smuggle into filler slots).
    return candidates[1] if len(candidates) > 1 else candidates[0]


def generate_tests(
    source: str,
    function_name: str,
    *,
    include_smoke: bool = True,
    include_failure: bool = True,
    max_tests: int = 8,
) -> list[GeneratedTest]:
    """Generate ``GeneratedTest`` cases for ``function_name`` in
    ``source``.  Returns ``[]`` if the function is missing or the
    source doesn't parse.  ``max_tests`` caps total cases to keep
    subprocess runtime bounded — smoke tests come first, failure
    tests fill the remaining budget.

    Handles both ``def`` and ``async def`` (calls are wrapped in
    ``asyncio.run(...)`` so the body actually executes — see
    :pyfunc:`_render_call`)."""
    fn = _extract_target_function(source, function_name)
    if fn is None:
        logger.info(f"generate_tests: function '{function_name}' not found")
        return []

    is_async = isinstance(fn, ast.AsyncFunctionDef)
    params = _all_params(fn)
    tests: list[GeneratedTest] = []

    # No-argument function: a single smoke test is the most we can do
    # without an oracle.  Not interesting but completes the contract.
    if not params:
        if include_smoke:
            call = f"{function_name}()"
            if is_async:
                call = f"__async_run__({call})"
            tests.append(GeneratedTest(
                name=f"test_{function_name}_smoke_noargs",
                call=call,
                description="Calls the function with no arguments.",
            ))
        return tests[:max_tests]

    # ── Smoke tests: vary ONE parameter at a time across its edge
    # values, keep the others at a single "typical" value.  This keeps
    # the count linear in (params * edges) instead of combinatorial.
    if include_smoke:
        for i, p in enumerate(params):
            for ev in _candidate_values(p.typename):
                call = _render_call(function_name, params, i, ev, is_async)
                short_ev = ev if len(ev) <= 12 else ev[:10] + "…"
                tests.append(GeneratedTest(
                    name=f"test_{function_name}_smoke_{p.name}_{_slug(ev)}",
                    call=call,
                    description=(
                        f"Smoke test: {p.name}={short_ev} "
                        f"(other args at typical values)."
                    ),
                ))

    # ── Failure tests: pass a deliberately wrong-typed value to each
    # *typed* parameter and expect any exception.  Skip un-typed params
    # (we have no opinion on what "wrong" means for them).
    if include_failure:
        for i, p in enumerate(params):
            bad = _FAILURE_VALUES_BY_TYPE.get(p.typename)
            if bad is None:
                continue
            call = _render_call(function_name, params, i, bad, is_async)
            tests.append(GeneratedTest(
                name=f"test_{function_name}_failure_{p.name}_wrongtype",
                call=call,
                expected_exception="any",
                description=(
                    f"Failure test: {p.name}={bad} (wrong type for "
                    f"annotated '{p.typename}'); expects any exception."
                ),
            ))

    # Dedupe test names: the polymorphic-fallback path can produce
    # the same slug for different values (e.g. '' and [] both slug
    # to "x"), and failure tests share "_wrongtype" within a param.
    # The strict envelope validator rejects duplicate names, so we
    # guarantee uniqueness by appending "_<n>" to the 2nd, 3rd, ...
    # occurrence while leaving the first occurrence untouched.
    counts: dict[str, int] = {}
    for t in tests:
        base = t.name
        n = counts.get(base, 0) + 1
        counts[base] = n
        if n > 1:
            t.name = f"{base}_{n}"

    return tests[:max_tests]


_SLUG_TABLE = str.maketrans({
    " ": "_", "'": "", '"': "", "[": "", "]": "",
    "{": "", "}": "", "(": "", ")": "", ",": "_",
    ":": "_", "-": "neg", ".": "p", "/": "_",
})


def _slug(value: str) -> str:
    """Translate a Python source snippet to something safe for use as
    a test name suffix.  Keeps things readable in the result dict."""
    s = value.strip().translate(_SLUG_TABLE).lower() or "x"
    return s[:24]


# ─────────────────────────────────────────────────────────────────────
# Test execution — subprocess-isolated harness
# ─────────────────────────────────────────────────────────────────────


# Module-level harness preamble.  Kept as a plain (non-f) string so
# braces don't need doubling and indentation is whatever it looks
# like in this file.  ``_build_harness`` glues this between the user's
# source and the per-test ``_run(...)`` lines.
#
# Result transport: the harness writes the JSON result to a dedicated
# file whose path is passed as ``sys.argv[1]``.  We deliberately do
# *not* use a stdout sentinel — architect-flagged previously: user
# code could ``print()`` a fake sentinel line (e.g. via ``atexit``)
# and spoof "passed=True".  An out-of-band file channel removes that
# entire attack surface — the parent never reads stdout for results.
# ---------------------------------------------------------------
# Trust-isolation design (architect-driven):
#
#   1. The entire harness runs *inside* `_harness_main()`.  All
#      sensitive references — json.dump, builtins.open, sys.argv[1],
#      asyncio.run — are captured as LOCALS at function entry,
#      before any user code can run.
#
#   2. User source is exec'd into a SEPARATE namespace dict
#      (`_user_ns`).  User code cannot see, name, or shadow the
#      harness's `_trusted_*` locals via attribute access.
#
#   3. _run evaluates each test call in a per-test globals dict
#      composed of (a copy of) `_user_ns` plus a single trusted
#      helper `__async_run__`.  Mutations a test makes to its eval
#      globals do not propagate to the next test or to harness
#      state.
#
#   4. The final envelope emit uses the captured `_trusted_*` refs
#      directly — even if user code monkeypatches `json.dump`,
#      `builtins.open`, or rebinds `asyncio.run`, our local
#      references are unaffected.
#
#   5. CPython note: `inspect.stack()[n].frame.f_locals` returns a
#      SNAPSHOT for function frames; mutating it does NOT change
#      the actual frame locals.  This blocks the architect's
#      frame-walk monkeypatch attack at the language level.  Full
#      adversarial robustness (ctypes / GC walking) is the explicit
#      job of the Phase 20.4 sandboxed runner.
# ---------------------------------------------------------------
_HARNESS_PREAMBLE = '''\
import asyncio as _asyncio_mod
import builtins as _builtins_mod
import json as _json_mod
import sys as _sys_mod
import traceback as _tb_mod


def _harness_main(_user_source, _expected_names):
    # --- Capture trusted refs as LOCALS (immune to monkeypatching) -
    _trusted_dump      = _json_mod.dump
    _trusted_open      = _builtins_mod.open
    _trusted_path      = _sys_mod.argv[1]
    _trusted_async_run = _asyncio_mod.run

    _results = []
    _user_ns = {}

    # --- Execute user source in an isolated namespace -------------
    try:
        exec(compile(_user_source, "<user_source>", "exec"), _user_ns)
    except BaseException as _src_err:
        # Compile / import failure in user source.  Emit an envelope
        # with zero results — the parent's row-count check will reject
        # it as HarnessCrash with a meaningful diff.
        _envelope = {
            "complete": True,
            "expected_names": _expected_names,
            "results": [],
        }
        with _trusted_open(_trusted_path, "w", encoding="utf-8") as _fh:
            _trusted_dump(_envelope, _fh)
        return

    # --- Per-test runner ------------------------------------------
    def _run(name, call_src, expected_exc=None):
        # Fresh eval globals per test: a shallow copy of user_ns plus
        # the trusted async helper.  A test that monkeypatches its own
        # globals cannot affect later tests.
        _eval_globals = dict(_user_ns)
        _eval_globals["__async_run__"] = _trusted_async_run
        try:
            exec(compile(call_src, "<test:" + name + ">", "exec"),
                 _eval_globals)
        except BaseException as e:
            err_type = type(e).__name__
            err_msg  = str(e)
            err_tb   = _tb_mod.format_exc()
            if expected_exc is None:
                _results.append({
                    "name": name, "passed": False,
                    "error_type": err_type, "message": err_msg,
                    "traceback": err_tb,
                })
            elif expected_exc == "any" or err_type == expected_exc:
                _results.append({
                    "name": name, "passed": True,
                    "error_type": err_type, "message": err_msg,
                    "traceback": "",
                })
            else:
                _results.append({
                    "name": name, "passed": False,
                    "error_type": err_type,
                    "message": ("expected " + str(expected_exc) +
                                " but got " + err_type + ": " + err_msg),
                    "traceback": err_tb,
                })
            return
        if expected_exc is None:
            _results.append({
                "name": name, "passed": True,
                "error_type": "", "message": "", "traceback": "",
            })
        else:
            _results.append({
                "name": name, "passed": False,
                "error_type": "NoExceptionRaised",
                "message": ("expected " + str(expected_exc) +
                            " but no exception was raised"),
                "traceback": "",
            })

'''


def _build_harness(source: str, tests: list[GeneratedTest]) -> str:
    """Render the standalone Python script that will be executed in
    the subprocess.  Structure:

        1. Harness preamble — imports + ``_harness_main`` definition
           (with all trusted refs captured as locals).
        2. INDENTED inside _harness_main: one ``_run(...)`` call per
           generated test, then the final envelope-emit block.
        3. Top-level invocation:  ``_harness_main(SRC, NAMES)``.

    The user's source is passed as a STRING ARGUMENT and exec'd into
    an isolated namespace inside _harness_main.  This is the heart of
    the trust-isolation design — see the comment block above
    _HARNESS_PREAMBLE for the full rationale.
    """
    # Per-test invocations.  Each call expression is embedded as a
    # string and exec()'d inside _run() so a SyntaxError in one test
    # surfaces in *that* row only, not as a harness-level crash.
    indent = "    "
    runner_lines: list[str] = []
    for t in tests:
        expected_repr = (
            repr(t.expected_exception) if t.expected_exception else "None"
        )
        runner_lines.append(
            f"{indent}_run({t.name!r}, {t.call!r}, "
            f"expected_exc={expected_repr})"
        )
    runner_block = "\n".join(runner_lines) if runner_lines else f"{indent}pass"

    expected_names = [t.name for t in tests]
    expected_names_literal = json.dumps(expected_names)

    # Final envelope-emit block — INDENTED into _harness_main.  Uses
    # the captured `_trusted_*` locals, NOT module-level names that
    # the user could monkeypatch.  This block must be the LAST thing
    # _harness_main does so an early _user_ns mutation cannot prevent
    # the genuine envelope from being written.
    emit_block = (
        f"{indent}_envelope = {{\n"
        f'{indent}    "complete": True,\n'
        f'{indent}    "expected_names": {expected_names_literal},\n'
        f'{indent}    "results": _results,\n'
        f"{indent}}}\n"
        f'{indent}with _trusted_open(_trusted_path, "w", '
        f'encoding="utf-8") as _fh:\n'
        f"{indent}    _trusted_dump(_envelope, _fh)\n"
    )

    # Pass user source as a triple-quoted raw string literal.  We
    # escape any embedded triple-quotes via repr() so arbitrary user
    # text round-trips safely.
    user_source_literal = repr(source)

    top_level_call = (
        f"\n_harness_main({user_source_literal}, "
        f"{expected_names_literal})\n"
    )

    return (
        "# ===== harness =====\n"
        + _HARNESS_PREAMBLE
        + runner_block + "\n\n"
        + emit_block
        + top_level_call
    )


def _read_result_envelope(
    path: str,
    expected_names: list[str],
) -> tuple[Optional[list[dict]], str]:
    """Read + validate the harness's completion envelope.

    Returns ``(rows, "")`` on success, or ``(None, reason)`` on any
    integrity failure.  The strict validation defends against
    architect-flagged forgery attempts where untrusted code writes
    its own fake JSON and exits before the harness can complete:

        * File missing / empty                → "missing/empty file"
        * Not a JSON object                   → "not an envelope"
        * Missing ``complete: True``          → "incomplete envelope"
        * ``expected_names`` mismatch         → "name-set mismatch"
        * ``results`` length mismatch         → "row-count mismatch"
        * Row name not in expected set        → "unexpected row name"

    Any of those means the file did not come from the harness's final
    write — the parent treats it as a crash and reports HarnessCrash.
    """
    # Wrap the entire validation in a broad except so an adversarial
    # envelope payload (e.g. unhashable items in expected_names, a
    # name field that is a list/dict, etc.) cannot crash the parent
    # process.  Any exception → invalid envelope → HarnessCrash.
    try:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = fh.read()
            if not payload.strip():
                return None, "result file is empty"
            envelope = json.loads(payload)
        except (OSError, json.JSONDecodeError) as e:
            return None, f"could not read result file: {e}"

        if not isinstance(envelope, dict):
            return None, "result is not a completion envelope (dict)"
        if envelope.get("complete") is not True:
            return None, "envelope missing 'complete: True' marker"

        got_names = envelope.get("expected_names")
        if not isinstance(got_names, list):
            return None, "envelope 'expected_names' is not a list"
        # All names must be hashable strings — else set() crashes.
        if not all(isinstance(n, str) for n in got_names):
            return None, "envelope 'expected_names' contains non-strings"
        if set(got_names) != set(expected_names):
            return None, "expected_names set does not match generated tests"

        rows = envelope.get("results")
        if not isinstance(rows, list):
            return None, "envelope 'results' is not a list"
        if len(rows) != len(expected_names):
            return None, (
                f"row count mismatch: harness wrote {len(rows)}, "
                f"expected {len(expected_names)}"
            )

        expected_set = set(expected_names)
        seen_names: set[str] = set()
        for r in rows:
            if not isinstance(r, dict):
                return None, "envelope contains non-dict row"
            row_name = r.get("name")
            if not isinstance(row_name, str):
                return None, f"row name is not a string: {row_name!r}"
            if row_name not in expected_set:
                return None, f"unexpected row name: {row_name!r}"
            if row_name in seen_names:
                return None, f"duplicate row name: {row_name!r}"
            seen_names.add(row_name)
        # And every expected name must have appeared exactly once.
        if seen_names != expected_set:
            return None, "row names do not cover the expected set"

        return rows, ""
    except Exception as e:  # pragma: no cover — defensive
        return None, f"envelope validation crashed: {type(e).__name__}: {e}"


def _run_subprocess(
    script_path: str,
    result_path: str,
    *,
    timeout: float,
    python: str,
    cwd: Optional[str] = None,
) -> tuple[str, str, bool, int]:
    """Run the harness; return ``(stdout, stderr, timed_out, returncode)``.

    The harness writes its result JSON to ``result_path`` (passed via
    argv[1]) — stdout is *not* a result channel, so user code can
    print whatever it likes without spoofing pass/fail.

    Isolated into its own helper so Phase 20.4's sandboxed runner can
    swap it out (e.g. with a chrooted/jailed implementation) without
    touching anything else in this module.
    """
    try:
        proc = subprocess.run(
            [python, script_path, result_path],
            capture_output=True,
            text=True,
            timeout=max(0.5, float(timeout)),
            cwd=cwd,
            # Strip the parent's PYTHONSTARTUP / -i flags so child
            # startup is deterministic.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return proc.stdout, proc.stderr, False, proc.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") \
            if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") \
            if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
        return out, err, True, -1


def run_tests(
    source: str,
    tests: list[GeneratedTest],
    *,
    timeout: float = 5.0,
    python: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Execute ``tests`` against ``source`` in a separate Python
    subprocess and return the structured result described at the top
    of this module.

    The subprocess inherits the parent's ``sys.executable`` by default
    so generated tests can ``import`` the same standard library and
    third-party packages the rest of the project uses.  Pass
    ``python="..."`` to pin a specific interpreter.

    Defensive guarantees:
        * Always returns a dict (never raises) so the dev loop can
          treat a malformed input as just "all tests failed".
        * On timeout, marks ``timed_out=True`` and reports a single
          synthetic failure so the critic sees a clear signal.
    """
    result = TestRunResult()
    if not tests:
        return result.to_dict()  # passed=True, total=0 — nothing to verify

    python = python or sys.executable
    harness = _build_harness(source, tests)

    # Two temp files: the harness script (.py) and the result channel
    # (.json).  The result file is allocated *empty* up-front so a
    # missing/empty file unambiguously means the subprocess crashed
    # before the harness could write anything.
    fd_py, script_path = tempfile.mkstemp(prefix="codetest_", suffix=".py", text=True)
    fd_rs, result_path = tempfile.mkstemp(prefix="codetest_", suffix=".json", text=True)
    try:
        with os.fdopen(fd_py, "w", encoding="utf-8") as fh:
            fh.write(harness)
        # Close the result file's fd immediately — the child writes to
        # it via path, not fd.  Leaving it open in the parent is
        # harmless but unnecessary.
        os.close(fd_rs)

        t0 = time.monotonic()
        stdout, stderr, timed_out, rc = _run_subprocess(
            script_path, result_path,
            timeout=timeout, python=python, cwd=cwd,
        )
        result.duration_sec = time.monotonic() - t0
        result.timed_out = timed_out

        if timed_out:
            result.total_tests = len(tests)
            result.failed_tests = len(tests)
            result.passed = False
            result.errors.append({
                "name": "<harness>",
                "error_type": "Timeout",
                "message": (
                    f"Subprocess exceeded {timeout:.1f}s timeout; "
                    f"all {len(tests)} test(s) marked failed."
                ),
                "traceback": stderr.strip()[-2000:],
            })
            return result.to_dict()

        expected_names = [t.name for t in tests]
        rows, fail_reason = _read_result_envelope(result_path, expected_names)
        if rows is None:
            # Either the subprocess died before writing the envelope
            # (SyntaxError / ImportError / os._exit) OR it wrote a
            # forged/incomplete envelope.  Both are treated as a hard
            # crash so the dev loop sees a clear "do not trust this"
            # signal — never accidentally pass on bad / spoofed input.
            result.total_tests = len(tests)
            result.failed_tests = len(tests)
            result.passed = False
            result.errors.append({
                "name": "<harness>",
                "error_type": "HarnessCrash",
                "message": (
                    f"Harness exited rc={rc} without a valid "
                    f"completion envelope ({fail_reason}). Likely a "
                    f"syntax/import error, an early exit, or a "
                    f"forged result attempt."
                ),
                "traceback": (stderr or stdout).strip()[-2000:],
            })
            return result.to_dict()

        result.total_tests = len(rows)
        for row in rows:
            if not row.get("passed"):
                result.failed_tests += 1
                result.errors.append({
                    "name": row.get("name", "<unnamed>"),
                    "error_type": row.get("error_type", ""),
                    "message": row.get("message", ""),
                    "traceback": row.get("traceback", "")[-2000:],
                })
        result.passed = (result.failed_tests == 0)
        return result.to_dict()
    finally:
        for path in (script_path, result_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def generate_and_run(
    source: str,
    function_name: str,
    *,
    timeout: float = 5.0,
    include_smoke: bool = True,
    include_failure: bool = True,
    max_tests: int = 8,
    python: Optional[str] = None,
) -> dict:
    """Convenience: generate, then run.  The most common entry point
    for callers that don't want to inspect the generated cases first."""
    tests = generate_tests(
        source, function_name,
        include_smoke=include_smoke,
        include_failure=include_failure,
        max_tests=max_tests,
    )
    out = run_tests(source, tests, timeout=timeout, python=python)
    out["function"] = function_name
    out["generated_tests"] = [
        {"name": t.name, "call": t.call,
         "expected_exception": t.expected_exception,
         "description": t.description}
        for t in tests
    ]
    return out


# ─────────────────────────────────────────────────────────────────────
# Future hooks — critic and code_learning integration
# ─────────────────────────────────────────────────────────────────────


def summarize_for_critic(result: dict) -> str:
    """Render a one-line, prompt-ready summary of a test run.

    Examples:
        ``"All 5 tests passed (0.04s)."``
        ``"3/5 tests failed: TypeError in test_x; AssertionError in test_y."``
        ``"Test harness timed out after 5.0s; result inconclusive."``

    The critic agent is meant to paste this directly into its
    reflection prompt, so it stays plain-text and self-contained.
    """
    if not isinstance(result, dict):
        return "No test result available."
    if result.get("timed_out"):
        return f"Test harness timed out after {result.get('duration_sec', 0):.1f}s; result inconclusive."
    total = int(result.get("total_tests") or 0)
    failed = int(result.get("failed_tests") or 0)
    dur = float(result.get("duration_sec") or 0.0)
    if total == 0:
        return "No tests were generated for this code."
    if failed == 0:
        return f"All {total} test(s) passed ({dur:.2f}s)."
    # Build a short " ; "-joined list of the first few failures so the
    # critic gets concrete error types without an exploding context.
    snippets: list[str] = []
    for err in (result.get("errors") or [])[:3]:
        et = err.get("error_type") or "Error"
        nm = err.get("name") or "<unnamed>"
        snippets.append(f"{et} in {nm}")
    more = ""
    extras = max(0, len(result.get("errors") or []) - 3)
    if extras:
        more = f" (+{extras} more)"
    return (
        f"{failed}/{total} test(s) failed in {dur:.2f}s: "
        f"{'; '.join(snippets)}{more}."
    )


def record_failures_to_learning(
    result: dict,
    learning,                # CodeLearning instance — duck-typed to avoid import
    *,
    file: str = "",
    context: str = "",
) -> int:
    """Persist each failed test as a `code_lessons` row via
    :pymod:`code_learning`.  Returns the number of rows inserted.

    Designed for the Phase 20.4 dev loop: after a fix is attempted,
    re-running tests and calling this helper turns every still-failing
    case into a future-recall lesson.  The fix itself can be attached
    later via ``CodeLearning.record_fix(lesson_id, ...)``.

    The function is duck-typed on ``learning`` so this module never has
    to import :pymod:`code_learning` (avoiding a circular import once
    the dev loop wires both together)."""
    if not isinstance(result, dict) or not result.get("errors"):
        return 0
    n = 0
    for err in result.get("errors") or []:
        try:
            learning.record_failure(
                error_type=str(err.get("error_type") or "TestFailure"),
                error_message=str(err.get("message") or ""),
                file=str(file or ""),
                context=str(context or err.get("traceback") or ""),
            )
            n += 1
        except Exception as e:
            # Silent contract — never let learning persistence break
            # the test runner.
            logger.warning(f"record_failures_to_learning: skip 1 — {e}")
    return n
