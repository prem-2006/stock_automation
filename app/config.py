"""
Application configuration using Pydantic BaseSettings.
Loads values from environment variables and .env file.

Compatible with Vercel serverless (uses /tmp for writable paths).
"""

import os
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Detect Vercel environment
IS_VERCEL = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram Bot Config ---
    TELEGRAM_BOT_TOKEN: str = Field(
        "", description="Telegram Bot Token"
    )

    # --- Server ---
    BASE_URL: str = "http://localhost:8000"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Database ---
    # On Vercel, use /tmp for SQLite (only writable directory)
    DATABASE_URL: str = "sqlite:////tmp/db/stock_screener.db" if IS_VERCEL else "sqlite:///./db/stock_screener.db"

    # --- Processing ---
    # Keep workers low to avoid Yahoo Finance rate limiting from server IPs
    MAX_WORKERS: int = 2 if IS_VERCEL else 3
    API_CALL_DELAY: float = 1.0  # 1 second between requests per worker
    MAX_RETRIES: int = 3

    # --- Cache ---
    CACHE_TTL_HOURS: int = 24

    # --- Logging ---
    LOG_LEVEL: str = "INFO"

    model_config = {
        "env_file": ".env" if not IS_VERCEL else None,
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


@lru_cache()
def get_settings() -> Settings:
    """Get cached application settings singleton."""
    return Settings()
