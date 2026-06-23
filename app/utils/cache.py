"""
Caching utilities for NSE data and historical stock prices.
Provides file-based cache with TTL and in-memory LRU cache.
"""

import json
import os
import time
from functools import lru_cache
from typing import Any, Optional

from app.utils.logger import get_logger

logger = get_logger("cache")


class FileCache:
    """File-based cache with TTL support for persistent data caching."""

    def __init__(self, cache_dir: str = "data", ttl_hours: int = 24):
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)

    def _get_cache_path(self, key: str) -> str:
        """Get the file path for a cache key."""
        safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.cache_dir, f"{safe_key}.cache")

    def _get_meta_path(self, key: str) -> str:
        """Get the metadata file path for a cache key."""
        safe_key = key.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.cache_dir, f"{safe_key}.meta")

    def get(self, key: str) -> Optional[str]:
        """
        Get cached value if it exists and hasn't expired.

        Args:
            key: Cache key

        Returns:
            Cached value as string, or None if expired/missing
        """
        cache_path = self._get_cache_path(key)
        meta_path = self._get_meta_path(key)

        if not os.path.exists(cache_path) or not os.path.exists(meta_path):
            return None

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)

            cached_time = meta.get("timestamp", 0)
            if time.time() - cached_time > self.ttl_seconds:
                logger.info(f"Cache expired for key: {key}")
                return None

            with open(cache_path, "r", encoding="utf-8") as f:
                data = f.read()

            logger.debug(f"Cache hit for key: {key}")
            return data

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Cache read error for key {key}: {e}")
            return None

    def set(self, key: str, value: str) -> None:
        """
        Store a value in the cache.

        Args:
            key: Cache key
            value: Value to cache (as string)
        """
        cache_path = self._get_cache_path(key)
        meta_path = self._get_meta_path(key)

        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(value)

            with open(meta_path, "w") as f:
                json.dump({"timestamp": time.time(), "key": key}, f)

            logger.debug(f"Cache set for key: {key}")

        except IOError as e:
            logger.error(f"Cache write error for key {key}: {e}")

    def invalidate(self, key: str) -> None:
        """Remove a cached item."""
        for path in [self._get_cache_path(key), self._get_meta_path(key)]:
            if os.path.exists(path):
                os.remove(path)
        logger.info(f"Cache invalidated for key: {key}")

    def clear_all(self) -> None:
        """Clear all cached items."""
        for filename in os.listdir(self.cache_dir):
            if filename.endswith((".cache", ".meta")):
                os.remove(os.path.join(self.cache_dir, filename))
        logger.info("All cache cleared")


class HistoricalDataCache:
    """
    SQLite-backed cache for historical OHLC data.
    Stores serialized DataFrames to avoid repeated yfinance calls.
    """

    def __init__(self, db_session_factory):
        self.db_session_factory = db_session_factory

    def get_cached_data(self, symbol: str) -> Optional[str]:
        """Get cached historical data JSON for a symbol."""
        from app.models import CachedStockData

        session = self.db_session_factory()
        try:
            cached = session.query(CachedStockData).filter_by(symbol=symbol).first()
            if cached and (time.time() - cached.cached_at.timestamp()) < 86400:  # 24 hours
                logger.debug(f"Historical cache hit for {symbol}")
                return cached.data_json
            return None
        finally:
            session.close()

    def set_cached_data(self, symbol: str, data_json: str) -> None:
        """Store historical data JSON for a symbol."""
        from app.models import CachedStockData
        from datetime import datetime, UTC

        session = self.db_session_factory()
        try:
            existing = session.query(CachedStockData).filter_by(symbol=symbol).first()
            if existing:
                existing.data_json = data_json
                existing.cached_at = datetime.now(UTC)
            else:
                cached = CachedStockData(
                    symbol=symbol,
                    data_json=data_json,
                    cached_at=datetime.now(UTC),
                )
                session.add(cached)
            session.commit()
            logger.debug(f"Historical cache set for {symbol}")
        except Exception as e:
            session.rollback()
            logger.error(f"Historical cache write error for {symbol}: {e}")
        finally:
            session.close()
