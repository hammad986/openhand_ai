"""
observability.py — Phase 43: Observability System
=================================================
Structured logging, tracing, and error classification.
"""
import time
import json
import logging
from typing import Dict, Any, List, Optional
import uuid

logger = logging.getLogger(__name__)

class ObservabilitySystem:
    def __init__(self):
        self.traces = {}
        self.errors = []

    def start_trace(self, task_id: str, agent: str) -> str:
        trace_id = str(uuid.uuid4())
        self.traces[task_id] = {
            "trace_id": trace_id,
            "task_id": task_id,
            "agent": agent,
            "start_time": time.time(),
            "steps": [],
            "status": "running"
        }
        return trace_id

    def log_step(self, task_id: str, action: str, details: Dict[str, Any], model_used: str = None, tool_used: str = None, success: bool = True):
        if task_id in self.traces:
            step = {
                "timestamp": time.time(),
                "action": action,
                "details": details,
                "model_used": model_used,
                "tool_used": tool_used,
                "success": success
            }
            self.traces[task_id]["steps"].append(step)

    def log_error(self, task_id: str, error_type: str, message: str, context: Dict[str, Any]):
        """
        error_type: 'model_error', 'tool_error', 'system_error'
        """
        error_record = {
            "timestamp": time.time(),
            "task_id": task_id,
            "type": error_type,
            "message": message,
            "context": context
        }
        self.errors.append(error_record)
        if task_id in self.traces:
            self.traces[task_id]["steps"].append({"action": "error", "error_type": error_type, "message": message})
            self.traces[task_id]["status"] = "failed"
            logger.error(f"[Trace {task_id}] {error_type}: {message}")

    def complete_trace(self, task_id: str, final_status: str = "completed"):
        if task_id in self.traces:
            self.traces[task_id]["end_time"] = time.time()
            self.traces[task_id]["duration"] = self.traces[task_id]["end_time"] - self.traces[task_id]["start_time"]
            if self.traces[task_id]["status"] != "failed":
                self.traces[task_id]["status"] = final_status

    def get_trace(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.traces.get(task_id)

_obs_instance = None
def get_observability() -> ObservabilitySystem:
    global _obs_instance
    if _obs_instance is None:
        _obs_instance = ObservabilitySystem()
    return _obs_instance
