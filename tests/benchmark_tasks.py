import os
import sys
import time
import json
try:
    import psutil
except ImportError:
    psutil = None

# Add parent dir to path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import MultiAgentOrchestrator
from safety_layer import tracker

class BenchmarkEngine:
    def __init__(self):
        self.tasks = [
            "Build a simple flask hello world app running on port 5000",
            "Fix this index error array: def get_item(arr, i): return arr[i] where arr=[1,2] and i=5",
            "Optimize this loop: for i in range(1000000): sum += i"
        ]
        self.results = []
        
    def run_benchmarks(self):
        print("Starting Real-World Benchmark Engine...")
        start_time = time.time()
        
        # Instantiate the orchestrator (simulated/mocked if LLM is unavailable)
        try:
            # Try to get an actual model or fallback
            model = None 
        except Exception:
            model = None
            
        orchestrator = MultiAgentOrchestrator(model=model, max_attempts=1)
        
        for idx, task in enumerate(self.tasks):
            print(f"\n--- Running Benchmark Task {idx+1}/{len(self.tasks)} ---")
            print(f"Task: {task}")
            
            task_start = time.time()
            
            try:
                # Capture initial memory/cpu
                if psutil:
                    cpu_before = psutil.cpu_percent(interval=None)
                else:
                    cpu_before = 0
                
                result = orchestrator.run(task)
                
                duration = time.time() - task_start
                success = result.get("ok", False)
                
                self.results.append({
                    "task": task,
                    "success": success,
                    "duration": duration,
                    "reason": result.get("reason", "unknown")
                })
                
                print(f"Result: {'SUCCESS' if success else 'FAILED'} (Duration: {duration:.2f}s)")
                
            except Exception as e:
                duration = time.time() - task_start
                print(f"Result: ERROR - {str(e)} (Duration: {duration:.2f}s)")
                self.results.append({
                    "task": task,
                    "success": False,
                    "duration": duration,
                    "reason": str(e)
                })
                
        total_time = time.time() - start_time
        success_count = sum(1 for r in self.results if r["success"])
        success_rate = (success_count / len(self.tasks)) * 100 if self.tasks else 0
        avg_time = total_time / len(self.tasks) if self.tasks else 0
        
        print("\n==================================================")
        print("BENCHMARK RESULTS")
        print("==================================================")
        print(f"Total Tasks:    {len(self.tasks)}")
        print(f"Successful:     {success_count}")
        print(f"Success Rate:   {success_rate:.1f}%")
        print(f"Total Time:     {total_time:.2f}s")
        print(f"Average Time:   {avg_time:.2f}s")
        print("==================================================")
        
        # Merge with ObservabilityTracker
        if tracker:
            for r in self.results:
                tracker.record_task(
                    success=r["success"],
                    exec_time=r["duration"],
                    provider="benchmark",
                    error_type=r["reason"] if not r["success"] else None
                )
            print("Metrics merged into global tracker.")

if __name__ == "__main__":
    benchmark = BenchmarkEngine()
    benchmark.run_benchmarks()
