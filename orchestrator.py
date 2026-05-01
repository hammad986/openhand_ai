"""
orchestrator.py — Phase 43: Consolidated Orchestrator
======================================================
Now acts as a lightweight wrapper around the STRICT execution pipeline
in workflow_engine.py, removing massive redundancies from the old 2000-line
implementation.
"""
from __future__ import annotations
import logging
from workflow_engine import get_workflow_engine

logger = logging.getLogger(__name__)

class Orchestrator:
    def __init__(self, config=None, memory=None, **kwargs):
        self.config = config
        self.memory = memory
        self.engine = get_workflow_engine()

    def run(self, task: str) -> dict:
        """
        Delegates completely to the WorkflowEngine to enforce
        the SINGLE strict pipeline:
        task → plan → tools → execute → verify → explain → store
        """
        logger.info(f"[Orchestrator] Delegating task to WorkflowEngine: {task[:50]}...")
        
        def _emit(kind: str, payload: dict):
            import json
            # Ensure serialization is safe
            payload_safe = {k: str(v) if not isinstance(v, (int, float, bool, str, list, dict, type(None))) else v for k, v in payload.items()}
            print(f"[ORCHESTRATOR] {json.dumps({'kind': kind, **payload_safe})}", flush=True)

        return self.engine.run(task=task, emit_fn=_emit)

    def think(self, task: str) -> dict:
        # Kept for backward compatibility but stripped of duplicated heuristics
        return {"complexity": "delegated", "strategy_hint": "workflow_engine"}

    def plan(self, task: str, complexity: str) -> list[str]:
        # Kept for backward compatibility 
        return ["Delegated to workflow_engine step_plan"]

    def execute(self, subtask: str, strategy: str, **kwargs) -> dict:
        # Kept for backward compatibility
        return self.run(subtask)

