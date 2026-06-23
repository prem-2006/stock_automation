"""
Production server launcher.

Runs the FastAPI application with uvicorn using production-grade settings.
"""

import uvicorn
from app.config import get_settings


def main():
    """Launch the application server."""
    settings = get_settings()

    print("=" * 60)
    print("  IPO Breakout Stock Screener")
    print(f"  Server: http://{settings.HOST}:{settings.PORT}")
    print(f"  Docs:   http://{settings.HOST}:{settings.PORT}/docs")
    print("=" * 60)

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
        log_level=settings.LOG_LEVEL.lower(),
        access_log=True,
        workers=1,  # Use 1 for SQLite; increase for PostgreSQL
    )


if __name__ == "__main__":
    main()
