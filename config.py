"""config.py - All settings"""

import os
from dotenv import load_dotenv
load_dotenv()


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # -- Remote API keys -------------------------------------------------------
    GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    NVIDIA_API_KEY     = os.getenv("NVIDIA_API_KEY", "")
    TOGETHER_API_KEY   = os.getenv("TOGETHER_API_KEY", "")

    # Gemini
    GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")

    # -- API endpoints ---------------------------------------------------------
    GROQ_URL       = "https://api.groq.com/openai/v1/chat/completions"
    OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
    NVIDIA_URL     = "https://integrate.api.nvidia.com/v1/chat/completions"
    TOGETHER_URL        = "https://api.together.xyz/v1/chat/completions"
    _base = (os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    OLLAMA_URL          = f"{_base}/api/chat"
    OLLAMA_GENERATE_URL = f"{_base}/api/generate"  # fallback when /api/chat is blocked

    # Gemini REST - no SDK required
    GEMINI_BASE_URL    = "https://generativelanguage.googleapis.com/v1beta/models"
    GEMINI_FLASH_MODEL = "gemini-1.5-flash"
    GEMINI_PRO_MODEL   = "gemini-1.5-pro"

    # -- Models ----------------------------------------------------------------
    MODELS = {
        "gemini":     "gemini-1.5-flash",                                    # escalates to pro automatically
        "groq":       os.getenv("GROQ_MODEL",       "llama-3.3-70b-versatile"),
        "openrouter": os.getenv("OPENROUTER_MODEL",  "deepseek/deepseek-chat"),  # DeepSeek first
        "nvidia":     os.getenv("NVIDIA_MODEL",      "meta/llama-3.3-70b-instruct"),
        "together":   os.getenv("TOGETHER_MODEL",    "Qwen/Qwen2.5-72B-Instruct-Turbo"),  # Qwen second
        "local":      os.getenv("LOCAL_MODEL",       "codellama"),
    }
    # Local planner model (phi3:mini via Ollama -- zero cost)
    OLLAMA_PLANNER_MODEL = os.getenv("OLLAMA_PLANNER_MODEL", "phi3:mini")

    # -- Cost per 1k tokens (USD) ----------------------------------------------
    COST_PER_1K = {
        "gemini":     0.000075,   # flash: ~$0.075/1M input tokens
        "groq":       0.0,
        "local":      0.0,
        "together":   0.0009,
        "openrouter": 0.0014,     # DeepSeek
        "nvidia":     0.001,
    }

    # Priority: Gemini -> OpenRouter (DeepSeek/Qwen) -> Groq -> NVIDIA -> Together
    # Can be overridden by env var API_PRIORITY (comma-separated provider names),
    # used by the BYOK mode of the web wrapper to enforce a custom fallback order.
    _API_PRIORITY_ENV = os.getenv("API_PRIORITY", "").strip()
    API_PRIORITY = (
        [p.strip().lower() for p in _API_PRIORITY_ENV.split(",") if p.strip()]
        if _API_PRIORITY_ENV
        else ["gemini", "openrouter", "groq", "nvidia", "together"]
    )
    ALLOW_LOCAL_FALLBACK      = _env_bool("ALLOW_LOCAL_FALLBACK", "0")
    DEFAULT_REMOTE_MODEL      = os.getenv("DEFAULT_REMOTE_MODEL", "gemini").strip().lower()

    # -- Loop controls ---------------------------------------------------------
    MAX_RETRIES                 = 3   # retries per individual API HTTP call
    PER_STEP_RETRY              = 3   # LLM fix attempts per plan step before backtrack
    MAX_AGENT_LOOPS             = 40  # hard cap on total loop iterations
    MAX_TOTAL_ATTEMPTS          = 40  # cumulative failure cap across all steps
    PLANNER_MAX_STEPS           = 10  # max steps the planner may produce
    # Phase 2: multi-stage planning
    STAGE_NAMES    = ["setup", "backend", "frontend", "testing", "finalization"]
    PLANNER_STAGED = True   # True=staged JSON plan; False=flat list fallback
    USER_INTERVENTION_THRESHOLD = 5   # cumulative failures before asking user

    # Gemini escalation: switch flash -> pro after this many consecutive step failures
    GEMINI_PRO_AFTER_FAILURES   = 2

    # -- Infrastructure --------------------------------------------------------
    TIMEOUT_SECONDS = 30
    OLLAMA_TIMEOUT  = 60    # phi3 can be slow on first token; keep separate from remote API timeout
    # Honors $WORKSPACE_DIR so the web wrapper can route each session to its
    # own subfolder (e.g. ./workspace/<sid>/) without touching tools.py.
    # Falls back to the shared "./workspace" when invoked from the CLI.
    WORKSPACE_DIR   = os.getenv("WORKSPACE_DIR") or "./workspace"
    MEMORY_FILE     = "./memory.json"
    CHECKPOINT_FILE = "./checkpoint.json"
    VECTOR_DB_DIR   = "./memory_vectors"
    EMBEDDING_DIM   = 256
    LOG_FILE        = "./agent.log"

    # ── Phase 21.2 — Editor review policy ──────────────────────────────────
    # Decides whether an AI code suggestion (`/api/code-action`) opens the
    # diff modal for review or is auto-applied.  Three modes:
    #   REQUEST_REVIEW  — always open the diff modal (safest, default).
    #   ALWAYS_PROCEED  — auto-apply every suggestion (fastest, riskiest).
    #   AGENT_DECIDES   — hybrid: auto-apply when the change is small
    #                     (≤ HYBRID_MAX_LINES changed AND ≤ HYBRID_MAX_HUNKS
    #                     in the unified diff) AND the action is in the
    #                     HYBRID_AUTO_ACTIONS allow-list.
    # The runtime value lives in data/review_policy.json so it can be
    # changed from Settings without an env-var/restart cycle.
    REVIEW_MODES               = ("REQUEST_REVIEW", "ALWAYS_PROCEED", "AGENT_DECIDES")
    REVIEW_MODE_DEFAULT        = os.getenv("REVIEW_MODE_DEFAULT", "REQUEST_REVIEW").strip().upper()
    REVIEW_HYBRID_MAX_LINES    = int(os.getenv("REVIEW_HYBRID_MAX_LINES", "10"))
    REVIEW_HYBRID_MAX_HUNKS    = int(os.getenv("REVIEW_HYBRID_MAX_HUNKS", "2"))
    REVIEW_HYBRID_AUTO_ACTIONS = ("optimize", "explain")

    # ── Phase 22 — Model routing roles ─────────────────────────────────────
    # Per-role model preference. Each role maps to a (provider, model) tuple
    # that the router can prefer when called from the editor / dev_loop.
    # Empty model means "use the provider's default in MODELS[provider]".
    # Roles:
    #   planner    — high-level decomposition (cheap, fast)
    #   coding     — Fix/Optimize/Refactor + dev_loop executor
    #   reasoning  — critic / explain / hard debugging (smarter, pricier)
    ROUTING_ROLES               = ("planner", "coding", "reasoning")
    ROUTING_PROVIDERS           = ("auto", "gemini", "openrouter", "groq",
                                   "nvidia", "together", "ollama")
    ROUTING_DEFAULT_PROVIDER    = os.getenv("ROUTING_DEFAULT_PROVIDER",
                                            "auto").strip().lower()
    ROUTING_DEFAULT_PLANNER     = os.getenv("ROUTING_PLANNER_MODEL",  "")
    ROUTING_DEFAULT_CODING      = os.getenv("ROUTING_CODING_MODEL",   "")
    ROUTING_DEFAULT_REASONING   = os.getenv("ROUTING_REASONING_MODEL", "")

    # ── Phase 22 — Terminal sandbox ─────────────────────────────────────────
    # `/api/terminal/run` exec limits.  Reuses the code_runner-style POSIX
    # rlimits where possible.  Restricted-command list below is enforced
    # token-by-token before exec so things like `; rm -rf /` cannot slip
    # through.
    TERMINAL_TIMEOUT_SEC      = int(os.getenv("TERMINAL_TIMEOUT_SEC",  "10"))
    TERMINAL_MAX_OUTPUT_BYTES = int(os.getenv("TERMINAL_MAX_OUTPUT_BYTES",
                                              str(256 * 1024)))
    TERMINAL_MEM_MB           = int(os.getenv("TERMINAL_MEM_MB",       "256"))
    # Block-list applied as a regex-word match against argv[0] basenames AND
    # against the raw command string (catches `bash -c 'rm -rf /'`).
    TERMINAL_BLOCKED_TOKENS   = (
        "rm", "shutdown", "reboot", "halt", "poweroff", "init",
        "mkfs", "fdisk", "dd", "mount", "umount", "useradd",
        "userdel", "passwd", "su", "sudo", "chown", "chmod",
        "kill", "killall", "pkill", "iptables", "nc", "ncat",
        "telnet", "ssh", "scp", "rsync", "wget", "curl",
        # Phase 22 hardening — block interpreter indirection so the
        # blocklist above can't be bypassed via `python -c "import os; …"`,
        # `node -e "require('child_process').exec('rm -rf /')"`, etc.
        # The terminal is for project-local read/build commands only —
        # full scripts belong in files run via the existing Run button.
        "python", "python3", "python2", "py", "ipython",
        "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "lua", "tcl",
        "bash", "sh", "zsh", "fish", "ksh", "csh", "dash",
        "awk", "gawk", "sed",
        "xargs", "find",   # `find -exec`, xargs can re-launch arbitrary cmds
    )
    # Flags / argv tokens that signal "interpret the next argument as code"
    # — checked even when the binary itself isn't on the blocklist (defense
    # in depth against e.g. `env python -c …`, `/usr/bin/env -S python -c`).
    TERMINAL_BLOCKED_FLAGS    = ("-c", "-e", "--exec", "--eval", "-S")
    # ── Phase 22 — Ollama base URL SSRF guard ──────────────────────────────
    # Hosts the Ollama client is allowed to talk to.  Reuses the existing
    # browser allowlist for the runtime list; this is the *seed* default
    # used when no allowlist file exists.
    OLLAMA_ALLOWED_HOSTS_DEFAULT = ("localhost", "127.0.0.1", "::1")

    # ── Phase 24 — Multi-mode terminal (Sandbox vs. Local Bridge) ──────────
    # The web app's terminal can run in one of two modes:
    #   "sandbox" — existing Phase 22 in-process sandbox (safe default)
    #   "local"   — proxies to a user-run Local Bridge for real OS access
    # The mode + bridge config is persisted at data/terminal_mode.json.
    TERMINAL_MODES                = ("sandbox", "local")
    TERMINAL_MODE_DEFAULT         = os.getenv("TERMINAL_MODE_DEFAULT",
                                              "sandbox").strip().lower()
    # Maximum allowed bridge URL length / token length on POST.
    TERMINAL_BRIDGE_URL_MAX       = 256
    TERMINAL_BRIDGE_TOKEN_MAX     = 256
    # Per-call timeouts when proxying to the Local Bridge.  Independent of
    # the sandbox limits — the bridge runs on the user's box, so they can
    # tolerate (and may want) longer-running commands like `pip install`.
    TERMINAL_BRIDGE_TIMEOUT_SEC   = int(os.getenv(
        "TERMINAL_BRIDGE_TIMEOUT_SEC", "120"))
    TERMINAL_BRIDGE_HEALTH_TIMEOUT_SEC = int(os.getenv(
        "TERMINAL_BRIDGE_HEALTH_TIMEOUT_SEC", "5"))
    # Max bytes we'll buffer from a single bridge response before truncating.
    TERMINAL_BRIDGE_MAX_RESPONSE_BYTES = int(os.getenv(
        "TERMINAL_BRIDGE_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))