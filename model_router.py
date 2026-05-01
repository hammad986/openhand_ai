"""
model_router.py — Phase 53: Hybrid Production Orchestration Engine
=================================================================
Smart LLM routing across providers with:
  • Task-based model selection
  • Dynamic config resolution
  • Strict Local vs Cloud handling for Ollama
  • Explicit observability
"""
from __future__ import annotations
import logging, os, time, threading, random, json
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# --- Production Reliability & Metrics ---
_METRICS = {
    "total_calls": 0,
    "retries": 0,
    "fallbacks": 0,
    "ollama_latency": [],
}
_ollama_semaphore = threading.Semaphore(4)  # Max 4 concurrent daemon calls

def classify_error(err_msg: str, status_code: int = None) -> str:
    """Categorize failure mode for precise handling."""
    err_lower = err_msg.lower()
    if "lookup" in err_lower or "dial" in err_lower or "connection" in err_lower:
        return "dns_network"
    if "timeout" in err_lower:
        return "timeout"
    if status_code == 404 or "not found" in err_lower:
        return "model_missing"
    return "invalid_response"

def advanced_retry(max_retries=3, base_delay=1.0):
    """Exponential backoff with jitter and error classification."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    err_type = classify_error(str(e))
                    if err_type == "model_missing" or attempt == max_retries - 1:
                        raise e
                    _METRICS["retries"] += 1
                    sleep_time = delay + random.uniform(0, 0.5)
                    logger.warning(f"[Retry] Attempt {attempt+1} failed ({err_type}). Retrying in {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    delay *= 2
        return wrapper
    return decorator

# Task type → preferred ranking: (cost_weight, quality_weight)
_TASK_WEIGHTS: Dict[str, tuple] = {
    "fast":   (0.7, 0.3),
    "plan":   (0.4, 0.6),
    "code":   (0.2, 0.8),
    "reason": (0.2, 0.8),
    "debug":  (0.5, 0.5),
    "default":(0.4, 0.6),
}

def classify_model(model_name: str) -> str:
    """Determine if a model requires external connectivity."""
    if model_name.endswith(":cloud"):
        return "cloud"
    return "local"

def get_ollama_base_url() -> str:
    """Unified configuration resolver for Ollama."""
    return (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_HOST")
        or "http://localhost:11434"
    ).rstrip("/")

class ModelRouter:
    """Routes LLM calls to the best available provider."""

    def __init__(self) -> None:
        self._failures: Dict[str, float] = {}  # provider_key → last_fail_ts
        self._COOLDOWN = 60.0  # seconds before retrying a failed provider

    def get_providers(self) -> Dict[str, Dict]:
        """Dynamic resolution of providers and active models."""
        return {
            "gemini-flash": {
                "tasks":    ["fast", "plan", "debug"],
                "cost":     0.1, "quality": 0.7,
                "env_key":  "GEMINI_API_KEY",
                "model":    os.environ.get("GEMINI_PLANNER_MODEL", "gemini-1.5-flash"),
                "provider": "gemini",
            },
            "gemini-pro": {
                "tasks":    ["code", "reason", "plan"],
                "cost":     0.4, "quality": 0.9,
                "env_key":  "GEMINI_API_KEY",
                "model":    os.environ.get("GEMINI_CODING_MODEL", "gemini-1.5-pro"),
                "provider": "gemini",
            },
            "groq-fast": {
                "tasks":    ["fast", "debug", "plan"],
                "cost":     0.05, "quality": 0.65,
                "env_key":  "GROQ_API_KEY",
                "model":    os.environ.get("GROQ_PLANNER_MODEL", "llama-3.1-8b-instant"),
                "provider": "groq",
            },
            "groq-large": {
                "tasks":    ["code", "reason"],
                "cost":     0.15, "quality": 0.82,
                "env_key":  "GROQ_API_KEY",
                "model":    os.environ.get("GROQ_CODING_MODEL", "llama-3.3-70b-versatile"),
                "provider": "groq",
            },
            "openrouter": {
                "tasks":    ["code", "reason", "plan"],
                "cost":     0.3, "quality": 0.85,
                "env_key":  "OPENROUTER_API_KEY",
                "model":    os.environ.get("OPENROUTER_CODING_MODEL", "deepseek/deepseek-coder"),
                "provider": "openrouter",
            },
            "ollama": {
                "tasks":    ["fast", "code", "plan", "reason", "debug"],
                "cost":     0.0, "quality": 0.6,
                "env_key":  None,
                "model":    os.environ.get("OLLAMA_LOCAL_MODEL", "phi3:mini"),
                "provider": "ollama",
            },
        }

    def _available(self, key: str, track_rejections: list = None) -> bool:
        providers = self.get_providers()
        meta = providers[key]
        env_key = meta.get("env_key")
        
        if env_key and not os.environ.get(env_key, "").strip():
            if track_rejections is not None: track_rejections.append({"model": key, "reason": "Missing API Key"})
            return False
            
        if meta["provider"] == "ollama":
            allow_local = os.environ.get("ALLOW_LOCAL_MODELS", "true").lower() == "true"
            ollama_enabled = os.environ.get("OLLAMA_ENABLED", "true").lower() == "true"
            
            if not ollama_enabled and not allow_local:
                if track_rejections is not None:
                    track_rejections.append({"model": key, "reason": "Ollama disabled"})
                return False
                
            model_type = classify_model(meta["model"])
            if not allow_local and model_type == "local":
                if track_rejections is not None:
                    track_rejections.append({"model": key, "reason": "Local offline models disabled in production"})
                return False
                
            if not self._ollama_reachable(meta["model"]):
                if track_rejections is not None:
                    track_rejections.append({"model": key, "reason": f"Ollama service unreachable at {get_ollama_base_url()}"})
                return False
                
        last_fail = self._failures.get(key, 0)
        if time.time() - last_fail < self._COOLDOWN:
            if track_rejections is not None: track_rejections.append({"model": key, "reason": f"In cooldown ({int(self._COOLDOWN - (time.time() - last_fail))}s left)"})
            return False
            
        return True

    def _ollama_reachable(self, model_name: str) -> bool:
        """Health check matching model type requirements."""
        base_url = get_ollama_base_url()
        try:
            import urllib.request
            # Universal check: is the daemon alive?
            urllib.request.urlopen(f'{base_url}/api/tags', timeout=2)
            
            # If cloud, we assume reachable if daemon is up; registry lookup happens during generation
            return True
        except Exception as e:
            logger.debug(f"[ModelRouter] Ollama health check failed: {e}")
            return False

    @classmethod
    def get_metrics(cls) -> dict:
        return _METRICS

    def _score(self, key: str, task_type: str) -> float:
        providers = self.get_providers()
        meta = providers[key]
        cw, qw = _TASK_WEIGHTS.get(task_type, _TASK_WEIGHTS["default"])
        return qw * meta["quality"] - cw * meta["cost"]

    def get_decision(self, task_type: str = "default", max_cost: float = 999.0) -> Dict[str, Any]:
        """Provides explicit tracing for model selection."""
        rejected = []
        eligible = []
        providers = self.get_providers()
        
        for k, m in providers.items():
            if not self._available(k, track_rejections=rejected):
                continue
            if task_type not in m["tasks"] + ["default"]:
                rejected.append({"model": k, "reason": f"Task type mismatch (needs {task_type})"})
                continue
            if m["cost"] > max_cost:
                rejected.append({"model": k, "reason": f"Too expensive ({m['cost']} > {max_cost})"})
                continue
            
            eligible.append(k)
            
        sorted_eligible = sorted(eligible, key=lambda k: self._score(k, task_type), reverse=True)
        return {
            "selected": sorted_eligible[0] if sorted_eligible else None,
            "eligible": sorted_eligible,
            "rejected": rejected
        }

    def select(self, task_type: str = "default",
               max_cost: float = 999.0) -> List[str]:
        """Return ranked list of provider keys for this task type."""
        return self.get_decision(task_type, max_cost)["eligible"]

    def call(self, prompt: str, task_type: str = "default",
             max_tokens: int = 2048, max_cost: float = 999.0,
             system: str = "", stream: bool = False) -> Dict[str, Any]:
        """Call LLM with automatic fallback. Enforces deterministic behavior."""
        ranking = self.select(task_type, max_cost)
        
        # Enforce determinism: cap temperature
        temp_map = {"code": 0.1, "plan": 0.3, "reason": 0.2, "debug": 0.1, "default": 0.4}
        temp = temp_map.get(task_type, 0.4)
        if not ranking:
            return {"text": "", "provider": "none", "model": "none",
                    "tokens": 0, "error": "no_provider_available"}

        providers = self.get_providers()

        for key in ranking:
            meta = providers[key]
            model_name = meta["model"]
            
            logger.info(f"[ModelRouter] Selected [{key}] -> Provider: {meta['provider']} | Model: {model_name} | Temp: {temp}")
            
            try:
                _METRICS["total_calls"] += 1
                start_ts = time.time()
                result = self._dispatch(meta, prompt, system, max_tokens, temp, stream)
                duration = time.time() - start_ts
                
                if meta["provider"] == "ollama":
                    _METRICS["ollama_latency"].append(duration)
                    # keep last 100 for memory safety
                    if len(_METRICS["ollama_latency"]) > 100:
                        _METRICS["ollama_latency"].pop(0)

                result["provider_key"] = key
                logger.info(f"[ModelRouter] Success: {meta['provider']} ({model_name}) in {duration:.2f}s")
                return result
            except Exception as e:
                err_str = str(e)
                logger.warning(f"[ModelRouter] Failure on {key}: {err_str}")
                
                # Strict fallback logic for Ollama
                if meta["provider"] == "ollama":
                    model_type = classify_model(model_name)
                    logger.error(f"[ModelRouter] Ollama execution failed. Model type: [{model_type.upper()}]")
                    
                    if model_type == "local":
                        # STRICT: Offline models cannot fallback to cloud
                        msg = f"Fatal Error: Local model '{model_name}' failed. Fallback to cloud blocked for privacy. Reason: {err_str}"
                        logger.error(f"[ModelRouter] {msg}")
                        return {
                            "text": "", "provider": "ollama", "model": model_name,
                            "tokens": 0, "error": msg, "provider_key": key
                        }
                    else:
                        # CLOUD: DNS/Registry failures allowed to fallback
                        logger.info(f"[ModelRouter] Cloud model '{model_name}' failed. Initiating fallback hierarchy.")
                        _METRICS["fallbacks"] += 1
                        self._failures[key] = time.time()
                        continue

                self._failures[key] = time.time()

        return {"text": "", "provider": "none", "model": "none",
                "tokens": 0, "error": "all_providers_failed"}

    def _dispatch(self, meta: dict, prompt: str,
                  system: str, max_tokens: int, temp: float, stream: bool = False) -> dict:
        provider = meta["provider"]
        model    = meta["model"]
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        if provider == "gemini":
            return self._call_gemini(model, messages, max_tokens, temp)
        elif provider == "groq":
            return self._call_groq(model, messages, max_tokens, temp)
        elif provider == "openrouter":
            return self._call_openrouter(model, messages, max_tokens, temp)
        elif provider == "ollama":
            return self._call_ollama(model, messages, max_tokens, temp, stream)
        raise ValueError(f"Unknown provider: {provider}")

    def _call_gemini(self, model: str, msgs: list, max_tokens: int, temp: float) -> dict:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        m = genai.GenerativeModel(model)
        text = "\n".join(x["content"] for x in msgs)
        resp = m.generate_content(text,
            generation_config={"max_output_tokens": max_tokens, "temperature": temp})
        out = resp.text or ""
        return {"text": out, "provider": "gemini", "model": model,
                "tokens": max(1, len(out.split()))}

    def _call_groq(self, model: str, msgs: list, max_tokens: int, temp: float) -> dict:
        import requests
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
            json={"model": model, "messages": msgs,
                  "max_tokens": max_tokens, "temperature": temp, "seed": 42}, timeout=30)
        r.raise_for_status()
        d = r.json()
        out = d["choices"][0]["message"]["content"]
        return {"text": out, "provider": "groq", "model": model,
                "tokens": d.get("usage", {}).get("total_tokens", 0)}

    def _call_openrouter(self, model: str, msgs: list, max_tokens: int, temp: float) -> dict:
        import requests
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                     "HTTP-Referer": "https://openhand.ai"},
            json={"model": model, "messages": msgs,
                  "max_tokens": max_tokens, "temperature": temp, "seed": 42}, timeout=30)
        r.raise_for_status()
        d = r.json()
        out = d["choices"][0]["message"]["content"]
        return {"text": out, "provider": "openrouter", "model": model,
                "tokens": d.get("usage", {}).get("total_tokens", 0)}

    def _call_ollama(self, model: str, msgs: list, max_tokens: int, temp: float, stream: bool = False) -> dict:
        """Strict execution path for Ollama models with concurrency control, streaming, and advanced retry."""
        import requests
        base = get_ollama_base_url()
        model_type = classify_model(model)
        
        @advanced_retry(max_retries=3, base_delay=1.0)
        def attempt_chat():
            chat_payload = {
                "model": model,
                "messages": msgs,
                "stream": stream,
                "options": {"num_predict": max_tokens, "temperature": temp, "seed": 42}
            }
            with _ollama_semaphore:
                r = requests.post(f"{base}/api/chat", json=chat_payload, timeout=120, stream=stream)
            
            if r.status_code != 200:
                err = r.text
                if r.status_code == 500:
                    try:
                        err = r.json().get("error", r.text)
                    except: pass
                err_type = classify_error(err, r.status_code)
                if "memory" in err.lower():
                    raise MemoryError(f"Ollama out of memory: {err}")
                if err_type == "dns_network":
                    raise ConnectionError(f"Ollama DNS/Network error: {err}")
                raise RuntimeError(f"Ollama error {r.status_code}: {err}")
            
            if stream:
                def token_gen():
                    full_text = ""
                    for line in r.iter_lines():
                        if line:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            full_text += content
                            yield {"text": content, "provider": "ollama", "model": model, "done": chunk.get("done", False), "full_text": full_text}
                return {"stream_generator": token_gen(), "provider": "ollama", "model": model}
            else:
                d = r.json()
                return d.get("message", {}).get("content", d.get("response", ""))

        try:
            # We trust attempt_chat to handle retries and stream=True
            out = attempt_chat()
            if stream and isinstance(out, dict) and "stream_generator" in out:
                return out
            if out:
                return {"text": out, "provider": "ollama", "model": model,
                        "tokens": max(1, len(out.split()))}
        except Exception as chat_err:
            if stream: 
                raise RuntimeError(f"Streaming failed: {chat_err}")
                
            logger.warning(f"[ModelRouter] /api/chat failed ({chat_err}), falling back to /api/generate")
            
            # Fallback to /api/generate only if strictly necessary (non-streaming only)
            gen_payload = {
                "model": model,
                "prompt": "\n".join(x["content"] for x in msgs),
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temp, "seed": 42}
            }
            try:
                with _ollama_semaphore:
                    r = requests.post(f"{base}/api/generate", json=gen_payload, timeout=120)
                if r.status_code == 500:
                    err = r.json().get("error", r.text)
                    if "memory" in err.lower():
                        raise MemoryError(f"Ollama out of memory: {err}")
                    raise RuntimeError(f"Ollama /api/generate error: {err}")
                r.raise_for_status()
                d = r.json()
                out = d.get("response", "")
                return {"text": out, "provider": "ollama", "model": model,
                        "tokens": max(1, len(out.split()))}
            except Exception as e:
                raise RuntimeError(f"Ollama execution completely failed: {e}")

    def provider_status(self) -> list[dict]:
        now = time.time()
        rows = []
        providers = self.get_providers()
        for key, meta in providers.items():
            last_fail = self._failures.get(key, 0)
            rows.append({
                "key":       key,
                "provider":  meta["provider"],
                "model":     meta["model"],
                "tasks":     meta["tasks"],
                "cost":      meta["cost"],
                "quality":   meta["quality"],
                "available": self._available(key),
                "cooldown_remaining": max(0, self._COOLDOWN - (now - last_fail))
                                      if last_fail else 0,
            })
        return sorted(rows, key=lambda r: (-r["quality"], r["cost"]))


# Module singleton
_router_instance = None
_router_lock = __import__("threading").Lock()

def get_router() -> ModelRouter:
    global _router_instance
    with _router_lock:
        if _router_instance is None:
            _router_instance = ModelRouter()
    return _router_instance
