"""
config_manager.py — Phase 43: Single Source of Truth for Configuration
======================================================================
Centralizes all environment variables and flags.
"""
import os
import logging
from typing import Any, Dict

class ConfigManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        self.allow_local_models = os.environ.get("ALLOW_LOCAL_MODELS", "false").lower() == "true"
        self.ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
        self.groq_api_key = os.environ.get("GROQ_API_KEY", "")
        self.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.log_level = os.environ.get("LOG_LEVEL", "INFO")
        
        # Setup basic logging
        logging.basicConfig(
            level=getattr(logging, self.log_level.upper(), logging.INFO),
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        )

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

def get_config() -> ConfigManager:
    return ConfigManager()
