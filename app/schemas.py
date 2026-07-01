"""
Pydantic schemas for API request/response validation.
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# --- Scan Request/Response ---

class ScanRequest(BaseModel):
    """Request body for initiating a stock scan."""
    year: int = Field(..., ge=0, le=2030, description="IPO year to scan (0 for all years)")
    phone_number: Optional[str] = Field(None, description="WhatsApp number for results delivery")


class ScanResultItem(BaseModel):
    """Individual stock result in a scan."""
    symbol: str
    company_name: str
    ipo_year: int
    ipo_first_month_high: Optional[float]
    breakout_month: Optional[str]
    breakout_close: Optional[float]
    current_price: Optional[float]
    pct_above_ipo_high: Optional[float]
    listing_date: Optional[str]
    qualified: bool

    model_config = {"from_attributes": True}


class ScanJobResponse(BaseModel):
    """Response for scan job status."""
    id: str
    year: int
    status: str
    total_stocks: int
    scanned_stocks: int
    qualified_stocks: int
    report_path: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ScanJobWithResults(ScanJobResponse):
    """Scan job response including detailed results."""
    results: List[ScanResultItem] = []


class ScanSummary(BaseModel):
    """Summary statistics for a completed scan."""
    year: int
    total_scanned: int
    qualified_count: int
    qualification_pct: float
    top_10: List[ScanResultItem]


# --- WhatsApp ---

class WhatsAppIncoming(BaseModel):
    """Parsed incoming WhatsApp message."""
    from_number: str
    body: str
    message_sid: Optional[str] = None


# --- Health Check ---

class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    version: str = "1.0.0"
    timestamp: datetime
