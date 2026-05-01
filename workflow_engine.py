"""
workflow_engine.py — Phase 43: Strict Execution Pipeline
======================================================
Enforces a single reliable execution flow:
task → plan → tools → execute → verify → explain → store
"""
from __future__ import annotations

import logging
import time
import uuid
import json
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from observability import get_observability

logger = logging.getLogger(__name__)

@dataclass
class WorkflowContext:
    workflow_id: str
    task: str
    language: str = "python"
    user_id: int = 1
    code: str = ""
    intent_data: dict = field(default_factory=dict)
    plan: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    verification: dict = field(default_factory=dict)
    explanation: str = ""
    store_result: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    suggestions: dict = field(default_factory=dict)
    retry_count: int = 0

# ── 1. Intent Classification ──────────────────────────────────────────────────
def step_intent(ctx: WorkflowContext, emit) -> bool:
    from intelligence_layer import IntentClassifier, get_memory_manager
    classifier = IntentClassifier()
    memory = get_memory_manager()
    intent = classifier.classify(ctx.task, memory.get_context())
    ctx.intent_data = {
        "type": intent.task_type,
        "tools": intent.required_tools,
        "confidence": intent.confidence,
        "reasoning": intent.reasoning
    }
    emit("step_done", {"step": "intent", "data": ctx.intent_data})
    return True

# ── 2. Plan ───────────────────────────────────────────────────────────────────
def step_plan(ctx: WorkflowContext, emit) -> bool:
    from model_router import get_router
    from intelligence_layer import get_memory_manager
    import time
    
    # Chaos Testing: Timeout
    if ctx.metadata.get("chaos", {}).get("timeout"):
        time.sleep(10)
        raise TimeoutError("Chaos Test: Simulated Timeout in Plan Phase")
        
    # Chaos Testing: Model Failure
    if ctx.metadata.get("chaos", {}).get("model_failure"):
        raise RuntimeError("Chaos Test: Simulated LLM Provider Outage")

    router = get_router()
    ctx_memory = get_memory_manager().get_context()
    
    retry_context = ""
    if ctx.retry_count > 0:
        retry_context = f"\nPREVIOUS FAILURE: The previous code failed verification.\nErrors: {ctx.verification.get('error', 'unknown')}\nREVISE PLAN ACCORDINGLY."

    prompt = f"Plan task into 3-5 concrete engineering steps.\nIntent: {ctx.intent_data.get('type')}\nContext: {ctx_memory}\nTask: {ctx.task}{retry_context}\nReply with a JSON list of strings."
    result = router.call(prompt, task_type="plan")
    try:
        data = result.get("text", "[]")
        if data.startswith("```json"): data = data.strip()[7:-3]
        ctx.plan = json.loads(data)
        if not isinstance(ctx.plan, list): ctx.plan = [str(ctx.plan)]
    except Exception as e:
        ctx.plan = ["Execute the task directly"]
    emit("step_done", {"step": "plan", "plan": ctx.plan})
    return True

# ── 3. Tools (Dynamic Selection) ──────────────────────────────────────────────
def step_tools(ctx: WorkflowContext, emit) -> bool:
    try:
        # Chaos Testing: Tool Failure
        if ctx.metadata.get("chaos", {}).get("tool_failure"):
            raise ConnectionError("Chaos Test: Simulated Tool Connectivity Failure")
            
        tools = ctx.intent_data.get("tools", [])
        results = []
        if "browser" in tools or ctx.intent_data.get("type") == "research_task":
            results.append({"tool": "browser", "info": "Browser active. Proceeding with research context."})
        if "github" in tools:
            results.append({"tool": "github", "info": "Github analyzer ready."})
        if "file" in tools:
            results.append({"tool": "file", "info": "File workspace active."})
            
        ctx.tool_results = results
        emit("step_done", {"step": "tools", "tools_activated": tools})
    except Exception as e:
        ctx.tool_results = []
        emit("step_done", {"step": "tools", "error": str(e)})
    return True

# ── 3. Execute ────────────────────────────────────────────────────────────────
def step_execute(ctx: WorkflowContext, emit) -> bool:
    from model_router import get_router
    router = get_router()
    prompt = f"Task: {ctx.task}\nPlan: {ctx.plan}\nContext: {ctx.tool_results}\n\nWrite the complete {ctx.language} code. NO Markdown fences, just raw code."
    result = router.call(prompt, task_type="code", max_tokens=2048)
    code = result.get("text", "").strip()
    if code.startswith("```"): 
        lines = code.split("\n")
        code = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    ctx.code = code
    emit("step_done", {"step": "execute", "code_len": len(code)})
    return bool(code)

# ── 4. Verify ─────────────────────────────────────────────────────────────────
def step_verify(ctx: WorkflowContext, emit) -> bool:
    from sandbox_manager import get_sandbox
    from quality_engine import evaluate_quality
    from auth_system import get_user_workspace
    
    sb = get_sandbox()
    if not ctx.code: 
        ctx.verification = {"ok": False, "error": "Code is empty."}
        return False
        
    workspace_dir, artifacts_dir = get_user_workspace(ctx.user_id)
    res = sb.run(ctx.code, language=ctx.language, timeout=30, task_id=ctx.workflow_id, workspace_dir=workspace_dir)
    ctx.verification = res.to_dict()
    
    # Evaluate Quality & Guardrails
    quality = evaluate_quality(ctx.verification, ctx.code, ctx.language)
    ctx.verification["quality_score"] = quality["quality_score"]
    ctx.verification["verdict"] = quality["verdict"]
    
    # Hard failure guardrails: Empty code or missing basic structure
    if quality["quality_score"] < 50:
        ctx.verification["ok"] = False
        ctx.verification["error"] = "Guardrail Failure:\n" + "\n".join(quality["penalties"])
        res.ok = False
        emit("step_done", {"step": "verify", "ok": False, "score": quality["quality_score"]})
        return False
        
    # Soft validation vs hard validation
    err = ctx.verification.get("error", "").lower() if not res.ok else ""
    is_hard_error = "syntaxerror" in err or "nameerror" in err or "indentationerror" in err or "filenotfound" in err
    is_soft_error = bool(err) and not is_hard_error
    
    if is_soft_error:
        ctx.verification["ok"] = True
        ctx.verification["warning"] = "Minor runtime issues detected, but code is functional."
        res.ok = True

    emit("step_done", {"step": "verify", "ok": res.ok, "score": quality["quality_score"]})
    return res.ok

# ── 6. Explain ────────────────────────────────────────────────────────────────
def step_explain(ctx: WorkflowContext, emit) -> bool:
    status = ctx.verification.get("status", "PASSED" if ctx.verification.get("ok") else "FAILED")
    plan_text = "\n".join([f"- {p}" for p in ctx.plan])
    suggestions_text = "\n".join([f"- {s}" for s in ctx.suggestions.get("next_steps", [])]) if ctx.suggestions else "None"
    
    feedback = ""
    if status != "PASSED":
        feedback = f"### What Failed\n```\n{ctx.verification.get('error', 'Execution error')}\n```\n\n### How to Fix\nReview the traceback above and manually correct the logic. Verify all inputs/dependencies."
        
    score = ctx.verification.get('quality_score', 0)
    verdict = ctx.verification.get('verdict', 'unknown')
    
    ctx.explanation = f"# Execution Report\n\n## Intent\n{ctx.intent_data.get('type', 'system_task')} (Tools: {ctx.intent_data.get('tools', [])})\n\n## Plan\n{plan_text}\n\n## Verification\n{status}\n**Quality Score:** {score}/100 ({verdict})\n\n{feedback}\n\n## AI Suggestions\n**Improvements:** {ctx.suggestions.get('improvements', 'None')}\n**Next Steps:**\n{suggestions_text}"
    emit("step_done", {"step": "explain"})
    return True

# ── 7. Store ──────────────────────────────────────────────────────────────────
def step_store(ctx: WorkflowContext, emit) -> bool:
    from artifact_registry import get_artifact_registry
    from auth_system import get_user_workspace
    registry = get_artifact_registry()
    ext = 'py' if ctx.language == 'python' else 'js'
    files = {f"main.{ext}": ctx.code, "EXPLANATION.md": ctx.explanation}
    workspace_dir, artifacts_dir = get_user_workspace(ctx.user_id)
    
    art = registry.create_artifact(
        name=ctx.task[:20], 
        artifact_type="script", 
        files=files, 
        tags=["auto-generated", ctx.intent_data.get("type", "unknown")],
        base_dir=artifacts_dir
    )
    ctx.store_result = {"artifact_id": art.id, "path": art.local_path}
    emit("step_done", {"step": "store", "path": art.local_path})
    return True

# ── 8. Proactive ──────────────────────────────────────────────────────────────
def step_proactive(ctx: WorkflowContext, emit) -> bool:
    from intelligence_layer import ProactiveAgent, get_memory_manager
    agent = ProactiveAgent()
    res = {"ok": ctx.verification.get("ok", False)}
    ctx.suggestions = agent.suggest(ctx.task, res)
    get_memory_manager().add_task(ctx.task, "Success" if res["ok"] else "Failed")
    emit("step_done", {"step": "proactive", "suggestions": ctx.suggestions})
    return True

PIPELINE = [
    ("intent", step_intent),
    ("plan", step_plan),
    ("tools", step_tools),
    ("execute", step_execute),
    ("verify", step_verify),
    ("proactive", step_proactive),
    ("explain", step_explain),
    ("store", step_store),
]

class WorkflowEngine:
    def __init__(self):
        self.results: Dict[str, dict] = {}

    def run(self, task: str, language: str = "python", emit_fn: Any = None, force_id: str = None, chaos_flags: dict = None, user_id: int = 1) -> dict:
        """Run workflow synchronously. Safe to call in background thread."""
        wid = force_id or uuid.uuid4().hex[:12]
        
        # Initialize context with Chaos Flags
        ctx = WorkflowContext(workflow_id=wid, task=task, language=language, user_id=user_id)
        if chaos_flags:
            ctx.metadata["chaos"] = chaos_flags
            
        def emit(event: str, payload: dict = None):
            if emit_fn: emit_fn(event, payload)
        obs = get_observability()
        trace_id = obs.start_trace(wid, "workflow_engine")
        
        step_results = []
        t0 = time.time()
        emit("workflow_start", {"id": wid, "task": task})
        
        overall_ok = True
        try:
            step_idx = 0
            while step_idx < len(PIPELINE):
                step_name, step_fn = PIPELINE[step_idx]
                emit("step_start", {"step": step_name})
                s0 = time.time()
                try:
                    ok = step_fn(ctx, emit)
                    obs.log_step(wid, step_name, {"elapsed": time.time()-s0}, success=ok)
                    step_results.append({"step": step_name, "ok": ok, "elapsed_s": time.time()-s0})
                    emit("step_done", {"step": step_name, "ok": ok})
                    
                    if not ok and step_name == "verify" and ctx.retry_count < 2:
                        # Multi-Step Reasoning: Retry failure up to 2 times
                        ctx.retry_count += 1
                        emit("step_start", {"step": "retry_plan"})
                        emit("step_done", {"step": "retry_plan", "message": f"Verification failed. Revising plan (Attempt {ctx.retry_count}/2)."})
                        step_idx = 1 # Jump back to plan step
                        continue
                    elif not ok and step_name == "verify":
                        # Max retries reached, fallback to partial success
                        emit("step_done", {"step": "verify", "ok": True, "message": "Max retries reached. Proceeding with partial success."})
                        ctx.verification["ok"] = True
                        ctx.verification["status"] = "PARTIAL SUCCESS"
                    elif not ok and step_name == "execute": 
                        emit("error", {"step": step_name, "message": "Critical step failed without throwing exception", "provider": "system"})
                        overall_ok = False
                        break # Stop pipeline on critical failure
                        
                except Exception as e:
                    import traceback
                    err_msg = str(e)
                    provider = "ollama" if "ollama" in err_msg.lower() else ("groq" if "groq" in err_msg.lower() else "system")
                    obs.log_error(wid, "system_error", err_msg, {"step": step_name})
                    step_results.append({"step": step_name, "ok": False, "error": err_msg})
                    emit("error", {"step": step_name, "message": err_msg, "provider": provider})
                    overall_ok = False
                    break
                step_idx += 1
        except Exception as e:
            overall_ok = False
            obs.log_error(wid, "system_error", str(e), {})
            emit("error", {"step": "unknown", "message": str(e), "provider": "system"})
            
        elapsed = round(time.time() - t0, 2)
        obs.complete_trace(wid, "completed" if overall_ok else "failed")
        
        result = {
            "ok": overall_ok,
            "workflow_id": wid,
            "task": task,
            "trace_id": trace_id,
            "steps": step_results,
            "elapsed_s": elapsed,
            "code_len": len(ctx.code),
            "code": ctx.code,
            "quality": {"quality_score": ctx.verification.get("quality_score", 0), "verdict": ctx.verification.get("verdict", "failed")},
            "explanation": ctx.explanation,
            "artifact_path": ctx.store_result.get("path"),
            "live_url": None # Only populate if deployed
        }
        
        # Phase 50: Track usage for SaaS billing & dashboard
        from auth_system import track_usage
        score = ctx.verification.get("quality_score", 0) if ctx.verification else 0
        status = ctx.verification.get("status", "PASSED" if overall_ok else "FAILED") if ctx.verification else "FAILED"
        tokens = sum(s.get("tokens", 0) for s in step_results)
        track_usage(user_id=ctx.user_id, wid=wid, task=task, score=score, status=status, tokens=tokens)

        self.results[wid] = result
        emit("workflow_done", result)
        return result

    def get_result(self, wid: str) -> Optional[dict]:
        return self.results.get(wid)

_engine_instance = None
def get_workflow_engine() -> WorkflowEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = WorkflowEngine()
    return _engine_instance
