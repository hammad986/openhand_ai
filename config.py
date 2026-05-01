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
    GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")

    # Phase 5 — Extended BYOK provider keys
    OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY     = os.getenv("ANTHROPIC_API_KEY", "")
    XAI_API_KEY           = os.getenv("XAI_API_KEY", "")
    AZURE_OPENAI_KEY      = os.getenv("AZURE_OPENAI_KEY", "")
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    FIREWORKS_API_KEY     = os.getenv("FIREWORKS_API_KEY", "")
    DEEPSEEK_API_KEY      = os.getenv("DEEPSEEK_API_KEY", "")
    MISTRAL_API_KEY       = os.getenv("MISTRAL_API_KEY", "")
    COHERE_API_KEY        = os.getenv("COHERE_API_KEY", "")
    HUGGINGFACE_API_KEY   = os.getenv("HUGGINGFACE_API_KEY", "")
    REPLICATE_API_KEY     = os.getenv("REPLICATE_API_KEY", "")
    ELEVENLABS_API_KEY    = os.getenv("ELEVENLABS_API_KEY", "")
    DEEPGRAM_API_KEY      = os.getenv("DEEPGRAM_API_KEY", "")

    # -- API endpoints ---------------------------------------------------------
    GROQ_URL            = "https://api.groq.com/openai/v1/chat/completions"
    OPENROUTER_URL      = "https://openrouter.ai/api/v1/chat/completions"
    NVIDIA_URL          = "https://integrate.api.nvidia.com/v1/chat/completions"
    TOGETHER_URL        = "https://api.together.xyz/v1/chat/completions"
    _base = (os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    OLLAMA_URL          = f"{_base}/api/chat"
    OLLAMA_GENERATE_URL = f"{_base}/api/generate"

    # Gemini REST - no SDK required
    GEMINI_BASE_URL    = "https://generativelanguage.googleapis.com/v1beta/models"
    GEMINI_FLASH_MODEL = "gemini-1.5-flash"
    GEMINI_PRO_MODEL   = "gemini-1.5-pro"

    # Phase 5 — Extended provider endpoints
    OPENAI_URL      = "https://api.openai.com/v1/chat/completions"
    ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"        # native format
    XAI_URL         = "https://api.x.ai/v1/chat/completions"
    FIREWORKS_URL   = "https://api.fireworks.ai/inference/v1/chat/completions"
    DEEPSEEK_URL    = "https://api.deepseek.com/v1/chat/completions"
    MISTRAL_URL     = "https://api.mistral.ai/v1/chat/completions"
    COHERE_URL      = "https://api.cohere.ai/v2/chat"                # native format
    HUGGINGFACE_URL = "https://api-inference.huggingface.co/models"
    REPLICATE_URL   = "https://api.replicate.com/v1/predictions"
    ELEVENLABS_URL  = "https://api.elevenlabs.io/v1/text-to-speech"
    DEEPGRAM_URL    = "https://api.deepgram.com/v1/speak"

    # -- Models ----------------------------------------------------------------
    MODELS = {
        # Existing providers
        "gemini":       "gemini-1.5-flash",
        "groq":         os.getenv("GROQ_MODEL",       "llama-3.3-70b-versatile"),
        "openrouter":   os.getenv("OPENROUTER_MODEL",  "deepseek/deepseek-chat"),
        "nvidia":       os.getenv("NVIDIA_MODEL",      "meta/llama-3.3-70b-instruct"),
        "together":     os.getenv("TOGETHER_MODEL",    "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        "local":        os.getenv("LOCAL_MODEL",       "codellama"),
        # Phase 5 — Core providers
        "openai":       os.getenv("OPENAI_MODEL",      "gpt-4o-mini"),
        "anthropic":    os.getenv("ANTHROPIC_MODEL",   "claude-3-5-haiku-20241022"),
        # Phase 5 — High value
        "xai":          os.getenv("XAI_MODEL",         "grok-beta"),
        "azure":        os.getenv("AZURE_MODEL",       "gpt-4o"),
        "bedrock":      os.getenv("BEDROCK_MODEL",     "anthropic.claude-3-haiku-20240307-v1:0"),
        "fireworks":    os.getenv("FIREWORKS_MODEL",   "accounts/fireworks/models/llama-v3p3-70b-instruct"),
        # Phase 5 — Open/Fallback
        "deepseek":     os.getenv("DEEPSEEK_MODEL",    "deepseek-coder"),
        "mistral":      os.getenv("MISTRAL_MODEL",     "mistral-small-latest"),
        "cohere":       os.getenv("COHERE_MODEL",      "command-r"),
        "huggingface":  os.getenv("HF_MODEL",          "Qwen/Qwen2.5-Coder-32B-Instruct"),
        # Phase 5 — Multimodal
        "replicate":    os.getenv("REPLICATE_MODEL",   "meta/llama-3-70b-instruct"),
        "elevenlabs":   "eleven_flash_v2_5",
        "deepgram":     "aura-asteria-en",
    }
    OLLAMA_PLANNER_MODEL = os.getenv("OLLAMA_PLANNER_MODEL", "phi3:mini")

    # -- Cost per 1k tokens (USD) ----------------------------------------------
    COST_PER_1K = {
        # Existing
        "gemini":       0.000075,
        "groq":         0.0,
        "local":        0.0,
        "together":     0.0009,
        "openrouter":   0.0014,
        "nvidia":       0.001,
        # Phase 5
        "openai":       0.00015,    # gpt-4o-mini input
        "anthropic":    0.0008,     # claude-3-5-haiku input
        "xai":          0.005,      # grok-beta estimate
        "azure":        0.00015,
        "bedrock":      0.00025,
        "fireworks":    0.0009,
        "deepseek":     0.00014,
        "mistral":      0.0002,
        "cohere":       0.00015,
        "huggingface":  0.0,
        "replicate":    0.00065,
        "elevenlabs":   0.0,
        "deepgram":     0.0,
    }

    # Priority: Gemini -> OpenRouter -> Groq -> NVIDIA -> Together
    # Can be overridden by env var API_PRIORITY (comma-separated provider names).
    _API_PRIORITY_ENV = os.getenv("API_PRIORITY", "").strip()
    API_PRIORITY = (
        [p.strip().lower() for p in _API_PRIORITY_ENV.split(",") if p.strip()]
        if _API_PRIORITY_ENV
        else ["gemini", "openrouter", "groq", "nvidia", "together"]
    )
    ALLOW_LOCAL_FALLBACK      = _env_bool("ALLOW_LOCAL_FALLBACK", "0")
    DEFAULT_REMOTE_MODEL      = os.getenv("DEFAULT_REMOTE_MODEL", "gemini").strip().lower()

    # Phase 5 — Plan-mode preferred provider order
    # Lite → fastest providers, Pro → balanced, Elite → strongest reasoning
    PLAN_PROVIDER_PREFERENCE = {
        "lite":  ["groq", "gemini", "deepseek", "mistral", "fireworks", "openrouter", "together", "openai"],
        "pro":   ["gemini", "openai", "anthropic", "openrouter", "together", "groq", "xai", "mistral"],
        "elite": ["anthropic", "openai", "xai", "gemini", "openrouter", "fireworks", "together", "nvidia"],
    }

    # Phase 5 — Provider capability map
    PROVIDER_CAPABILITIES = {
        "openai":      {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": True},
        "anthropic":   {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": True},
        "gemini":      {"thinking": True,  "coding": True,  "debugging": True,  "fast": True,  "multimodal": True},
        "groq":        {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "openrouter":  {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": True},
        "xai":         {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": False},
        "azure":       {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": True},
        "bedrock":     {"thinking": True,  "coding": True,  "debugging": True,  "fast": False, "multimodal": False},
        "fireworks":   {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "deepseek":    {"thinking": True,  "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "mistral":     {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "cohere":      {"thinking": False, "coding": True,  "debugging": False, "fast": True,  "multimodal": False},
        "huggingface": {"thinking": False, "coding": True,  "debugging": False, "fast": False, "multimodal": False},
        "together":    {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "nvidia":      {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
        "replicate":   {"thinking": False, "coding": False, "debugging": False, "fast": False, "multimodal": True},
        "elevenlabs":  {"thinking": False, "coding": False, "debugging": False, "fast": True,  "multimodal": True},
        "deepgram":    {"thinking": False, "coding": False, "debugging": False, "fast": True,  "multimodal": True},
        "local":       {"thinking": False, "coding": True,  "debugging": True,  "fast": True,  "multimodal": False},
    }

    # -- Loop controls ---------------------------------------------------------
    MAX_RETRIES                 = 3
    PER_STEP_RETRY              = 3
    MAX_AGENT_LOOPS             = 40
    MAX_TOTAL_ATTEMPTS          = 40
    PLANNER_MAX_STEPS           = 10
    STAGE_NAMES    = ["setup", "backend", "frontend", "testing", "finalization"]
    PLANNER_STAGED = True
    USER_INTERVENTION_THRESHOLD = 5

    GEMINI_PRO_AFTER_FAILURES   = 2

    # -- Infrastructure --------------------------------------------------------
    TIMEOUT_SECONDS = 30
    OLLAMA_TIMEOUT  = 60
    WORKSPACE_DIR   = os.getenv("WORKSPACE_DIR") or "./workspace"
    MEMORY_FILE     = "./memory.json"
    CHECKPOINT_FILE = "./checkpoint.json"
    VECTOR_DB_DIR   = "./memory_vectors"
    EMBEDDING_DIM   = 256
    LOG_FILE        = "./agent.log"

    # ── Phase 21.2 — Editor review policy ──────────────────────────────────
    REVIEW_MODES               = ("REQUEST_REVIEW", "ALWAYS_PROCEED", "AGENT_DECIDES")
    REVIEW_MODE_DEFAULT        = os.getenv("REVIEW_MODE_DEFAULT", "REQUEST_REVIEW").strip().upper()
    REVIEW_HYBRID_MAX_LINES    = int(os.getenv("REVIEW_HYBRID_MAX_LINES", "10"))
    REVIEW_HYBRID_MAX_HUNKS    = int(os.getenv("REVIEW_HYBRID_MAX_HUNKS", "2"))
    REVIEW_HYBRID_AUTO_ACTIONS = ("optimize", "explain")

    # ── Phase 22 — Model routing roles ─────────────────────────────────────
    ROUTING_ROLES               = ("planner", "coding", "reasoning")
    ROUTING_PROVIDERS           = ("auto", "gemini", "openrouter", "groq",
                                   "nvidia", "together", "ollama", "openai",
                                   "anthropic", "xai", "fireworks", "deepseek",
                                   "mistral", "cohere")
    ROUTING_DEFAULT_PROVIDER    = os.getenv("ROUTING_DEFAULT_PROVIDER",
                                            "auto").strip().lower()
    ROUTING_DEFAULT_PLANNER     = os.getenv("ROUTING_PLANNER_MODEL",  "")
    ROUTING_DEFAULT_CODING      = os.getenv("ROUTING_CODING_MODEL",   "")
    ROUTING_DEFAULT_REASONING   = os.getenv("ROUTING_REASONING_MODEL", "")

    # ── Phase 22 — Terminal sandbox ─────────────────────────────────────────
    TERMINAL_TIMEOUT_SEC      = int(os.getenv("TERMINAL_TIMEOUT_SEC",  "10"))
    TERMINAL_MAX_OUTPUT_BYTES = int(os.getenv("TERMINAL_MAX_OUTPUT_BYTES",
                                              str(256 * 1024)))
    TERMINAL_MEM_MB           = int(os.getenv("TERMINAL_MEM_MB",       "256"))
    TERMINAL_BLOCKED_TOKENS   = (
        "rm", "shutdown", "reboot", "halt", "poweroff", "init",
        "mkfs", "fdisk", "dd", "mount", "umount", "useradd",
        "userdel", "passwd", "su", "sudo", "chown", "chmod",
        "kill", "killall", "pkill", "iptables", "nc", "ncat",
        "telnet", "ssh", "scp", "rsync", "wget", "curl",
        "python", "python3", "python2", "py", "ipython",
        "node", "nodejs", "deno", "bun",
        "perl", "ruby", "php", "lua", "tcl",
        "bash", "sh", "zsh", "fish", "ksh", "csh", "dash",
        "awk", "gawk", "sed",
        "xargs", "find",
    )
    TERMINAL_BLOCKED_FLAGS    = ("-c", "-e", "--exec", "--eval", "-S")
    OLLAMA_ALLOWED_HOSTS_DEFAULT = ("localhost", "127.0.0.1", "::1")

    # ── Phase 24 — Multi-mode terminal ──────────────────────────────────────
    TERMINAL_MODES                = ("sandbox", "local")
    TERMINAL_MODE_DEFAULT         = os.getenv("TERMINAL_MODE_DEFAULT",
                                              "sandbox").strip().lower()
    TERMINAL_BRIDGE_URL_MAX       = 256
    TERMINAL_BRIDGE_TOKEN_MAX     = 256
    TERMINAL_BRIDGE_TIMEOUT_SEC   = int(os.getenv(
        "TERMINAL_BRIDGE_TIMEOUT_SEC", "120"))
    TERMINAL_BRIDGE_HEALTH_TIMEOUT_SEC = int(os.getenv(
        "TERMINAL_BRIDGE_HEALTH_TIMEOUT_SEC", "5"))
    TERMINAL_BRIDGE_MAX_RESPONSE_BYTES = int(os.getenv(
        "TERMINAL_BRIDGE_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
