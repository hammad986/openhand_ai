"""
router.py - Smart Multi-API Router v2
Intelligence:
  1. Rate limit detection (429 + Retry-After)
  2. Cost-aware routing (tracks tokens spent)
  3. Response quality scoring
  4. Per-API health tracking (error rate, latency)
  5. Adaptive priority — bad APIs demoted automatically
"""

import json
import re
import time
import logging
import statistics
import requests
from dataclasses import dataclass, field
from typing import Optional
from config import Config

logger = logging.getLogger(__name__)


@dataclass
class APIHealth:
    name: str
    calls:              int   = 0
    errors:             int   = 0
    total_tokens:       int   = 0
    total_cost:         float = 0.0
    latencies:          list  = field(default_factory=list)
    rate_limited_until: float = 0.0
    auth_failed:        bool  = False

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.calls, 1)

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies[-10:]) if self.latencies else 999

    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self.rate_limited_until

    def score(self) -> float:
        """Lower = better. Combines cost + reliability + speed."""
        if self.auth_failed:
            return 999.0
        if self.is_rate_limited:
            return 999.0
        return (
            self.total_cost * 1000
            + self.error_rate * 50
            + min(self.avg_latency, 30)
        )

    def summary(self) -> dict:
        return {
            "calls":        self.calls,
            "errors":       self.errors,
            "error_rate":   f"{self.error_rate:.1%}",
            "avg_latency":  f"{self.avg_latency:.1f}s",
            "tokens_used":  self.total_tokens,
            "cost_usd":     f"${self.total_cost:.5f}",
            "score":        f"{self.score():.2f}",
            "rate_limited": self.is_rate_limited,
            "auth_failed":  self.auth_failed,
        }


class LLMRouter:
    def __init__(self, config: Config, force_model: str = None):
        self.config       = config
        self.force_model  = self._normalize_model_name(force_model)
        self.last_used_api: Optional[str] = None
        # Phase 23: include `ollama` as a tracked provider in addition to
        # the keys present in MODELS (which only has `local`).
        _names = set(config.MODELS) | {"ollama"}
        self.health = {name: APIHealth(name=name) for name in _names}
        # Gemini key
        self._gemini_keys     = [config.GEMINI_API_KEY] if config.GEMINI_API_KEY else []
        self._gemini_key_idx  = 0      # current active key
        self._gemini_use_pro  = False  # set True on repeated failures; reset on step success
        self._response_cache  = {}

    # ── Public ────────────────────────────────────────────────────────────────
    def chat(self, messages: list, system: str = None, max_tokens: int = 2048) -> str:
        if system:
            messages = [{"role": "system", "content": system}] + messages

        if self.force_model:
            ranked = self._ranked_priority()
            if self.force_model in ranked:
                priority = [self.force_model] + [a for a in ranked if a != self.force_model]
            else:
                priority = ranked
        else:
            priority = self._ranked_priority()
        if not priority:
            raise RuntimeError(
                "No API available. Add at least one remote API key in .env "
                "or set ALLOW_LOCAL_FALLBACK=1 to enable local ollama."
            )
        logger.info(f"[Router] Priority: {priority}")

        cache_key = hash(json.dumps(messages))
        if cache_key in self._response_cache:
            return self._response_cache[cache_key]

        for idx, api_name in enumerate(priority):
            h = self.health[api_name]
            if h.auth_failed:
                logger.warning(f"[Router] {api_name} disabled for this session due to auth failure")
                continue
            if h.is_rate_limited:
                wait = int(h.rate_limited_until - time.time())
                logger.warning(f"[Router] {api_name} rate-limited {wait}s, skipping")
                continue
            if idx > 0:
                time.sleep(2)

            try:
                t0       = time.time()
                response = self._call_with_retry(api_name, messages, max_tokens)
                elapsed  = time.time() - t0
                tokens   = self._estimate_tokens(messages, response)
                cost     = tokens * self.config.COST_PER_1K.get(api_name, 0) / 1000

                h.calls        += 1
                h.total_tokens += tokens
                h.total_cost   += cost
                h.latencies.append(elapsed)
                self.last_used_api = api_name

                logger.info(json.dumps({
                    "provider_used": api_name,
                    "fallback": api_name != priority[0]
                }))
                logger.info(f"[Router] OK {api_name} | {elapsed:.1f}s | ~{tokens}tok | ${cost:.5f} | Q={self._quality(response):.0f}")
                self._response_cache[cache_key] = response
                return response

            except _RateLimitError as e:
                h.errors += 1
                h.rate_limited_until = time.time() + e.retry_after
                logger.warning(f"[Router] {api_name} rate limited {e.retry_after}s")
            except Exception as e:
                h.errors += 1
                msg = str(e).lower()
                if ("http 401" in msg) or ("invalid_api_key" in msg) or ("unauthorized" in msg):
                    h.auth_failed = True
                    logger.warning(f"[Router] {api_name} auth failed; disabling for this session")
                logger.warning(f"[Router] FAIL {api_name}: {type(e).__name__}: {e}")

        raise RuntimeError(f"All APIs failed.\n{json.dumps(self.stats(), indent=2)}")

    # -- Phase 23 - Role-based routing ------------------------------------
    # Reads `data/model_routing.json` and dispatches to the requested
    # role (planner / coding / reasoning).  Falls back to `coding` when
    # the requested role is `reasoning` and no reasoning model is set.
    _ROUTING_PATH = "data/model_routing.json"
    _ROUTING_VALID_ROLES = ("planner", "coding", "reasoning")
    _ROUTING_VALID_PROVIDERS = ("auto", "gemini", "openrouter", "groq",
                                "nvidia", "together", "ollama")

    def _load_routing(self) -> dict:
        """Read + cache routing config; mtime-invalidates so UI saves
        are picked up without restarting the process.  Returns a dict
        with keys {provider, planner, coding, reasoning} - empty model
        strings mean "use the provider's default in MODELS".
        """
        import os, json
        cache = getattr(self, "_routing_cache", None)
        try:
            mtime = os.path.getmtime(self._ROUTING_PATH)
        except OSError:
            mtime = 0.0
        if cache and cache.get("_mtime") == mtime:
            return cache["data"]
        data = {"provider": "auto", "planner": "", "coding": "",
                "reasoning": ""}
        try:
            with open(self._ROUTING_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                prov = str(raw.get("provider", "")).strip().lower()
                if prov in self._ROUTING_VALID_PROVIDERS:
                    data["provider"] = prov
                for role in self._ROUTING_VALID_ROLES:
                    # Accept both `planner` (Phase 22) and `planner_model`
                    # (Phase 23 spec) for forward/backward compatibility.
                    v = raw.get(role) or raw.get(role + "_model") or ""
                    if isinstance(v, str):
                        data[role] = v.strip()
        except (OSError, ValueError):
            pass
        self._routing_cache = {"_mtime": mtime, "data": data}
        return data

    def _get_model_for(self, provider: str, role: str, cfg: dict) -> str | None:
        if role not in self._ROUTING_VALID_ROLES:
            raise ValueError(f"unknown role: {role!r}")
            
        prov_cfg = cfg.get("providers", {}).get(provider, {})
        model = prov_cfg.get(f"{role}_model")
        if not model and role == "reasoning":
            model = prov_cfg.get("coding_model")
            
        # backward compat check flat root if not in provider block
        if not model:
            model = cfg.get(role) or cfg.get(f"{role}_model")
            if not model and role == "reasoning":
                model = cfg.get("coding") or cfg.get("coding_model")
                
        # fallback to config.MODELS
        if not model:
            import os
            model = os.getenv(f"{provider.upper()}_{role.upper()}_MODEL")
            
        if not model:
            if provider == "ollama":
                model = self.config.MODELS.get("local")
            else:
                model = self.config.MODELS.get(provider)
                
        return model or None

    def chat_role(self, role: str, messages: list,
                  system: str | None = None,
                  max_tokens: int = 2048) -> str:
        cfg = self._load_routing()
        provider = cfg.get("provider", "auto") or "auto"

        if system:
            messages = [{"role": "system", "content": system}] + messages

        ranked = self._ranked_priority()
        if provider != "auto" and provider in ranked:
            priority = [provider] + [a for a in ranked if a != provider]
        elif provider != "auto" and provider == "ollama" and self.config.ALLOW_LOCAL_FALLBACK:
            priority = ["ollama"] + ranked
        else:
            priority = ranked

        if not priority:
            raise RuntimeError(
                "No API available. Add at least one remote API key in .env "
                "or set ALLOW_LOCAL_FALLBACK=1 to enable local ollama."
            )
        logger.info(f"[Router] role={role} provider={provider} priority={priority}")

        cache_key = hash(json.dumps(messages) + role)
        if cache_key in self._response_cache:
            return self._response_cache[cache_key]

        for idx, api_name in enumerate(priority):
            h = self.health.get(api_name)
            if h is None or h.auth_failed or h.is_rate_limited:
                continue
            if idx > 0:
                time.sleep(2)
            try:
                t0 = time.time()
                # Phase 29: Per-provider, per-role model selection
                effective_model = self._get_model_for(api_name, role, cfg)
                response = self._call_with_retry(api_name, messages, max_tokens, model=effective_model)
                elapsed = time.time() - t0
                tokens = self._estimate_tokens(messages, response)
                cost = tokens * self.config.COST_PER_1K.get(api_name, 0) / 1000
                h.calls += 1
                h.total_tokens += tokens
                h.total_cost += cost
                h.latencies.append(elapsed)
                self.last_used_api = api_name
                self.last_role = role
                self.last_role_model = effective_model
                self.last_role_provider = api_name
                
                logger.info(json.dumps({
                    "provider_used": api_name,
                    "fallback": api_name != priority[0]
                }))
                
                logger.info(f"[Router] OK role={role} {api_name}({effective_model or 'default'}) | {elapsed:.1f}s | ~{tokens}tok")
                self._response_cache[cache_key] = response
                return response
            except _RateLimitError as e:
                h.errors += 1
                h.rate_limited_until = time.time() + e.retry_after
                logger.warning(f"[Router] {api_name} rate limited {e.retry_after}s")
            except Exception as e:
                h.errors += 1
                msg = str(e).lower()
                if ("http 401" in msg) or ("invalid_api_key" in msg) or ("unauthorized" in msg):
                    h.auth_failed = True
                logger.warning(f"[Router] FAIL role={role} {api_name}: {type(e).__name__}: {e}")

        raise RuntimeError(f"All APIs failed for role={role}.\n{json.dumps(self.stats(), indent=2)}")

    def stats(self) -> dict:
        return {name: h.summary() for name, h in self.health.items()}

    def print_stats(self):
        print("\n📊 API Health Report")
        print(f"{'API':<14}{'Calls':<7}{'Errors':<8}{'Latency':<11}{'Cost':<12}{'Score':<8}Rtlmt")
        print("─" * 65)
        for name, h in sorted(self.health.items(), key=lambda x: x[1].score()):
            s = h.summary()
            print(f"{name:<14}{s['calls']:<7}{s['errors']:<8}{s['avg_latency']:<11}"
                  f"{s['cost_usd']:<12}{s['score']:<8}{s['rate_limited']}")

    # ── Ranking ───────────────────────────────────────────────────────────────
    def _ranked_priority(self) -> list:
        available = self._available_apis()
        available = [a for a in available if not self.health[a].auth_failed]
        ranked = sorted(available, key=lambda a: self.health[a].score())
        logger.info(f"[Router] Scores: {[(a, f'{self.health[a].score():.1f}') for a in ranked]}")
        return ranked

    def _available_apis(self) -> list:
        c = self.config
        available = []
        if c.ALLOW_LOCAL_FALLBACK: available.extend(["ollama", "local"])
        if c.NVIDIA_API_KEY:     available.append("nvidia")
        if c.GROQ_API_KEY:       available.append("groq")
        if self._gemini_keys:    available.append("gemini")
        if c.OPENROUTER_API_KEY: available.append("openrouter")
        if c.TOGETHER_API_KEY:   available.append("together")
        return available

    def _normalize_model_name(self, name: str | None) -> str | None:
        if not name:
            return None
        n = name.strip().lower()
        if n == "grok":
            return "groq"
        return n

    # ── Retry ─────────────────────────────────────────────────────────────────
    def _call_with_retry(self, api_name, messages, max_tokens, model: str | None = None) -> str:
        delay = 1
        for attempt in range(2):
            try:
                return self._dispatch(api_name, messages, max_tokens, model=model)
            except _RateLimitError:
                raise
            except requests.exceptions.Timeout:
                if attempt < 1:
                    time.sleep(delay); delay *= 2
                else:
                    raise

    # ── Dispatch ──────────────────────────────────────────────────────────────
    def _dispatch(self, name, messages, max_tokens, model: str | None = None) -> str:
        # Phase 23: optional `model` overrides the per-provider default
        # in Config.MODELS so role-based routing can pin a specific
        # checkpoint per call (e.g. coding="deepseek-coder-v2").
        c = self.config
        if name == "gemini":
            return self._gemini(messages, max_tokens, model=model)
        if name == "groq":
            return self._compat(c.GROQ_URL, c.GROQ_API_KEY, model or c.MODELS["groq"], messages, max_tokens)
        if name == "openrouter":
            return self._compat(c.OPENROUTER_URL, c.OPENROUTER_API_KEY, model or c.MODELS["openrouter"],
                                messages, max_tokens,
                                extra={"HTTP-Referer":"http://localhost","X-Title":"AI-Dev-Agent"})
        if name == "nvidia":
            return self._compat(c.NVIDIA_URL, c.NVIDIA_API_KEY, model or c.MODELS["nvidia"], messages, max_tokens)
        if name == "together":
            return self._compat(c.TOGETHER_URL, c.TOGETHER_API_KEY, model or c.MODELS["together"], messages, max_tokens)
        if name == "ollama":
            # Phase 23 first-class ollama dispatch (per-call model arg).
            return self._call_ollama_chat(model or c.MODELS["local"], messages, max_tokens)
        if name == "local":
            return self._ollama(messages, max_tokens) if not model else self._call_ollama_chat(model, messages, max_tokens)
        raise ValueError(f"Unknown API: {name}")

    def _compat(self, url, key, model, messages, max_tokens, extra=None) -> str:
        if not key:
            raise ValueError("Missing API key")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(extra or {})}
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
        resp    = requests.post(url, headers=headers, json=payload, timeout=self.config.TIMEOUT_SECONDS)
        if resp.status_code == 429:
            raise _RateLimitError(int(resp.headers.get("Retry-After", 60)))
        if resp.status_code >= 400:
            detail = (resp.text or "").strip().replace("\n", " ")[:600]
            raise requests.HTTPError(f"HTTP {resp.status_code} from {url}: {detail}")
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _ollama(self, messages, max_tokens) -> str:
        payload = {
            "model":   self.config.MODELS["local"],
            "messages": messages,
            "stream":   False,
            "options": {"num_predict": max_tokens, "temperature": 0.3},
        }
        resp = requests.post(
            self.config.OLLAMA_URL, json=payload,
            timeout=self.config.OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    # Phase 22 -- first-class Ollama chat with explicit model arg.
    # Used by the role-based router (/api/code-action, dev_loop) when the
    # user has selected a specific local model in Settings -> Model Routing.
    # Falls back to /api/generate exactly like the planner path so it works
    # even when an interactive 'ollama run' session is open.
    def _call_ollama_chat(self, model, messages, max_tokens=2048):
        if not model:
            raise ValueError('ollama: model name required')
        chat_payload = {
            'model':    model,
            'messages': messages,
            'stream':   False,
            'options':  {'num_predict': max_tokens, 'temperature': 0.3},
        }
        try:
            resp = requests.post(
                self.config.OLLAMA_URL, json=chat_payload,
                timeout=self.config.OLLAMA_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                msg  = (data.get('message') or {}).get('content', '')
                if msg.strip():
                    return msg.strip()
        except (requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError):
            pass
        # Fallback: /api/generate (single prompt; works during 'ollama run')
        prompt = '\n\n'.join(
            f"{m.get('role','user').upper()}: {m.get('content','')}"
            for m in messages
        )
        gen_payload = {
            'model':  model,
            'prompt': prompt,
            'stream': False,
            'options': {'num_predict': max_tokens, 'temperature': 0.3},
        }
        resp2 = requests.post(
            self.config.OLLAMA_GENERATE_URL, json=gen_payload,
            timeout=self.config.OLLAMA_TIMEOUT,
        )
        resp2.raise_for_status()
        return (resp2.json().get('response') or '').strip()

    def _gemini(self, messages: list, max_tokens: int, model: str | None = None) -> str:
        """Gemini REST API with 3-key rotation and flash->pro escalation.
        No SDK required -- pure HTTP.  Handles alternating-role constraint.
        """
        c = self.config
        # Phase 23: explicit model arg wins over flash/pro escalation logic.
        if not model:
            model = c.GEMINI_PRO_MODEL if self._gemini_use_pro else c.GEMINI_FLASH_MODEL

        # Convert OpenAI-style messages -> Gemini contents format
        # Gemini requires strictly alternating user/model turns.
        system_text = ""
        gemini_contents: list = []
        for m in messages:
            role    = m.get("role", "user")
            content = (m.get("content") or "").strip()
            if role == "system":
                system_text = content
                continue
            g_role = "model" if role == "assistant" else "user"
            # Merge consecutive same-role turns (required by Gemini)
            if gemini_contents and gemini_contents[-1]["role"] == g_role:
                gemini_contents[-1]["parts"][0]["text"] += "\n\n" + content
            else:
                gemini_contents.append({"role": g_role, "parts": [{"text": content}]})

        # First turn must be "user"
        if not gemini_contents or gemini_contents[0]["role"] == "model":
            gemini_contents.insert(0, {"role": "user", "parts": [{"text": "Begin."}]})

        payload: dict = {
            "contents": gemini_contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
        }
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}

        n = len(self._gemini_keys)
        if n == 0:
            raise ValueError("No Gemini API keys configured (set GEMINI_API_KEY in .env)")

        last_err: Exception = RuntimeError("no attempt made")
        all_rate_limited    = True
        min_retry_after     = 60

        for attempt in range(n):
            idx = (self._gemini_key_idx + attempt) % n
            key = self._gemini_keys[idx]
            url = f"{c.GEMINI_BASE_URL}/{model}:generateContent?key={key}"
            try:
                resp = requests.post(url, json=payload, timeout=c.TIMEOUT_SECONDS)
                body = resp.text

                # 429 or quota exhaustion -> rotate key
                if resp.status_code == 429 or (
                    resp.status_code >= 400
                    and any(kw in body.lower() for kw in ("quota", "exhausted", "rate"))
                ):
                    min_retry_after = max(
                        min_retry_after,
                        int(resp.headers.get("Retry-After", 60))
                    )
                    logger.warning(f"[Gemini] key[{idx+1}/{n}] rate-limited, rotating...")
                    continue  # try next key

                resp.raise_for_status()
                all_rate_limited = False

                data = resp.json()
                try:
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                except (KeyError, IndexError, TypeError) as e:
                    raise ValueError(
                        f"Unexpected Gemini response: {body[:200]}"
                    ) from e

                if not text:
                    raise ValueError("Gemini returned empty text")

                self._gemini_key_idx = idx   # park on working key
                tag = "pro" if self._gemini_use_pro else "flash"
                logger.info(f"[Gemini] OK {tag} key[{idx+1}/{n}]")
                return text

            except requests.exceptions.Timeout:
                all_rate_limited = False
                last_err = requests.exceptions.Timeout(f"Gemini key[{idx+1}] timed out")
            except (ValueError, requests.HTTPError) as e:
                all_rate_limited = False
                last_err = e

        if all_rate_limited:
            raise _RateLimitError(min_retry_after)
        raise RuntimeError(
            f"All {n} Gemini key(s) failed after {n} attempts. Last: {last_err}"
        )

    # -- Public planner helper -------------------------------------------------
    def chat_planner(self, task: str) -> list:
        """Local phi3:mini planner via Ollama (POST /api/chat).

        Returns a list of step strings, or [] ONLY on connection/timeout failures
        (caller falls back to Gemini-flash in that case).
        Parse errors trigger one automatic retry with a simpler prompt --
        phi3 sometimes adds explanation text around the JSON array.
        """
        PLANNER_TIMEOUT = self.config.OLLAMA_TIMEOUT   # 60 s
        PLANNER_URL     = self.config.OLLAMA_URL        # http://localhost:11434/api/chat
        PLANNER_MODEL   = self.config.OLLAMA_PLANNER_MODEL  # phi3:mini
        MAX_STEPS       = self.config.PLANNER_MAX_STEPS

        def _build_prompt(strict: bool) -> str:
            if strict:
                return (
                    f"Break this software task into {MAX_STEPS} or fewer ordered steps.\n"
                    f"TASK: {task}\n\n"
                    "Output a JSON array of strings ONLY. No explanation. Example:\n"
                    '["Install flask and sqlite3", "Write app.py", "Run python app.py"]\n'
                    "Rules: install packages first, init DB before server, start server before tests.\n"
                    "JSON array:"
                )
            return (
                "You are a software project planner. Break this task into ordered steps.\n\n"
                f"TASK: {task}\n\n"
                "Return STRICT JSON array of step strings ONLY:\n"
                '["step 1", "step 2", ...]\n\n'
                f"Rules:\n"
                f"- Maximum {MAX_STEPS} steps\n"
                "- Each step is ONE atomic action (write file / install packages / run script)\n"
                "- Install packages BEFORE running any code\n"
                "- Initialize database BEFORE starting server\n"
                "- Start server BEFORE browser tests\n"
                "- Be specific: name exact files, packages, ports\n"
                "Return ONLY the JSON array, no extra text."
            )

        def _parse_steps(raw: str):
            """Extract JSON array from raw phi3 output, tolerating surrounding text."""
            # strip markdown fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$",          "", raw.strip())
            # Try direct parse first
            try:
                steps = json.loads(raw)
                if isinstance(steps, list) and steps:
                    return steps
            except json.JSONDecodeError:
                pass
            # Fallback: extract first [...] block from the text
            m = re.search(r'(\[.*?\])', raw, re.DOTALL)
            if m:
                try:
                    steps = json.loads(m.group(1))
                    if isinstance(steps, list) and steps:
                        return steps
                except json.JSONDecodeError:
                    pass
            return None

        def _call_phi3(strict_prompt: bool) -> str:
            prompt_text = _build_prompt(strict_prompt)
            # -- Try /api/chat first (preferred: full conversation support) --
            chat_payload = {
                "model":    PLANNER_MODEL,
                "messages": [{"role": "user", "content": prompt_text}],
                "stream":   False,
                "options":  {"num_predict": 700, "temperature": 0.1},
            }
            print(f"  [Planner] phi3:mini -> POST {PLANNER_URL} (timeout={PLANNER_TIMEOUT}s)", flush=True)
            try:
                resp = requests.post(PLANNER_URL, json=chat_payload, timeout=PLANNER_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()["message"]["content"].strip()
                # 500 means model is busy (e.g. interactive 'ollama run' is open)
                logger.warning(f"[Planner] /api/chat returned {resp.status_code}, trying /api/generate")
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout):
                logger.warning("[Planner] /api/chat timed out, trying /api/generate")
            # -- Fallback: /api/generate (works even with 'ollama run' active) --
            gen_url = self.config.OLLAMA_GENERATE_URL
            gen_payload = {
                "model":  PLANNER_MODEL,
                "prompt": prompt_text,
                "stream": False,
                "options": {"num_predict": 700, "temperature": 0.1},
            }
            print(f"  [Planner] phi3:mini -> POST {gen_url} (generate fallback)", flush=True)
            resp2 = requests.post(gen_url, json=gen_payload, timeout=PLANNER_TIMEOUT)
            resp2.raise_for_status()
            return resp2.json()["response"].strip()

        # ── Attempt 1: normal prompt ─────────────────────────────────────────
        try:
            raw1 = _call_phi3(strict_prompt=False)
            logger.debug(f"[Planner] phi3 raw output: {raw1[:200]}")
            steps = _parse_steps(raw1)
            if steps and all(isinstance(s, str) for s in steps):
                pruned = [s.strip() for s in steps if s.strip()][:MAX_STEPS]
                print(f"  [Planner] phi3:mini OK -> {len(pruned)} steps", flush=True)
                logger.info(f"[Router] phi3 planner OK: {len(pruned)} steps")
                return pruned
            # phi3 responded but JSON was garbled -- retry with a stricter prompt
            print("  [Planner] phi3 output not clean JSON, retrying with strict prompt ...", flush=True)
            logger.warning(f"[Router] phi3 parse fail (attempt 1), retrying. Raw: {raw1[:120]}")

        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout,
                ConnectionRefusedError) as e:
            # Ollama is not reachable -- fall back to Gemini immediately
            print(f"  [Planner] phi3 unavailable ({type(e).__name__}) -> falling back to Gemini", flush=True)
            logger.info(f"[Router] Ollama unreachable ({type(e).__name__}): {str(e)[:80]}")
            return []

        except requests.exceptions.Timeout:
            print("  [Planner] phi3 timed out -> falling back to Gemini", flush=True)
            logger.info("[Router] Ollama phi3 timed out")
            return []

        except Exception as e:
            logger.warning(f"[Router] phi3 attempt 1 unexpected error: {type(e).__name__}: {str(e)[:80]}")
            # Don't give up yet -- try the strict prompt

        # ── Attempt 2: strict / terse prompt ─────────────────────────────────
        try:
            raw2 = _call_phi3(strict_prompt=True)
            logger.debug(f"[Planner] phi3 retry raw: {raw2[:200]}")
            steps = _parse_steps(raw2)
            if steps and all(isinstance(s, str) for s in steps):
                pruned = [s.strip() for s in steps if s.strip()][:MAX_STEPS]
                print(f"  [Planner] phi3:mini OK (retry) -> {len(pruned)} steps", flush=True)
                logger.info(f"[Router] phi3 planner OK (retry): {len(pruned)} steps")
                return pruned
            logger.warning(f"[Router] phi3 retry also failed to produce valid JSON. Raw: {raw2[:120]}")
            print("  [Planner] phi3 retry failed to return valid JSON -> falling back to Gemini", flush=True)

        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout,
                ConnectionRefusedError, requests.exceptions.Timeout) as e:
            print(f"  [Planner] phi3 retry: {type(e).__name__} -> falling back to Gemini", flush=True)
            logger.info(f"[Router] phi3 retry connection error: {str(e)[:80]}")

        except Exception as e:
            logger.warning(f"[Router] phi3 retry unexpected: {type(e).__name__}: {str(e)[:80]}")
            print(f"  [Planner] phi3 retry error ({type(e).__name__}) -> falling back to Gemini", flush=True)

        return []

    def chat_staged_planner(self, task: str) -> list:
        """Phase 2 planner: returns staged plan as list of dicts.

        Format: [{"stage": "setup", "steps": ["..."]}, ...]

        Falls back to flat chat_planner() if phi3 doesn't produce
        valid staged JSON (e.g. phi3:mini misses the schema).
        """
        STAGE_NAMES   = self.config.STAGE_NAMES
        PLANNER_URL   = self.config.OLLAMA_URL
        GEN_URL       = self.config.OLLAMA_GENERATE_URL
        MODEL         = self.config.OLLAMA_PLANNER_MODEL
        TIMEOUT       = self.config.OLLAMA_TIMEOUT
        MAX_STEPS     = self.config.PLANNER_MAX_STEPS

        stage_list = ", ".join(f'"{s}"' for s in STAGE_NAMES)
        prompt = (
            f"You are a software project planner. Decompose this task into ordered stages.\n\n"
            f"TASK: {task}\n\n"
            f"Available stages (use only what is needed): {stage_list}\n\n"
            "Return STRICT JSON array of stage objects ONLY:\n"
            "[\n"
            '  {"stage": "setup",   "steps": ["Install flask sqlite3", "Write requirements.txt"]},\n'
            '  {"stage": "backend", "steps": ["Write app.py with routes", "Write init_db.py"]},\n'
            '  {"stage": "testing", "steps": ["Start Flask server on port 5000", "Test login via browser"]}\n'
            "]\n\n"
            "Rules:\n"
            f"- Total steps across all stages <= {MAX_STEPS}\n"
            "- Each step is ONE atomic action\n"
            "- Install packages in setup BEFORE any backend steps\n"
            "- Init DB in backend BEFORE starting server\n"
            "- Start server in testing BEFORE browser tests\n"
            "- Return ONLY the JSON array, no extra text."
        )

        def _call(use_generate: bool) -> str:
            if use_generate:
                payload = {"model": MODEL, "prompt": prompt, "stream": False,
                           "options": {"num_predict": 900, "temperature": 0.1}}
                print(f"  [StagedPlanner] phi3 -> {GEN_URL}", flush=True)
                r = requests.post(GEN_URL, json=payload, timeout=TIMEOUT)
                r.raise_for_status()
                return r.json()["response"].strip()
            else:
                payload = {"model": MODEL,
                           "messages": [{"role": "user", "content": prompt}],
                           "stream": False,
                           "options": {"num_predict": 900, "temperature": 0.1}}
                print(f"  [StagedPlanner] phi3 -> {PLANNER_URL}", flush=True)
                r = requests.post(PLANNER_URL, json=payload, timeout=TIMEOUT)
                if r.status_code != 200:
                    logger.warning(f"[StagedPlanner] /api/chat {r.status_code}, trying /api/generate")
                    return _call(use_generate=True)
                return r.json()["message"]["content"].strip()

        def _parse_staged(raw: str):
            raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$",           "", raw.strip())
            # direct parse
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    return data
            except json.JSONDecodeError:
                pass
            # extract first [...] block
            m = re.search(r"(\[.*\])", raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        return data
                except json.JSONDecodeError:
                    pass
            return None

        def _validate(stages: list) -> bool:
            """Each stage must have 'stage' string and 'steps' non-empty list of strings."""
            for s in stages:
                if not isinstance(s, dict): return False
                if not isinstance(s.get("stage"), str): return False
                steps = s.get("steps", [])
                if not steps or not all(isinstance(x, str) for x in steps): return False
            return True

        # -- Attempt --
        for use_gen in (False, True):
            try:
                raw = _call(use_generate=use_gen)
                staged = _parse_staged(raw)
                if staged and _validate(staged):
                    total = sum(len(s["steps"]) for s in staged)
                    print(f"  [StagedPlanner] OK: {len(staged)} stages, {total} steps", flush=True)
                    logger.info(f"[Router] Staged phi3 planner OK: {len(staged)} stages, {total} steps")
                    return staged
                logger.warning(f"[StagedPlanner] parse attempt use_gen={use_gen} failed")
                if not use_gen:
                    continue  # try generate fallback
            except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout,
                    ConnectionRefusedError, requests.exceptions.Timeout,
                    requests.exceptions.ReadTimeout) as e:
                print(f"  [StagedPlanner] phi3 {type(e).__name__} on use_gen={use_gen}", flush=True)
                logger.info(f"[Router] StagedPlanner connection error: {str(e)[:80]}")
                if not use_gen:
                    continue
            except Exception as e:
                logger.warning(f"[Router] StagedPlanner error use_gen={use_gen}: {type(e).__name__}: {str(e)[:80]}")
                if not use_gen:
                    continue

        # -- Ultimate fallback: flat plan -> wrap in single stage --
        print("  [StagedPlanner] phi3 failed, falling back to flat planner", flush=True)
        flat = self.chat_planner(task)
        if flat:
            return [{"stage": "execution", "steps": flat}]
        return []

    def _quality(self, r: str) -> float:
        return min(len(r) / 20, 50) + (20 if "```" in r else 0) + (15 if r.strip().startswith("{") else 0)

    def _estimate_tokens(self, messages, response) -> int:
        return len(" ".join(m.get("content","") for m in messages) + response) // 4


class _RateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after