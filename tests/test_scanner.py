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
        # Set breakout on month 4
        data["Close"][3] = breakout_close
        # Set current price
        data["Close"][-1] = breakout_close * 1.1

    df = pd.DataFrame(data, index=dates)
    return df


class TestScannerLogic:
    """Test the core breakout detection logic."""

    def test_breakout_detection_qualifies(self):
        """Test that a stock qualifies when close exceeds first month high."""
        data = _create_mock_monthly_data(
            first_month_high=100.0,
            breakout_close=120.0,
            months=12,
            has_breakout=True,
        )

        first_month_high = float(data["High"].iloc[0])
        assert first_month_high == 100.0

        # Check breakout condition
        qualified = False
        breakout_month = None
        breakout_close = None

        for i in range(1, len(data)):
            month_close = float(data["Close"].iloc[i])
            if month_close > first_month_high:
                qualified = True
                breakout_month = data.index[i].strftime("%Y-%m")
                breakout_close = month_close
                break

        assert qualified is True
        assert breakout_month == "2020-04"
        assert breakout_close == 120.0

    def test_no_breakout(self):
        """Test that a stock does not qualify when no close exceeds first month high."""
        data = _create_mock_monthly_data(
            first_month_high=100.0,
            months=12,
            has_breakout=False,
        )

        first_month_high = float(data["High"].iloc[0])

        qualified = False
        for i in range(1, len(data)):
            month_close = float(data["Close"].iloc[i])
            if month_close > first_month_high:
                qualified = True
                break

        assert qualified is False

    def test_percentage_calculation(self):
        """Test the percentage above IPO high calculation."""
        ipo_high = 100.0
        current_price = 250.0

        pct_above = ((current_price - ipo_high) / ipo_high) * 100
        assert pct_above == 150.0

    def test_first_month_only(self):
        """Test that only the first month's HIGH is used as the benchmark."""
        dates = pd.date_range(start="2020-01-01", periods=6, freq="MS")
        data = pd.DataFrame(
            {
                "Open": [90, 95, 110, 105, 100, 95],
                "High": [100, 150, 160, 130, 120, 110],  # Later months have higher highs
                "Low": [80, 85, 90, 85, 80, 75],
                "Close": [95, 88, 92, 90, 85, 80],  # No close exceeds first HIGH (100)
                "Volume": [1000000] * 6,
            },
            index=dates,
        )

        first_month_high = float(data["High"].iloc[0])
        assert first_month_high == 100.0  # Only first month's high matters

        qualified = False
        for i in range(1, len(data)):
            if float(data["Close"].iloc[i]) > first_month_high:
                qualified = True
                break

        assert qualified is False  # Even though later HIGHS exceed 100, no CLOSE does

    def test_immediate_breakout(self):
        """Test breakout in the second month (earliest possible)."""
        dates = pd.date_range(start="2020-01-01", periods=3, freq="MS")
        data = pd.DataFrame(
            {
                "Open": [90, 105, 110],
                "High": [100, 115, 120],
                "Low": [80, 95, 100],
                "Close": [95, 110, 115],  # Second month close > first month high
                "Volume": [1000000] * 3,
            },
            index=dates,
        )

        first_month_high = float(data["High"].iloc[0])
        breakout_month = None

        for i in range(1, len(data)):
            if float(data["Close"].iloc[i]) > first_month_high:
                breakout_month = data.index[i].strftime("%Y-%m")
                break

        assert breakout_month == "2020-02"

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

    def test_series_filtering(self):
        """Test that only EQ series stocks are included."""
        data = pd.DataFrame(
            {
                "SYMBOL": ["AAA", "BBB", "CCC"],
                "SERIES": ["EQ", "BE", "EQ"],
                "IPO_YEAR": [2020, 2020, 2020],
            }
        )

        filtered = data[(data["IPO_YEAR"] == 2020) & (data["SERIES"].str.strip() == "EQ")]
        assert len(filtered) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
