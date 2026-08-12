"""
config.py — Application settings.

WHY PYDANTIC-SETTINGS?
----------------------
Pydantic-settings gives you a class where every attribute is:
  1. Read from environment variables (or a .env file) automatically
  2. Type-validated — if MAX_UPLOAD_SIZE_MB is set to "banana" your app
     fails immediately on startup with a clear error, not mid-request
  3. Documented — you can see every config option in one place

The alternative is os.getenv("MAX_UPLOAD_SIZE_MB", "25") scattered
everywhere. That works but is fragile and hard to discover.

USAGE:
  from app.config import settings
  print(settings.anthropic_api_key)   # reads ANTHROPIC_API_KEY from env
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Tell pydantic-settings to read from a .env file if present.
    # env_file_encoding ensures accented characters in paths don't break things.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # If an extra env var is present that isn't in this class, ignore it
        # rather than raising a validation error.
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    anthropic_api_key: str                        # required — no default
    model_name: str = "claude-opus-4-5"

    # ── Upload limits ─────────────────────────────────────────────────────────
    max_upload_size_mb: int = 25
    max_file_count: int = 2000

    # ── Agent behaviour ───────────────────────────────────────────────────────
    # Each agent node in LangGraph will time out after this many seconds.
    # Prevents one slow agent from blocking the whole review indefinitely.
    agent_timeout_seconds: int = 45

    # ── Job store ─────────────────────────────────────────────────────────────
    # "memory" = in-process dict (fine for v1 single-instance demo)
    # "redis"  = Redis-backed (v2, for persistence / horizontal scaling)
    job_store_backend: str = "memory"


# Instantiate once at import time. Every other module does:
#   from app.config import settings
# This is the Python "module singleton" pattern — the Settings object is
# created once when this module is first imported and then reused everywhere.
settings = Settings()
