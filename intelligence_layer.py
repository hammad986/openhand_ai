"""
intelligence_layer.py — Phase 46: Autonomous Intelligence
=========================================================
Adds intent classification, context memory, and proactive reasoning to the workflow engine.
"""
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Any

from model_router import get_router

logger = logging.getLogger(__name__)

@dataclass
class TaskIntent:
    task_type: str        # build_task, analysis_task, research_task, system_task
    required_tools: List[str] # browser, github, file, local_sandbox
    confidence: float
    reasoning: str

class MemoryManager:
    def __init__(self, max_history: int = 5):
        self.max_history = max_history
        self.history = []

    def add_task(self, task: str, result: str):
        self.history.append({"task": task, "result": result})
        if len(self.history) > self.max_history:
            self.history.pop(0)

    def get_context(self) -> str:
        if not self.history:
            return "No previous tasks."
        ctx = "Recent task history:\n"
        for i, item in enumerate(self.history):
            ctx += f"{i+1}. Task: {item['task']} -> Result: {item['result']}\n"
        return ctx

class IntentClassifier:
    def classify(self, task: str, memory_context: str) -> TaskIntent:
        router = get_router()
        prompt = f"""
You are the Intent Classification Layer of an autonomous AI.
Classify the following task into exactly one of these types:
- build_task (website, app, API)
- analysis_task (resume, github, file)
- research_task (web scraping, search)
- system_task (debug, fix, improve)

Available tools to select from (choose 1 or more): [browser, github, file, local_sandbox]

Recent context:
{memory_context}

Task: {task}

Return ONLY a JSON object:
{{
    "task_type": "...",
    "required_tools": ["..."],
    "confidence": 0.95,
    "reasoning": "Brief explanation"
}}
"""
        try:
            res = router.call(prompt, task_type="plan")
            data = res.get("text", "")
            if data.startswith("```json"): data = data.strip()[7:-3]
            elif data.startswith("```"): data = data.strip()[3:-3]
            
            parsed = json.loads(data)
            return TaskIntent(
                task_type=parsed.get("task_type", "system_task"),
                required_tools=parsed.get("required_tools", ["local_sandbox"]),
                confidence=float(parsed.get("confidence", 0.5)),
                reasoning=parsed.get("reasoning", "Fallback parsing")
            )
        except Exception as e:
            logger.error(f"Intent classification failed: {e}")
            return TaskIntent("build_task", ["local_sandbox"], 0.1, "Fallback due to error")

class ProactiveAgent:
    def suggest(self, task: str, execution_result: dict) -> Dict[str, Any]:
        router = get_router()
        status = "Success" if execution_result.get("ok") else "Failed"
        prompt = f"""
You are the Proactive Intelligence Layer.
The system just completed a task: {task}
Status: {status}

Provide 2 constructive suggestions for what the user or system should do next.
Return ONLY a JSON object:
{{
    "improvements": "1-sentence suggestion on how to improve this artifact",
    "next_steps": ["Step 1", "Step 2"]
}}
"""
        try:
            res = router.call(prompt, task_type="plan")
            data = res.get("text", "")
            if data.startswith("```json"): data = data.strip()[7:-3]
            elif data.startswith("```"): data = data.strip()[3:-3]
            return json.loads(data)
        except Exception:
            return {"improvements": "Review logs for details.", "next_steps": ["Check output"]}

_memory_instance = None
def get_memory_manager() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = MemoryManager()
    return _memory_instance
