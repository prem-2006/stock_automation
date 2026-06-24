"""
Telegram Webhook Router.

Handles incoming Telegram messages and manages conversation state.
Implements a state machine for the chat flow:
  idle → awaiting_year → processing → completed
"""

import os
import threading
import traceback
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db, get_session_factory
from app.models import Conversation, ScanJob
from app.services.scanner_service import ScannerService
from app.services.telegram_service import TelegramService
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("webhook")

router = APIRouter(tags=["Telegram"])

# Service instances
scanner_service = ScannerService()
telegram_service = TelegramService()

# Detect Vercel
IS_VERCEL = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None


def _get_or_create_conversation(db: Session, chat_id: str) -> Conversation:
    """Get existing conversation or create a new one. Never crashes."""
    try:
        conv = db.query(Conversation).filter_by(phone_number=chat_id).first()
        if not conv:
            conv = Conversation(
                phone_number=chat_id,
                current_state="idle",
                created_at=datetime.now(UTC),
                last_message_at=datetime.now(UTC),
            )
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv
    except Exception as e:
        logger.error(f"Failed to get/create conversation for {chat_id}: {e}")
        db.rollback()
        # Fallback in-memory object so we don't crash the handler
        return Conversation(phone_number=chat_id, current_state="idle")


def _update_conversation_state(chat_id: str, state: str, scan_id: str = None) -> None:
    """Update conversation state safely."""
    try:
        SessionLocal = get_session_factory()
        with SessionLocal() as db:
            conv = db.query(Conversation).filter_by(phone_number=chat_id).first()
            if conv:
                conv.current_state = state
                if scan_id:
                    conv.current_scan_id = scan_id
                db.commit()
    except Exception as e:
        logger.error(f"Failed to get session for conversation update: {e}")


def _process_scan_and_notify(scan_id: str, chat_id: str) -> None:
    """
    Background worker:
    1. Runs the scan (which saves to DB).
    2. Builds the Excel report.
    3. Sends the Telegram notification + file.
    """
    try:
        logger.info(f"Background scan started for job {scan_id}")

        # Run scan
        summary = scanner_service.run_scan(scan_id)

        # Format summary message
        message = telegram_service.format_scan_summary(summary)

        # Generate Excel report
        try:
            from app.services.excel_service import ExcelService
            excel_service = ExcelService()
            report_path = excel_service.generate_report(scan_id)

            if report_path and os.path.exists(report_path):
                # Send document + caption
                telegram_service.send_document(int(chat_id), report_path, message)
            else:
                raise Exception("Report path is empty or file doesn't exist")

        except Exception as e:
            logger.error(f"Failed to attach report for scan {scan_id}: {e}")
            telegram_service.send_message(int(chat_id), message + f"\n\n⚠️ Failed to generate Excel report: {e}")

    except Exception as notify_err:
        logger.error(f"Failed to send error notification: {notify_err}")

    finally:
        # Reset conversation state
        _update_conversation_state(chat_id, "idle")


@router.post("/webhook/telegram")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Handle incoming Telegram messages via Webhook.
    Returns 200 OK immediately so Telegram doesn't retry.
    """
    try:
        update = await request.json()
        logger.debug(f"Telegram Update: {update}")
        
        # Only process regular messages
        if "message" not in update:
            return JSONResponse({"status": "ok"})
            
        message = update["message"]
        
        if "text" not in message or "chat" not in message:
            return JSONResponse({"status": "ok"})
            
        chat_id = str(message["chat"]["id"])
        text = message["text"].strip().lower()
        
        logger.info(f"Received Telegram from {chat_id}: '{text}'")

        # Get or create conversation
        conv = _get_or_create_conversation(db, chat_id)
        conv.last_message_at = datetime.now(UTC)
        try:
            db.commit()
        except Exception:
            db.rollback()

        # State machine logic
        if text in ("hi", "hello", "hey", "start", "menu", "help", "/start"):
            # Reset to greeting state
            conv.current_state = "awaiting_year"
            conv.current_scan_id = None
            try:
                db.commit()
            except Exception:
                db.rollback()

            telegram_service.send_message(
                int(chat_id),
                "👋 Welcome to the <b>IPO Breakout Scanner</b>!\n\n"
                "To get started, please tell me the IPO year you want to scan.\n\n"
                "📅 <b>Enter IPO Year</b> (Example: 2020)"
            )

        elif conv.current_state == "awaiting_year":
            # Expecting a year
            if not text.isdigit() or len(text) != 4:
                telegram_service.send_message(
                    int(chat_id),
                    "❌ Invalid year format.\n\n"
                    "Please enter a valid 4-digit year (e.g., 2021)."
                )
            else:
                year = int(text)
                current_year = datetime.now().year

                if year < 2000 or year > current_year:
                    telegram_service.send_message(
                        int(chat_id),
                        f"❌ Year must be between 2000 and {current_year}."
                    )
                else:
                    # Valid year, create scan job
                    try:
                        scan_id = scanner_service.create_scan_job(year, phone_number=chat_id)

                        # Update conversation state
                        conv.current_state = "processing"
                        conv.current_scan_id = scan_id
                        db.commit()
                        
                        telegram_service.send_message(
                            int(chat_id),
                            f"🔍 <b>Scanning IPO year {year}...</b>\n\n"
                            f"⏳ This may take a few minutes depending on the number of stocks.\n"
                            f"I'll send you the results with an Excel report once done."
                        )

                        if IS_VERCEL:
                            # On Vercel, we can't spawn threads reliably, so we use BackgroundTasks
                            background_tasks.add_task(_process_scan_and_notify, scan_id, chat_id)
                        else:
                            # Standard server: spawn a daemon thread
                            thread = threading.Thread(
                                target=_process_scan_and_notify,
                                args=(scan_id, chat_id),
                                daemon=True,
                            )
                            thread.start()

                    except Exception as e:
                        logger.error(f"Error starting scan: {e}")
                        db.rollback()
                        telegram_service.send_message(
                            int(chat_id),
                            "❌ Failed to start the scan. Please try again."
                        )
                        conv.current_state = "idle"
                        try:
                            db.commit()
                        except:
                            db.rollback()

        elif conv.current_state == "processing":
            telegram_service.send_message(
                int(chat_id),
                "⏳ Your previous scan is still running.\n\n"
                "Please wait for it to finish. You'll receive the report shortly!"
            )

        elif conv.current_state == "completed":
            conv.current_state = "awaiting_year"
            try:
                db.commit()
            except Exception:
                db.rollback()

            telegram_service.send_message(
                int(chat_id),
                "✅ Your last scan is complete!\n\n"
                "Would you like to scan another year?\n\n"
                "📅 <b>Enter IPO Year</b> (Example: 2020)"
            )

        else:
            # Default / idle state
            conv.current_state = "awaiting_year"
            try:
                db.commit()
            except Exception:
                db.rollback()

            telegram_service.send_message(
                int(chat_id),
                "👋 Welcome to the <b>IPO Breakout Scanner</b>!\n\n"
                "📅 <b>Enter IPO Year</b> (Example: 2018)"
            )

        # Final commit for any remaining changes
        try:
            db.commit()
        except Exception:
            db.rollback()

    except Exception as e:
        logger.error(f"CRITICAL: Webhook handler crashed: {e}", exc_info=True)

    # Always return 200 OK so Telegram doesn't retry
    return JSONResponse({"status": "ok"})