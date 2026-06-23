"""
Application configuration using Pydantic BaseSettings.
Loads values from environment variables and .env file.
"""

import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Twilio ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_NUMBER: str = "whatsapp:+14155238886"

    # --- Server ---
    BASE_URL: str = "http://localhost:8000"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Database ---
    DATABASE_URL: str = "sqlite:///./db/stock_screener.db"

    # --- Processing ---
    MAX_WORKERS: int = 10
    API_CALL_DELAY: float = 0.5
    MAX_RETRIES: int = 3

    # --- Cache ---
    CACHE_TTL_HOURS: int = 24

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


@lru_cache()
def get_settings() -> Settings:
    """Get cached application settings singleton."""
    return Settings()
