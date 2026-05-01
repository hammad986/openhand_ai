import os
import sys
import json
import logging
import time
import re
from typing import Dict, Any, List, Optional

from code_runner import run_code
from knowledge_graph import KnowledgeGraph, NodeTypes, EdgeTypes

logger = logging.getLogger("tool_engine")

class ToolRegistry:
    """Maintains discovery metadata for all self-generated AI tools."""
    def __init__(self, tools_dir: str = "tools/generated"):
        self.tools_dir = tools_dir
        self.tools: Dict[str, Dict[str, Any]] = {}
        os.makedirs(self.tools_dir, exist_ok=True)
        self.load_all()

    def load_all(self):
        registry_file = os.path.join(self.tools_dir, "registry.json")
        if os.path.exists(registry_file):
            try:
                with open(registry_file, "r") as f:
                    self.tools = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load tool registry: {e}")

    def save_registry(self):
        registry_file = os.path.join(self.tools_dir, "registry.json")
        with open(registry_file, "w") as f:
            json.dump(self.tools, f, indent=2)

    def register_tool(self, name: str, description: str, input_schema: str, code: str):
        filepath = os.path.join(self.tools_dir, f"{name}.py")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)
        
        self.tools[name] = {
            "description": description,
            "input_schema": input_schema,
            "filepath": filepath
        }
        self.save_registry()

    def list_tools(self) -> List[Dict[str, Any]]:
        return [{"name": k, **v} for k, v in self.tools.items()]


class ToolEngine:
    """Phase 26D: Tool Creation & Self-Improving Intelligence Layer."""
    def __init__(self, llm: Any, kg: KnowledgeGraph):
        self.llm = llm
        self.kg = kg
        self.registry = ToolRegistry()

    def _call_llm(self, prompt: str) -> str:
        if hasattr(self.llm, "chat") and callable(getattr(self.llm, "chat")):
            return self.llm.chat([{"role": "user", "content": prompt}], max_tokens=2048)
        if callable(self.llm):
            return self.llm(prompt)
        return ""

    def evaluate_tool_needs(self, task: str) -> Optional[Dict[str, str]]:
        """Analyzes task against existing registry to avoid tool explosion."""
        existing_tools = "\n".join([f"- {t['name']}: {t['description']}" for t in self.registry.list_tools()])
        
        prompt = f"""
Task: {task}

Existing Tools in Registry:
{existing_tools if existing_tools else "None"}

Analyze if the task requires a REUSABLE utility/tool that doesn't exist yet (e.g., API testing mock, complex data parser, custom validator).
If an existing tool suffices, or if no dedicated tool is needed, respond with "needs_new_tool": false.
Otherwise, propose a new tool.

Return exactly valid JSON:
{{
  "needs_new_tool": true/false,
  "tool_name": "name_in_snake_case",
  "tool_description": "brief functional description",
  "tool_input_schema": "def func_name(arg1: str) -> dict:"
}}
"""
        response = self._call_llm(prompt)
        try:
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if data.get("needs_new_tool"):
                    return data
        except Exception as e:
            logger.error(f"[tool_engine] Evaluation failed: {e}")
        return None

    def create_tool(self, spec: Dict[str, str]) -> bool:
        """Generates, validates, and registers a new tool dynamically."""
        name = spec.get("tool_name", f"tool_{int(time.time())}")
        desc = spec.get("tool_description", "")
        schema = spec.get("tool_input_schema", "")
        
        prompt = f"""
Create a standalone, highly-reliable Python module for this tool:
Name: {name}
Description: {desc}
Schema: {schema}

CRITICAL SAFETY RULES:
- Do NOT use `os.system`, `subprocess`, `eval()`, or `exec()`.
- Keep it stateless and purely functional.
Return ONLY valid Python code block.
"""
        response = self._call_llm(prompt)
        code = ""
        match = re.search(r'```(?:python)?\n(.*?)\n```', response, re.DOTALL)
        if match:
            code = match.group(1).strip()
        else:
            code = response.strip()

        # PART 7: Safety Layer & Sandbox validation
        dangerous_patterns = [
            "subprocess", "os.system", "os.popen", "eval(", "exec(", 
            "__import__", "getattr", "setattr", "delattr", "socket", 
            "shutil", "builtins"
        ]
        if any(p in code.lower() for p in dangerous_patterns):
            logger.warning(f"Tool {name} rejected due to dangerous operations.")
            return False

        # Sandbox execution smoke test - Enable strict import restriction
        smoke_test_code = code + f"\n\nprint('Tool {name} initialized successfully.')"
        res = run_code(smoke_test_code, timeout=3.0, restrict_imports=True)
        
        if res.status == "success":
            self.registry.register_tool(name, desc, schema, code)
            
            # Observability logging
            logger.info(json.dumps({
                "tool_created": name,
                "reason": desc,
                "used_in_task": True
            }))
            
            # Integration with Knowledge Graph
            tool_id = f"tool::{name}"
            self.kg.add_node(tool_id, NodeTypes.PATTERN, {"description": desc, "schema": schema})
            return True
        else:
            logger.warning(f"Tool {name} failed validation sandbox: {res.error}")
            return False

    def self_improve(self, task: str, execution_results: List[Any]):
        """PART 4: Analyzes repeated errors post-task to optimize architecture."""
        errors = []
        for h in execution_results:
            if hasattr(h, "run_result") and h.run_result and h.run_result.get("status") != "success":
                err = str(h.run_result.get("error", ""))
                if err:
                    errors.append(err)
                    
        if not errors:
            return
            
        prompt = f"""
The system struggled with the task '{task}'.
Repeated execution errors encountered:
{errors}

Is there a generalized helper function, validator, or parser tool that could permanently prevent these errors across the architecture?
If yes, return JSON:
{{
  "needs_new_tool": true,
  "tool_name": "name_in_snake_case",
  "tool_description": "description",
  "tool_input_schema": "def func(val):..."
}}
Otherwise return needs_new_tool: false.
"""
        response = self._call_llm(prompt)
        try:
            match = re.search(r'\{.*\}', response, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if data.get("needs_new_tool"):
                    logger.info("Self-Improvement loop triggered new tool creation.")
                    self.create_tool(data)
        except Exception as e:
            pass
