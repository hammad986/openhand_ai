"""
code_learning.py — Phase 20.2 Code Learning Memory
====================================================

High-level interface over `memory.code_lessons`. Lets the system:

  1. Record a failed code execution → returns a lesson id.
  2. Attach a successful fix later (by id) → marks success=1.
  3. Look up similar past errors before attempting a new fix and
     get back a ready-to-paste "PAST FIXES" prompt block.

Persistence + raw CRUD live in `memory.py` (table: code_lessons,
methods: insert_code_lesson / update_code_lesson_fix /
list_code_lessons).  This module deliberately stays thin and pure —
no I/O of its own except through the supplied Memory instance.

Why not vector search? The Phase 20.2 spec is explicit: "start
simple, string similarity on error message, no vector DB yet."
We use stdlib `difflib.SequenceMatcher` so there are zero new
dependencies.  When (and if) the project moves to embeddings later,
swap out `_score_similarity` and the rest of the API stays stable.

Quick usage
-----------

    from code_learning import CodeLearning
    cl = CodeLearning(memory)

    # Failure-first pattern (runner / executor)
    lid = cl.record_failure(
        error_type="TypeError",
        error_message="NoneType has no attribute 'strip'",
        file="utils.py",
        context="line 42: token = raw.strip()",
    )

    # …later, after the fix lands…
    cl.record_fix(lid, fix_applied="add `if raw is None: return ''`")

    # One-shot pattern (when caller has both already)
    cl.record_lesson(
        error_type="ImportError",
        error_message="No module named 'foo'",
        file="bar.py",
        fix_applied="pip install foo",
        success=True,
    )

    # Before fixing a new bug — inject knowledge into the LLM prompt
    block = cl.lookup_for_prompt(
        error_message="AttributeError: NoneType object has no attribute 'split'",
        error_type="AttributeError",
    )
    if block:
        prompt = f"{block}\\n\\n{user_prompt}"
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checking only
    from memory import Memory


# ─────────────────────────────────────────────────────────────────────
# Tunables (kept module-level so callers can override per-call)
# ─────────────────────────────────────────────────────────────────────

# How many candidate rows to pull from SQL before scoring.  Pre-filtered
# by error_type when supplied, so this rarely bites.  Higher = more
# recall, lower = faster.
DEFAULT_CANDIDATE_LIMIT = 200

# Minimum similarity score (0..1) for a lesson to be considered
# "relevant enough" to surface.  0.40 is permissive but filters out
# obvious noise; tune with real data.
DEFAULT_MIN_SCORE = 0.40

# Hard cap on lessons rendered into the prompt block — protects the
# context window.  Each rendered lesson costs ~80–200 tokens.
DEFAULT_TOP_K = 3


# ─────────────────────────────────────────────────────────────────────
# Normalisation — make similarity scoring robust to noise
# ─────────────────────────────────────────────────────────────────────

# Patterns that change between otherwise-identical error messages and
# would otherwise punish similarity scores unfairly.  Order matters:
# strip path-shaped tokens *before* line-number patterns so a token
# like `C:\foo\bar.py:42` normalises cleanly in two passes.
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Windows absolute paths (drive-letter prefixed).
    (re.compile(r"\b[a-zA-Z]:\\[^\s:\"']+\.py\b"),  "<path>"),
    # Unix absolute paths.  Negative lookbehind on word chars so a
    # relative path like `src/foo/bar.py` doesn't get partially eaten
    # as `/foo/bar.py` (leaving a stray `src` outside the token);
    # the relative-path rule below handles those instead.
    (re.compile(r'(?<!\w)(?:/[^\s:"\']+)+\.py'),     "<path>"),
    # Relative paths with at least one separator (./foo/bar.py,
    # foo/bar.py, ..\\src\\x.py). Keeps bare `bar.py` mentions intact
    # so module names are still informative for SequenceMatcher.
    (re.compile(r"(?:\.{1,2}[\\/])?(?:[\w\-]+[\\/])+[\w\-]+\.py"),
     "<path>"),
    # Trailing `:NN` (and optional `:CC` column) right after a path
    # token. Run *after* path normalization so `<path>:42:7` → `<path>`.
    (re.compile(r"<path>(?::\d+){1,2}\b"),           "<path>"),
    # Stand-alone `line 42` / `Line 42` references.
    (re.compile(r"\bline\s+\d+", re.IGNORECASE),     "line <n>"),
    # Pointer / memory addresses.
    (re.compile(r"\b0x[0-9a-fA-F]+\b"),              "<hex>"),
    # Any remaining numeric run (catches short ids too — small numbers
    # in error messages are almost never the *meaningful* part).
    (re.compile(r"\b\d+\b"),                         "<num>"),
    # Collapse residual whitespace last so all the substitutions
    # above can leave gaps without changing scores.
    (re.compile(r"\s+"),                             " "),
)

# Short-text guard: SequenceMatcher on very short strings produces
# misleadingly-high scores (two 4-char strings with one common letter
# can land at 0.5+).  We require either side to clear this length
# *after* normalization before trusting the ratio.
_MIN_NORMALIZED_LENGTH = 12


def _normalize(text: str) -> str:
    """Strip volatile noise so two structurally-similar errors score
    high even when paths / line numbers / pointer addresses differ."""
    s = (text or "").strip().lower()
    for pat, repl in _NORMALIZERS:
        s = pat.sub(repl, s)
    return s.strip()


def _score_similarity(a: str, b: str) -> float:
    """Symmetric ratio in [0, 1].  Cheap and dependency-free.

    Returns 0.0 for empty inputs and for very short normalized strings
    (see ``_MIN_NORMALIZED_LENGTH``) to suppress the false-positive
    band where SequenceMatcher overrates short-token overlap."""
    if not a or not b:
        return 0.0
    na, nb = _normalize(a), _normalize(b)
    if len(na) < _MIN_NORMALIZED_LENGTH or len(nb) < _MIN_NORMALIZED_LENGTH:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ─────────────────────────────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────────────────────────────

class CodeLearning:
    """Façade over the code_lessons table.

    Stateless aside from the Memory handle, so it's safe to construct
    once at app start (`CodeLearning(app_memory)`) or on demand.
    """

    def __init__(self, memory: "Memory") -> None:
        self.memory = memory

    # -- recording ----------------------------------------------------

    def record_failure(
        self,
        error_type: str,
        error_message: str,
        file: str = "",
        context: str = "",
    ) -> int | None:
        """Step 2a of the spec: a code-execution attempt failed and we
        don't yet know the fix.  Returns the row id so the caller can
        later call `record_fix(id, ...)` once the fix is verified."""
        return self.memory.insert_code_lesson(
            error_type=error_type,
            error_message=error_message,
            file=file,
            fix_applied="",
            success=False,
            context=context,
        )

    def record_fix(
        self,
        lesson_id: int,
        fix_applied: str,
        success: bool = True,
    ) -> bool:
        """Step 2b: attach a verified fix to a previously-recorded
        failure row.  ``success`` defaults to True because the caller
        only invokes this once they've confirmed the fix worked.

        Returns True if a row was updated, False if the id was bogus
        (caller can fall back to `record_lesson` to insert fresh)."""
        return self.memory.update_code_lesson_fix(
            lesson_id, fix_applied, success=success)

    def record_lesson(
        self,
        error_type: str,
        error_message: str,
        file: str = "",
        fix_applied: str = "",
        success: bool = True,
        context: str = "",
    ) -> int | None:
        """One-shot: insert a complete lesson when caller already has
        both error and fix.  Useful for backfills, manual additions,
        and the critic loop where reflection produces both pieces at
        once."""
        return self.memory.insert_code_lesson(
            error_type=error_type,
            error_message=error_message,
            file=file,
            fix_applied=fix_applied,
            success=success,
            context=context,
        )

    # -- retrieval ----------------------------------------------------

    def find_similar(
        self,
        error_message: str,
        error_type: str | None = None,
        *,
        limit: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        only_successful: bool = True,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> list[dict]:
        """Return the top-K most-similar past lessons, scored
        ``[0..1]`` and sorted descending.  Each dict has every column
        from `code_lessons` plus a ``score`` field.

        Defaults bias toward usefulness: only successful fixes, decent
        similarity floor, K=3 entries.  All thresholds are caller-
        overridable."""
        if not (error_message or "").strip():
            return []

        # Pre-filter in SQL: same error_type if provided + only
        # successful rows (the lessons we'd actually want to copy).
        # Falling back to "no type filter" lets near-misses still
        # surface (e.g. ValueError vs TypeError on the same op).
        candidates = self.memory.list_code_lessons(
            error_type=error_type,
            only_successful=only_successful,
            limit=candidate_limit,
        )
        if error_type and len(candidates) < limit:
            # Backfill from cross-type pool for thin slices.
            extras = self.memory.list_code_lessons(
                error_type=None,
                only_successful=only_successful,
                limit=candidate_limit,
            )
            seen = {c["id"] for c in candidates}
            candidates += [e for e in extras if e["id"] not in seen]

        scored: list[dict] = []
        for row in candidates:
            score = _score_similarity(error_message, row["error_message"])
            if score >= min_score:
                row = dict(row)  # don't mutate the caller's list
                row["score"] = round(score, 3)
                scored.append(row)

        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[: max(1, int(limit))]

    # -- prompt assembly ---------------------------------------------

    @staticmethod
    def format_past_fixes(lessons: list[dict]) -> str:
        """Render scored lessons as a labelled prompt block.  Empty
        string when ``lessons`` is empty so the caller can branch on
        ``if block:`` cleanly."""
        if not lessons:
            return ""
        lines: list[str] = ["### PAST FIXES",
                            "Lessons learned from previous bug fixes — "
                            "consider reusing these patterns when relevant.",
                            ""]
        for i, l in enumerate(lessons, 1):
            score_pct = int(round(float(l.get("score", 0)) * 100))
            err_type = l.get("error_type") or "error"
            err_msg = (l.get("error_message") or "").strip()
            file = l.get("file") or "(no file)"
            fix = (l.get("fix_applied") or "").strip() or "(fix not recorded)"
            # One self-contained block per lesson — keep each short so
            # K=3 lessons fit comfortably in the prompt window.
            lines.append(f"[{i}] {err_type} — {file}  (similarity {score_pct}%)")
            lines.append(f"    error: {err_msg[:200]}")
            lines.append(f"    fix:   {fix[:400]}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def lookup_for_prompt(
        self,
        error_message: str,
        error_type: str | None = None,
        **kwargs,
    ) -> str:
        """Convenience: ``find_similar`` + ``format_past_fixes`` in
        one call.  Returns the empty string when nothing scores high
        enough — exactly what callers want for ``prompt = block + …``
        patterns."""
        return self.format_past_fixes(
            self.find_similar(error_message, error_type, **kwargs))


__all__ = ["CodeLearning"]
