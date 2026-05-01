import time
import json
import logging
from typing import Dict, Any, List

from knowledge_graph import KnowledgeGraph, NodeTypes, EdgeTypes
from mcp_context import MCPContext

logger = logging.getLogger("uncertainty")

class UncertaintyEngine:
    """Phase 28: Human-in-the-Loop + Uncertainty Resolution System."""
    def __init__(self, llm: Any, kg: KnowledgeGraph, mcp: MCPContext):
        self.llm = llm
        self.kg = kg
        self.mcp = mcp
        
    def _call_llm(self, prompt: str) -> str:
        if hasattr(self.llm, "chat") and callable(getattr(self.llm, "chat")):
            return self.llm.chat([{"role": "user", "content": prompt}], max_tokens=1024)
        if callable(self.llm):
            return self.llm(prompt)
        return ""
        
    def evaluate_confidence(self, task_description: str) -> float:
        """PART 1 & 5: Detect ambiguity and generate a confidence score (0-1)."""
        prompt = f"""
Task: {task_description}

Evaluate the clarity and lack of ambiguity in this task. Are there multiple equally valid but conflicting implementation strategies? Is critical information missing?
Rate confidence from 0.0 to 1.0, where 1.0 means perfectly clear and only one right way.
Return ONLY valid JSON: {{"confidence_score": 0.85, "reason": "..."}}
"""
        try:
            res = self._call_llm(prompt)
            import re
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                return float(data.get("confidence_score", 1.0))
        except Exception as e:
            logger.error(f"[Uncertainty] Evaluation failed: {e}")
            
        return 0.8 # Default safe score

    def generate_query(self, task_description: str) -> Dict[str, Any]:
        """PART 2: Generate structured options for human clarification."""
        prompt = f"""
Task: {task_description}

The system is uncertain about the best approach. 
Generate a clarification question for the human user and 3-4 distinct implementation options.
Include a 'Safest Option' flag.
Return ONLY valid JSON:
{{
  "question": "Which approach should be used?",
  "options": [
    "Option 1",
    "Option 2"
  ],
  "safest_option": "Option 1"
}}
"""
        try:
            res = self._call_llm(prompt)
            import re
            match = re.search(r'\{.*\}', res, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                data["id"] = f"query_{int(time.time())}"
                return data
        except Exception:
            pass
            
        return {
            "id": f"query_{int(time.time())}",
            "question": "How should we proceed?",
            "options": ["Proceed with standard implementation", "Abort"],
            "safest_option": "Proceed with standard implementation"
        }

    def trigger_human_in_loop(self, task_description: str) -> str:
        """PART 4 & 7: Pause execution, query human, wait for response or fallback."""
        query_data = self.generate_query(task_description)
        
        logger.warning(f"Triggering Human-in-the-Loop: {query_data['question']}")
        
        # Store in MCP to trigger UI updates and pause execution
        self.mcp.update("world", "pending_human_query", query_data)
        
        # Wait loop (simulating async UI wait)
        wait_time = 0
        timeout = 45 # Wait up to 45s for human interaction
        
        while wait_time < timeout:
            # Check if UI resolved it in MCP
            world_state = self.mcp.get_context("world")
            if "human_response" in world_state:
                response = world_state["human_response"]
                if response.get("query_id") == query_data["id"]:
                    decision = response.get("decision")
                    logger.info(f"Human provided decision: {decision}")
                    self._learn_decision(task_description, decision)
                    # Clear query
                    self.mcp.update("world", "pending_human_query", None)
                    return decision
            
            time.sleep(1)
            wait_time += 1
            
        # PART 7: Fallback
        fallback = query_data.get("safest_option", query_data["options"][0])
        logger.warning(f"Human inactive. Fallback to safest option: {fallback}")
        self._learn_decision(task_description, fallback)
        self.mcp.update("world", "pending_human_query", None)
        return fallback

    def _learn_decision(self, task: str, decision: str):
        """PART 6: Store user preference in Knowledge Graph for future automated routing."""
        node_id = f"decision::{hash(task)}"
        self.kg.add_node(node_id, NodeTypes.PATTERN, {"task": task, "preferred_strategy": decision})
        self.kg.save()
