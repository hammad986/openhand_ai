"""goal_engine.py — Phase 19: Self-driven goals & proactive intelligence.

The engine is intentionally *additive*: it sits next to the existing
TaskChainRunner and Memory rather than rewriting them. It produces new
chains tagged `system_generated=1` and persists `goal_*` lifecycle
events so the existing UI / SSE pipeline can surface them.

Design contract
───────────────
1. Trigger surface is small: callers invoke `maybe_trigger(reason)`
   from places that already have a Memory / runner handle (the chain
   completion hook in web_app.py). Triggers run on a daemon thread
   so they never block a request.
2. Safety stack — every trigger walks the same gate sequence:
       enabled → cooldown → daily cap → analyze → confidence →
       loop guard (don't trigger on system chain completion) → execute
3. Generated chains use **importance=2** (low) so user tasks always
   outrank them in `next_pending_task` ORDER BY priority. The chain
   runner's own scheduler then handles execution; the engine never
   touches that loop.
4. Configuration lives in the existing `default_config` settings dict
   under the `auto_goals` key (validated in web_app.validate_config).
   Defaults err on the side of safety: disabled, low cap, slow
   cooldown, high confidence floor.

The engine never raises into the caller — every public method either
returns a structured result or `None`.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional


# Defaults applied when the settings dict is missing keys. Mirrors the
# shape used in web_app.default_managed_config() so a half-populated
# config still works.
DEFAULTS = {
    "enabled":          False,
    "max_per_day":      3,
    "min_confidence":   0.6,
    "cooldown_seconds": 1800,   # 30 min
    "max_per_run":      1,      # never spawn more than 1 chain per cycle
    "importance":       2,      # below user tasks (default 5)
    "window_seconds":   86400,  # look back 24h for failure signals
    "min_signal_count": 3,      # need ≥3 occurrences before acting
}


def _coerce_settings(raw: dict | None) -> dict:
    """Merge `raw` over DEFAULTS with light type coercion. Always
    returns a complete dict so callers don't have to .get() every key."""
    out = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in out:
            continue
        try:
            if isinstance(out[k], bool):
                out[k] = bool(v)
            elif isinstance(out[k], int):
                out[k] = int(v)
            elif isinstance(out[k], float):
                out[k] = float(v)
            else:
                out[k] = v
        except (TypeError, ValueError):
            continue
    # Clamp ranges to sane bounds so a malformed config can't disable
    # the safety stack.
    out["max_per_day"]      = max(0, min(int(out["max_per_day"]), 50))
    out["min_confidence"]   = max(0.0, min(float(out["min_confidence"]), 1.0))
    out["cooldown_seconds"] = max(60, int(out["cooldown_seconds"]))
    out["max_per_run"]      = max(1, min(int(out["max_per_run"]), 5))
    out["importance"]       = max(1, min(int(out["importance"]), 10))
    out["window_seconds"]   = max(300, int(out["window_seconds"]))
    out["min_signal_count"] = max(1, int(out["min_signal_count"]))
    return out


def _today_midnight_str() -> str:
    """SQLite text date for 00:00:00 today (local) — used by the
    daily-cap query."""
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0,
                       microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _phrase_goal(signal: dict) -> str:
    """Turn a raw signal dict from `Memory.failure_signals` into a
    concrete improvement goal the planner can decompose. Kept short
    and prescriptive — the planner gets to fill in the steps."""
    err = (signal.get("key") or "generic").strip() or "generic"
    sample = (signal.get("sample_subtask") or "").strip()
    n = int(signal.get("count", 0))
    base = (f"Investigate the recurring '{err}' failure that has "
            f"happened {n} times in the last 24 hours. "
            f"Identify the root cause, propose a concrete fix, "
            f"and record the resolution as a learning so future "
            f"runs avoid the same failure.")
    if sample:
        base += f" Most recent failing subtask: \"{sample[:160]}\"."
    return base


class GoalEngine:
    """Stateless analyzer + thin scheduler. All persistence lives in
    Memory; cooldown is in-process (resets on restart, which is
    acceptable — restart implies a clean slate)."""

    def __init__(self,
                 runner,                       # TaskChainRunner
                 memory,                       # Memory
                 settings_loader: Callable[[], dict],
                 emit_event: Optional[Callable[[str, int | None, dict], None]] = None):
        self.runner   = runner
        self.memory   = memory
        self._load    = settings_loader
        # emit_event(kind, chain_id, payload) — persists to goal_events
        # AND surfaces on whatever live channel the caller wires up.
        self._emit_ev = emit_event or (lambda k, c, p: None)
        self._lock      = threading.Lock()
        self._last_run  = 0.0   # monotonic-ish wall-clock seconds
        self._last_skip = ""    # human-readable last-skip reason (debug)

    # ────────────────────────────────────────────────────────────
    # Public entry
    # ────────────────────────────────────────────────────────────
    def maybe_trigger(self, reason: str,
                      parent_chain_id: int | None = None,
                      parent_system_generated: bool = False) -> dict:
        """Synchronous gate-walk + (if all gates pass) generate goals.
        Returns a structured result dict so the caller can log /
        surface what happened. Never raises.

        `parent_system_generated` is the loop guard: if the chain that
        just completed was itself goal-engine-produced, we skip — we
        do NOT want goals to spawn meta-goals about themselves."""
        result = {"ok": False, "reason": "", "spawned": []}
        try:
            cfg = _coerce_settings(self._load() or {})

            if not cfg["enabled"]:
                result["reason"] = "auto_goals disabled"
                self._last_skip = result["reason"]
                return result

            if parent_system_generated:
                # ── Safety: hard loop guard ──
                result["reason"] = "skip: parent chain is system_generated"
                self._last_skip = result["reason"]
                return result

            now = time.time()
            if (now - self._last_run) < cfg["cooldown_seconds"]:
                remaining = int(cfg["cooldown_seconds"] - (now - self._last_run))
                result["reason"] = f"cooldown: {remaining}s remaining"
                self._last_skip = result["reason"]
                return result

            # ── Daily cap ──
            today_used = self.memory.count_system_chains_since(
                _today_midnight_str())
            if today_used >= cfg["max_per_day"]:
                result["reason"] = (f"daily cap reached "
                                    f"({today_used}/{cfg['max_per_day']})")
                self._last_skip = result["reason"]
                return result

            # ── One trigger at a time ──
            if not self._lock.acquire(blocking=False):
                result["reason"] = "another trigger is in flight"
                self._last_skip = result["reason"]
                return result
            try:
                signals = self._analyze(cfg)
                if not signals:
                    self._last_run = now   # consume cooldown anyway
                    result["reason"] = "no actionable signals"
                    self._last_skip = result["reason"]
                    return result

                # Apply confidence floor + per-run cap
                top = [s for s in signals
                       if s["confidence"] >= cfg["min_confidence"]]
                if not top:
                    self._last_run = now
                    result["reason"] = (f"no signal met confidence "
                                        f">= {cfg['min_confidence']}")
                    self._last_skip = result["reason"]
                    return result

                budget = min(cfg["max_per_run"],
                             cfg["max_per_day"] - today_used)
                spawned = []
                for sig in top[:budget]:
                    chain_info = self._spawn_chain(sig, cfg, reason,
                                                   parent_chain_id)
                    if chain_info:
                        spawned.append(chain_info)
                self._last_run = now
                result["ok"] = bool(spawned)
                result["reason"] = (f"spawned {len(spawned)} chain(s) "
                                    f"from {len(top)} candidate(s) "
                                    f"(trigger={reason})")
                result["spawned"] = spawned
                return result
            finally:
                self._lock.release()
        except Exception as e:
            result["reason"] = f"engine error: {e}"
            return result

    # ────────────────────────────────────────────────────────────
    # Analyze
    # ────────────────────────────────────────────────────────────
    def _analyze(self, cfg: dict) -> list[dict]:
        """Pull signals from Memory and rank them by confidence DESC.
        Today this is just `failure_signals`; future signal kinds
        (slow_workflow, low_success_tool, knowledge_gap) plug in here
        without changing the gate logic above."""
        signals: list[dict] = []
        try:
            signals.extend(self.memory.failure_signals(
                window_seconds=cfg["window_seconds"],
                min_count=cfg["min_signal_count"]))
        except Exception:
            pass
        # Stable sort by confidence DESC, then count DESC.
        signals.sort(key=lambda s: (-float(s.get("confidence", 0.0)),
                                    -int(s.get("count", 0))))
        return signals

    # ────────────────────────────────────────────────────────────
    # Spawn
    # ────────────────────────────────────────────────────────────
    def _spawn_chain(self, signal: dict, cfg: dict,
                     trigger_reason: str,
                     parent_chain_id: int | None) -> dict | None:
        """Create a system-generated chain for one signal. The chain
        runner's own decomposition handles sub-task planning — we
        just hand it the goal text. Returns a small dict for the
        result payload, or None if creation failed."""
        goal_text = _phrase_goal(signal)
        confidence = float(signal.get("confidence", 0.0))
        auto_source = f"{signal.get('kind','signal')}:{signal.get('key','')}"
        try:
            info = self.runner.create_chain(
                goal_text,
                importance=cfg["importance"],
                system_generated=True,
                confidence=confidence,
                auto_source=auto_source,
            )
        except Exception as e:
            self._emit_ev("goal_skipped", None, {
                "reason": f"create_chain raised: {e}",
                "signal": signal,
            })
            return None
        cid = info.get("chain_id") if isinstance(info, dict) else None
        if cid is None:
            return None
        payload = {
            "chain_id":     cid,
            "goal":         goal_text,
            "importance":   cfg["importance"],
            "confidence":   round(confidence, 3),
            "auto_source":  auto_source,
            "trigger":      trigger_reason,
            "parent_chain": parent_chain_id,
            "tasks":        len(info.get("tasks") or []),
        }
        # Persistent event + live emission (handler decides where).
        self._emit_ev("goal_generated", cid, payload)
        return payload

    # ────────────────────────────────────────────────────────────
    # Manual force-run
    # ────────────────────────────────────────────────────────────
    def force_run_once(self) -> dict:
        """Bypass cooldown for exactly one cycle. Daily cap, confidence
        threshold, and the loop guard still apply. Thread-safe — uses
        the same `_lock` the regular trigger holds, so concurrent
        callers never produce more than one cycle's worth of chains."""
        # Take the lock first so we don't race with an in-flight
        # trigger that's about to update _last_run too.
        with self._lock:
            self._last_run = 0.0
        return self.maybe_trigger("manual_run_now")

    # ────────────────────────────────────────────────────────────
    # Diagnostics
    # ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        """Snapshot of the engine for the UI / debug endpoint."""
        cfg = _coerce_settings(self._load() or {})
        used = self.memory.count_system_chains_since(_today_midnight_str())
        cooldown_left = max(0, int(cfg["cooldown_seconds"]
                                   - (time.time() - self._last_run)))
        return {
            "enabled":          cfg["enabled"],
            "settings":         cfg,
            "today_used":       used,
            "today_remaining":  max(0, cfg["max_per_day"] - used),
            "cooldown_remaining_seconds": cooldown_left,
            "last_skip_reason": self._last_skip,
        }
