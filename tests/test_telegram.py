"""
Tests for the Telegram message formatting and sending.
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point at the test database before any app module reads settings
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.db")

from app.services.scanner_service import SKIP_NO_LISTING_MONTH, SKIP_NO_PRICE, SKIP_OLDER_LISTING
from app.services.telegram_service import format_scan_summary, send_telegram_message, split_message


SUMMARY = {
    "year": 2024,
    "total_listed": 144,
    "total_scanned": 131,
    "qualified_count": 2,
    "skipped": {SKIP_OLDER_LISTING: 3, SKIP_NO_LISTING_MONTH: 1},
    "no_listing_month_symbols": ["HAL"],
    "qualified_list": [
        {"symbol": "M&M", "current_price": 3047.8, "ipo_first_month_high": 727.0, "pct_above_ipo_high": 319.23},
        {"symbol": "SWIGGY", "current_price": 456.0, "ipo_first_month_high": 456.0, "pct_above_ipo_high": 0.0},
    ],
}


class TestFormatScanSummary:
    """Test the scan result message."""

    def test_lists_prices_for_each_qualified_stock(self):
        msg = format_scan_summary(SUMMARY)

        assert "(Current price vs IPO first-month high)" in msg
        assert "1. <b>M&amp;M</b> ₹3,047.80 vs ₹727.00 (+319.23%)" in msg
        assert "2. <b>SWIGGY</b> ₹456.00 vs ₹456.00 (+0.00%)" in msg

    def test_reports_checked_count_and_skip_reasons(self):
        msg = format_scan_summary(SUMMARY)

        assert "IPOs checked: <b>131</b> (of 144 NSE listings)" in msg
        assert "• 3 were already trading before their IPO year" in msg
        assert "• 1 have no price data for their listing month: HAL" in msg
        assert "Excel" not in msg

    def test_all_years_with_reference_month(self):
        summary = {**SUMMARY, "year": 0, "skipped": {SKIP_NO_PRICE: 4}}
        msg = format_scan_summary(summary, reference_month="September 2026")

        assert "IPO Year: <b>ALL</b>" in msg
        assert "Reference Month: <b>September 2026</b>" in msg
        assert "(September 2026 close vs IPO first-month high)" in msg
        assert "• 4 have no September 2026 close" in msg

    def test_names_at_most_ten_symbols_without_listing_month_data(self):
        symbols = [f"S{i}" for i in range(13)]
        summary = {**SUMMARY, "skipped": {SKIP_NO_LISTING_MONTH: 13}, "no_listing_month_symbols": symbols}
        msg = format_scan_summary(summary)

        assert "S9 +3 more" in msg
        assert "S10" not in msg


class TestSendTelegramMessage:
    """Test chunking and rate-limit handling."""

    def test_long_message_is_split_on_line_boundaries(self):
        text = "\n".join(f"{i}. <b>SYM{i}</b> ₹1,000.00 vs ₹900.00 (+11.11%)" for i in range(300))

        chunks = split_message(text)

        assert len(chunks) > 1
        assert all(len(c) <= 4000 for c in chunks)
        assert "".join(chunks).rstrip("\n") == text

    def test_retries_after_rate_limit(self):
        limited = MagicMock(status_code=429)
        limited.json.return_value = {"parameters": {"retry_after": 2}}
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = [limited, MagicMock(status_code=200)]

        with patch("app.services.telegram_service.httpx.Client", return_value=client), \
                patch("app.services.telegram_service.time.sleep") as sleep:
            assert send_telegram_message("token", 111, "hi") is True

        sleep.assert_called_once_with(2)
        assert client.post.call_count == 2

    def test_rejected_message_returns_false(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = MagicMock(status_code=400, text="can't parse entities")

        with patch("app.services.telegram_service.httpx.Client", return_value=client):
            assert send_telegram_message("token", 111, "<b>broken") is False
