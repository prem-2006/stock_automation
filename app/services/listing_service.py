"""
Listing universe service.

Builds the list of every company that listed in a given year across Indian
exchanges, not just the NSE main board:

- NSE main board  (EQUITY_L.csv)
- NSE SME/Emerge  (SME_EQUITY_L.csv)
- BSE-only companies (mostly BSE SME). BSE publishes no listing dates, so each
  one is dated by the month its Yahoo Finance price history starts.

Companies Yahoo has no prices for (almost all NSE SME stocks) are kept in the
universe but flagged "unavailable", so counts stay honest and no time is spent
requesting data that does not exist.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Dict, List, Optional, Set

import pandas as pd
import requests
import yfinance as yf

from app.config import get_settings
from app.services.nse_service import (
    LIVE_FETCH_TIMEOUT,
    NSEService,
    live_source_blocked,
    mark_live_source_failed,
)
from app.utils.cache import FileCache
from app.utils.logger import get_logger

logger = get_logger("listing_service")

BSE_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}

# BSE scrip codes of companies listed since ~2016 start at 540000
BSE_MIN_CODE = 540000
BSE_SNAPSHOT = os.path.join("data", "bse_listings.csv")

# Tickers per bulk Yahoo request, and a liquid ticker that proves Yahoo answered
CHUNK_SIZE = 100
CONTROL_TICKER = "RELIANCE.NS"

# One bulk Yahoo download and one universe build at a time, even with both bots scanning
_DOWNLOAD_LOCK = threading.Lock()
_UNIVERSE_LOCK = threading.Lock()

# Seconds a single Yahoo request may take, and how long to wait for another bot's turn
DOWNLOAD_TIMEOUT = 20
LOCK_WAIT_SECONDS = 120


@contextmanager
def _turn(lock: threading.Lock, wait: float = LOCK_WAIT_SECONDS):
    """
    Take the lock for politeness between bots, but never wait forever: if the
    other bot is stuck, carry on without it rather than hanging this scan too.
    """
    acquired = lock.acquire(timeout=wait)
    try:
        yield
    finally:
        if acquired:
            lock.release()


def _load_json(raw: Optional[str]) -> dict:
    """Parse a cached JSON object; an unreadable cache entry counts as empty."""
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def download_monthly_history(tickers: List[str]) -> Dict[str, Optional[pd.DataFrame]]:
    """
    Monthly OHLC history for many tickers in a few bulk requests.

    Prices are actual traded prices (splits/bonuses only) with a tz-naive
    month-start index. Each ticker maps to its frame, or to None when Yahoo has
    no data. Tickers whose chunk was inconclusive (request failed or throttled,
    detected through a liquid control ticker) are left out so callers can retry.
    """
    unique = list(dict.fromkeys(tickers))
    frames: Dict[str, Optional[pd.DataFrame]] = {}

    with _turn(_DOWNLOAD_LOCK):
        for i in range(0, len(unique), CHUNK_SIZE):
            part = unique[i:i + CHUNK_SIZE]
            chunk: Dict[str, Optional[pd.DataFrame]] = {}
            for attempt in (1, 2):  # one quick retry: a throttled chunk often succeeds a moment later
                try:
                    data = yf.download(
                        part + [CONTROL_TICKER], period="max", interval="1mo",
                        auto_adjust=False, group_by="ticker", threads=True, progress=False,
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                except Exception as e:
                    logger.warning(f"Bulk Yahoo request failed for {len(part)} tickers: {e}")
                    data = None

                chunk = {}
                for ticker in part + [CONTROL_TICKER]:
                    try:
                        frame = data[ticker].dropna(subset=["High", "Close"])
                        chunk[ticker] = frame if len(frame) else None
                    except Exception:
                        chunk[ticker] = None

                if chunk[CONTROL_TICKER] is not None:
                    break
                if attempt == 1:
                    time.sleep(3)

            if chunk[CONTROL_TICKER] is None:
                logger.warning(f"Yahoo did not answer a chunk of {len(part)} tickers; skipping it")
                continue

            frames.update({t: chunk[t] for t in part})
            if i + CHUNK_SIZE < len(unique):
                time.sleep(0.5)  # be gentle between chunks

    return frames


class ListingService:
    """Builds the full listing universe for an IPO year."""

    def __init__(self, nse_service: Optional[NSEService] = None):
        settings = get_settings()
        is_vercel = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None
        cache_dir = "/tmp/data" if is_vercel else "data"
        self.nse_service = nse_service or NSEService()
        self.cache = FileCache(cache_dir=cache_dir, ttl_hours=settings.CACHE_TTL_HOURS)
        # First-trade months never change, so they are kept for a year
        self.dating_cache = FileCache(cache_dir=cache_dir, ttl_hours=24 * 365)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_stocks_by_ipo_year(self, year: int) -> List[Dict]:
        """Thread-safe wrapper: two bots asking at once build the universe one after the other."""
        with _turn(_UNIVERSE_LOCK, wait=300):
            return self._build_universe(year)

    def _build_universe(self, year: int) -> List[Dict]:
        """
        Every company that listed in `year` (all years for 0) on NSE, NSE SME or BSE.

        Each dict has: symbol, company_name, listing_date, isin, segment
        ("NSE" / "NSE SME" / "BSE"), yf_symbol, bse_symbol (BSE id for the
        pre-IPO history check, or None) and unavailable (True when Yahoo has
        no prices for it).
        """
        # The BSE list is independent of the NSE lists, so fetch it while they load
        with ThreadPoolExecutor(max_workers=1) as pool:
            bse_future = pool.submit(self._fetch_bse_list)
            main = self.nse_service.get_stocks_by_ipo_year(year)
            sme = self.nse_service.get_sme_stocks_by_ipo_year(year)
            bse_list = bse_future.result()
        bse_id_by_isin = dict(zip(bse_list["ISIN_NUMBER"], bse_list["scrip_id"])) if not bse_list.empty else {}

        stocks: List[Dict] = []
        seen_isins: Set[str] = set()
        for stock in main + sme:
            if stock["isin"] and stock["isin"] in seen_isins:
                continue
            seen_isins.add(stock["isin"])

            stock["yf_symbol"] = f"{stock['symbol']}.NS"
            if bse_id_by_isin:
                stock["bse_symbol"] = bse_id_by_isin.get(stock["isin"])
            else:
                # BSE list unavailable: assume the BSE id matches the NSE symbol (main board only)
                stock["bse_symbol"] = stock["symbol"] if stock["segment"] == "NSE" else None
            stock["unavailable"] = False
            stocks.append(stock)

        self._flag_unavailable([s for s in stocks if s["segment"] == "NSE SME"])

        for scrip in self._bse_only_dated(self._all_nse_isins(), bse_list):
            first_trade = scrip["first_trade"]
            if year and int(first_trade[:4]) != year:
                continue
            stocks.append({
                "symbol": scrip["scrip_id"],
                "company_name": scrip["name"],
                "listing_date": pd.Timestamp(f"{first_trade}-01"),
                "isin": scrip["isin"],
                "segment": "BSE",
                "yf_symbol": f"{scrip['scrip_id']}.BO",
                "bse_symbol": None,
                "unavailable": False,
            })

        by_segment: Dict[str, int] = {}
        for s in stocks:
            by_segment[s["segment"]] = by_segment.get(s["segment"], 0) + 1
        logger.info(f"Listing universe for {year}: {len(stocks)} companies {by_segment}")
        return stocks

    # ------------------------------------------------------------------ #
    # NSE helpers
    # ------------------------------------------------------------------ #

    def _all_nse_isins(self) -> Set[str]:
        """ISINs of every NSE main-board and SME company (all years)."""
        isins: Set[str] = set()
        for df in (self.nse_service.fetch_equity_list(), self.nse_service.fetch_sme_list()):
            if not df.empty and "ISIN NUMBER" in df.columns:
                isins.update(df["ISIN NUMBER"].dropna().astype(str).str.strip())
        return isins

    def _flag_unavailable(self, stocks: List[Dict]) -> None:
        """Mark stocks Yahoo has no price history for, using one bulk check cached for a day."""
        if not stocks:
            return

        raw = self.cache.get("sme_availability")
        availability: Dict[str, bool] = _load_json(raw)

        missing = [s["yf_symbol"] for s in stocks if s["yf_symbol"] not in availability]
        if missing:
            for ticker, first_month in self._first_trade_months(missing).items():
                availability[ticker] = first_month is not None
            self.cache.set("sme_availability", json.dumps(availability))

        for stock in stocks:
            # Unknown (inconclusive check) stays available, so the scan simply tries it
            stock["unavailable"] = availability.get(stock["yf_symbol"]) is False

    # ------------------------------------------------------------------ #
    # BSE helpers
    # ------------------------------------------------------------------ #

    def _fetch_bse_list(self) -> pd.DataFrame:
        """BSE active equity list (SCRIP_CD, scrip_id, Scrip_Name, ISIN_NUMBER, GROUP); empty on failure."""
        cached = self.cache.get("bse_equity_list")
        if cached is not None:
            try:
                return pd.DataFrame(json.loads(cached))
            except ValueError:
                pass

        if not live_source_blocked("bse_list"):
            try:
                response = requests.get(BSE_LIST_URL, headers=BSE_HEADERS, timeout=LIVE_FETCH_TIMEOUT * 2)
                response.raise_for_status()
                rows = response.json()
                if rows:
                    self.cache.set("bse_equity_list", json.dumps(rows))
                    logger.info(f"Fetched {len(rows)} BSE equity scrips")
                    return pd.DataFrame(rows)
            except Exception as e:
                logger.warning(f"BSE equity list fetch failed ({e}); using bundled snapshot")
                mark_live_source_failed("bse_list")

        return pd.DataFrame()

    def _bse_only_dated(self, nse_isins: Set[str], bse_list: pd.DataFrame) -> List[Dict]:
        """
        BSE companies that are not on NSE and have a known first-trade month.

        First-trade months come from the bundled snapshot, then from a
        persistent cache, and only scrips new since then are dated live.
        """
        known: Dict[str, str] = {}  # scrip_id -> "YYYY-MM", or "" when Yahoo has no data
        snapshot = pd.DataFrame()
        if os.path.exists(BSE_SNAPSHOT):
            snapshot = pd.read_csv(BSE_SNAPSHOT, dtype=str).fillna("")
            known.update(zip(snapshot["scrip_id"], snapshot["first_trade"]))

        raw = self.dating_cache.get("bse_first_trade")
        extra: Dict[str, str] = _load_json(raw)
        known.update(extra)

        # Candidates: the live list when we have it, otherwise the snapshot
        if not bse_list.empty:
            frame = bse_list.rename(columns={"Scrip_Name": "name", "ISIN_NUMBER": "isin"})
            frame["code"] = pd.to_numeric(frame["SCRIP_CD"], errors="coerce")
        elif not snapshot.empty:
            frame = snapshot.rename(columns={"Scrip_Name": "name", "ISIN_NUMBER": "isin"})
            frame["code"] = pd.to_numeric(frame["SCRIP_CD"], errors="coerce")
        else:
            return []

        frame = frame[
            frame["isin"].astype(str).str.startswith("INE")  # equity shares, not ETFs/units/bonds
            & ~frame["isin"].isin(nse_isins)
            & (frame["code"] >= BSE_MIN_CODE)
        ]

        undated = [s for s in frame["scrip_id"] if s not in known]
        if undated:
            logger.info(f"Dating {len(undated)} new BSE scrips from Yahoo Finance")
            months = self._first_trade_months([f"{s}.BO" for s in undated])
            for ticker, first_month in months.items():
                extra[ticker[:-3]] = first_month or ""
            known.update(extra)
            self.dating_cache.set("bse_first_trade", json.dumps(extra))

        return [
            {"scrip_id": r.scrip_id, "name": r.name, "isin": r.isin, "first_trade": known[r.scrip_id]}
            for r in frame.itertuples()
            if known.get(r.scrip_id)
        ]

    # ------------------------------------------------------------------ #
    # Yahoo helper
    # ------------------------------------------------------------------ #

    def _first_trade_months(self, tickers: List[str]) -> Dict[str, Optional[str]]:
        """
        Month ("YYYY-MM") each ticker's Yahoo monthly history starts, or None
        when Yahoo has no data. Inconclusive checks are left out of the result.
        """
        frames = download_monthly_history(tickers)
        return {
            ticker: (frame.index[0].strftime("%Y-%m") if frame is not None else None)
            for ticker, frame in frames.items()
        }
