import os
import time
import json
try:
    import psutil
except ImportError:
    psutil = None
import threading

class ExecutionController:
    def __init__(self, max_iterations=3, max_commands=10, max_runtime=60):
        self.max_iterations = max_iterations
        self.max_commands = max_commands
        self.max_runtime = max_runtime
        
        self.current_iterations = 0
        self.current_commands = 0
        self.start_time = time.time()
        
        # Infinite Loop Detection
        self.error_history = []
        self.no_progress_count = 0
        
        # Resource constraints
        self.max_concurrent_tasks = 2
        self.max_threads = 3
        
    def reset(self):
        self.current_iterations = 0
        self.current_commands = 0
        self.start_time = time.time()
        self.error_history = []
        self.no_progress_count = 0

    def check_limits(self):
        elapsed = time.time() - self.start_time
        if self.current_iterations >= self.max_iterations:
            return {"terminated": True, "reason": "limit_exceeded", "iterations": self.current_iterations, "commands": self.current_commands, "detail": "max_iterations"}
        if self.current_commands >= self.max_commands:
            return {"terminated": True, "reason": "limit_exceeded", "iterations": self.current_iterations, "commands": self.current_commands, "detail": "max_commands"}
        if elapsed >= self.max_runtime:
            return {"terminated": True, "reason": "limit_exceeded", "iterations": self.current_iterations, "commands": self.current_commands, "detail": "max_runtime"}
        
        # Thread & resource limits
        if threading.active_count() > self.max_threads:
            pass # Soft limit logging can be added here
        
        try:
            cpu_usage = psutil.cpu_percent(interval=None)
            if cpu_usage > 90.0:
                pass # Soft CPU limit exceeded
        except Exception:
            pass
            
        return None

    def record_error(self, error_str):
        if not error_str:
            return None
        self.error_history.append(error_str)
        if len(self.error_history) >= 2 and self.error_history[-1] == self.error_history[-2]:
            return {"terminated": True, "reason": "loop_detected", "detail": "same error repeated"}
        return None
        
    def record_no_progress(self):
        self.no_progress_count += 1
        if self.no_progress_count >= 2:
            return {"terminated": True, "reason": "loop_detected", "detail": "no_progress"}
        return None


class TerminalSafety:
    BLOCKED_CMDS = ["rm -rf", "del /f", "format", "shutdown", ":(){ :|:& };:"]
    
    @staticmethod
    def is_safe(cmd: str, cwd: str) -> bool:
        if not cmd:
            return False
            
        cmd_lower = cmd.lower().strip()
        
        # Block network shells
        if "nc -e" in cmd_lower or "bash -i" in cmd_lower or "powershell -nop -c" in cmd_lower:
            return False
            
        for blocked in TerminalSafety.BLOCKED_CMDS:
            if blocked in cmd_lower:
                return False
                
        # Validate CWD
        project_dir = os.path.abspath(os.path.dirname(__file__))
        target_dir = os.path.abspath(cwd)
        if not target_dir.startswith(project_dir):
            return False
            
        return True


class ObservabilityTracker:
    def __init__(self, metrics_file="data/metrics.json"):
        self.metrics_file = metrics_file
        self.load()
        
    def load(self):
        try:
            if os.path.exists(self.metrics_file):
                with open(self.metrics_file, "r") as f:
                    self.metrics = json.load(f)
            else:
                self.metrics = {
                    "total_tasks": 0,
                    "successful_tasks": 0,
                    "success_rate": 0.0,
                    "total_execution_time": 0.0,
                    "avg_execution_time": 0.0,
                    "provider_usage": {},
                    "error_types_frequency": {}
                }
        except Exception:
            self.metrics = {
                "total_tasks": 0,
                "successful_tasks": 0,
                "success_rate": 0.0,
                "total_execution_time": 0.0,
                "avg_execution_time": 0.0,
                "provider_usage": {},
                "error_types_frequency": {}
            }
            
    def save(self):
        os.makedirs(os.path.dirname(self.metrics_file), exist_ok=True)
        try:
            with open(self.metrics_file, "w") as f:
                json.dump(self.metrics, f, indent=2)
        except Exception:
            pass
            
    def record_task(self, success: bool, exec_time: float, provider: str, error_type: str = None):
        self.metrics["total_tasks"] += 1
        if success:
            self.metrics["successful_tasks"] += 1
            
        if self.metrics["total_tasks"] > 0:
            self.metrics["success_rate"] = self.metrics["successful_tasks"] / self.metrics["total_tasks"]
        
        self.metrics["total_execution_time"] += exec_time
        if self.metrics["total_tasks"] > 0:
            self.metrics["avg_execution_time"] = self.metrics["total_execution_time"] / self.metrics["total_tasks"]
        
        if provider:
            self.metrics["provider_usage"][provider] = self.metrics["provider_usage"].get(provider, 0) + 1
            
        if error_type:
            self.metrics["error_types_frequency"][error_type] = self.metrics["error_types_frequency"].get(error_type, 0) + 1
            
        self.save()

# Global instances
tracker = ObservabilityTracker()

import re

# Basic patterns for prompt injection / jailbreaks
INJECTION_PATTERNS = [
    r"(?i)(ignore\s+all\s+previous\s+instructions)",
    r"(?i)(you\s+are\s+now\s+in\s+developer\s+mode)",
    r"(?i)(system\s+prompt)",
    r"(?i)(bypass\s+rules)",
    r"(?i)(print\s+your\s+instructions)"
]

# Patterns for data exfiltration
EXFILTRATION_PATTERNS = [
    r"(?i)(curl|wget)\s+.*http.*(\?|&)data=",
    r"(?i)nc\s+-e\s+/bin/(ba)?sh"
]

def check_prompt_safety(prompt: str) -> tuple[bool, str]:
    """Check if the prompt contains malicious patterns."""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, prompt):
            return False, "Prompt injection attempt detected."
            
    for pattern in EXFILTRATION_PATTERNS:
        if re.search(pattern, prompt):
            return False, "Data exfiltration attempt detected."
            
    return True, "Safe"

def check_system_resources() -> tuple[bool, str]:
    """Monitor CPU and Memory to prevent OOM or starvation."""
    try:
        mem = psutil.virtual_memory()
        if mem.percent > 90.0:
            return False, f"Memory usage critical ({mem.percent}%). Throttling new tasks."
            
        cpu = psutil.cpu_percent(interval=0.1)
        if cpu > 95.0:
            return False, f"CPU usage critical ({cpu}%). Throttling new tasks."
    except Exception as e:
        pass
        
    return True, "Resources OK"

def sanitize_filepath(path: str) -> str:
    """Ensure filepath doesn't escape workspace (Path traversal defense)."""
    if ".." in path or str(path).startswith("/"):
        raise PermissionError("Path traversal blocked. Allowed only inside ./workspace")
    return path

def enforce_tool_sandbox(tool_name: str, params: dict) -> tuple[bool, str]:
    """Ensure tools operate within strict sandboxes."""
    if tool_name in ["file_read", "file_write", "file"]:
        path = str(params.get("path", ""))
        if ".." in path or path.startswith("/"):
            return False, "Path traversal blocked. Use ./workspace only."
            
    elif tool_name == "browser":
        url = str(params.get("url", ""))
        # Safe mode / restricted domains
        blocked_domains = ["internal.corp", "localhost:admin", "169.254.169.254"]
        if any(d in url for d in blocked_domains):
            return False, "Browser access to internal/restricted domains is blocked."
            
    elif tool_name == "api_caller":
        url = str(params.get("url", ""))
        allowlist = ["api.github.com", "api.weather.gov", "openrouter.ai", "api.groq.com"]
        if not any(d in url for d in allowlist):
            return False, f"API caller restricted to allowlist. {url} blocked."
            
    return True, "Allowed"
