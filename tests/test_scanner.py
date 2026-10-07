"""
Tests for the IPO Breakout Scanner Service.

Uses mock yfinance data for deterministic, offline testing.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point at the test database before any app module reads settings
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.db")

from app.services.scanner_service import (
    IST,
    ScannerService,
    SKIP_NO_LISTING_MONTH,
    SKIP_OLDER_LISTING,
    SKIP_TOO_RECENT,
    breakout_months,
)


def _create_mock_monthly_data(
    first_month_high: float = 100.0,
    breakout_close: float = 120.0,
    current_price: float = None,
    months: int = 12,
    has_breakout: bool = True,
):
    """Create mock monthly OHLC data for testing."""
    dates = pd.date_range(start="2020-01-01", periods=months, freq="MS")

    data = {
        "Open": [first_month_high * 0.9] * months,
        "High": [first_month_high] + [first_month_high * 0.8] * (months - 1),
        "Low": [first_month_high * 0.7] * months,
        "Close": [first_month_high * 0.85] * months,
        "Volume": [1000000] * months,
    }

    if has_breakout and months > 3:
        # Set breakout on the previous month
        data["Close"][-2] = breakout_close
        # Set current price
        data["Close"][-1] = current_price if current_price is not None else breakout_close * 1.1

    df = pd.DataFrame(data, index=dates)
    return df


class TestScannerLogic:
    """Test the core breakout detection logic."""

    def test_breakout_detection_qualifies(self):
        """Test that a stock qualifies when previous month close is below first month high."""
        data = _create_mock_monthly_data(
            first_month_high=100.0,
            breakout_close=80.0,
            current_price=110.0,
            months=12,
            has_breakout=True,
        )

        first_month_high = float(data["High"].iloc[0])
        prev_close = float(data["Close"].iloc[-2])
        assert first_month_high == 100.0

        # Check breakout condition
        current_price = float(data["Close"].iloc[-1])
        qualified = prev_close < first_month_high and current_price >= first_month_high
        
        assert qualified is True

    def test_no_breakout(self):
        """Test that a stock does not qualify when previous month close exceeds first month high."""
        data = _create_mock_monthly_data(
            first_month_high=100.0,
            breakout_close=120.0,
            months=12,
            has_breakout=True,
        )

        first_month_high = float(data["High"].iloc[0])
        prev_close = float(data["Close"].iloc[-2])

        current_price = float(data["Close"].iloc[-1])
        qualified = prev_close < first_month_high and current_price >= first_month_high

        assert qualified is False

    def test_percentage_calculation(self):
        """Test the percentage above IPO high calculation."""
        ipo_high = 100.0
        current_price = 250.0

        pct_above = ((current_price - ipo_high) / ipo_high) * 100
        assert pct_above == 150.0

    def test_insufficient_data(self):
        """Test handling of stocks with insufficient data."""
        dates = pd.date_range(start="2020-01-01", periods=1, freq="MS")
        data = pd.DataFrame(
            {
                "Open": [90],
                "High": [100],
                "Low": [80],
                "Close": [95],
                "Volume": [1000000],
            },
            index=dates,
        )

        # Cannot check breakout with only 1 month of data
        assert len(data) < 2

    def test_insufficient_data(self):
        """Test handling of stocks with insufficient data."""
        dates = pd.date_range(start="2020-01-01", periods=1, freq="MS")
        data = pd.DataFrame(
            {
                "Open": [90],
                "High": [100],
                "Low": [80],
                "Close": [95],
                "Volume": [1000000],
            },
            index=dates,
        )

        # Cannot check breakout with only 1 month of data
        assert len(data) < 2


class TestNSEDataProcessing:
    """Test NSE data parsing and filtering."""

    def test_year_filtering(self):
        """Test filtering stocks by IPO year."""
        data = pd.DataFrame(
            {
                "SYMBOL": ["AAA", "BBB", "CCC", "DDD"],
                "NAME OF COMPANY": ["Company A", "Company B", "Company C", "Company D"],
                "DATE OF LISTING": pd.to_datetime(
                    ["2018-03-15", "2018-07-20", "2019-01-10", "2020-05-05"]
                ),
                "IPO_YEAR": [2018, 2018, 2019, 2020],
                "SERIES": ["EQ", "EQ", "EQ", "EQ"],
            }
        )

        filtered = data[data["IPO_YEAR"] == 2018]
        assert len(filtered) == 2
        assert list(filtered["SYMBOL"]) == ["AAA", "BBB"]

    def test_no_series_filtering(self):
        """Test that all series stocks are included (SME, BE, EQ)."""
        data = pd.DataFrame(
            {
                "SYMBOL": ["AAA", "BBB", "CCC"],
                "SERIES": ["EQ", "BE", "SM"],
                "IPO_YEAR": [2020, 2020, 2020],
            }
        )

        filtered = data[data["IPO_YEAR"] == 2020]
        assert len(filtered) == 3


def _candles(start: str, highs, closes):
    """Monthly candles indexed like yfinance (month start, IST)."""
    idx = pd.date_range(start=start, periods=len(highs), freq="MS", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": closes, "High": highs, "Low": closes, "Close": closes}, index=idx)


@pytest.fixture
def scanner(monkeypatch):
    monkeypatch.setattr("app.services.scanner_service.time.sleep", lambda s: None)
    return ScannerService()


# Breakout in October 2026 at the live price, with September 2026 as the previous month
LIVE_OCT = ((2026, 10), (2026, 9), True)


def _scan(scanner, monkeypatch, nse, bse=None, listing="2024-10-22", year=2024,
          months=((2024, 12), (2024, 11), False), live_price=None):
    """Run _scan_single_stock for symbol ABC against canned NSE/BSE candles and a live price."""
    frames = {"ABC.NS": nse, "ABC.BO": bse}
    monkeypatch.setattr(scanner, "_fetch_monthly_data", lambda sym, retry_on_empty=True: frames.get(sym))
    ticker = MagicMock()
    ticker.fast_info.get.return_value = live_price
    monkeypatch.setattr("app.services.scanner_service.yf.Ticker", lambda sym: ticker)
    stock = {"symbol": "ABC", "company_name": "ABC Ltd", "listing_date": listing}
    return scanner._scan_single_stock(stock, year, months)


def _listed_march_2026(sep_close):
    """IPO in March 2026 with a first-month high of 100; candles up to the running October."""
    highs = [100.0, 95.0, 95.0, 95.0, 95.0, 99.0, 108.0, 106.0]
    closes = [90.0, 80.0, 85.0, 88.0, 92.0, 95.0, sep_close, 97.0]
    return _candles("2026-03-01", highs, closes)


class TestScanSingleStock:
    """Test the real screening code path with mocked market data."""

    def test_fetches_actual_prices_not_dividend_adjusted(self, scanner, monkeypatch):
        ticker = MagicMock()
        ticker.history.return_value = _candles("2024-10-01", [100.0, 90.0], [95.0, 85.0])
        monkeypatch.setattr("app.services.scanner_service.yf.Ticker", lambda sym: ticker)

        scanner._fetch_monthly_data("ABC.NS")

        assert ticker.history.call_args.kwargs["auto_adjust"] is False

    def test_fresh_breakout_qualifies(self, scanner, monkeypatch):
        # September closed below the March IPO high and October's live price is above it
        nse = _listed_march_2026(sep_close=95.0)
        r = _scan(scanner, monkeypatch, nse, listing="2026-03-12", year=2026, months=LIVE_OCT, live_price=104.0)

        assert r["skip_reason"] is None
        assert r["ipo_first_month_high"] == 100.0
        assert r["previous_month_close"] == 95.0
        assert r["current_price"] == 104.0
        assert r["qualified"] is True
        assert r["pct_above_ipo_high"] == 4.0
        assert r["breakout_month"] == "2026-10"

    def test_already_above_last_month_is_not_a_fresh_breakout(self, scanner, monkeypatch):
        nse = _listed_march_2026(sep_close=105.0)
        r = _scan(scanner, monkeypatch, nse, listing="2026-03-12", year=2026, months=LIVE_OCT, live_price=110.0)

        assert r["skip_reason"] is None
        assert r["qualified"] is False
        assert r["current_price"] is None  # live price not even fetched

    def test_still_below_ipo_high_is_not_qualified(self, scanner, monkeypatch):
        nse = _listed_march_2026(sep_close=95.0)
        r = _scan(scanner, monkeypatch, nse, listing="2026-03-12", year=2026, months=LIVE_OCT, live_price=99.99)

        assert r["skip_reason"] is None
        assert r["qualified"] is False
        assert r["pct_above_ipo_high"] == -0.01

    def test_float_noise_does_not_hide_a_stock_at_its_ipo_high(self, scanner, monkeypatch):
        nse = _candles("2024-10-01", [394.95000457763672, 390.0], [348.85, 380.0])
        r = _scan(scanner, monkeypatch, nse, months=((2024, 12), (2024, 11), True), live_price=394.9499938964844)

        assert r["qualified"] is True
        assert r["current_price"] == 394.95

    def test_ipo_last_month_can_break_out_this_month(self, scanner, monkeypatch):
        # Listed in September: its own September close is the previous-month close
        nse = _candles("2026-09-01", [581.95, 643.5], [510.35, 618.3])
        r = _scan(scanner, monkeypatch, nse, listing="2026-09-17", year=2026, months=LIVE_OCT, live_price=618.0)

        assert r["previous_month_close"] == 510.35
        assert r["qualified"] is True

    def test_listed_this_month_is_too_recent(self, scanner, monkeypatch):
        nse = _candles("2026-10-01", [300.0], [290.0])
        r = _scan(scanner, monkeypatch, nse, listing="2026-10-06", year=2026, months=LIVE_OCT, live_price=310.0)

        assert r["skip_reason"] == SKIP_TOO_RECENT
        assert r["qualified"] is False

    def test_old_bse_company_newly_listed_on_nse_is_not_an_ipo(self, scanner, monkeypatch):
        nse = _candles("2026-08-01", [500.0, 520.0], [480.0, 510.0])
        bse = _candles("2002-03-01", [10.0] * 5, [9.0] * 5)
        r = _scan(scanner, monkeypatch, nse, bse, listing="2026-04-20", year=2026, months=LIVE_OCT, live_price=530.0)

        assert r["skip_reason"] == SKIP_OLDER_LISTING
        assert r["qualified"] is False

    def test_missing_listing_month_is_not_guessed(self, scanner, monkeypatch):
        # Listed on the last trading day of March; Yahoo has no candle for March
        nse = _candles("2018-04-01", [582.0, 600.0], [560.0, 590.0])
        r = _scan(scanner, monkeypatch, nse, listing="2018-03-28", year=2018, months=((2018, 5), (2018, 4), False))

        assert r["skip_reason"] == SKIP_NO_LISTING_MONTH
        assert r["qualified"] is False

    def test_bse_candle_fills_listing_month_missing_on_nse(self, scanner, monkeypatch):
        nse = _candles("2024-11-01", [210.0, 270.0], [200.0, 260.0])
        bse = _candles("2024-10-01", [250.0, 211.0], [205.0, 201.0])
        r = _scan(scanner, monkeypatch, nse, bse)

        assert r["skip_reason"] is None
        assert r["ipo_first_month_high"] == 250.0
        assert r["qualified"] is True
        assert r["pct_above_ipo_high"] == 4.0

    def test_all_scan_holds_each_stock_to_its_listing_year(self, scanner, monkeypatch):
        nse = _candles("2016-05-01", [50.0, 55.0], [45.0, 52.0])
        bse = _candles("2003-01-01", [5.0] * 3, [4.0] * 3)
        r = _scan(scanner, monkeypatch, nse, bse, listing="2016-05-10", year=0, months=((2016, 7), (2016, 6), False))

        assert r["skip_reason"] == SKIP_OLDER_LISTING

    def test_selected_breakout_month_uses_its_close(self, scanner, monkeypatch):
        # Bot 2: November closed below the October IPO high, December closed above it
        nse = _candles("2024-10-01", [100.0, 95.0, 120.0, 90.0], [90.0, 92.0, 110.0, 85.0])
        r = _scan(scanner, monkeypatch, nse)

        assert r["previous_month_close"] == 92.0
        assert r["current_price"] == 110.0
        assert r["qualified"] is True
        assert r["pct_above_ipo_high"] == 10.0

    def test_selected_breakout_month_before_listing_is_too_recent(self, scanner, monkeypatch):
        nse = _candles("2024-10-01", [100.0, 95.0], [100.0, 92.0])
        r = _scan(scanner, monkeypatch, nse, months=((2024, 10), (2024, 9), False))

        assert r["skip_reason"] == SKIP_TOO_RECENT
        assert r["qualified"] is False

    def test_breakout_months(self, monkeypatch):
        monkeypatch.setattr("app.services.scanner_service.now_ist", lambda: datetime(2026, 10, 8, tzinfo=IST))

        assert breakout_months() == ((2026, 10), (2026, 9), True)
        assert breakout_months(8, 2026) == ((2026, 8), (2026, 7), False)
        assert breakout_months(1, 2027) == ((2027, 1), (2026, 12), False)


class TestBuildSummary:
    """Test the counts reported to the user."""

    def test_counts_only_checked_stocks(self, scanner):
        job = MagicMock(id="job-1", year=2024, status="completed", total_stocks=5)
        results = [
            {"symbol": "A", "qualified": True, "pct_above_ipo_high": 5.0, "skip_reason": None},
            {"symbol": "B", "qualified": True, "pct_above_ipo_high": 50.0, "skip_reason": None},
            {"symbol": "C", "qualified": False, "pct_above_ipo_high": -3.0, "skip_reason": None},
            {"symbol": "D", "qualified": False, "skip_reason": SKIP_OLDER_LISTING},
            {"symbol": "HAL", "qualified": False, "skip_reason": SKIP_NO_LISTING_MONTH},
        ]

        summary = scanner._build_summary(job, results, LIVE_OCT)

        assert summary["breakout_month"] == (2026, 10)
        assert summary["previous_month"] == (2026, 9)
        assert summary["live"] is True
        assert summary["total_listed"] == 5
        assert summary["total_scanned"] == 3
        assert summary["qualified_count"] == 2
        assert [r["symbol"] for r in summary["qualified_list"]] == ["B", "A"]
        assert summary["skipped"] == {SKIP_OLDER_LISTING: 1, SKIP_NO_LISTING_MONTH: 1}
        assert summary["no_listing_month_symbols"] == ["HAL"]
        assert summary["qualification_pct"] == 66.67


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
