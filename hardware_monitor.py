"""
hardware_monitor.py — Phase 36: Hardware-Aware Adaptation
==========================================================
Monitors CPU/RAM continuously. Drives "Low Resource Mode"
and eligible model filters (e.g. blocking Ollama on low RAM).
Catches SIGINT (Ctrl+C) for graceful shutdown.
"""
import logging
import os
import signal
import sys
import threading
import time
from typing import Callable, Dict, List

import psutil

logger = logging.getLogger(__name__)

# Constants
OLLAMA_MIN_RAM_GB = 4.5  # Need 4.5GB free to safely run small LLMs
HIGH_MEM_THRESHOLD_PCT = 85.0
HIGH_CPU_THRESHOLD_PCT = 90.0

class HardwareMonitor:
    def __init__(self):
        self.cpu_pct = 0.0
        self.mem_pct = 0.0
        self.mem_avail_gb = 0.0
        self.low_resource_mode = False
        self.ollama_eligible = True
        
        self._history: List[Dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._shutdown_hooks: List[Callable] = []

        self._start_monitor()
        self._install_signal_handler()

    def _start_monitor(self):
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def _monitor_loop(self):
        while not self._stop.is_set():
            try:
                cpu = psutil.cpu_percent(interval=1.0)
                mem = psutil.virtual_memory()
                mem_avail = mem.available / (1024**3)
                
                # Update state
                with self._lock:
                    self.cpu_pct = cpu
                    self.mem_pct = mem.percent
                    self.mem_avail_gb = mem_avail
                    
                    self.ollama_eligible = self.mem_avail_gb >= OLLAMA_MIN_RAM_GB
                    
                    if self.mem_pct > HIGH_MEM_THRESHOLD_PCT or self.cpu_pct > HIGH_CPU_THRESHOLD_PCT:
                        if not self.low_resource_mode:
                            logger.warning("[HardwareMonitor] High load detected. Enabling Low Resource Mode.")
                        self.low_resource_mode = True
                    elif self.mem_pct < (HIGH_MEM_THRESHOLD_PCT - 10) and self.cpu_pct < (HIGH_CPU_THRESHOLD_PCT - 20):
                        if self.low_resource_mode:
                            logger.info("[HardwareMonitor] Load normalized. Disabling Low Resource Mode.")
                        self.low_resource_mode = False

                    # Keep last 60 seconds of history
                    self._history.append({
                        "ts": time.time(),
                        "cpu": cpu,
                        "mem": mem.percent
                    })
                    if len(self._history) > 60:
                        self._history.pop(0)

            except Exception as e:
                logger.error(f"[HardwareMonitor] Error in loop: {e}")
            time.sleep(1.0)

    def status(self) -> dict:
        with self._lock:
            return {
                "cpu_pct": self.cpu_pct,
                "mem_pct": self.mem_pct,
                "mem_avail_gb": round(self.mem_avail_gb, 2),
                "low_resource_mode": self.low_resource_mode,
                "ollama_eligible": self.ollama_eligible,
                "history": self._history[-30:] # Last 30 seconds for UI
            }

    def register_shutdown_hook(self, hook: Callable):
        self._shutdown_hooks.append(hook)

    def _install_signal_handler(self):
        def handler(signum, frame):
            logger.warning("\n[HardwareMonitor] SIGINT received! Initiating graceful shutdown...")
            print("\nShutting down gracefully. Saving state...")
            for hook in self._shutdown_hooks:
                try:
                    hook()
                except Exception as e:
                    logger.error(f"Error in shutdown hook: {e}")
            self._stop.set()
            sys.exit(0)
            
        try:
            signal.signal(signal.SIGINT, handler)
        except ValueError:
            pass # Fails if not main thread

# Singleton
_hw_monitor = None
_hw_lock = threading.Lock()

def get_hardware_monitor() -> HardwareMonitor:
    global _hw_monitor
    with _hw_lock:
        if _hw_monitor is None:
            _hw_monitor = HardwareMonitor()
    return _hw_monitor
