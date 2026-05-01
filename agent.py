"""
agent.py - Autonomous Coding Agent v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Loop: Task → Plan → Execute → Observe → Fix → Loop → Done

Features:
  • Project context injection
  • Long-term learnings injection
  • Error self-fix with retry counter
  • Similar task recall
  • Auto learning capture
  • Real execution logging
"""

import json, re, logging
from collections import defaultdict
from config import Config
from memory import Memory
from router import LLMRouter
from tools  import Tools

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("agent.log")]
)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert AI coding agent that autonomously builds software.

{tools_schema}

{project_context}

{learnings}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE FORMAT — STRICT JSON ONLY:
{{
  "thought":  "What I'm planning or why",
  "action":   "tool_name or null",
  "args":     {{}},
  "output":   "Message to user if no action",
  "done":     false
}}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AGENT RULES:
1.  Respond ONLY with valid JSON — no markdown, no preamble.
2.  One action per step. Think before acting.
3.  Always read a file before editing it.
4.  After writing code, RUN it to verify it works.
5.  If code fails, FIX it — never give up before {max_retries} attempts.
6.  Set "done": true IMMEDIATELY after the current step's goal is achieved.
    A step is done the moment its primary action succeeds (file written, package
    installed, server started). Do NOT wait for extra confirmation actions.
7.  For web projects: use server_start (not run_shell/run_python) to launch the server,
    then browser_navigate to confirm it is live.
8.  Write production-quality code. No TODOs, no stubs, no placeholder comments.
9.  If a prior action failed, do NOT repeat the exact same action+args. Change approach.
10. File paths: pass only the filename or relative path inside workspace (e.g. "app.py").
    Never prefix with "workspace/" — the tool resolves it automatically.
11. To install packages: run_shell("pip install <pkg1> <pkg2>").
    Chains like "pip install X && pip install Y" are also supported.
12. When an error is given, read its category and apply the stated strategy exactly.
13. NEVER write the same file a second time in the same step. If write_file already
    succeeded for a file, set "done": true — do not rewrite it.
14. NEVER repeat an action that already succeeded in the current step.
    The system tracks which actions have already been completed; duplicates are skipped.
15. Each plan step executes exactly ONCE unless it FAILED. Move forward, not backward.
"""


PLANNER_PROMPT = """You are a software execution planner.

Decompose the user task into {max_steps} or fewer ATOMIC, ordered steps.
Each step is ONE concrete action: write a file, install a package, run a script,
start a server, or test an endpoint.  Never combine multiple actions in one step.

Ordering rules:
- Install dependencies BEFORE running any code.
- Initialize databases BEFORE starting the server.
- Start the server BEFORE running browser tests.
- The LAST step must validate or test the result.

Return STRICT JSON only — no prose, no markdown:
{{
    "steps": [
        "Write requirements.txt listing flask, flask-sqlalchemy, werkzeug",
        "Install dependencies with pip",
        "Write app.py with routes /login /register /dashboard /logout",
        "Write HTML templates: base, login, register, dashboard",
        "Write and run init_db.py to create the SQLite database",
        "Start Flask server on port 5000 using server_start",
        "Test registration and login via browser automation"
    ]
}}

Rules:
- Be specific: name exact files, ports, packages.
- Each step must be independently testable.
- Maximum {max_steps} steps.
"""


class Agent:
    def __init__(self, config: Config = None, memory: Memory = None, force_model: str | None = None):
        self.config  = config  or Config()
        self.memory  = memory  or Memory()
        self.router  = LLMRouter(config=self.config, force_model=force_model)
        self.tools   = Tools(config=self.config)

    # ──────────────────────────────────────────────────────────────────────────
    # Phase 8 — structured records for the outer orchestrator
    # ──────────────────────────────────────────────────────────────────────────
    # Each record is a single-line JSON payload prefixed with [AGENT_RECORD]
    # so the orchestrator (orchestrator.py) can THINK→PLAN→VERIFY→REFLECT
    # around the agent without modifying tools.py / router.py / main.py.
    # CLI users see the extra lines but they're harmless.
    def _emit_record(self, kind: str, payload: dict) -> None:
        try:
            line = json.dumps({"kind": kind, **payload}, default=str)
        except Exception:
            line = json.dumps({"kind": kind, "error": "unserialisable payload"})
        print(f"[AGENT_RECORD] {line}", flush=True)

    # ──────────────────────────────────────────────────────────────────────────
    # PUBLIC: run a task
    # ──────────────────────────────────────────────────────────────────────────
    def run(self, task: str) -> str:
        logger.info(f"\n{'='*55}\nTASK: {task}\n{'='*55}")
        print(f"\n{'━'*55}")
        print(f"🎯 TASK: {task}")
        print(f"{'━'*55}")

        # ── Recall similar tasks ─────────────────────────
        similar = self.memory.find_similar_task(task)
        if similar:
            print(f"  📚 Found {len(similar)} similar past task(s)")

        self.memory.add_message("user", task)

        system = SYSTEM_PROMPT.format(
            tools_schema    = Tools.schema(),
            project_context = self.memory.project_context_prompt(),
            learnings       = self.memory.learnings_prompt(task),
            max_retries     = self.config.MAX_RETRIES,
        )

        checkpoint = self.memory.load_checkpoint(task)
        if checkpoint:
            plan_steps    = checkpoint.get("plan_steps") or self._plan_task(task)
            plan_stages   = checkpoint.get("plan_stages", [])
            step_stage_map= checkpoint.get("step_stage_map", {})
            completed_steps = set(checkpoint.get("completed_steps", []))
            current_step  = min(int(checkpoint.get("current_step", 0)), max(len(plan_steps) - 1, 0))
            loop_count    = int(checkpoint.get("loop_count", 0))
            error_count   = int(checkpoint.get("error_count", 0))
            last_output   = checkpoint.get("last_output", "")
            last_error    = checkpoint.get("last_error", "")
            state_stack   = checkpoint.get("state_stack", [])
            blocked_attempts = defaultdict(set)
            for k, v in checkpoint.get("blocked_attempts", {}).items():
                blocked_attempts[int(k)] = set(v)
            print(f"  Resuming: step {current_step + 1}/{max(len(plan_steps), 1)}, {len(completed_steps)} steps already done")
        else:
            plan_stages, plan_steps, step_stage_map = self._plan_staged_task(task)
            current_step  = 0
            loop_count    = 0
            error_count   = 0
            last_output   = ""
            last_error    = ""
            state_stack   = []
            blocked_attempts  = defaultdict(set)
            completed_steps   = set()   # step strings confirmed done
        # Per-step success tracker -- prevents re-executing already-succeeded actions
        step_completed_actions: dict = defaultdict(set)  # {step_idx: {fingerprint, ...}}
        # Execution counters (always start fresh -- not persisted in checkpoint)
        step_loop_count = 0   # loops within the current step (resets on step advance)
        total_failures  = 0   # cumulative errors across all steps this session

        if not plan_steps:
            plan_steps = [task]

        # -- Stage-aware plan display --
        if plan_stages:
            print("\n  Plan (staged):")
            for si, stage_obj in enumerate(plan_stages, 1):
                sname = stage_obj.get("stage", f"stage{si}").upper()
                print(f"   Stage {si}/{len(plan_stages)}: {sname}")
                for step_str in stage_obj.get("steps", []):
                    idx = next((i for i, s in enumerate(plan_steps) if s == step_str), -1)
                    mark = "OK" if step_str in completed_steps else (">>" if idx == current_step else " .")
                    num  = idx + 1 if idx >= 0 else "?"
                    print(f"     [{mark}] {num}. {step_str}")
        else:
            print("\n  Plan:")
            for i, step in enumerate(plan_steps, 1):
                mark = "OK" if step in completed_steps else (">>" if i - 1 == current_step else " .")
                print(f"   [{mark}] {i}. {step}")

        def persist_checkpoint():
            self.memory.save_checkpoint({
                "task":             task,
                "plan_steps":       plan_steps,
                "plan_stages":      plan_stages,
                "step_stage_map":   step_stage_map,
                "completed_steps":  sorted(completed_steps),
                "current_step":     current_step,
                "loop_count":       loop_count,
                "error_count":      error_count,
                "last_output":      last_output,
                "last_error":       last_error,
                "state_stack":      state_stack[-20:],
                "blocked_attempts": {str(k): sorted(v) for k, v in blocked_attempts.items()},
            })

        try:
            while (
                loop_count    < self.config.MAX_AGENT_LOOPS
                and current_step < len(plan_steps)
                and total_failures < self.config.MAX_TOTAL_ATTEMPTS
            ):
                # -- Early exit: all steps already completed --
                if len(completed_steps) >= len(plan_steps) and plan_steps:
                    logger.info("[Agent] All steps completed -- early exit")
                    print("  All steps completed, exiting loop")
                    break

                loop_count     += 1
                step_loop_count += 1
                active_step = plan_steps[current_step]

                # -- Step-skip guard: jump over already-completed steps immediately --
                if active_step in completed_steps:
                    print(f"  [skip] Step {current_step+1} already done: {active_step[:60]}")
                    if current_step < len(plan_steps) - 1:
                        current_step    += 1
                        error_count      = 0
                        step_loop_count  = 0
                        self.router._gemini_use_pro = False
                    else:
                        break
                    persist_checkpoint()
                    continue

                logger.info(
                    f"[Agent] Loop {loop_count}/{self.config.MAX_AGENT_LOOPS} | "
                    f"Step {current_step + 1}/{len(plan_steps)} | Stage: {step_stage_map.get(active_step, 'n/a')} | "
                    f"Failures {total_failures}/{self.config.MAX_TOTAL_ATTEMPTS}"
                )

                # -- Pattern memory injection: prepend top-1 relevant learning --
                top_pattern = self.memory.learnings_prompt(active_step)
                pattern_hint = f"\n[Memory] Relevant pattern: {top_pattern[:200]}" if top_pattern and top_pattern.strip() != "No learnings yet." else ""

                # Context minimization: last 6 messages only (~3x fewer tokens per call)
                messages = self.memory.get_messages(last_n=6)
                messages.append({
                    "role": "user",
                    "content": self._step_instruction(
                        current_step, plan_steps,
                        blocked_attempts[current_step],
                        last_error=last_error,
                        stage_name=step_stage_map.get(active_step, ""),
                        completed_count=len(completed_steps),
                    ) + pattern_hint,
                })

                try:
                    raw = self.router.chat(messages, system=system)
                except RuntimeError as e:
                    msg = f"LLM unavailable: {e}"
                    logger.error(msg)
                    persist_checkpoint()
                    self.memory.log_task(task, "error", msg)
                    return msg

                parsed = self._parse(raw)
                if not parsed:
                    logger.warning(f"[Agent] Bad JSON:\n{raw[:200]}")
                    self.memory.add_message("assistant", raw)
                    self.memory.add_message(
                        "user",
                        "Your response is not valid JSON. Reply ONLY with a JSON object.",
                    )
                    persist_checkpoint()
                    continue

                thought = parsed.get("thought", "")
                action = parsed.get("action")
                args = parsed.get("args", {})
                output = parsed.get("output", "")
                done = parsed.get("done", False)

                if thought:
                    print(f"\n  💭 {thought}")

                if action:
                    fingerprint = self._fingerprint_action(action, args)

                    # ── Guard 1: skip actions that already FAILED this step ──────────
                    if fingerprint in blocked_attempts[current_step]:
                        observation = (
                            f"Action '{action}' with identical args already failed for step '{active_step}'. "
                            "Choose a different tool or different arguments."
                        )
                        self.memory.add_message("assistant", json.dumps(parsed))
                        self.memory.add_message("user", f"Observation: {observation}")
                        persist_checkpoint()
                        continue

                    # ── Guard 2: skip actions that already SUCCEEDED this step ───────
                    if fingerprint in step_completed_actions[current_step]:
                        print(f"  ⏭ {action} already completed for this step — auto-advancing")
                        observation = (
                            f"[SYSTEM] Action '{action}' already succeeded in the current step '{active_step}'. "
                            f"Do NOT repeat it. Set \"done\": true now to advance to the next step."
                        )
                        self.memory.add_message("assistant", json.dumps(parsed))
                        self.memory.add_message("user", f"Observation: {observation}")
                        # Force-advance immediately — the LLM is stuck in a re-write loop
                        if current_step < len(plan_steps) - 1:
                            current_step    += 1
                            error_count      = 0
                            step_loop_count  = 0
                            self.router._gemini_use_pro = False
                            transition = (
                                f"Step auto-advanced. Now on step {current_step + 1}/{len(plan_steps)}: "
                                f"{plan_steps[current_step]}"
                            )
                            self.memory.add_message("user", transition)
                        persist_checkpoint()
                        continue

                    args_preview = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
                    print(f"  🔧 {action}({args_preview})")

                    snapshot = self._snapshot_before_action(
                        action=action,
                        args=args,
                        current_step=current_step,
                        last_output=last_output,
                        error_count=error_count,
                    )
                    state_stack.append(snapshot)

                    result = self.tools.execute(action, **args)
                    success = result["success"]
                    out = result["output"]
                    err = result["error"]

                    if success:
                        print(f"  OK {out[:250]}")
                        observation = f"TOOL '{action}' SUCCEEDED for step '{active_step}':\n{out}"
                        error_count = 0
                        last_output = out
                        # Record action + step as completed
                        step_completed_actions[current_step].add(fingerprint)
                        completed_steps.add(active_step)

                        # Log stage completion when last step of a stage finishes
                        if plan_stages and step_stage_map.get(active_step):
                            sn = step_stage_map[active_step]
                            so = next((s for s in plan_stages if s["stage"] == sn), None)
                            if so and all(s in completed_steps for s in so.get("steps", [])):
                                print(f"  [Stage DONE] {sn.upper()} complete")
                                logger.info(f"[Agent] Stage '{sn}' completed")

                        if last_error and action in ("run_python", "run_shell", "write_file", "diff_edit"):
                            self.memory.add_learning(
                                "error_fix",
                                f"Fixed '{last_error[:120]}' via {action} ({active_step[:80]})",
                            )
                            last_error = ""

                        if len(state_stack) > 40:
                            state_stack = state_stack[-40:]

                        # If LLM forgot to set done=True, nudge it strongly
                        if not done:
                            observation += (
                                f"\n\n[SYSTEM] The action SUCCEEDED. "
                                f"Step goal: '{active_step}'.\n"
                                "If this step is now complete, you MUST set \"done\": true "
                                "in your NEXT response. "
                                "Do NOT write the same file again or repeat this action."
                            )
                    else:
                        print(f"  ❌ {err[:250]}")
                        blocked_attempts[current_step].add(fingerprint)
                        category = self._categorize_error(err)
                        strategy = self._strategy_for_error(category)
                        rollback_msg = self._rollback_from_snapshot(state_stack.pop() if state_stack else None)

                        observation = (
                            f"TOOL '{action}' FAILED on step '{active_step}':\n{err}\n"
                            f"Error category: {category}. Strategy: {strategy}.\n"
                            f"Rollback: {rollback_msg}.\n"
                            f"Fix attempt {error_count + 1}/{self.config.PER_STEP_RETRY}. "
                            "Do not repeat the same action+args."
                        )
                        error_count   += 1
                        last_error     = err
                        total_failures += 1

                        # Escalate to Gemini-pro after GEMINI_PRO_AFTER_FAILURES per-step failures
                        if error_count >= self.config.GEMINI_PRO_AFTER_FAILURES:
                            if not self.router._gemini_use_pro:
                                logger.info(
                                    f"[Agent] Escalating Gemini flash->pro after "
                                    f"{error_count} failures on step {current_step + 1}"
                                )
                            self.router._gemini_use_pro = True

                        # User intervention after USER_INTERVENTION_THRESHOLD cumulative failures
                        if total_failures == self.config.USER_INTERVENTION_THRESHOLD:
                            print(
                                f"\n  ⚠️  {total_failures} cumulative failures. "
                                f"Last: [{category.upper()}] {err[:100]}"
                            )
                            print(
                                "  💡  Enter a hint/correction (or press Enter to auto-continue): ",
                                end="", flush=True,
                            )
                            try:
                                hint = input().strip()
                                if hint:
                                    self.memory.add_message("user", f"[USER HINT]: {hint}")
                                    observation += f"\n[USER HINT from operator]: {hint}"
                                    logger.info(f"[Agent] User hint received: {hint[:100]}")
                            except (EOFError, KeyboardInterrupt):
                                pass

                        if error_count >= self.config.PER_STEP_RETRY and current_step > 0:
                            current_step -= 1
                            error_count   = 0
                            observation  += f"\nBacktracked to step {current_step + 1} to try a safer alternative path."

                        elif error_count >= self.config.PER_STEP_RETRY * 2:
                            msg = f"Too many failures ({error_count}). Last error: {err}"
                            logger.error(msg)
                            # Phase 8 — emit final-failure step_result + reflection
                            # so the orchestrator can decide whether to adapt &
                            # retry the whole sub-task with a new strategy.
                            self._emit_record("step_result", {
                                "step_index": current_step,
                                "step_text":  active_step,
                                "success":    False,
                                "tool":       action,
                                "error_reason": err[:300],
                                "error_category": category,
                                "attempts":   step_loop_count,
                                "summary":    msg[:200],
                            })
                            self._emit_record("reflection", {
                                "step_index":      current_step,
                                "worked":          False,
                                "failed_attempts": step_loop_count,
                                "error_category":  category,
                                "recommendation":  strategy[:200],
                            })
                            try:
                                self.memory.add_learning(
                                    "step_reflection",
                                    f"FAILED step '{active_step[:80]}' "
                                    f"after {step_loop_count} tries — "
                                    f"category={category}; try: {strategy[:120]}",
                                )
                            except Exception:
                                pass
                            persist_checkpoint()
                            self.memory.log_task(task, "failed", msg, self.router.last_used_api or "")
                            return msg

                    self.memory.add_message("assistant", json.dumps(parsed))
                    self.memory.add_message("user", f"Observation: {observation}")

                    if success and done:
                        # Phase 8 — emit success step_result + reflection BEFORE
                        # the counters are reset, so the orchestrator sees the
                        # accurate per-step attempt count.
                        attempts_used = step_loop_count
                        self._emit_record("step_result", {
                            "step_index": current_step,
                            "step_text":  active_step,
                            "success":    True,
                            "tool":       action,
                            "error_reason": None,
                            "attempts":   attempts_used,
                            "summary":    str(out)[:200],
                        })
                        self._emit_record("reflection", {
                            "step_index":      current_step,
                            "worked":          True,
                            "failed_attempts": max(0, attempts_used - 1),
                            "tool":            action,
                            "recommendation":  "keep this approach for similar steps",
                        })
                        if attempts_used > 1:
                            try:
                                self.memory.add_learning(
                                    "step_reflection",
                                    f"OK step '{active_step[:80]}' succeeded "
                                    f"with {action} after {attempts_used} tries",
                                )
                            except Exception:
                                pass
                        if current_step < len(plan_steps) - 1:
                            current_step    += 1
                            error_count      = 0
                            step_loop_count  = 0              # reset for new step
                            self.router._gemini_use_pro = False  # back to flash on success
                            transition = (
                                f"Step completed. Move to step {current_step + 1}/{len(plan_steps)}: "
                                f"{plan_steps[current_step]}"
                            )
                            self.memory.add_message("user", transition)
                        else:
                            persist_checkpoint()
                            break

                    persist_checkpoint()
                    continue

                if output:
                    last_output = output
                    print(f"\n  💬 {output[:400]}")
                    self.memory.add_message("assistant", output)

                if done:
                    if current_step < len(plan_steps) - 1:
                        current_step    += 1
                        error_count      = 0
                        step_loop_count  = 0              # reset for new step
                        self.router._gemini_use_pro = False  # back to flash on success
                        transition = f"Step completed. Move to step {current_step + 1}/{len(plan_steps)}: {plan_steps[current_step]}"
                        self.memory.add_message("assistant", json.dumps(parsed))
                        self.memory.add_message("user", transition)
                        persist_checkpoint()
                        continue
                    break

                persist_checkpoint()

        except Exception as e:
            logger.exception("Agent loop crashed; checkpoint saved")
            persist_checkpoint()
            msg = f"Agent crashed. Checkpoint saved for resume. Error: {e}"
            self.memory.log_task(task, "crashed", msg, self.router.last_used_api or "")
            return msg

        status = "done" if loop_count < self.config.MAX_AGENT_LOOPS else "timeout"
        self.memory.log_task(
            task, status, last_output,
            self.router.last_used_api or "",
            tokens=sum(h.total_tokens for h in self.router.health.values())
        )
        if status in ("done", "timeout"):   # both paths must clear the stale checkpoint
            self.memory.clear_checkpoint()

        print(f"\n{'━'*55}")
        print(f"✅ Status: {status.upper()} | API: {self.router.last_used_api} | Loops: {loop_count}")
        print(f"{'━'*55}\n")
        return last_output or "Task completed."

    # ──────────────────────────────────────────────────────────────────────────
    # Simple chat (no tools)
    # ──────────────────────────────────────────────────────────────────────────
    def chat(self, message: str) -> str:
        self.memory.add_message("user", message)
        resp = self.router.chat(self.memory.get_messages())
        self.memory.add_message("assistant", resp)
        return resp

    # ──────────────────────────────────────────────────────────────────────────
    # JSON parser
    # ──────────────────────────────────────────────────────────────────────────
    def _parse(self, text: str) -> dict | None:
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try: return json.loads(m.group())
                except Exception: pass
        return None

    def _plan_task(self, task: str) -> list[str]:
        # Phase 1: phi3:mini local planner (zero-cost, fast, no API call)
        steps = self.router.chat_planner(task)
        if steps:
            logger.info(f"[Agent] Planner: phi3 local ({len(steps)} steps)")
            return steps

        # Phase 2: fall back to main LLM (Gemini-flash or whatever is available)
        planner_system = PLANNER_PROMPT.format(max_steps=self.config.PLANNER_MAX_STEPS)
        try:
            raw = self.router.chat(
                [{"role": "user", "content": task}],
                system=planner_system,
                max_tokens=800,
            )
            parsed = self._parse(raw) or {}
            steps = parsed.get("steps", []) if isinstance(parsed, dict) else []
            steps = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
            if steps:
                logger.info(f"[Agent] Planner: remote LLM ({len(steps)} steps)")
                return steps[: self.config.PLANNER_MAX_STEPS]
        except Exception as e:
            logger.warning(f"Planner failed, using fallback plan: {e}")
        return [task]

    def _plan_staged_task(self, task: str) -> tuple:
        """Phase 2 staged planner.  Returns (plan_stages, plan_steps, step_stage_map).

        Tries chat_staged_planner (phi3 -> /api/generate fallback) first.
        Falls back to flat _plan_task() wrapped in a single 'execution' stage.
        """
        if self.config.PLANNER_STAGED:
            staged = self.router.chat_staged_planner(task)
            if staged:
                plan_steps: list = []
                step_stage_map: dict = {}
                for stage_obj in staged:
                    sname = stage_obj.get("stage", "execution")
                    for s in stage_obj.get("steps", []):
                        if s and s not in plan_steps:
                            plan_steps.append(s)
                            step_stage_map[s] = sname
                if plan_steps:
                    logger.info(
                        f"[Agent] StagedPlanner: {len(staged)} stages, {len(plan_steps)} steps"
                    )
                    return staged, plan_steps, step_stage_map

        # Fallback: flat list wrapped in single execution stage
        flat = self._plan_task(task)
        return [{"stage": "execution", "steps": flat}], flat, {s: "execution" for s in flat}

    def _needs_llm(self, step_loop_count: int, last_error: str, prev_done: bool) -> bool:
        """Execution-first gate: return True only when an LLM call is warranted.

        True  -- first attempt on new step / error needs diagnosis / step not done yet.
        False -- previous action succeeded with done=True (step just advanced;
                  next iteration resets step_loop_count=0 and will call LLM for new step).

        In practice this always returns True in the current loop structure because:
        - step_loop_count is reset to 0 on every step advance
        - last_error is non-empty when any action fails
        The method is here as documented policy and for future deterministic-step shortcuts.
        """
        if step_loop_count <= 1:
            return True   # First attempt -- always needs generation
        if last_error:
            return True   # Error occurred -- LLM must diagnose and fix
        if not prev_done:
            return True   # Step not yet complete -- need next action
        return False      # Advancing to next step; next loop will call LLM with step_loop_count=0

    def _step_instruction(self, current_step: int, plan_steps: list[str],
                          blocked_attempts: set[str], last_error: str = "",
                          stage_name: str = "", completed_count: int = 0) -> str:
        blocked = "\n".join(f"- {x[:160]}" for x in list(blocked_attempts)[-5:]) or "- none"
        error_hint = ""
        variation_hint = ""
        if last_error:
            cat = self._categorize_error(last_error)
            strategy = self._strategy_for_error(cat)
            error_hint = (
                f"\nLast error category : [{cat.upper()}]"
                f"\nRecommended strategy: {strategy}"
            )
            # Phase 8 — retry-with-variation. Suggest concrete alternative
            # tools/approaches instead of just "don't repeat the same args".
            alternatives = self._alternatives_for_category(cat)
            if alternatives:
                variation_hint = (
                    "\nTRY A DIFFERENT APPROACH this attempt:\n  "
                    + "\n  ".join(f"• {a}" for a in alternatives)
                )
        stage_hint = f"\nCurrent stage: {stage_name.upper()}" if stage_name else ""
        return (
            f"Current plan step {current_step + 1}/{len(plan_steps)}: "
            f"{plan_steps[current_step]}\n"
            f"Steps completed so far: {completed_count}/{len(plan_steps)}"
            f"{stage_hint}"
            f"{error_hint}"
            f"{variation_hint}\n"
            "Failed action fingerprints for this step (do not repeat):\n"
            f"{blocked}"
        )

    def _alternatives_for_category(self, category: str) -> list[str]:
        """Phase 8 — concrete alternative actions to suggest on retry."""
        alts = {
            "file_not_found": [
                "Call list_files() and use the EXACT path it shows",
                "Try a relative path (just the filename) without any prefix",
                "Re-create the file with write_file before referencing it",
            ],
            "syntax": [
                "read_file the failing file, then diff_edit only the bad line",
                "Rewrite the whole file fresh with write_file (small files)",
                "Run the file with run_python to see the precise error line",
            ],
            "import": [
                "run_shell('pip install <pkg>') then retry",
                "Switch to a stdlib equivalent if the package is optional",
            ],
            "port_conflict": [
                "Call server_stop first to free the port",
                "Pick a different port (5001, 8080, 8000) for server_start",
            ],
            "network": [
                "Wait a moment and retry once — do not hammer the API",
                "Use a different model/provider via the router",
            ],
            "runtime": [
                "read_file the failing source, locate the line in the traceback",
                "Add a tiny test script that isolates the failing call",
                "diff_edit the precise broken line instead of rewriting",
            ],
            "loop_detected": [
                "Use a completely different tool than the one that just failed",
                "Change the args (different path, different content, different cmd)",
                "Simplify the step — try a smaller intermediate goal first",
            ],
            "permission": [
                "Drop any system path; only use workspace-relative filenames",
                "Stop the server (server_stop) if you're rewriting its files",
            ],
            "unknown": [
                "Gather context: list_files, then read_file the related files",
                "Pick a different tool entirely and approach the goal sideways",
            ],
        }
        return alts.get(category, alts["unknown"])

    def _fingerprint_action(self, action: str, args: dict) -> str:
        try:
            args_s = json.dumps(args or {}, sort_keys=True, default=str)
        except Exception:
            args_s = str(args)
        return f"{action}|{args_s}"

    def _snapshot_before_action(self, action: str, args: dict, current_step: int,
                                last_output: str, error_count: int) -> dict:
        snapshot = {
            "action": action,
            "args": args,
            "step": current_step,
            "last_output": last_output,
            "error_count": error_count,
        }
        if action in {"write_file", "delete_file", "search_replace", "diff_edit"} and isinstance(args, dict):
            path = args.get("path")
            if path:
                prior = self.tools.read_file(path)
                snapshot["path"] = path
                snapshot["file_existed"] = prior["success"]
                snapshot["file_content"] = prior["output"] if prior["success"] else ""
        return snapshot

    def _rollback_from_snapshot(self, snapshot: dict | None) -> str:
        if not snapshot or "path" not in snapshot:
            return "No file rollback required"

        path = snapshot["path"]
        if snapshot.get("file_existed"):
            res = self.tools.write_file(path=path, content=snapshot.get("file_content", ""))
            return f"Restored previous file content for {path}" if res["success"] else f"Rollback failed for {path}: {res['error']}"

        res = self.tools.delete_file(path=path)
        if res["success"]:
            return f"Removed newly-created file {path}"
        return f"No rollback delete needed for {path}"

    def _categorize_error(self, err: str) -> str:
        msg = (err or "").lower()
        # Check most-specific patterns first
        if any(x in msg for x in ("no such file", "not found", "does not exist",
                                   "cannot find", "filenotfounderror", "resolved →")):
            return "file_not_found"
        if any(x in msg for x in ("syntaxerror", "invalid syntax",
                                   "indentationerror", "unexpected indent",
                                   "unexpected eof")):
            return "syntax"
        if any(x in msg for x in ("modulenotfounderror", "no module named",
                                   "importerror", "cannot import")):
            return "import"
        if any(x in msg for x in ("address already in use", "port",
                                   "already in use", "bind:", "eaddrinuse")):
            return "port_conflict"
        if any(x in msg for x in ("permission", "access denied",
                                   "readonly", "operation not permitted")):
            return "permission"
        if any(x in msg for x in ("timeout", "timed out", "429",
                                   "rate limit", "connection", "dns", "network",
                                   "did not bind", "did not become ready")):
            return "network"
        if any(x in msg for x in ("traceback", "runtimeerror", "typeerror",
                                   "valueerror", "assertionerror", "attributeerror",
                                   "keyerror", "indexerror", "nameerror")):
            return "runtime"
        if any(x in msg for x in ("blocked", "do not repeat", "same action",
                                   "already failed")):
            return "loop_detected"
        return "unknown"

    def _strategy_for_error(self, category: str) -> str:
        strategies = {
            "file_not_found": (
                "Call list_files() first to see what actually exists. "
                "Then use the exact filename shown — never guess paths."
            ),
            "syntax": (
                "Call read_file on the failing file, locate the exact bad line, "
                "fix it with diff_edit, then run again to confirm."
            ),
            "import": (
                "Run run_shell('pip install <missing_package>') first, "
                "then retry the code."
            ),
            "port_conflict": (
                "Call server_stop to free the port, or switch to a different port "
                "(5001, 8080, 8000).  Then retry server_start."
            ),
            "permission": (
                "Use only workspace-relative filenames. "
                "Avoid system directories or privileged commands."
            ),
            "network": (
                "The API or server may be unavailable. "
                "Wait briefly, then retry.  Do not hammer with identical requests."
            ),
            "runtime": (
                "Read the full traceback. Use read_file to inspect the failing code. "
                "Fix the root cause with diff_edit, then rerun."
            ),
            "loop_detected": (
                "You are repeating a failed action. "
                "Choose a completely different tool or different arguments."
            ),
            "unknown": (
                "Gather more context via list_files or read_file, "
                "then choose a different fix path."
            ),
        }
        return strategies.get(category, strategies["unknown"])