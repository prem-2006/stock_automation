"""
WhatsApp Messaging Service via Twilio.

Handles sending text messages and media attachments to WhatsApp users.
Includes request signature validation for webhook security.
"""

import hashlib
import hmac
import os
from typing import Optional
from urllib.parse import urlencode

from twilio.rest import Client
from twilio.request_validator import RequestValidator

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("whatsapp_service")


class WhatsAppService:
    """Service for sending WhatsApp messages via Twilio."""

    def __init__(self):
        self.settings = get_settings()
        self._client = None
        self._validator = None

    @property
    def client(self) -> Client:
        """Lazy-initialize Twilio client."""
        if self._client is None:
            if not self.settings.TWILIO_ACCOUNT_SID or not self.settings.TWILIO_AUTH_TOKEN:
                logger.warning("Twilio credentials not configured — messages will be logged only")
                return None
            self._client = Client(
                self.settings.TWILIO_ACCOUNT_SID,
                self.settings.TWILIO_AUTH_TOKEN,
            )
        return self._client

    @property
    def validator(self) -> RequestValidator:
        """Lazy-initialize Twilio request validator."""
        if self._validator is None:
            self._validator = RequestValidator(self.settings.TWILIO_AUTH_TOKEN)
        return self._validator

    def send_message(self, to: str, body: str) -> Optional[str]:
        """
        Send a text message via WhatsApp.

        Args:
            to: Recipient WhatsApp number (format: whatsapp:+1234567890)
            body: Message text

        Returns:
            Message SID if successful, None otherwise
        """
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        if self.client is None:
            logger.info(f"[DRY RUN] WhatsApp to {to}: {body}")
            return "dry_run_sid"

        try:
            message = self.client.messages.create(
                from_=self.settings.TWILIO_WHATSAPP_NUMBER,
                to=to,
                body=body,
            )
            logger.info(f"WhatsApp message sent to {to} (SID: {message.sid})")
            return message.sid

        except Exception as e:
            logger.error(f"Failed to send WhatsApp message to {to}: {e}")
            return None

    def send_message_with_attachment(
        self, to: str, body: str, media_url: str
    ) -> Optional[str]:
        """
        Send a WhatsApp message with a media attachment.

        Args:
            to: Recipient WhatsApp number
            body: Message text (caption)
            media_url: Public URL of the media file to attach

        Returns:
            Message SID if successful, None otherwise
        """
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        if self.client is None:
            logger.info(f"[DRY RUN] WhatsApp + attachment to {to}: {body} | Media: {media_url}")
            return "dry_run_sid"

        try:
            message = self.client.messages.create(
                from_=self.settings.TWILIO_WHATSAPP_NUMBER,
                to=to,
                body=body,
                media_url=[media_url],
            )
            logger.info(
                f"WhatsApp message with attachment sent to {to} (SID: {message.sid})"
            )
            return message.sid

        except Exception as e:
            logger.error(f"Failed to send WhatsApp attachment to {to}: {e}")
            # Fallback: send text-only message
            return self.send_message(to, body + f"\n\n📎 Report: {media_url}")

    def validate_request(self, url: str, params: dict, signature: str) -> bool:
        """
        Validate an incoming Twilio webhook request signature.

        Args:
            url: The full webhook URL
            params: Request parameters (form data)
            signature: X-Twilio-Signature header value

        Returns:
            True if the request is valid
        """
        if not self.settings.TWILIO_AUTH_TOKEN:
            logger.warning("Skipping Twilio signature validation (no auth token)")
            return True

        try:
            return self.validator.validate(url, params, signature)
        except Exception as e:
            logger.error(f"Signature validation error: {e}")
            return False

    def format_scan_summary(self, summary: dict) -> str:
        """
        Format a scan summary as a WhatsApp message.

        Args:
            summary: Scan summary dict

        Returns:
            Formatted message string
        """
        top_stocks = summary.get("top_10", [])
        top_list = ""
        for i, stock in enumerate(top_stocks[:10], 1):
            symbol = stock.get("symbol", "N/A")
            pct = stock.get("pct_above_ipo_high", 0) or 0
            top_list += f"{i}. {symbol} ({pct:+.1f}%)\n"

        message = (
            f"📊 *IPO Breakout Scan Completed*\n\n"
            f"📅 Year: *{summary.get('year')}*\n"
            f"🔍 Stocks Scanned: *{summary.get('total_scanned', 0)}*\n"
            f"✅ Qualified Stocks: *{summary.get('qualified_count', 0)}*\n"
            f"📈 Qualification Rate: *{summary.get('qualification_pct', 0):.1f}%*\n\n"
        )

        if top_list:
            message += f"🏆 *Top Results:*\n{top_list}\n"

        message += "📎 Excel report attached below."

        return message
