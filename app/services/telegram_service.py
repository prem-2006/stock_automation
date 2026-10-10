"""
Telegram Messaging Service.

Handles sending text messages to Telegram users and formatting scan results.
"""

import calendar
import html
import time
from typing import List, Optional, Tuple

import httpx

from app.config import get_settings
from app.services.scanner_service import (
    SKIP_NO_DATA,
    SKIP_NO_LISTING_MONTH,
    SKIP_OLDER_LISTING,
    SKIP_TOO_RECENT,
)
from app.utils.logger import get_logger

logger = get_logger("telegram_service")

# Telegram message limit is 4096 characters
MAX_MESSAGE_LEN = 4000

# How many symbols to name when listing stocks that have no listing-month data
MAX_NAMED_SYMBOLS = 10


def split_message(text: str, max_len: int = MAX_MESSAGE_LEN) -> List[str]:
    """Split text into chunks on line boundaries so HTML tags are never broken."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(token: str, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> bool:
    """
    Send an HTML message via the Bot API, chunked to fit Telegram's size limit.

    Args:
        token: Bot token
        chat_id: Recipient Telegram chat ID
        text: Message text (HTML formatting)
        reply_markup: Optional inline keyboard, attached to the last chunk

    Returns:
        True if every chunk was sent, False otherwise
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = split_message(text)

    try:
        with httpx.Client() as client:
            for i, chunk in enumerate(chunks):
                payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
                if reply_markup and i == len(chunks) - 1:
                    payload["reply_markup"] = reply_markup

                resp = client.post(url, json=payload, timeout=10.0)
                if resp.status_code == 429:
                    # Long results go out as several messages; wait out Telegram's flood limit
                    try:
                        retry_after = int(resp.json()["parameters"]["retry_after"])
                    except Exception:
                        retry_after = 1
                    time.sleep(min(retry_after, 60))
                    resp = client.post(url, json=payload, timeout=10.0)

                if resp.status_code != 200:
                    # Log Telegram's own reason (e.g. "can't parse entities"), never the URL with the token
                    logger.error(f"Telegram rejected message to {chat_id}: {resp.status_code} {resp.text[:300]}")
                    return False
        return True
    except Exception as e:
        logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
        return False


def _price(value) -> str:
    return f"₹{value:,.2f}" if isinstance(value, (int, float)) else "N/A"


def _month(year_month: Tuple[int, int], short: bool = False) -> str:
    year, month = year_month
    return calendar.month_abbr[month] if short else f"{calendar.month_name[month]} {year}"


def format_scan_summary(summary: dict) -> str:
    """
    Format a scan summary as a Telegram HTML message.

    Args:
        summary: Scan summary dict from ScannerService

    Returns:
        Formatted message string
    """
    year = summary.get("year")
    breakout, previous, live = summary["breakout_month"], summary["previous_month"], summary.get("live")
    lines = [
        "📊 <b>IPO Breakout Scan Complete</b>",
        "",
        f"📅 IPO Year: <b>{'ALL' if year == 0 else year}</b>",
        f"🗓 Breakout Month: <b>{_month(breakout)}</b>" + (" (live price)" if live else ""),
        f"📏 Rule: every monthly close from listing to {_month(previous)} below the IPO first-month high, "
        f"{'current price' if live else _month(breakout) + ' close'} at or above it (first breakout)",
        f"🔍 IPOs checked: <b>{summary.get('total_scanned', 0)}</b>"
        f" (of {summary.get('total_listed', 0)} NSE listings)",
        f"✅ Qualified: <b>{summary.get('qualified_count', 0)}</b>",
    ]

    qualified = summary.get("qualified_list", [])
    if qualified:
        now_label = "Now" if live else f"{_month(breakout, short=True)} close"
        lines += ["", "🏆 <b>Qualified Stocks:</b>"]
        for i, stock in enumerate(qualified, 1):
            pct = stock.get("pct_above_ipo_high") or 0
            lines.append(
                f"{i}. <b>{html.escape(str(stock.get('symbol', 'N/A')))}</b> "
                f"IPO high {_price(stock.get('ipo_first_month_high'))} | "
                f"{_month(previous, short=True)} close {_price(stock.get('previous_month_close'))} | "
                f"{now_label} {_price(stock.get('current_price'))} ({pct:+.2f}%)"
            )

    skipped = summary.get("skipped") or {}
    if skipped:
        labels = {
            SKIP_OLDER_LISTING: "were already trading before their IPO year (not fresh IPOs)",
            SKIP_NO_LISTING_MONTH: "have no price data for their listing month",
            SKIP_TOO_RECENT: (
                f"listed in {_month(breakout)}{'' if live else ' or later'}, "
                f"so there is no {_month(previous)} close to compare"
            ),
            SKIP_NO_DATA: "have no price data on Yahoo Finance",
        }
        lines += ["", "ℹ️ <b>Not checked:</b>"]
        for reason, label in labels.items():
            if not skipped.get(reason):
                continue
            line = f"• {skipped[reason]} {label}"
            symbols = summary.get("no_listing_month_symbols") or []
            if reason == SKIP_NO_LISTING_MONTH and symbols:
                line += ": " + ", ".join(html.escape(s) for s in symbols[:MAX_NAMED_SYMBOLS])
                if len(symbols) > MAX_NAMED_SYMBOLS:
                    line += f" +{len(symbols) - MAX_NAMED_SYMBOLS} more"
            lines.append(line)

    return "\n".join(lines)


class TelegramService:
    """Service for sending Telegram messages (Bot 1)."""

    def __init__(self):
        self.settings = get_settings()

    def send_message(self, chat_id: int, text: str) -> bool:
        """
        Send a text message via Telegram.

        Args:
            chat_id: Recipient Telegram chat ID
            text: Message text (supports HTML formatting)

        Returns:
            True if successful, False otherwise
        """
        if not self.settings.TELEGRAM_BOT_TOKEN:
            logger.info(f"[DRY RUN] Telegram to {chat_id}: {text}")
            return True

        sent = send_telegram_message(self.settings.TELEGRAM_BOT_TOKEN, chat_id, text)
        if sent:
            logger.info(f"Telegram message sent to {chat_id}")
        return sent
