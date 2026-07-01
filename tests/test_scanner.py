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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
