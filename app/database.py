"""
SQLAlchemy database setup for SQLite.
Handles engine creation, session management, and table initialization.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("database")

Base = declarative_base()

# Global engine and session factory (initialized on startup)
_engine = None
_SessionLocal = None


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        db_url = settings.DATABASE_URL

        # Ensure the database directory exists
        if db_url.startswith("sqlite:///"):
            db_path = db_url.replace("sqlite:///", "")
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

        _engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},  # Required for SQLite
            echo=False,
            pool_pre_ping=True,
        )
        logger.info(f"Database engine created: {db_url}")

    return _engine


def get_session_factory():
    """Get or create the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionLocal


def get_db() -> Session:
    """
    FastAPI dependency for database sessions.
    Yields a session and ensures cleanup.
    """
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all database tables."""
    from app.models import Stock, ScanJob, ScanResult, Conversation, CachedStockData  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created successfully")


def drop_db():
    """Drop all database tables (for testing)."""
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    logger.info("Database tables dropped")
