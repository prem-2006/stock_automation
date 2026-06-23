"""
FastAPI Application Entry Point.

Configures the application with:
- CORS middleware
- Static file serving for reports
- Router registration
- Startup/shutdown lifecycle events
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.routers import webhook, scan
from app.utils.logger import setup_logger, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager."""
    # --- Startup ---
    settings = get_settings()
    setup_logger(log_level=settings.LOG_LEVEL)
    logger = get_logger("main")

    logger.info("=" * 60)
    logger.info("  IPO Breakout Scanner - Starting up")
    logger.info("=" * 60)

    # Create required directories
    os.makedirs("db", exist_ok=True)
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    os.makedirs(os.path.join("app", "static", "reports"), exist_ok=True)

    # Initialize database
    init_db()
    logger.info("Database initialized")

    # Log configuration
    logger.info(f"Base URL: {settings.BASE_URL}")
    logger.info(f"Max Workers: {settings.MAX_WORKERS}")
    logger.info(f"Twilio configured: {bool(settings.TWILIO_ACCOUNT_SID)}")

    logger.info("Application startup complete")
    logger.info("=" * 60)

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
            "docs": f"{settings.BASE_URL}/docs",
            "health": f"{settings.BASE_URL}/health",
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
