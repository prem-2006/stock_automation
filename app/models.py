"""
SQLAlchemy ORM models for the stock screening application.
Defines tables for stocks, scan jobs, results, conversations, and cache.
"""

from datetime import datetime, UTC
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship
from app.database import Base


class Stock(Base):
    """NSE-listed stock information."""

    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), unique=True, nullable=False, index=True)
    company_name = Column(String(255), nullable=False)
    series = Column(String(10), default="EQ")
    isin = Column(String(20), nullable=True)
    listing_date = Column(DateTime, nullable=True)
    ipo_year = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    def __repr__(self):
        return f"<Stock(symbol='{self.symbol}', ipo_year={self.ipo_year})>"


class ScanJob(Base):
    """Tracks a stock scanning job."""

    __tablename__ = "scan_jobs"

    id = Column(String(36), primary_key=True)  # UUID
    year = Column(Integer, nullable=False)
    status = Column(String(20), default="pending")  # pending, running, completed, failed
    total_stocks = Column(Integer, default=0)
    scanned_stocks = Column(Integer, default=0)
    qualified_stocks = Column(Integer, default=0)
    report_path = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    phone_number = Column(String(50), nullable=True)  # WhatsApp requester
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime, nullable=True)

    # Relationship to scan results
    results = relationship("ScanResult", back_populates="scan_job", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ScanJob(id='{self.id}', year={self.year}, status='{self.status}')>"


class ScanResult(Base):
    """Individual stock screening result."""

    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(String(36), ForeignKey("scan_jobs.id"), nullable=False, index=True)
    symbol = Column(String(50), nullable=False)
    company_name = Column(String(255), nullable=False)
    ipo_year = Column(Integer, nullable=False)
    ipo_first_month_high = Column(Float, nullable=True)
    breakout_month = Column(String(20), nullable=True)  # YYYY-MM format
    breakout_close = Column(Float, nullable=True)
    current_price = Column(Float, nullable=True)
    pct_above_ipo_high = Column(Float, nullable=True)
    listing_date = Column(DateTime, nullable=True)
    qualified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    # Relationship to scan job
    scan_job = relationship("ScanJob", back_populates="results")

    def __repr__(self):
        return f"<ScanResult(symbol='{self.symbol}', qualified={self.qualified})>"


class Conversation(Base):
    """WhatsApp conversation state tracking."""

    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String(50), unique=True, nullable=False, index=True)
    current_state = Column(String(20), default="idle")  # idle, awaiting_year, processing, completed
    current_scan_id = Column(String(36), nullable=True)
    last_message_at = Column(DateTime, default=lambda: datetime.now(UTC))
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))

    def __repr__(self):
        return f"<Conversation(phone='{self.phone_number}', state='{self.current_state}')>"


class CachedStockData(Base):
    """Cache for historical OHLC data to minimize API calls."""

    __tablename__ = "cached_stock_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), unique=True, nullable=False, index=True)
    data_json = Column(Text, nullable=False)  # Serialized DataFrame as JSON
    cached_at = Column(DateTime, default=lambda: datetime.now(UTC))

    def __repr__(self):
        return f"<CachedStockData(symbol='{self.symbol}')>"


# Additional indexes for performance
Index("idx_scan_results_scan_id_qualified", ScanResult.scan_id, ScanResult.qualified)
Index("idx_stocks_ipo_year", Stock.ipo_year)
