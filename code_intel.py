"""
code_intel.py — Phase 20.1 Code Intelligence Engine
=====================================================

Read-only static analysis layer for the Multi-Agent AI Dev System.
Gives the agent a structural understanding of its own codebase before
any editing or execution features are added (those are Phases 20.4+).

What this module does
---------------------
1. **AST analysis** of a single Python file — extracts top-level
   functions, classes (with their methods), imports, decorators,
   docstrings and line ranges.
2. **Codebase indexing** — walks a project root, analyses every `.py`
   file, returns ``{relative_path: file_analysis}``.  Honors a
   reasonable default ignore list (``__pycache__``, ``.git``,
   ``venv`` / ``.venv``, ``node_modules``, ``attached_assets``,
   ``workspace``, ``memory_vectors``, ``templates``).
3. **Dependency graph** — for every indexed file, resolves which
   *other indexed files* it imports (intra-project edges only — no
   stdlib / third-party noise) and produces both a forward graph
   (``imports``) and reverse graph (``imported_by``).
4. **Symbol lookup helpers** — ``find_symbol`` and ``find_usages``
   for "where is X defined / referenced?" queries.
5. **Disk cache** — ``code_intel_cache.json`` keyed by file mtime so
   repeated calls are O(changed-files) instead of O(repo).

What this module deliberately does NOT do
-----------------------------------------
* No file edits, no code generation, no execution. This is the
  read-only foundation; the autonomous dev loop (20.5) and sandboxed
  runner (20.4) will build on top of it.
* No web routes, no UI hooks, no changes to existing modules. It can
  be imported and used by anything (CLI, REPL, future API), but it
  does not register itself anywhere on import.

Quick usage
-----------
    from code_intel import build_index, build_dependency_graph, summary

    idx = build_index(".")
    dep = build_dependency_graph(idx)
    print(summary(idx, dep))

CLI
---
Running this module directly produces a JSON report of the current
project for inspection / piping into ``jq``::

    python3 code_intel.py                 # summary + top deps
    python3 code_intel.py --full          # full index + graph
    python3 code_intel.py --file foo.py   # one-file analysis
    python3 code_intel.py --symbol Memory # locate a symbol
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from typing import Iterable

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

# Directories never worth analysing — saves time and avoids parsing
# vendored / generated / binary-blob trees.
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache",
    "attached_assets", "memory_vectors", "workspace", "templates",
    ".local", ".cache",
})

# Files we skip even when their extension matches.
DEFAULT_IGNORE_FILE_PREFIXES: tuple[str, ...] = (
    "_fix_",       # one-off repair scripts
    "_verify_",    # one-off verification scripts
)

CACHE_FILENAME = "code_intel_cache.json"
CACHE_VERSION = 1   # bump when the analysis schema changes


# ─────────────────────────────────────────────────────────────────────
# Single-file analysis
# ─────────────────────────────────────────────────────────────────────

def _safe_get_docstring(node: ast.AST) -> str:
    """ast.get_docstring with a guard for nodes that don't accept it."""
    try:
        ds = ast.get_docstring(node)
        return (ds or "").strip().splitlines()[0] if ds else ""
    except Exception:
        return ""


def _decorator_name(dec: ast.AST) -> str:
    """Render a decorator AST node back into a readable string.

    We only need a best-effort label (for display / search), not a
    perfectly faithful reconstruction.
    """
    try:
        if isinstance(dec, ast.Name):
            return dec.id
        if isinstance(dec, ast.Attribute):
            return f"{_decorator_name(dec.value)}.{dec.attr}"
        if isinstance(dec, ast.Call):
            return _decorator_name(dec.func)
        return ast.unparse(dec) if hasattr(ast, "unparse") else dec.__class__.__name__
    except Exception:
        return "<decorator>"


def _function_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a compact `name(arg1, arg2, ...)` string for display."""
    try:
        args = [a.arg for a in fn.args.args]
        if fn.args.vararg:
            args.append("*" + fn.args.vararg.arg)
        if fn.args.kwarg:
            args.append("**" + fn.args.kwarg.arg)
        return f"{fn.name}({', '.join(args)})"
    except Exception:
        return f"{fn.name}(...)"


def _walk_imports(tree: ast.Module) -> list[dict]:
    """Collect every Import / ImportFrom in a module.

    Returns a list of ``{module, names, level, line}`` dicts.  ``level``
    is the relative-import depth (0 for absolute imports).
    """
    out: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append({
                    "module": alias.name,
                    "names": [alias.asname or alias.name.split(".")[0]],
                    "level": 0,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            out.append({
                "module": node.module or "",
                "names": [a.asname or a.name for a in node.names],
                "level": node.level or 0,
                "line": node.lineno,
            })
    return out


def analyze_file(file_path: str) -> dict:
    """Parse one Python file and return a structured analysis.

    Schema::

        {
          "path": "memory.py",
          "lines": 2843,
          "ok": True,
          "error": None,
          "module_doc": "first line of module docstring",
          "imports": [ {module, names, level, line}, ... ],
          "functions": [
              {name, signature, line, end_line, decorators, doc, is_async}
          ],
          "classes": [
              {name, line, end_line, bases, decorators, doc,
               methods: [ {name, signature, line, decorators, doc, is_async} ]}
          ],
        }

    On parse failure returns ``{"ok": False, "error": "..."}`` plus the
    path/line count so the caller can still locate the file.
    """
    result: dict = {
        "path": file_path,
        "lines": 0,
        "ok": False,
        "error": None,
        "module_doc": "",
        "imports": [],
        "functions": [],
        "classes": [],
    }
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        result["lines"] = source.count("\n") + 1
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        result["error"] = f"SyntaxError: {e.msg} (line {e.lineno})"
        return result
    except OSError as e:
        result["error"] = f"OSError: {e}"
        return result
    except Exception as e:  # defensive — never let one bad file kill the run
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["module_doc"] = _safe_get_docstring(tree)
    result["imports"] = _walk_imports(tree)

    # Top-level functions / classes only — nested defs are reported
    # under their parent class (methods) but free nested functions
    # inside other functions are intentionally omitted to keep the
    # index focused on the public surface.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result["functions"].append({
                "name": node.name,
                "signature": _function_signature(node),
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "decorators": [_decorator_name(d) for d in node.decorator_list],
                "doc": _safe_get_docstring(node),
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })
        elif isinstance(node, ast.ClassDef):
            methods: list[dict] = []
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        "name": sub.name,
                        "signature": _function_signature(sub),
                        "line": sub.lineno,
                        "decorators": [_decorator_name(d) for d in sub.decorator_list],
                        "doc": _safe_get_docstring(sub),
                        "is_async": isinstance(sub, ast.AsyncFunctionDef),
                    })
            result["classes"].append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "bases": [_decorator_name(b) for b in node.bases],
                "decorators": [_decorator_name(d) for d in node.decorator_list],
                "doc": _safe_get_docstring(node),
                "methods": methods,
            })

    result["ok"] = True
    return result


# ─────────────────────────────────────────────────────────────────────
# Whole-project indexing
# ─────────────────────────────────────────────────────────────────────

def _iter_python_files(
    root: str,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
    ignore_file_prefixes: tuple[str, ...] = DEFAULT_IGNORE_FILE_PREFIXES,
) -> Iterable[str]:
    """Yield absolute paths to every .py file we want to analyse."""
    ignore_set = set(ignore_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        # In-place prune so os.walk doesn't descend into ignored dirs.
        dirnames[:] = [d for d in dirnames
                       if d not in ignore_set and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if any(fn.startswith(p) for p in ignore_file_prefixes):
                continue
            yield os.path.join(dirpath, fn)


def _load_cache(cache_path: str) -> dict:
    """Load the on-disk cache or return an empty stub."""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version") == CACHE_VERSION:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": CACHE_VERSION, "files": {}}


def _save_cache(cache_path: str, cache: dict) -> None:
    """Best-effort cache write — failures are non-fatal."""
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
    except OSError:
        pass


def build_index(
    root: str = ".",
    *,
    use_cache: bool = True,
    ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS,
) -> dict[str, dict]:
    """Analyse every .py file under ``root`` and return ``{rel_path: analysis}``.

    Uses a mtime-keyed cache (``code_intel_cache.json``) so re-running
    on an unchanged tree is essentially free.
    """
    root_abs = os.path.abspath(root)
    cache_path = os.path.join(root_abs, CACHE_FILENAME)
    cache = _load_cache(cache_path) if use_cache else {"version": CACHE_VERSION, "files": {}}

    index: dict[str, dict] = {}
    fresh: dict[str, dict] = {}

    for abs_path in _iter_python_files(root_abs, ignore_dirs=ignore_dirs):
        rel = os.path.relpath(abs_path, root_abs)
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            continue

        cached = cache["files"].get(rel)
        if use_cache and cached and cached.get("_mtime") == mtime:
            entry = cached.get("data")
            if entry:
                index[rel] = entry
                fresh[rel] = cached
                continue

        analysis = analyze_file(abs_path)
        # Record under the relative path so cache/lookup is portable.
        analysis["path"] = rel
        index[rel] = analysis
        fresh[rel] = {"_mtime": mtime, "data": analysis}

    if use_cache:
        cache["files"] = fresh
        _save_cache(cache_path, cache)

    return index


# ─────────────────────────────────────────────────────────────────────
# Dependency graph
# ─────────────────────────────────────────────────────────────────────

def _module_to_relpaths(index: dict[str, dict]) -> dict[str, str]:
    """Map a Python module path (``foo.bar``) to its file's rel path.

    For top-level files, ``memory.py`` becomes module ``memory``.  For
    package files, ``pkg/sub/mod.py`` becomes ``pkg.sub.mod`` and the
    package itself (``pkg/sub/__init__.py``) becomes ``pkg.sub``.
    """
    out: dict[str, str] = {}
    for rel in index:
        no_ext = rel[:-3] if rel.endswith(".py") else rel
        parts = no_ext.replace("\\", "/").split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out[".".join(parts)] = rel
    return out


def _package_of(rel_path: str) -> str:
    """Return the dotted package name that contains ``rel_path``.

    Used as the base for resolving relative imports.  ``foo/bar/baz.py``
    lives in package ``foo.bar``; ``baz.py`` at the root lives in the
    empty (top-level) package.  ``__init__.py`` is treated as living
    inside its own package, not below it (matching Python semantics).
    """
    no_ext = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    parts = no_ext.replace("\\", "/").split("/")
    # __init__.py represents its own package, so its package context
    # IS the directory it sits in (drop "__init__" only).
    if parts and parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _resolve_relative(level: int, module: str, source_pkg: str) -> str | None:
    """Resolve ``from <relative> import ...`` to an absolute module name.

    Mirrors Python's import machinery: starting from ``source_pkg``,
    drop ``level - 1`` trailing components, then append ``module`` if
    given.  Returns ``None`` on under-flow (relative import escaping
    the project root).
    """
    if level <= 0:
        return module or None
    pkg_parts = source_pkg.split(".") if source_pkg else []
    # `level=1` means "this package", `level=2` means "parent", etc.
    drop = level - 1
    if drop > len(pkg_parts):
        return None  # escapes the root — can't resolve
    base = pkg_parts[: len(pkg_parts) - drop] if drop else pkg_parts
    if module:
        base = base + module.split(".")
    return ".".join(base) if base else None


def build_dependency_graph(index: dict[str, dict]) -> dict[str, dict]:
    """Build forward + reverse dependency graphs, scoped to indexed files.

    Returns::

        {
          "imports":     {rel_path: [other_rel_path, ...]},   # forward
          "imported_by": {rel_path: [other_rel_path, ...]},   # reverse
          "external":    {rel_path: [stdlib_or_third_party]}, # informational
        }

    Only edges between files that exist in ``index`` are recorded — we
    don't claim ``stdlib`` or third-party imports are "dependencies"
    of the project for graph purposes, but we surface them under
    ``external`` so the caller can still see them.

    Resolution rules (Phase 20.1 fix following architect review):
      * ``ImportFrom.level > 0`` (relative imports) are resolved
        against the source file's package context — previously they
        were silently dropped.
      * For ``from a.b import c, d``, we first try to resolve each
        imported name as a submodule (``a.b.c``, ``a.b.d``) so
        package-style intra-project edges are not lost.
      * Only as a *secondary* heuristic do we shrink the dotted
        path (``a.b`` → ``a``) — kept for the case where someone
        imports a name *defined inside* a parent module.
    """
    mod_to_path = _module_to_relpaths(index)
    forward: dict[str, set[str]] = {rel: set() for rel in index}
    external: dict[str, set[str]] = {rel: set() for rel in index}

    for rel, analysis in index.items():
        source_pkg = _package_of(rel)
        for imp in analysis.get("imports", []):
            level = int(imp.get("level") or 0)
            mod = imp.get("module") or ""

            # Step 1 — resolve the dotted module path to absolute form.
            # `import x` and `from x import y` are level=0; relative
            # imports get rewritten against the source's package.
            if level > 0:
                abs_mod = _resolve_relative(level, mod, source_pkg)
                if abs_mod is None:
                    continue  # escaped root — can't be intra-project
            else:
                abs_mod = mod
            if not abs_mod:
                continue

            # Step 2 — try to match a real file in the index.
            matched: str | None = None

            # 2a. Submodule-of-imported-name match. `from pkg.sub
            #     import a, b` may mean a/b are submodules of pkg.sub
            #     (very common for package layouts). Try those first.
            if level > 0 or mod:  # only meaningful for ImportFrom
                for name in imp.get("names", []):
                    if name == "*" or not name:
                        continue
                    cand = f"{abs_mod}.{name}" if abs_mod else name
                    if cand in mod_to_path:
                        target = mod_to_path[cand]
                        if target != rel:
                            forward[rel].add(target)
                            matched = target  # at least one hit

            # 2b. Direct module match (covers `import x` and the
            #     ImportFrom case where the module itself is the file).
            if abs_mod in mod_to_path:
                target = mod_to_path[abs_mod]
                if target != rel:
                    forward[rel].add(target)
                    matched = target

            # 2c. Parent-module fallback — `from a.b.c import x` where
            #     x is a *name inside* a/b.py (not a separate file).
            #     Kept as a last resort because it can over-collapse.
            if matched is None:
                parts = abs_mod.split(".")
                for i in range(len(parts) - 1, 0, -1):
                    cand = ".".join(parts[:i])
                    if cand in mod_to_path:
                        target = mod_to_path[cand]
                        if target != rel:
                            forward[rel].add(target)
                            matched = target
                        break

            if matched is None:
                external[rel].add(abs_mod)

    reverse: dict[str, set[str]] = {rel: set() for rel in index}
    for src, dsts in forward.items():
        for dst in dsts:
            reverse[dst].add(src)

    return {
        "imports":     {k: sorted(v) for k, v in forward.items()},
        "imported_by": {k: sorted(v) for k, v in reverse.items()},
        "external":    {k: sorted(v) for k, v in external.items()},
    }


# ─────────────────────────────────────────────────────────────────────
# Symbol queries
# ─────────────────────────────────────────────────────────────────────

def find_symbol(index: dict[str, dict], name: str) -> list[dict]:
    """Return every place ``name`` is *defined* (function, class, method)."""
    hits: list[dict] = []
    for rel, analysis in index.items():
        for fn in analysis.get("functions", []):
            if fn["name"] == name:
                hits.append({"file": rel, "kind": "function",
                             "line": fn["line"], "signature": fn["signature"]})
        for cls in analysis.get("classes", []):
            if cls["name"] == name:
                hits.append({"file": rel, "kind": "class",
                             "line": cls["line"], "bases": cls["bases"]})
            for m in cls.get("methods", []):
                if m["name"] == name:
                    hits.append({"file": rel, "kind": "method",
                                 "class": cls["name"], "line": m["line"],
                                 "signature": m["signature"]})
    return hits


def find_usages(root: str, name: str,
                ignore_dirs: Iterable[str] = DEFAULT_IGNORE_DIRS) -> list[dict]:
    """Best-effort grep-by-AST: every file where ``name`` appears as a
    Name or Attribute reference.  Returns ``[{file, line, context}, ...]``.

    This is intentionally simple — full call-site resolution would
    require type inference, which is out of scope for 20.1.
    """
    hits: list[dict] = []
    root_abs = os.path.abspath(root)
    for abs_path in _iter_python_files(root_abs, ignore_dirs=ignore_dirs):
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=abs_path)
        except Exception:
            continue
        rel = os.path.relpath(abs_path, root_abs)
        lines = source.splitlines()
        for node in ast.walk(tree):
            matched = False
            if isinstance(node, ast.Name) and node.id == name:
                matched = True
            elif isinstance(node, ast.Attribute) and node.attr == name:
                matched = True
            if matched:
                ln = getattr(node, "lineno", 0)
                ctx = lines[ln - 1].strip() if 0 < ln <= len(lines) else ""
                hits.append({"file": rel, "line": ln, "context": ctx})
    return hits


# ─────────────────────────────────────────────────────────────────────
# Summaries
# ─────────────────────────────────────────────────────────────────────

def summary(index: dict[str, dict],
            graph: dict[str, dict] | None = None) -> dict:
    """High-level project stats useful for dashboards / debug logs."""
    files = list(index.values())
    ok_files = [f for f in files if f.get("ok")]
    n_funcs = sum(len(f.get("functions", [])) for f in ok_files)
    n_classes = sum(len(f.get("classes", [])) for f in ok_files)
    n_methods = sum(len(c.get("methods", []))
                    for f in ok_files for c in f.get("classes", []))
    n_lines = sum(int(f.get("lines", 0)) for f in ok_files)

    out: dict = {
        "files": len(files),
        "files_ok": len(ok_files),
        "files_failed": len(files) - len(ok_files),
        "lines_total": n_lines,
        "functions_total": n_funcs,
        "classes_total": n_classes,
        "methods_total": n_methods,
        "largest_files": sorted(
            [{"file": f["path"], "lines": f.get("lines", 0)} for f in ok_files],
            key=lambda x: x["lines"], reverse=True,
        )[:5],
        "parse_errors": [
            {"file": f["path"], "error": f.get("error")}
            for f in files if not f.get("ok")
        ],
    }

    if graph:
        # Most-depended-on files = good "core module" indicator.
        in_degree = sorted(
            [(k, len(v)) for k, v in graph["imported_by"].items()],
            key=lambda x: x[1], reverse=True,
        )
        out["most_imported"] = [{"file": k, "imported_by_count": n}
                                for k, n in in_degree[:5] if n > 0]
    return out


# ─────────────────────────────────────────────────────────────────────
# CLI entry point — pure JSON to stdout for easy piping
# ─────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    args = set(argv)
    t0 = time.time()

    if "--file" in argv:
        i = argv.index("--file")
        if i + 1 >= len(argv):
            print("usage: --file PATH", file=sys.stderr)
            return 2
        result = analyze_file(argv[i + 1])
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0

    if "--symbol" in argv:
        i = argv.index("--symbol")
        if i + 1 >= len(argv):
            print("usage: --symbol NAME", file=sys.stderr)
            return 2
        idx = build_index(".")
        json.dump(find_symbol(idx, argv[i + 1]), sys.stdout, indent=2)
        print()
        return 0

    idx = build_index(".")
    graph = build_dependency_graph(idx)
    if "--full" in args:
        json.dump({"index": idx, "graph": graph,
                   "summary": summary(idx, graph),
                   "elapsed_ms": int((time.time() - t0) * 1000)},
                  sys.stdout, indent=2)
    else:
        json.dump({"summary": summary(idx, graph),
                   "elapsed_ms": int((time.time() - t0) * 1000)},
                  sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
