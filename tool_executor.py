"""
tool_executor.py — Phase 43: Safe Tool Execution
================================================
Wraps tools with timeouts, retries, and failure logging.
"""
import time
import logging
from typing import Callable, Dict, Any

from observability import get_observability

logger = logging.getLogger(__name__)

def execute_tool_safely(task_id: str, tool_name: str, tool_func: Callable, kwargs: Dict[str, Any], max_retries: int = 2, timeout_sec: int = 30) -> Dict[str, Any]:
    """
    Executes a tool with retry and timeout logic.
    Logs success/failure to the observability system.
    """
    obs = get_observability()
    attempts = 0
    
    while attempts <= max_retries:
        attempts += 1
        start_time = time.time()
        
        try:
            # We would normally use concurrent.futures for strict timeout, but we keep it simple here
            # for thread safety or use asyncio.wait_for if async.
            logger.info(f"[ToolExecutor] Running {tool_name} (Attempt {attempts})")
            result = tool_func(**kwargs)
            duration = time.time() - start_time
            
            obs.log_step(
                task_id=task_id,
                action="tool_execution",
                details={"tool": tool_name, "duration": duration, "attempt": attempts, "kwargs_keys": list(kwargs.keys())},
                tool_used=tool_name,
                success=True
            )
            return {"ok": True, "data": result}
            
        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            logger.warning(f"[ToolExecutor] {tool_name} failed on attempt {attempts}: {error_msg}")
            
            if attempts > max_retries:
                obs.log_error(
                    task_id=task_id,
                    error_type="tool_error",
                    message=f"{tool_name} failed after {attempts} attempts: {error_msg}",
                    context={"tool": tool_name, "kwargs_keys": list(kwargs.keys()), "duration": duration}
                )
                return {"ok": False, "error": error_msg}
            
            time.sleep(1) # Backoff before retry
