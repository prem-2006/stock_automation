"""
IPO Breakout Scanner Service.

Core screening engine that:
1. Fetches monthly OHLC data for each stock using yfinance
2. Identifies the first listed month's HIGH
3. Checks for monthly close breakout above IPO HIGH
4. Generates results with parallel processing

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
from datetime import datetime, UTC
from typing import List, Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

from app.config import get_settings
from app.database import get_session_factory
from app.models import ScanJob, ScanResult
from app.services.nse_service import NSEService
from app.services.excel_service import ExcelService
from app.utils.logger import get_logger

logger = get_logger("scanner")


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
        self.excel_service = ExcelService()

    def create_scan_job(self, year: int, phone_number: Optional[str] = None) -> str:
        """
        Create a new scan job record in the database.

        Args:
            year: IPO year to scan
            phone_number: Optional WhatsApp number for results delivery

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

    def run_scan(self, scan_id: str) -> Dict:
        """
        Execute the full scanning pipeline for a given scan job.

        Args:
            scan_id: UUID of the scan job

        Returns:
            Summary dict with results
        """
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
                return self._build_summary(job, [])

            # Step 2: Scan each stock in parallel
            results = self._scan_stocks_parallel(stocks, year)

            # Step 3: Save results to database — one by one, skip failures
            qualified_results = []
            saved_count = 0

            for result in results:
                try:
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
                        listing_date=result.get("listing_date"),
                        qualified=bool(result.get("qualified", False)),
                    )
                    session.add(scan_result)
                    session.flush()  # Flush each record to catch errors immediately
                    saved_count += 1

                    if result.get("qualified"):
                        qualified_results.append(result)

                except Exception as e:
                    session.rollback()  # Rollback the failed single insert
                    logger.warning(
                        f"Failed to save result for {result.get('symbol', '?')}: {e}"
                    )
                    # Re-fetch the job after rollback
                    job = session.query(ScanJob).filter_by(id=scan_id).first()
                    continue

            job.scanned_stocks = saved_count
            session.commit()

            # Step 4: Generate Excel report (protected)
            report_path = None
            try:
                report_path = self.excel_service.generate_report(
                    year=year,
                    scan_id=scan_id,
                    results=results,
                    qualified_results=qualified_results,
                )
            except Exception as e:
                logger.error(f"Excel report generation failed: {e}", exc_info=True)
                # Continue anyway — user still gets text results

            # Step 5: Update job status
            job.status = "completed"
            job.qualified_stocks = len(qualified_results)
            job.report_path = report_path
            job.completed_at = datetime.now(UTC)
            session.commit()

            logger.info(
                f"Scan completed for year {year}: "
                f"{len(results)} scanned, {len(qualified_results)} qualified"
            )

            return self._build_summary(job, qualified_results)

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

    def _scan_stocks_parallel(self, stocks: List[Dict], year: int) -> List[Dict]:
        """
        Scan multiple stocks in parallel using ThreadPoolExecutor.

        Args:
            stocks: List of stock dicts with symbol, company_name, listing_date
            year: IPO year

        Returns:
            List of result dicts for each stock
        """
        results = []
        max_workers = min(self.settings.MAX_WORKERS, len(stocks))

        logger.info(f"Scanning {len(stocks)} stocks with {max_workers} workers")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_stock = {
                executor.submit(self._scan_single_stock, stock, year): stock
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
                    })

        return results

    def _scan_single_stock(self, stock: Dict, year: int) -> Dict:
        """
        Scan a single stock for the IPO breakout condition.
        Fully protected against bad data from yfinance.

        Args:
            stock: Dict with symbol, company_name, listing_date
            year: IPO year

        Returns:
            Result dict with all screening data
        """
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
        }

        # Rate limiting with jitter to avoid Yahoo Finance rate limiting
        delay = self.settings.API_CALL_DELAY + random.uniform(0.2, 1.0)
        time.sleep(delay)

        try:
            # Fetch monthly OHLC data with retries
            monthly_data = self._fetch_monthly_data(yf_symbol)

            if monthly_data is None or monthly_data.empty:
                logger.warning(f"No monthly data for {symbol}")
                return result

            # Handle MultiIndex columns from yfinance
            if isinstance(monthly_data.columns, pd.MultiIndex):
                monthly_data.columns = monthly_data.columns.get_level_values(0)

            # Ensure we have the required columns
            required_cols = {"High", "Close"}
            if not required_cols.issubset(set(monthly_data.columns)):
                logger.warning(f"Missing required columns for {symbol}: {monthly_data.columns.tolist()}")
                return result

            # Drop rows where High or Close is NaN
            monthly_data = monthly_data.dropna(subset=["High", "Close"])

            if len(monthly_data) < 2:
                logger.warning(f"Insufficient data for {symbol} (only {len(monthly_data)} months)")
                return result

            # Strict IPO Year Check: Reject cross-listed stocks that traded before the requested year
            first_yf_date = monthly_data.index[0]
            first_yf_year = first_yf_date.year

            if first_yf_year < year:
                logger.info(f"Skipping {symbol}: True IPO was in {first_yf_year}, before requested year {year}")
                result["error"] = f"Older IPO ({first_yf_year})"
                return result

            # Step 1: Get the first listed month's HIGH
            first_month_high = safe_float(monthly_data["High"].iloc[0])
            if first_month_high is None or first_month_high <= 0:
                logger.warning(f"Invalid first month high for {symbol}: {monthly_data['High'].iloc[0]}")
                return result

            result["ipo_first_month_high"] = safe_round(first_month_high)

            # Get current price and previous month close
            current_price = safe_float(monthly_data["Close"].iloc[-1])
            if current_price is not None and current_price > 0:
                result["current_price"] = safe_round(current_price)
                
            if len(monthly_data) >= 2:
                prev_close = safe_float(monthly_data["Close"].iloc[-2])
                if prev_close is not None and prev_close > 0:
                    result["previous_month_close"] = safe_round(prev_close)

            # Update listing date from data if not available
            if result["listing_date"] is None:
                result["listing_date"] = monthly_data.index[0]

            # Step 2: Check if previous month close > first month high
            prev_close = result.get("previous_month_close")
            if prev_close is not None and prev_close > first_month_high:
                result["qualified"] = True
                try:
                    result["breakout_month"] = monthly_data.index[-2].strftime("%Y-%m")
                except Exception:
                    result["breakout_month"] = str(monthly_data.index[-2])[:7]
                result["breakout_close"] = prev_close

                if current_price is not None and first_month_high > 0:
                    result["pct_above_ipo_high"] = safe_round(
                        ((current_price - first_month_high) / first_month_high) * 100
                    )

            return result

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            result["error"] = str(e)[:200]
            return result

    def _fetch_monthly_data(self, yf_symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch monthly OHLC data from yfinance with retry logic.

        Args:
            yf_symbol: Yahoo Finance symbol (e.g., 'RELIANCE.NS')

        Returns:
            DataFrame with monthly OHLC data or None
        """
        for attempt in range(1, self.settings.MAX_RETRIES + 1):
            try:
                ticker = yf.Ticker(yf_symbol)
                data = ticker.history(period="max", interval="1mo")

                if data is not None and not data.empty:
                    # Remove rows with all NaN values
                    data = data.dropna(how="all")
                    if not data.empty:
                        return data
                else:
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

    def _build_summary(self, job: ScanJob, qualified_results: List[Dict]) -> Dict:
        """Build a summary dict from scan results."""
        # Sort qualified results by % above IPO high (descending)
        sorted_qualified = sorted(
            qualified_results,
            key=lambda x: safe_float(x.get("pct_above_ipo_high")) or 0,
            reverse=True,
        )

        top_10 = sorted_qualified[:10]

        qualification_pct = 0.0
        total = job.total_stocks or 0
        if total > 0:
            qualification_pct = round(
                (len(qualified_results) / total) * 100, 2
            )

        return {
            "scan_id": job.id,
            "year": job.year,
            "status": job.status,
            "total_scanned": total,
            "qualified_count": len(qualified_results),
            "qualification_pct": qualification_pct,
            "report_path": job.report_path,
            "top_10": top_10,
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
                "report_path": job.report_path,
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
