"""
IPO Breakout Scanner Service.

Core screening engine that:
1. Fetches monthly OHLC data for each stock using yfinance
2. Identifies the first listed month's HIGH
3. Finds fresh breakouts: the previous month closed below that IPO HIGH and
   the current month (live price) is at or above it
4. Generates results with parallel processing

All prices are actual traded prices (adjusted for splits/bonuses, NOT for
dividends), so every number matches what NSE / TradingView charts show.

Fully hardened against:
- NaN / Inf / missing data from yfinance
- Database transaction failures
- Network timeouts
- Corrupt / incomplete OHLC data
"""

import math
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, UTC
from typing import List, Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

from app.config import get_settings
from app.database import get_session_factory
from app.models import ScanJob, ScanResult
from app.services.nse_service import NSEService
from app.utils.logger import get_logger

logger = get_logger("scanner")

# NSE runs on Indian Standard Time (no DST); the server itself may be on UTC.
IST = timezone(timedelta(hours=5, minutes=30))

# Why a stock was left out of the comparison (result["skip_reason"])
SKIP_OLDER_LISTING = "older_listing"            # traded on NSE/BSE before its IPO year: not a fresh IPO
SKIP_NO_LISTING_MONTH = "no_listing_month_data"  # price data does not cover the listing month
SKIP_TOO_RECENT = "too_recent"                   # listed in/after the breakout month: no previous-month close
SKIP_NO_DATA = "no_data"                         # no or insufficient price data

# (breakout (year, month), previous (year, month), whether the breakout month is still running)
BreakoutMonths = Tuple[Tuple[int, int], Tuple[int, int], bool]


def now_ist() -> datetime:
    """Current date and time in IST."""
    return datetime.now(IST)


def breakout_months(target_month: Optional[int] = None, target_year: Optional[int] = None) -> BreakoutMonths:
    """
    Months for the breakout check.

    Without a target (Bot 1) the breakout month is the running month, judged on
    the live price, e.g. October (live) with September as the previous month.
    With a target (Bot 2) it is that month, judged on its close.
    """
    live = target_month is None or target_year is None
    if live:
        today = now_ist()
        target_year, target_month = today.year, today.month
    previous = (target_year - 1, 12) if target_month == 1 else (target_year, target_month - 1)
    return (target_year, target_month), previous, live


def safe_float(v):
    """Convert any value to a safe float, returning None for NaN/Inf/invalid."""
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def safe_round(v, digits=2):
    """Round a value safely, returning None if not a valid number."""
    f = safe_float(v)
    if f is None:
        return None
    return round(f, digits)


class ScannerService:
    """IPO breakout stock screening engine."""

    def __init__(self):
        self.settings = get_settings()
        self.nse_service = NSEService()

    def create_scan_job(self, year: int, phone_number: Optional[str] = None) -> str:
        """
        Create a new scan job record in the database.

        Args:
            year: IPO year to scan
            phone_number: Optional chat identifier for results delivery

        Returns:
            Scan job ID (UUID)
        """
        scan_id = str(uuid.uuid4())
        SessionLocal = get_session_factory()
        session = SessionLocal()

        try:
            job = ScanJob(
                id=scan_id,
                year=year,
                status="pending",
                phone_number=phone_number,
                created_at=datetime.now(UTC),
            )
            session.add(job)
            session.commit()
            logger.info(f"Created scan job {scan_id} for year {year}")
            return scan_id
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create scan job: {e}")
            raise
        finally:
            session.close()

    def run_scan(self, scan_id: str, target_month: int = None, target_year_override: int = None) -> Dict:
        """
        Execute the full scanning pipeline for a given scan job.

        Args:
            scan_id: UUID of the scan job
            target_month: Optional breakout month (1-12), judged on its close. Defaults to
                the running month, judged on the live price.
            target_year_override: Optional year for the breakout month (used with target_month).

        Returns:
            Summary dict with results
        """
        months = breakout_months(target_month, target_year_override)

        SessionLocal = get_session_factory()
        session = SessionLocal()

        try:
            # Get scan job
            job = session.query(ScanJob).filter_by(id=scan_id).first()
            if not job:
                raise ValueError(f"Scan job {scan_id} not found")

            year = job.year
            job.status = "running"
            session.commit()
            logger.info(f"Starting scan for IPO year {year} (job: {scan_id})")

            # Step 1: Get stocks for the given IPO year
            try:
                stocks = self.nse_service.get_stocks_by_ipo_year(year)
            except Exception as e:
                logger.error(f"Failed to fetch stock list for year {year}: {e}")
                stocks = []

            job.total_stocks = len(stocks)
            session.commit()

            if not stocks:
                job.status = "completed"
                job.completed_at = datetime.now(UTC)
                job.error_message = f"No stocks found for IPO year {year}"
                session.commit()
                logger.warning(f"No stocks found for year {year}")
                return self._build_summary(job, [], months)

            # Step 2: Scan each stock in parallel
            results = self._scan_stocks_parallel(stocks, year, months)

            # Step 3: Save results to database — one by one, skip failures
            for result in results:
                try:
                    # Convert listing_date to native python datetime for SQLite
                    listing_date_raw = result.get("listing_date")
                    listing_date_py = None
                    if listing_date_raw is not None:
                        try:
                            if isinstance(listing_date_raw, str):
                                listing_date_py = pd.to_datetime(listing_date_raw).to_pydatetime()
                            elif hasattr(listing_date_raw, "to_pydatetime"):
                                listing_date_py = listing_date_raw.to_pydatetime()
                            else:
                                listing_date_py = listing_date_raw
                        except Exception:
                            pass

                    scan_result = ScanResult(
                        scan_id=scan_id,
                        symbol=result.get("symbol", "UNKNOWN"),
                        company_name=result.get("company_name", "Unknown"),
                        ipo_year=year,
                        ipo_first_month_high=safe_round(result.get("ipo_first_month_high")),
                        breakout_month=result.get("breakout_month"),
                        breakout_close=safe_round(result.get("breakout_close")),
                        previous_month_close=safe_round(result.get("previous_month_close")),
                        current_price=safe_round(result.get("current_price")),
                        pct_above_ipo_high=safe_round(result.get("pct_above_ipo_high")),
                        listing_date=listing_date_py,
                        qualified=bool(result.get("qualified", False)),
                    )
                    session.add(scan_result)
                    session.flush()  # Flush each record to catch errors immediately

                except Exception as e:
                    session.rollback()  # Rollback the failed single insert
                    logger.warning(
                        f"Failed to save result for {result.get('symbol', '?')}: {e}"
                    )
                    # Re-fetch the job after rollback
                    job = session.query(ScanJob).filter_by(id=scan_id).first()
                    continue

            # Step 4: Update job status
            checked = [r for r in results if not r.get("skip_reason")]
            job.status = "completed"
            job.scanned_stocks = len(checked)
            job.qualified_stocks = sum(1 for r in checked if r.get("qualified"))
            job.completed_at = datetime.now(UTC)
            session.commit()

            logger.info(
                f"Scan completed for year {year}: {len(results)} listed, "
                f"{job.scanned_stocks} checked, {job.qualified_stocks} qualified"
            )

            return self._build_summary(job, results, months)

        except Exception as e:
            # Top-level catch — always rollback first, then try to mark job as failed
            try:
                session.rollback()
            except Exception:
                pass  # If rollback fails, nothing more we can do

            logger.error(f"Scan failed for job {scan_id}: {e}", exc_info=True)

            try:
                job = session.query(ScanJob).filter_by(id=scan_id).first()
                if job:
                    job.status = "failed"
                    job.error_message = str(e)[:500]
                    job.completed_at = datetime.now(UTC)
                    session.commit()
            except Exception as db_err:
                logger.error(f"Failed to update job status after error: {db_err}")

            raise
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _scan_stocks_parallel(self, stocks: List[Dict], year: int, months: BreakoutMonths) -> List[Dict]:
        """
        Scan multiple stocks in parallel using ThreadPoolExecutor.

        Args:
            stocks: List of stock dicts with symbol, company_name, listing_date
            year: IPO year
            months: Breakout and previous month (see breakout_months)

        Returns:
            List of result dicts for each stock
        """
        results = []
        max_workers = min(self.settings.MAX_WORKERS, len(stocks))

        logger.info(f"Scanning {len(stocks)} stocks with {max_workers} workers")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_stock = {
                executor.submit(self._scan_single_stock, stock, year, months): stock
                for stock in stocks
            }

            for future in as_completed(future_to_stock):
                stock = future_to_stock[future]
                try:
                    result = future.result(timeout=60)  # 60s timeout per stock
                    results.append(result)
                    status = "✓ QUALIFIED" if result.get("qualified") else "✗ Not qualified"
                    logger.debug(f"  {stock['symbol']}: {status}")
                except Exception as e:
                    logger.error(f"Error scanning {stock.get('symbol', '?')}: {e}")
                    results.append({
                        "symbol": stock.get("symbol", "UNKNOWN"),
                        "company_name": stock.get("company_name", "Unknown"),
                        "qualified": False,
                        "error": str(e)[:200],
                        "skip_reason": SKIP_NO_DATA,
                    })

        return results

    def _scan_single_stock(self, stock: Dict, year: int, months: BreakoutMonths) -> Dict:
        """
        Scan a single stock for a fresh IPO breakout:
          1. the previous month closed below the IPO first-month high, and
          2. the breakout month (live price while it runs) is at or above it.
        Fully protected against bad data from yfinance.

        Args:
            stock: Dict with symbol, company_name, listing_date
            year: IPO year (0 = all years)
            months: Breakout and previous month (see breakout_months)
         Returns:
            Result dict with all screening data. "skip_reason" is set (see SKIP_*)
            when the stock could not be compared against its IPO-month high.
        """
        breakout_month, prev_month, live = months
        symbol = stock.get("symbol", "UNKNOWN")
        yf_symbol = f"{symbol}.NS"

        result = {
            "symbol": symbol,
            "company_name": stock.get("company_name", "Unknown"),
            "listing_date": stock.get("listing_date"),
            "ipo_year": year,
            "qualified": False,
            "ipo_first_month_high": None,
            "breakout_month": None,
            "breakout_close": None,
            "previous_month_close": None,
            "current_price": None,
            "pct_above_ipo_high": None,
            "skip_reason": None,
        }

        # Rate limiting with jitter to avoid Yahoo Finance rate limiting
        delay = self.settings.API_CALL_DELAY + random.uniform(0.2, 1.0)
        time.sleep(delay)

        try:
            # Fetch monthly OHLC data with retries
            monthly_data = self._fetch_monthly_data(yf_symbol)

            if monthly_data is None or monthly_data.empty:
                logger.warning(f"No monthly data for {symbol}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            # Handle MultiIndex columns from yfinance
            if isinstance(monthly_data.columns, pd.MultiIndex):
                monthly_data.columns = monthly_data.columns.get_level_values(0)

            # Ensure we have the required columns
            required_cols = {"High", "Close"}
            if not required_cols.issubset(set(monthly_data.columns)):
                logger.warning(f"Missing required columns for {symbol}: {monthly_data.columns.tolist()}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            # Drop rows where High or Close is NaN
            monthly_data = monthly_data.dropna(subset=["High", "Close"])

            if monthly_data.empty:
                logger.warning(f"No valid monthly candles for {symbol}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            # The year the stock must have IPO'd in: its NSE listing year
            # (an ALL scan holds every stock to its own listing year).
            listing = self._to_timestamp(stock.get("listing_date"))
            ipo_year = listing.year if listing is not None else (year or None)

            # Strict IPO year check: reject stocks that traded before their IPO year.
            # BSE history counts too, because old BSE companies show up in the NSE
            # list with a recent "listing date" once they start trading on NSE.
            if ipo_year is None or monthly_data.index[0].year >= ipo_year:
                bse_earlier = self._fetch_bse_months_before(symbol, monthly_data.index[0])
                if bse_earlier is not None:
                    monthly_data = pd.concat([bse_earlier, monthly_data])

            first_month = monthly_data.index[0]
            if ipo_year is not None and first_month.year < ipo_year:
                logger.info(f"Skipping {symbol}: trading since {first_month:%Y-%m}, before IPO year {ipo_year}")
                result["error"] = f"Older listing ({first_month:%Y-%m})"
                result["skip_reason"] = SKIP_OLDER_LISTING
                return result

            # The first candle must be the listing month, otherwise its HIGH is not the IPO-month high
            if listing is not None and (first_month.year, first_month.month) > (listing.year, listing.month):
                logger.info(
                    f"Skipping {symbol}: no price data for listing month {listing:%Y-%m} "
                    f"(data starts {first_month:%Y-%m})"
                )
                result["error"] = f"No data for listing month {listing:%Y-%m}"
                result["skip_reason"] = SKIP_NO_LISTING_MONTH
                return result

            # Step 1: Get the first listed month's HIGH, to the paisa like NSE quotes.
            # Prices are rounded to the paisa throughout so Yahoo's float noise
            # (304.6499938...) cannot decide a stock sitting exactly at its IPO high.
            first_month_high = safe_round(monthly_data["High"].iloc[0])
            if first_month_high is None or first_month_high <= 0:
                logger.warning(f"Invalid first month high for {symbol}: {monthly_data['High'].iloc[0]}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            result["ipo_first_month_high"] = first_month_high

            # Update listing date from data if not available
            if result["listing_date"] is None:
                result["listing_date"] = monthly_data.index[0]

            # The previous month has to be the IPO month or later
            if prev_month < (first_month.year, first_month.month):
                result["skip_reason"] = SKIP_TOO_RECENT
                return result

            closes = {(idx.year, idx.month): row["Close"] for idx, row in monthly_data.iterrows()}

            # Condition 1: the previous month closed below the IPO first-month high
            prev_close = safe_round(closes.get(prev_month))
            if prev_close is None or prev_close <= 0:
                logger.warning(f"No close for {prev_month[0]}-{prev_month[1]:02d} for {symbol}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            result["previous_month_close"] = prev_close
            if prev_close >= first_month_high:
                return result  # Already at/above its IPO high last month: not a fresh breakout

            # Condition 2: the breakout month is at or above the IPO first-month high
            current_price = None
            if live:
                # Live price via fast_info to avoid end-of-month / start-of-month yf bugs
                try:
                    current_price = safe_float(yf.Ticker(yf_symbol).fast_info.get("lastPrice"))
                except Exception as e:
                    logger.warning(f"fast_info failed for {symbol}: {e}")
            if current_price is None:
                current_price = closes.get(breakout_month)

            current_price = safe_round(current_price)
            if current_price is None or current_price <= 0:
                logger.warning(f"No price for {breakout_month[0]}-{breakout_month[1]:02d} for {symbol}")
                result["skip_reason"] = SKIP_NO_DATA
                return result

            result["current_price"] = current_price
            result["pct_above_ipo_high"] = safe_round(
                ((current_price - first_month_high) / first_month_high) * 100
            )
            if current_price >= first_month_high:
                result["qualified"] = True
                result["breakout_month"] = f"{breakout_month[0]}-{breakout_month[1]:02d}"
                result["breakout_close"] = current_price

            return result

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            result["error"] = str(e)[:200]
            result["qualified"] = False
            result["skip_reason"] = SKIP_NO_DATA
            return result

    @staticmethod
    def _to_timestamp(value) -> Optional[pd.Timestamp]:
        """Parse a listing date (str / datetime / Timestamp) to a Timestamp; None if missing or invalid."""
        if value is None:
            return None
        try:
            ts = pd.Timestamp(value)
        except (ValueError, TypeError):
            return None
        return None if pd.isna(ts) else ts

    def _fetch_bse_months_before(self, symbol: str, before: pd.Timestamp) -> Optional[pd.DataFrame]:
        """
        BSE monthly candles strictly before `before` (the first NSE candle), or None.

        A result means the company was already trading on BSE before its NSE data
        starts, i.e. its NSE listing was not its IPO.
        """
        data = self._fetch_monthly_data(f"{symbol}.BO", retry_on_empty=False)
        if data is None or not {"High", "Close"}.issubset(data.columns):
            return None
        earlier = data[data.index < before].dropna(subset=["High", "Close"])
        return earlier if not earlier.empty else None

    def _fetch_monthly_data(self, yf_symbol: str, retry_on_empty: bool = True) -> Optional[pd.DataFrame]:
        """
        Fetch monthly OHLC data from yfinance with retry logic.

        Prices are actual traded prices: adjusted for splits/bonuses but NOT for
        dividends. yfinance's default (auto_adjust=True) also scales old prices
        down by every dividend paid since, which understates the IPO-month high
        against today's real price and inflates "% above IPO high".

        Args:
            yf_symbol: Yahoo Finance symbol (e.g., 'RELIANCE.NS')
            retry_on_empty: Retry when Yahoo returns no rows. False when an empty
                result is expected (e.g. a stock that is not listed on BSE).

        Returns:
            DataFrame with monthly OHLC data or None
        """
        for attempt in range(1, self.settings.MAX_RETRIES + 1):
            try:
                ticker = yf.Ticker(yf_symbol)
                data = ticker.history(period="max", interval="1mo", auto_adjust=False)

                if data is not None and not data.empty:
                    # Remove rows with all NaN values
                    data = data.dropna(how="all")
                    if not data.empty:
                        return data

                if not retry_on_empty:
                    return None
                logger.warning(f"Empty data for {yf_symbol} on attempt {attempt}")

            except Exception as e:
                logger.warning(
                    f"yfinance fetch attempt {attempt}/{self.settings.MAX_RETRIES} "
                    f"for {yf_symbol} failed: {e}"
                )

            if attempt < self.settings.MAX_RETRIES:
                # Exponential backoff with jitter to avoid rate limits
                backoff = (2 ** attempt) + random.uniform(0.5, 2.0)
                time.sleep(backoff)

        return None

    def _build_summary(self, job: ScanJob, results: List[Dict], months: BreakoutMonths) -> Dict:
        """Build a summary dict from scan results for the given breakout months."""
        skipped: Dict[str, int] = {}
        for r in results:
            if r.get("skip_reason"):
                skipped[r["skip_reason"]] = skipped.get(r["skip_reason"], 0) + 1

        checked = [r for r in results if not r.get("skip_reason")]

        # Sort qualified results by % above IPO high (descending)
        qualified = sorted(
            (r for r in checked if r.get("qualified")),
            key=lambda x: safe_float(x.get("pct_above_ipo_high")) or 0,
            reverse=True,
        )

        qualification_pct = 0.0
        if checked:
            qualification_pct = round((len(qualified) / len(checked)) * 100, 2)

        breakout_month, prev_month, live = months
        return {
            "scan_id": job.id,
            "year": job.year,
            "status": job.status,
            "breakout_month": breakout_month,
            "previous_month": prev_month,
            "live": live,
            "total_listed": job.total_stocks or 0,
            "total_scanned": len(checked),
            "qualified_count": len(qualified),
            "qualification_pct": qualification_pct,
            "skipped": skipped,
            "no_listing_month_symbols": sorted(
                r["symbol"] for r in results if r.get("skip_reason") == SKIP_NO_LISTING_MONTH
            ),
            "qualified_list": qualified,
        }

    def get_scan_status(self, scan_id: str) -> Optional[Dict]:
        """Get the current status of a scan job."""
        SessionLocal = get_session_factory()
        session = SessionLocal()

        try:
            job = session.query(ScanJob).filter_by(id=scan_id).first()
            if not job:
                return None

            result = {
                "scan_id": job.id,
                "year": job.year,
                "status": job.status,
                "total_stocks": job.total_stocks,
                "scanned_stocks": job.scanned_stocks,
                "qualified_stocks": job.qualified_stocks,
                "error_message": job.error_message,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            }

            # Include qualified results if completed
            if job.status == "completed":
                qualified = (
                    session.query(ScanResult)
                    .filter_by(scan_id=scan_id, qualified=True)
                    .order_by(ScanResult.pct_above_ipo_high.desc())
                    .all()
                )
                result["qualified_results"] = [
                    {
                        "symbol": r.symbol,
                        "company_name": r.company_name,
                        "ipo_first_month_high": r.ipo_first_month_high,
                        "breakout_month": r.breakout_month,
                        "breakout_close": r.breakout_close,
                        "previous_month_close": r.previous_month_close,
                        "current_price": r.current_price,
                        "pct_above_ipo_high": r.pct_above_ipo_high,
                    }
                    for r in qualified
                ]

            return result
        except Exception as e:
            logger.error(f"Failed to get scan status: {e}")
            return None
        finally:
            try:
                session.close()
            except Exception:
                pass
