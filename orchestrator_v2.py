import json
import logging
import time
import re
import concurrent.futures
from typing import List, Dict, Any, Optional
from mcp_context import MCPContext
from knowledge_graph import KnowledgeGraph
from tool_engine import ToolEngine
from reasoning_engine_v2 import ReasoningEngine

logger = logging.getLogger("orchestrator_v2")

class SubTask:
    def __init__(self, id: str, description: str, dependencies: List[str]):
        self.id = id
        self.description = description
        self.dependencies = dependencies
        self.status = "pending" # pending, running, completed, failed
        self.result = None
        self.assigned_agent = None

class WorldInteractionLayer:
    """Phase 27: World Interaction Layer handling external API calls and browser access."""
    def __init__(self, llm: Any):
        self.llm = llm

    def fetch_url(self, url: str) -> str:
        """Safe data fetching abstraction."""
        return f"[Mock World Data] Fetched content from {url}"

    def explore_api(self, endpoint: str) -> str:
        """API exploration utility."""
        return f"[Mock API Spec] Extracted endpoints for {endpoint}"

class OrchestratorV2:
    """Phase 27: Multi-Agent Orchestrator & Long-Term Planning Engine."""
    def __init__(self, llm: Any, kg: KnowledgeGraph):
        self.llm = llm
        self.kg = kg
        self.mcp = MCPContext()
        
        # Core engines
        self.reasoning_engine = ReasoningEngine(self.llm, self.kg)
        self.tool_engine = ToolEngine(self.llm, self.kg)
        self.world = WorldInteractionLayer(self.llm)
        
        self.tasks: Dict[str, SubTask] = {}
        
    def _call_llm(self, prompt: str) -> str:
        if hasattr(self.llm, "chat") and callable(getattr(self.llm, "chat")):
            return self.llm.chat([{"role": "user", "content": prompt}], max_tokens=2048)
        if callable(self.llm):
            return self.llm(prompt)
        return ""

    def planner_breakdown(self, goal: str) -> List[SubTask]:
        """Planner Agent: Breaks large goals into parallel sub-tasks."""
        prompt = f"""
You are the Planner Agent. Break down the following goal into discrete sub-tasks.
Goal: {goal}

Return exactly a JSON list of dictionaries with keys:
- id: task identifier (e.g., 'task_1')
- description: what needs to be done
- dependencies: list of task IDs that must complete first

Respond ONLY with valid JSON.
"""
        response = self._call_llm(prompt)
        tasks = []
        try:
            match = re.search(r'\[.*\]', response, re.DOTALL)
            raw_data = match.group(0) if match else response
            data = json.loads(raw_data)
            for item in data:
                tasks.append(SubTask(
                    id=item.get("id"),
                    description=item.get("description"),
                    dependencies=item.get("dependencies", [])
                ))
        except Exception as e:
            logger.error(f"Planner failed to parse tasks: {e}")
            # Fallback to a single task
            tasks.append(SubTask("task_1", goal, []))
        return tasks

    def execute_task(self, task: SubTask):
        """Executor Agent / Coder Agent handling a specific task."""
        logger.info(f"[Orchestrator] Starting task: {task.id}")
        self.mcp.broadcast_message("Orchestrator", "Coder", f"Start {task.id}")
        
        # Check world interaction requirements
        if "http" in task.description or "api" in task.description.lower():
            self.mcp.broadcast_message("Coder", "Researcher", f"Need world data for {task.id}")
            world_data = self.world.fetch_url("http://example.com/api")
            task.description += f"\nWorld Context: {world_data}"
            
        # Phase 28: Uncertainty Detection & Human-in-the-Loop
        from uncertainty_engine import UncertaintyEngine
        uncertainty = UncertaintyEngine(self.llm, self.kg, self.mcp)
        
        confidence = uncertainty.evaluate_confidence(task.description)
        if confidence < 0.6:
            decision = uncertainty.trigger_human_in_loop(task.description)
            task.description += f"\n\n[CRITICAL HUMAN INSTRUCTION]: {decision}"
        
        # Trigger Reasoning Engine (Phase 26A-D integration)
        res = self.reasoning_engine.run_dev_loop(task.description)
        
        if res.get("success"):
            task.status = "completed"
            task.result = res.get("final_code")
            self.mcp.broadcast_message("Coder", "Orchestrator", f"Task {task.id} success.")
        else:
            task.status = "failed"
            self.mcp.broadcast_message("Coder", "Debugger", f"Task {task.id} failed, requires manual review.")
            
        self.mcp.update("tasks", task.id, {"status": task.status, "result": task.result})
        return task

    def run_goal(self, main_goal: str) -> Dict[str, Any]:
        """Main Long-Term Execution Loop."""
        logger.info(f"--- Starting Multi-Agent Orchestrator for Goal ---")
        
        # 1. Planning Phase
        subtasks = self.planner_breakdown(main_goal)
        for t in subtasks:
            self.tasks[t.id] = t
            
        self.mcp.update("tasks", "plan", [{"id": t.id, "desc": t.description} for t in subtasks])
        
        # 2. Parallel Execution Engine
        # Constraints: limit agent spawning / thread pooling to 3 parallel max
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            while any(t.status in ["pending", "running"] for t in self.tasks.values()):
                # Find unblocked tasks
                executable = [
                    t for t in self.tasks.values() 
                    if t.status == "pending" and all(self.tasks[dep].status == "completed" for dep in t.dependencies)
                ]
                
                if not executable:
                    # Check for deadlock
                    if any(t.status == "failed" for t in self.tasks.values()):
                        logger.error("Deadlock reached due to failed dependency.")
                        break
                    time.sleep(1)
                    continue
                
                # Dispatch tasks
                future_to_task = {}
                for t in executable:
                    t.status = "running"
                    self.mcp.update("tasks", t.id, {"status": "running"})
                    future = executor.submit(self.execute_task, t)
                    future_to_task[future] = t
                
                # Wait for at least one to finish before checking again
                for future in concurrent.futures.as_completed(future_to_task):
                    completed_task = future_to_task[future]
                    try:
                        future.result() # raises exceptions if any
                    except Exception as e:
                        logger.error(f"Task {completed_task.id} raised exception: {e}")
                        completed_task.status = "failed"

        # 3. Result Merging & Feedback Loop
        final_results = {t_id: t.result for t_id, t in self.tasks.items()}
        success_count = sum(1 for t in self.tasks.values() if t.status == "completed")
        
        # Observability Logging
        log_payload = {
            "agents": ["planner", "coder", "debugger", "researcher", "executor"],
            "tasks": len(self.tasks),
            "completed": success_count,
            "strategy": "parallel execution via ThreadPool",
            "mcp_version": self.mcp.version
        }
        logger.info(json.dumps(log_payload))
        self.mcp.update("memory", "last_run", log_payload)

        return {
            "goal": main_goal,
            "success": success_count == len(self.tasks),
            "results": final_results,
            "observability": log_payload
        }
