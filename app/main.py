"""
FastAPI Application Entry Point.

Configures the application with:
- CORS middleware
- Static file serving for reports
- Router registration
- Startup/shutdown lifecycle events

Compatible with both Vercel serverless and traditional server deployment.
"""

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.routers import webhook, scan
from app.utils.logger import setup_logger, get_logger

# Detect if running on Vercel (serverless)
IS_VERCEL = os.environ.get("VERCEL", "") == "1" or os.environ.get("VERCEL_ENV") is not None

# On Vercel, only /tmp is writable
TEMP_DIR = "/tmp" if IS_VERCEL else "."


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager."""
    # --- Startup ---
    settings = get_settings()
    setup_logger(log_level=settings.LOG_LEVEL)
    logger = get_logger("main")

    logger.info("IPO Breakout Scanner - Starting up")

    # Create required directories (use /tmp on Vercel)
    if IS_VERCEL:
        os.makedirs("/tmp/db", exist_ok=True)
        os.makedirs("/tmp/data", exist_ok=True)
        os.makedirs("/tmp/reports", exist_ok=True)
    else:
        os.makedirs("db", exist_ok=True)
        os.makedirs("data", exist_ok=True)
        os.makedirs("logs", exist_ok=True)
        os.makedirs(os.path.join("app", "static", "reports"), exist_ok=True)

    # Initialize database
    init_db()
    logger.info("Database initialized")
    logger.info(f"Environment: {'Vercel Serverless' if IS_VERCEL else 'Traditional Server'}")
    logger.info(f"Telegram Bot configured: {bool(settings.TELEGRAM_BOT_TOKEN)}")
    logger.info("Application startup complete")

    yield

    # --- Shutdown ---
    logger.info("Application shutting down...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="IPO Breakout Stock Screener",
        description=(
            "WhatsApp-integrated stock screening bot that identifies NSE stocks "
            "breaking above their IPO first-month high. Supports automated scanning, "
            "Excel report generation, and WhatsApp delivery via Twilio."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # --- CORS Middleware ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Static Files (for serving Excel reports) ---
    if not IS_VERCEL:
        reports_dir = os.path.join("app", "static", "reports")
        os.makedirs(reports_dir, exist_ok=True)
        app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # --- Register Routers ---
    app.include_router(webhook.router)
    app.include_router(scan.router)

    # --- Root endpoint ---
    @app.get("/")
    async def root():
        return {
            "application": "IPO Breakout Stock Screener",
            "version": "1.0.0",
            "environment": "Vercel Serverless" if IS_VERCEL else "Traditional Server",
            "docs": "/docs",
            "endpoints": {
                "webhook": "POST /webhook/whatsapp",
                "scan": "POST /scan",
                "status": "GET /scan/{scan_id}",
                "reports": "GET /reports/{filename}",
                "health": "GET /health",
                "scans_list": "GET /scans",
            },
        }

    return app


# Create the app instance
app = create_app()
