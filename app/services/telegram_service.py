"""
Telegram Messaging Service.

Handles sending text messages and file attachments to Telegram users.
"""

import httpx
from typing import Optional
import os

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("telegram_service")


class TelegramService:
    """Service for sending Telegram messages."""

    def __init__(self):
        self.settings = get_settings()
        self.base_url = f"https://api.telegram.org/bot{self.settings.TELEGRAM_BOT_TOKEN}"

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

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }

        try:
            # We use httpx synchronously here for simplicity, though async is preferred for high-throughput
            with httpx.Client() as client:
                resp = client.post(url, json=payload, timeout=10.0)
                resp.raise_for_status()
                logger.info(f"Telegram message sent to {chat_id}")
                return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
            return False

    def send_document(self, chat_id: int, file_path: str, caption: str = "") -> bool:
        """
        Send a Telegram message with a document attachment.

        Args:
            chat_id: Recipient Telegram chat ID
            file_path: Local path to the file to attach
            caption: Message text (caption)

        Returns:
            True if successful, False otherwise
        """
        if not self.settings.TELEGRAM_BOT_TOKEN:
            logger.info(f"[DRY RUN] Telegram + attachment to {chat_id}: {caption} | File: {file_path}")
            return True

        url = f"{self.base_url}/sendDocument"
        
        try:
            with open(file_path, "rb") as f:
                files = {"document": (os.path.basename(file_path), f)}
                data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
                
                with httpx.Client() as client:
                    resp = client.post(url, data=data, files=files, timeout=60.0)
                    resp.raise_for_status()
                    logger.info(f"Telegram document sent to {chat_id}")
                    return True
        except Exception as e:
            logger.error(f"Failed to send Telegram document to {chat_id}: {e}")
            return False

    def format_scan_summary(self, summary: dict) -> str:
        """
        Format a scan summary as a Telegram HTML message.

        Args:
            summary: Scan summary dict

        Returns:
            Formatted message string
        """
        qualified_stocks = summary.get("qualified_list", [])
        top_list = ""
        for i, stock in enumerate(qualified_stocks, 1):
            symbol = stock.get("symbol", "N/A")
            pct = stock.get("pct_above_ipo_high", 0) or 0
            top_list += f"{i}. <b>{symbol}</b> ({pct:+.1f}%)\n"

        message = (
            f"📊 <b>IPO Breakout Scan Completed</b>\n\n"
            f"📅 Year: <b>{summary.get('year')}</b>\n"
            f"🔍 Stocks Scanned: <b>{summary.get('total_scanned', 0)}</b>\n"
            f"✅ Qualified Stocks: <b>{summary.get('qualified_count', 0)}</b>\n\n"
        )

        if top_list:
            message += f"🏆 <b>Qualified Stocks:</b>\n{top_list}\n"

        message += "📎 Excel report attached below."

        return message
