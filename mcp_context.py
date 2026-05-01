import time
import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("mcp_context")

class MCPContext:
    """
    Phase 27: Multi-Context Protocol (MCP)
    A unified context layer providing shared state and versioning across all agents.
    """
    def __init__(self):
        self.version = 1
        self.history: List[Dict[str, Any]] = []
        self.state: Dict[str, Dict[str, Any]] = {
            "tasks": {},
            "agents": {},
            "memory": {},
            "tools": {},
            "world": {}
        }

    def update(self, category: str, key: str, value: Any):
        """Update a specific context category and increment version."""
        if category not in self.state:
            self.state[category] = {}
        
        # Save history for rollback/versioning tracking
        old_value = self.state[category].get(key)
        self.history.append({
            "version": self.version,
            "category": category,
            "key": key,
            "old_value": old_value,
            "timestamp": time.time()
        })
        
        self.state[category][key] = value
        self.version += 1
        
        # Cap history to avoid memory bloat (Phase 28 cleanup)
        if len(self.history) > 500:
            del self.history[:len(self.history) - 500]

    def get_context(self, category: str) -> Dict[str, Any]:
        """Retrieve the current context for a category."""
        return self.state.get(category, {})

    def get_full_context(self) -> Dict[str, Any]:
        """Retrieve the fully unified context state."""
        return {
            "version": self.version,
            "state": self.state
        }
        
    def rollback(self, target_version: int):
        """Revert the state back to a specific version based on history."""
        if target_version >= self.version:
            return
            
        logger.info(f"Rolling back MCP Context from v{self.version} to v{target_version}")
        # Rollback by applying old values backwards
        for record in reversed(self.history):
            if record["version"] >= target_version:
                cat = record["category"]
                k = record["key"]
                if record["old_value"] is None:
                    if k in self.state[cat]:
                        del self.state[cat][k]
                else:
                    self.state[cat][k] = record["old_value"]
        
        self.history = [r for r in self.history if r["version"] < target_version]
        self.version = target_version

    def broadcast_message(self, sender: str, receiver: str, content: str):
        """Multi-Agent Communication message bus."""
        msg = {
            "sender": sender,
            "receiver": receiver,
            "content": content,
            "timestamp": time.time()
        }
        if "messages" not in self.state["agents"]:
            self.state["agents"]["messages"] = []
        self.state["agents"]["messages"].append(msg)
        self.update("agents", "last_message", msg)
