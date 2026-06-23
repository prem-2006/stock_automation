"""
WhatsApp Webhook Router.

Handles incoming Twilio WhatsApp messages and manages conversation state.
Implements a state machine for the chat flow:
  idle → awaiting_year → processing → completed

Compatible with Vercel serverless (synchronous) and traditional servers (background threads).
"""

import os
import threading
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
    """Get existing conversation or create a new one."""
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


def _process_scan_and_notify(scan_id: str, phone_number: str):
    """Run the scan and send results via WhatsApp."""
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

            whatsapp_service.send_message_with_attachment(
                to=phone_number,
                body=message,
                media_url=media_url,
            )
        else:
            # On Vercel, /tmp files aren't publicly accessible, send text only
            whatsapp_service.send_message(to=phone_number, body=message)

        # Update conversation state
        from app.database import get_session_factory

        SessionLocal = get_session_factory()
        session = SessionLocal()
        try:
            conv = session.query(Conversation).filter_by(phone_number=phone_number).first()
            if conv:
                conv.current_state = "completed"
                conv.current_scan_id = None
                session.commit()
        finally:
            session.close()

        logger.info(f"Scan completed and results sent: {scan_id}")

    except Exception as e:
        logger.error(f"Scan failed: {scan_id} — {e}", exc_info=True)

        # Notify user of failure
        whatsapp_service.send_message(
            to=phone_number,
            body=f"❌ Sorry, the scan failed due to an error: {str(e)[:200]}\n\nPlease try again by sending 'Hi'.",
        )

        # Reset conversation state
        from app.database import get_session_factory

        SessionLocal = get_session_factory()
        session = SessionLocal()
        try:
            conv = session.query(Conversation).filter_by(phone_number=phone_number).first()
            if conv:
                conv.current_state = "idle"
                conv.current_scan_id = None
                session.commit()
        finally:
            session.close()


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

    Twilio sends POST requests with form data when a user messages the WhatsApp number.
    This endpoint processes the message, manages conversation state, and responds with TwiML.

    On Vercel: scan runs synchronously, results sent via Twilio API before response.
    On traditional server: scan runs in background thread.
    """
    from twilio.twiml.messaging_response import MessagingResponse

    phone_number = From
    message_body = Body.strip().lower()

    logger.info(f"Received WhatsApp from {phone_number}: '{Body.strip()}'")

    # Get or create conversation
    conv = _get_or_create_conversation(db, phone_number)
    conv.last_message_at = datetime.now(UTC)
    db.commit()  # Commit immediately to release SQLite write locks

    # Create TwiML response
    resp = MessagingResponse()

    # State machine logic
    if message_body in ("hi", "hello", "hey", "start", "menu", "help"):
        # Reset to greeting state
        conv.current_state = "awaiting_year"
        conv.current_scan_id = None
        db.commit()

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
                db.commit()

                if IS_VERCEL:
                    # Serverless: run synchronously, send results via Twilio API
                    # Reply immediately, then process
                    resp.message(
                        f"🔍 *Scanning IPO year {year}...*\n\n"
                        f"⏳ Processing now. Results will be sent shortly."
                    )

                    # Run scan synchronously and send results via Twilio API
                    _process_scan_and_notify(scan_id, phone_number)
                else:
                    # Traditional server: background thread
                    resp.message(
                        f"🔍 *Scanning IPO year {year}...*\n\n"
                        f"⏳ This may take a few minutes depending on the number of stocks.\n"
                        f"I'll send you the results with an Excel report once done.\n\n"
                        f"Scan ID: `{scan_id[:8]}`"
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
            status = scanner_service.get_scan_status(scan_id)
            if status:
                if status.get("status") == "completed":
                    conv.current_state = "completed"
                    db.commit()
                    resp.message(
                        "✅ Your scan is complete!\n\n"
                        "Would you like to scan another year?\n\n"
                        "📅 *Enter IPO Year* (Example: 2020)"
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
            db.commit()
            resp.message(
                "Something went wrong. Let's start over.\n\n"
                "Send *Hi* to begin a new scan."
            )

    elif conv.current_state == "completed":
        # Offer to scan again
        conv.current_state = "awaiting_year"
        db.commit()

        resp.message(
            "✅ Your last scan is complete!\n\n"
            "Would you like to scan another year?\n\n"
            "📅 *Enter IPO Year* (Example: 2020)"
        )

    else:
        # Default / idle state
        conv.current_state = "awaiting_year"
        db.commit()

        resp.message(
            "👋 Welcome to the *IPO Breakout Scanner*!\n\n"
            "📅 *Enter IPO Year* (Example: 2018)"
        )

    db.commit()

    return Response(content=str(resp), media_type="application/xml")
