"""
WhatsApp Webhook Router.

Handles incoming Twilio WhatsApp messages and manages conversation state.
Implements a state machine for the chat flow:
  idle → awaiting_year → processing → completed

Compatible with Vercel serverless (synchronous) and traditional servers (background threads).
Fully hardened — no unhandled exceptions will crash the bot.
"""

import os
import threading
import traceback
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, Form, Request, Response, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Conversation, ScanJob
from app.services.scanner_service import ScannerService
from app.services.whatsapp_service import WhatsAppService
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("webhook")

router = APIRouter(prefix="/webhook", tags=["WhatsApp"])

# Service instances
scanner_service = ScannerService()
whatsapp_service = WhatsAppService()

# Detect Vercel
IS_VERCEL = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None


def _get_or_create_conversation(db: Session, phone_number: str) -> Conversation:
    """Get existing conversation or create a new one. Never crashes."""
    try:
        conv = db.query(Conversation).filter_by(phone_number=phone_number).first()
        if not conv:
            conv = Conversation(
                phone_number=phone_number,
                current_state="idle",
                last_message_at=datetime.now(UTC),
            )
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv
    except Exception as e:
        logger.error(f"Failed to get/create conversation for {phone_number}: {e}")
        db.rollback()
        # Create in-memory conversation as fallback
        conv = Conversation(
            phone_number=phone_number,
            current_state="idle",
            last_message_at=datetime.now(UTC),
        )
        try:
            db.add(conv)
            db.commit()
            db.refresh(conv)
        except Exception:
            db.rollback()
        return conv


def _process_scan_and_notify(scan_id: str, phone_number: str):
    """Run the scan and send results via WhatsApp. Never crashes."""
    try:
        logger.info(f"Scan started: {scan_id}")
        summary = scanner_service.run_scan(scan_id)

        # Send results via WhatsApp
        message = whatsapp_service.format_scan_summary(summary)

        report_path = summary.get("report_path")
        if report_path and not IS_VERCEL:
            settings = get_settings()
            filename = report_path.replace("\\", "/").split("/")[-1]
            media_url = f"{settings.BASE_URL}/reports/{filename}"

            try:
                whatsapp_service.send_message_with_attachment(
                    to=phone_number,
                    body=message,
                    media_url=media_url,
                )
            except Exception as e:
                logger.error(f"Failed to send attachment, sending text only: {e}")
                whatsapp_service.send_message(to=phone_number, body=message)
        else:
            whatsapp_service.send_message(to=phone_number, body=message)

        # Update conversation state
        _update_conversation_state(phone_number, "completed")

        logger.info(f"Scan completed and results sent: {scan_id}")

    except Exception as e:
        logger.error(f"Scan failed: {scan_id} — {e}", exc_info=True)

        # Notify user of failure — but protect against notification failure too
        try:
            whatsapp_service.send_message(
                to=phone_number,
                body=(
                    f"❌ Sorry, the scan failed due to an error.\n\n"
                    f"Please try again by sending 'Hi'."
                ),
            )
        except Exception as notify_err:
            logger.error(f"Failed to send error notification: {notify_err}")

        # Reset conversation state
        _update_conversation_state(phone_number, "idle")


def _update_conversation_state(phone_number: str, new_state: str):
    """Safely update conversation state in a fresh session. Never crashes."""
    try:
        from app.database import get_session_factory
        SessionLocal = get_session_factory()
        session = SessionLocal()
        try:
            conv = session.query(Conversation).filter_by(phone_number=phone_number).first()
            if conv:
                conv.current_state = new_state
                conv.current_scan_id = None
                session.commit()
        except Exception as e:
            logger.error(f"Failed to update conversation state: {e}")
            session.rollback()
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Failed to get session for conversation update: {e}")


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request,
    Body: str = Form(""),
    From: str = Form(""),
    To: str = Form(""),
    MessageSid: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Handle incoming WhatsApp messages from Twilio.

    This entire handler is wrapped in a top-level try/catch so that
    the user ALWAYS gets a response, even if something unexpected fails.
    """
    from twilio.twiml.messaging_response import MessagingResponse

    phone_number = From
    resp = MessagingResponse()

    try:
        message_body = Body.strip().lower()
        logger.info(f"Received WhatsApp from {phone_number}: '{Body.strip()}'")

        # Get or create conversation
        conv = _get_or_create_conversation(db, phone_number)
        conv.last_message_at = datetime.now(UTC)
        try:
            db.commit()
        except Exception:
            db.rollback()

        # State machine logic
        if message_body in ("hi", "hello", "hey", "start", "menu", "help"):
            # Reset to greeting state
            conv.current_state = "awaiting_year"
            conv.current_scan_id = None
            try:
                db.commit()
            except Exception:
                db.rollback()

            resp.message(
                "👋 Welcome to the *IPO Breakout Scanner*!\n\n"
                "This bot screens NSE-listed stocks by IPO year and identifies stocks "
                "that broke above their first-month listing high.\n\n"
                "📅 *Enter IPO Year* (Example: 2018)"
            )

        elif conv.current_state == "awaiting_year":
            # Try to parse year
            try:
                year = int(Body.strip())
                if 1990 <= year <= datetime.now().year:
                    # Valid year — start scanning
                    conv.current_state = "processing"

                    # Create scan job
                    scan_id = scanner_service.create_scan_job(year, phone_number=phone_number)
                    conv.current_scan_id = scan_id
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

                    if IS_VERCEL:
                        resp.message(
                            f"🔍 *Scanning IPO year {year}...*\n\n"
                            f"⏳ Processing now. Results will be sent shortly."
                        )
                        _process_scan_and_notify(scan_id, phone_number)
                    else:
                        resp.message(
                            f"🔍 *Scanning IPO year {year}...*\n\n"
                            f"⏳ This may take a few minutes depending on the number of stocks.\n"
                            f"I'll send you the results with an Excel report once done."
                        )
                        thread = threading.Thread(
                            target=_process_scan_and_notify,
                            args=(scan_id, phone_number),
                            daemon=True,
                        )
                        thread.start()
                else:
                    resp.message(
                        f"⚠️ Please enter a valid year between 1990 and {datetime.now().year}.\n\n"
                        f"📅 *Enter IPO Year* (Example: 2018)"
                    )
            except ValueError:
                resp.message(
                    "⚠️ That doesn't look like a valid year.\n\n"
                    "📅 *Enter IPO Year* (Example: 2018)"
                )

        elif conv.current_state == "processing":
            # Scan is already running
            scan_id = conv.current_scan_id
            if scan_id:
                try:
                    status = scanner_service.get_scan_status(scan_id)
                except Exception:
                    status = None

                if status:
                    if status.get("status") == "completed":
                        conv.current_state = "completed"
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
                        resp.message(
                            "✅ Your scan is complete!\n\n"
                            "Would you like to scan another year?\n\n"
                            "📅 *Enter IPO Year* (Example: 2020)"
                        )
                    elif status.get("status") == "failed":
                        conv.current_state = "idle"
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
                        resp.message(
                            "❌ Your previous scan encountered an issue.\n\n"
                            "Send *Hi* to start a fresh scan."
                        )
                    else:
                        progress = ""
                        if status.get("total_stocks", 0) > 0:
                            progress = (
                                f"\n📊 Progress: {status.get('scanned_stocks', 0)}"
                                f"/{status.get('total_stocks', 0)} stocks scanned"
                            )
                        resp.message(
                            f"⏳ Your scan is still in progress.{progress}\n\n"
                            f"Please wait — I'll send the results automatically when done."
                        )
                else:
                    resp.message("⏳ Your scan is being processed. Please wait...")
            else:
                conv.current_state = "idle"
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                resp.message(
                    "Something went wrong. Let's start over.\n\n"
                    "Send *Hi* to begin a new scan."
                )

        elif conv.current_state == "completed":
            # Offer to scan again
            conv.current_state = "awaiting_year"
            try:
                db.commit()
            except Exception:
                db.rollback()

            resp.message(
                "✅ Your last scan is complete!\n\n"
                "Would you like to scan another year?\n\n"
                "📅 *Enter IPO Year* (Example: 2020)"
            )

        else:
            # Default / idle state
            conv.current_state = "awaiting_year"
            try:
                db.commit()
            except Exception:
                db.rollback()

            resp.message(
                "👋 Welcome to the *IPO Breakout Scanner*!\n\n"
                "📅 *Enter IPO Year* (Example: 2018)"
            )

        # Final commit for any remaining changes
        try:
            db.commit()
        except Exception:
            db.rollback()

    except Exception as e:
        # TOP-LEVEL CATCH: If ANYTHING crashes, always reply gracefully
        logger.error(f"CRITICAL: Webhook handler crashed: {e}", exc_info=True)
        resp = MessagingResponse()  # Fresh response in case the old one is corrupt
        resp.message(
            "⚠️ Something unexpected happened. Please try again by sending *Hi*."
        )

    return Response(content=str(resp), media_type="application/xml")
