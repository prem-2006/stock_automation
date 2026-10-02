"""
Telegram Bot 2 Webhook Router - Manual Month/Year Selection.

Same stock screening conditions as Bot 1, but user picks:
  1. IPO Year (e.g. 2024 or ALL)
  2. Reference Month - the month whose close is compared against IPO first-month high

Conversation flow:
  idle -> awaiting_year -> awaiting_month -> processing -> idle
"""

import os
import threading
from datetime import datetime, UTC

import httpx
from fastapi import APIRouter, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db, get_session_factory
from app.models import Conversation
from app.services.scanner_service import ScannerService
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("webhook_bot2")
router = APIRouter(tags=["Telegram Bot 2"])
settings = get_settings()
scanner_service = ScannerService()
IS_VERCEL = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None
BOT2_PREFIX = "bot2_"

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December"
}

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

# In-memory store for pending year while user picks month
_pending_years: dict = {}


# ---------------------------------------------------------------------------
# Telegram API helpers
# ---------------------------------------------------------------------------

def _bot2_url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT2_TOKEN}/{method}"


def _send_message(chat_id: int, text: str, reply_markup: dict = None) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        max_len = 4000
        chunks = []
        if len(text) <= max_len:
            chunks.append(text)
        else:
            current = ""
            for line in text.split("\n"):
                if len(current) + len(line) + 1 > max_len:
                    if current:
                        chunks.append(current)
                    current = line + "\n"
                else:
                    current += line + "\n"
            if current:
                chunks.append(current)
        with httpx.Client() as client:
            for chunk in chunks:
                payload["text"] = chunk
                r = client.post(_bot2_url("sendMessage"), json=payload, timeout=10.0)
                r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Bot2 send_message error: {e}")
        return False


def _send_document(chat_id: int, file_path: str, caption: str = "") -> bool:
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f)}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            with httpx.Client() as client:
                r = client.post(_bot2_url("sendDocument"), data=data, files=files, timeout=60.0)
                r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Bot2 send_document error: {e}")
        return False


def _month_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Jan", "callback_data": "month_1"},
             {"text": "Feb", "callback_data": "month_2"},
             {"text": "Mar", "callback_data": "month_3"}],
            [{"text": "Apr", "callback_data": "month_4"},
             {"text": "May", "callback_data": "month_5"},
             {"text": "Jun", "callback_data": "month_6"}],
            [{"text": "Jul", "callback_data": "month_7"},
             {"text": "Aug", "callback_data": "month_8"},
             {"text": "Sep", "callback_data": "month_9"}],
            [{"text": "Oct", "callback_data": "month_10"},
             {"text": "Nov", "callback_data": "month_11"},
             {"text": "Dec", "callback_data": "month_12"}],
        ]
    }


def _year_keyboard() -> dict:
    today_year = datetime.now().year
    keyboard = []
    row = []
    
    # Generate years from current year down to 2015
    for year in range(today_year, 2014, -1):
        row.append({"text": str(year), "callback_data": f"year_{year}"})
        if len(row) == 3:
            keyboard.append(row)
            row = []
            
    # Add any remaining years if not a multiple of 3
    if row:
        keyboard.append(row)
        
    keyboard.append([{"text": "ALL NSE Stocks", "callback_data": "year_0"}])
    return {"inline_keyboard": keyboard}



def _answer_callback(cq_id: str) -> None:
    try:
        with httpx.Client() as client:
            client.post(_bot2_url("answerCallbackQuery"), json={"callback_query_id": cq_id}, timeout=5.0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Conversation helpers
# ---------------------------------------------------------------------------

def _get_or_create_conv(db: Session, chat_id: str) -> Conversation:
    key = BOT2_PREFIX + chat_id
    try:
        conv = db.query(Conversation).filter_by(phone_number=key).first()
        if not conv:
            conv = Conversation(
                phone_number=key,
                current_state="idle",
                created_at=datetime.now(UTC),
                last_message_at=datetime.now(UTC),
            )
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv
    except Exception as e:
        logger.error(f"Bot2 get/create conv error: {e}")
        db.rollback()
        return Conversation(phone_number=BOT2_PREFIX + chat_id, current_state="idle")


def _update_conv_state(chat_id: str, state: str) -> None:
    key = BOT2_PREFIX + chat_id
    try:
        SessionLocal = get_session_factory()
        with SessionLocal() as db:
            conv = db.query(Conversation).filter_by(phone_number=key).first()
            if conv:
                conv.current_state = state
                db.commit()
    except Exception as e:
        logger.error(f"Bot2 update conv error: {e}")


# ---------------------------------------------------------------------------
# Background scan worker
# ---------------------------------------------------------------------------

def _run_scan_and_notify(scan_id: str, chat_id: str, target_month: int, target_year: int) -> None:
    try:
        logger.info(f"Bot2 scan start: job={scan_id} month={target_month}/{target_year}")
        summary = scanner_service.run_scan(scan_id, target_month=target_month, target_year_override=target_year)

        year = summary.get("year", "?")
        total = summary.get("total_scanned", 0)
        q_count = summary.get("qualified_count", 0)
        q_stocks = summary.get("qualified_list", [])
        month_name = MONTH_NAMES.get(target_month, str(target_month))

        top_list = ""
        for i, stock in enumerate(q_stocks, 1):
            symbol = stock.get("symbol", "N/A")
            pct = stock.get("pct_above_ipo_high", 0) or 0
            top_list += f"{i}. <b>{symbol}</b> ({pct:+.1f}%)\n"

        year_label = "ALL years" if year == 0 else str(year)
        msg = (
            f"\U0001f4ca <b>IPO Breakout Scan Complete</b>\n\n"
            f"\U0001f4c5 Year: <b>{year_label}</b>\n"
            f"\U0001f5d3 Reference Month: <b>{month_name} {target_year}</b>\n"
            f"\U0001f50d Stocks Scanned: <b>{total}</b>\n"
            f"\u2705 Qualified: <b>{q_count}</b>\n\n"
        )
        if top_list:
            msg += f"\U0001f3c6 <b>Qualified Stocks:</b>\n{top_list}\n"
        msg += "\U0001f4ce Excel report attached below."

        report_path = summary.get("report_path")
        if report_path and os.path.exists(report_path):
            _send_message(int(chat_id), msg)
            _send_document(int(chat_id), report_path, "Your detailed Excel report")
        else:
            _send_message(int(chat_id), msg)

    except Exception as e:
        logger.error(f"Bot2 scan error: {e}", exc_info=True)
        _send_message(int(chat_id), f"\u274c Scan failed: {e}")
    finally:
        _update_conv_state(chat_id, "idle")


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook/telegram2")
async def telegram2_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Bot 2: manual year + month selection.
    """
    try:
        update = await request.json()

        # ---- Callback query (inline button press) ----
        if "callback_query" in update:
            cq = update["callback_query"]
            cq_id = cq["id"]
            chat_id = str(cq["message"]["chat"]["id"])
            data = cq.get("data", "")
            _answer_callback(cq_id)

            if data.startswith("year_"):
                year = int(data.split("_")[1])
                _pending_years[chat_id] = year
                
                conv = _get_or_create_conv(db, chat_id)
                conv.current_state = "awaiting_month"
                try:
                    db.commit()
                except Exception:
                    db.rollback()

                year_label = "ALL years" if year == 0 else str(year)
                _send_message(
                    int(chat_id),
                    f"✅ Year set to: <b>{year_label}</b>\n\n"
                    f"🗓 <b>Step 2:</b> Select the reference month for comparison:\n\n"
                    f"👇 Tap a month below:",
                    reply_markup=_month_keyboard()
                )
                return JSONResponse({"status": "ok"})

            elif data.startswith("month_"):
                month_num = int(data.split("_")[1])
                year = _pending_years.get(chat_id)
                if year is None:
                    _send_message(int(chat_id), "\u274c Session expired. Type <b>Hi</b> to start again.")
                    return JSONResponse({"status": "ok"})

                month_name = MONTH_NAMES[month_num]
                today = datetime.now()
                target_year = today.year - 1 if month_num >= today.month else today.year

                conv = _get_or_create_conv(db, chat_id)
                try:
                    scan_id = scanner_service.create_scan_job(year, phone_number=BOT2_PREFIX + chat_id)
                    conv.current_state = "processing"
                    conv.current_scan_id = scan_id
                    db.commit()

                    year_label = "ALL NSE stocks" if year == 0 else f"IPO year {year}"
                    _send_message(
                        int(chat_id),
                        f"\U0001f50d <b>Scanning {year_label}...</b>\n\n"
                        f"\U0001f5d3 Reference Month: <b>{month_name} {target_year}</b>\n\n"
                        f"\u23f3 Please wait, this may take a few minutes."
                    )

                    if IS_VERCEL:
                        background_tasks.add_task(_run_scan_and_notify, scan_id, chat_id, month_num, target_year)
                    else:
                        threading.Thread(
                            target=_run_scan_and_notify,
                            args=(scan_id, chat_id, month_num, target_year),
                            daemon=True,
                        ).start()

                    _pending_years.pop(chat_id, None)

                except Exception as e:
                    logger.error(f"Bot2 scan start error: {e}")
                    db.rollback()
                    _send_message(int(chat_id), "\u274c Failed to start scan. Try again.")
                    conv.current_state = "idle"
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

            return JSONResponse({"status": "ok"})

        # ---- Regular text message ----
        if "message" not in update:
            return JSONResponse({"status": "ok"})

        message = update["message"]
        if "text" not in message or "chat" not in message:
            return JSONResponse({"status": "ok"})

        chat_id = str(message["chat"]["id"])
        text = message["text"].strip()
        text_lower = text.lower()
        text_upper = text.upper().strip()

        logger.info(f"Bot2 from {chat_id}: '{text}'")

        conv = _get_or_create_conv(db, chat_id)
        conv.last_message_at = datetime.now(UTC)
        try:
            db.commit()
        except Exception:
            db.rollback()

        # Greeting
        if text_lower in ("hi", "hello", "hey", "start", "menu", "help", "/start"):
            conv.current_state = "awaiting_year"
            conv.current_scan_id = None
            try:
                db.commit()
            except Exception:
                db.rollback()
            _send_message(
                int(chat_id),
                "\U0001f44b Welcome to <b>IPO Breakout Scanner v2</b>!\n\n"
                "This bot lets you choose both the <b>IPO year</b> AND the <b>reference month</b> "
                "for the scan.\n\n"
                "\U0001f4c5 <b>Step 1:</b> Select the IPO year below:",
                reply_markup=_year_keyboard()
            )
            return JSONResponse({"status": "ok"})

        # Awaiting year
        if conv.current_state == "awaiting_year":
            if text_upper == "ALL":
                year = 0
            elif not text.isdigit() or len(text) != 4:
                _send_message(int(chat_id), "\u274c Please enter a valid 4-digit year or type <b>ALL</b>.")
                return JSONResponse({"status": "ok"})
            else:
                year = int(text)
                current_year = datetime.now().year
                if year < 2000 or year > current_year:
                    _send_message(int(chat_id), f"\u274c Year must be between 2000 and {current_year}.")
                    return JSONResponse({"status": "ok"})

            _pending_years[chat_id] = year
            conv.current_state = "awaiting_month"
            try:
                db.commit()
            except Exception:
                db.rollback()

            year_label = "ALL years" if year == 0 else str(year)
            _send_message(
                int(chat_id),
                f"\u2705 Year set to: <b>{year_label}</b>\n\n"
                f"\U0001f5d3 <b>Step 2:</b> Select the reference month for comparison:\n\n"
                f"\U0001f447 Tap a month below:",
                reply_markup=_month_keyboard()
            )
            return JSONResponse({"status": "ok"})

        # Awaiting month (typed)
        if conv.current_state == "awaiting_month":
            month_num = MONTH_MAP.get(text_lower.strip())
            if month_num is None:
                _send_message(int(chat_id), "\u274c Tap a month button above, or type the month name (e.g. <b>June</b>).")
                return JSONResponse({"status": "ok"})

            year = _pending_years.get(chat_id)
            if year is None:
                _send_message(int(chat_id), "\u274c Session expired. Type <b>Hi</b> to start again.")
                return JSONResponse({"status": "ok"})

            month_name = MONTH_NAMES[month_num]
            today = datetime.now()
            target_year = today.year - 1 if month_num >= today.month else today.year

            try:
                scan_id = scanner_service.create_scan_job(year, phone_number=BOT2_PREFIX + chat_id)
                conv.current_state = "processing"
                conv.current_scan_id = scan_id
                db.commit()

                year_label = "ALL NSE stocks" if year == 0 else f"IPO year {year}"
                _send_message(
                    int(chat_id),
                    f"\U0001f50d <b>Scanning {year_label}...</b>\n\n"
                    f"\U0001f5d3 Reference Month: <b>{month_name} {target_year}</b>\n\n"
                    f"\u23f3 Please wait, this may take a few minutes."
                )

                if IS_VERCEL:
                    background_tasks.add_task(_run_scan_and_notify, scan_id, chat_id, month_num, target_year)
                else:
                    threading.Thread(
                        target=_run_scan_and_notify,
                        args=(scan_id, chat_id, month_num, target_year),
                        daemon=True,
                    ).start()

                _pending_years.pop(chat_id, None)

            except Exception as e:
                logger.error(f"Bot2 scan start error: {e}")
                db.rollback()
                _send_message(int(chat_id), "\u274c Failed to start scan. Try again.")
                conv.current_state = "idle"
                try:
                    db.commit()
                except Exception:
                    db.rollback()

            return JSONResponse({"status": "ok"})

        # Processing
        if conv.current_state == "processing":
            _send_message(int(chat_id), "\u23f3 Scan still running... you'll get the report shortly!")
            return JSONResponse({"status": "ok"})

        # Default
        conv.current_state = "awaiting_year"
        try:
            db.commit()
        except Exception:
            db.rollback()
        _send_message(
            int(chat_id),
            "\U0001f44b Welcome to <b>IPO Breakout Scanner v2</b>!\n\n"
            "\U0001f4c5 <b>Step 1:</b> Select the IPO year below:",
            reply_markup=_year_keyboard()
        )

    except Exception as e:
        logger.error(f"Bot2 CRITICAL crash: {e}", exc_info=True)

    return JSONResponse({"status": "ok"})