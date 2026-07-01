"""
NSE Stock List Service.

Fetches and manages the list of NSE-listed stocks.
Uses NSE equity master CSV as primary source and yfinance as fallback.
"""

import io
import os
import time
from datetime import datetime
from typing import List, Dict, Optional

import pandas as pd
import requests

from app.config import get_settings
from app.utils.cache import FileCache
from app.utils.logger import get_logger

logger = get_logger("nse_service")

# NSE equity list URL (official source)
NSE_EQUITY_URL = "https://nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"
NSE_EQUITY_CSV_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# Browser-like headers required by NSE
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.nseindia.com/",
}


class NSEService:
    """Service for fetching and managing NSE stock data."""

    def __init__(self):
        settings = get_settings()
        # Use /tmp on Vercel (only writable directory)
        is_vercel = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None
        cache_dir = "/tmp/data" if is_vercel else "data"
        self.cache = FileCache(cache_dir=cache_dir, ttl_hours=settings.CACHE_TTL_HOURS)
        self.max_retries = settings.MAX_RETRIES
        self._session = None

    def _get_session(self) -> requests.Session:
        """Get or create a requests session with NSE headers."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(NSE_HEADERS)
            # Visit main page first to get cookies
            try:
                self._session.get("https://www.nseindia.com", timeout=10)
            except Exception:
                pass
        return self._session

    def fetch_equity_list(self) -> pd.DataFrame:
        """
        Fetch the complete list of NSE-listed equities with listing dates.

        Returns:
            DataFrame with columns: SYMBOL, NAME OF COMPANY, DATE OF LISTING, etc.
        """
        # Try cache first
        cached_data = self.cache.get("nse_equity_list")
        if cached_data is not None:
            logger.info("Using cached NSE equity list")
            df = pd.read_csv(io.StringIO(cached_data))
            return df

        # Fetch from NSE
        df = self._fetch_from_nse_csv()
        if df is not None and not df.empty:
            # Cache the data
            self.cache.set("nse_equity_list", df.to_csv(index=False))
            return df

        # Fallback: try to load from local file
        local_path = os.path.join("data", "nse_equity_master.csv")
        if os.path.exists(local_path):
            logger.info("Using local NSE equity master file")
            df = pd.read_csv(local_path)
            return df

        logger.error("Failed to fetch NSE equity list from all sources")
        return pd.DataFrame()

    def _fetch_from_nse_csv(self) -> Optional[pd.DataFrame]:
        """Fetch equity list CSV from NSE archives."""
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(f"Fetching NSE equity CSV (attempt {attempt}/{self.max_retries})")
                session = self._get_session()
                response = session.get(NSE_EQUITY_CSV_URL, timeout=30)
                response.raise_for_status()

                df = pd.read_csv(io.StringIO(response.text))

                # Clean column names
                df.columns = df.columns.str.strip()

                # Parse listing date
                if " DATE OF LISTING" in df.columns:
                    df.rename(columns={" DATE OF LISTING": "DATE OF LISTING"}, inplace=True)
                if "DATE OF LISTING" in df.columns:
                    df["DATE OF LISTING"] = pd.to_datetime(
                        df["DATE OF LISTING"], format="%d-%b-%Y", errors="coerce"
                    )
                    df["IPO_YEAR"] = df["DATE OF LISTING"].dt.year

                logger.info(f"Fetched {len(df)} stocks from NSE")
                return df

            except requests.exceptions.RequestException as e:
                logger.warning(f"NSE CSV fetch attempt {attempt} failed: {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)  # Exponential backoff

        return None

    def get_stocks_by_ipo_year(self, year: int) -> List[Dict]:
        """
        Get all stocks whose IPO/listing year matches the given year.

        Args:
            year: IPO year to filter by

        Returns:
            List of dicts with symbol, company_name, listing_date
        """
        df = self.fetch_equity_list()

        if df.empty:
            logger.warning(f"No equity data available to filter for year {year}")
            return []

        # Filter by IPO year
        if year == 0:
            filtered = df
            logger.info("Scanning ALL stocks (year=0)")
        elif "IPO_YEAR" in df.columns:
            filtered = df[df["IPO_YEAR"] == year]
        elif "DATE OF LISTING" in df.columns:
            df["DATE OF LISTING"] = pd.to_datetime(df["DATE OF LISTING"], errors="coerce")
            filtered = df[df["DATE OF LISTING"].dt.year == year]
        else:
            logger.error("No listing date column found in equity data")
            return []

        # We do not filter by SERIES anymore, so all stocks (including SME IPOs) are scanned.

        stocks = []
        for _, row in filtered.iterrows():
            symbol_col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
            name_col = "NAME OF COMPANY" if "NAME OF COMPANY" in df.columns else df.columns[1]

            stock = {
                "symbol": str(row.get(symbol_col, "")).strip(),
                "company_name": str(row.get(name_col, "")).strip(),
                "listing_date": row.get("DATE OF LISTING"),
            }

            if stock["symbol"]:
                stocks.append(stock)

        logger.info(f"Found {len(stocks)} stocks for IPO year {year}")
        return stocks

    def get_all_ipo_years(self) -> List[int]:
        """Get all available IPO years from the equity list."""
        df = self.fetch_equity_list()

        if df.empty or "IPO_YEAR" not in df.columns:
            return []

        years = sorted(df["IPO_YEAR"].dropna().unique().astype(int).tolist(), reverse=True)
        return years
