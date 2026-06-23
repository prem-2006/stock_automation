"""
Scan API Router.

Provides REST API endpoints for:
- Manually triggering stock scans
- Checking scan status
- Downloading reports
- Health checks
"""

import os
import threading
from datetime import datetime, UTC
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from app.schemas import ScanRequest, ScanJobResponse, HealthResponse
from app.services.scanner_service import ScannerService
from app.services.whatsapp_service import WhatsAppService
from app.utils.logger import get_logger

logger = get_logger("scan_router")

router = APIRouter(tags=["Scan"])

scanner_service = ScannerService()
whatsapp_service = WhatsAppService()


def _run_scan_background(scan_id: str, phone_number: Optional[str] = None):
    """Execute scan in background thread."""
    try:
        summary = scanner_service.run_scan(scan_id)

        # If a phone number was provided, send results via WhatsApp
        if phone_number:
            message = whatsapp_service.format_scan_summary(summary)
            report_path = summary.get("report_path")

            if report_path:
                from app.config import get_settings

                settings = get_settings()
                filename = report_path.replace("\\", "/").split("/")[-1]
                media_url = f"{settings.BASE_URL}/reports/{filename}"
                whatsapp_service.send_message_with_attachment(
                    to=phone_number, body=message, media_url=media_url
                )
            else:
                whatsapp_service.send_message(to=phone_number, body=message)

    except Exception as e:
        logger.error(f"Background scan error: {e}", exc_info=True)


@router.post("/scan", response_model=dict)
async def trigger_scan(request: ScanRequest):
    """
    Manually trigger a stock scan for a given IPO year.

    The scan runs in the background. Use GET /scan/{scan_id} to check status.

    Args:
        request: ScanRequest with year and optional phone_number

    Returns:
        Dict with scan_id and status
    """
    year = request.year
    phone_number = request.phone_number

    logger.info(f"Manual scan triggered for year {year}")

    # Create scan job
    scan_id = scanner_service.create_scan_job(year, phone_number=phone_number)

    # Launch background scan
    thread = threading.Thread(
        target=_run_scan_background,
        args=(scan_id, phone_number),
        daemon=True,
    )
    thread.start()

    return {
        "scan_id": scan_id,
        "year": year,
        "status": "started",
        "message": f"Scan started for IPO year {year}. Use GET /scan/{scan_id} to check status.",
    }


@router.get("/scan/{scan_id}")
async def get_scan_status(scan_id: str):
    """
    Get the current status and results of a scan job.

    Args:
        scan_id: UUID of the scan job

    Returns:
        Scan job status with results if completed
    """
    status = scanner_service.get_scan_status(scan_id)

    if not status:
        raise HTTPException(status_code=404, detail=f"Scan job {scan_id} not found")

    return status


@router.get("/reports/{filename}")
async def download_report(filename: str):
    """
    Download a generated Excel report.

    Args:
        filename: Name of the report file

    Returns:
        Excel file download
    """
    reports_dir = os.path.join("app", "static", "reports")
    filepath = os.path.join(reports_dir, filename)

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Report not found")

    # Security: prevent path traversal
    real_path = os.path.realpath(filepath)
    real_reports = os.path.realpath(reports_dir)
    if not real_path.startswith(real_reports):
        raise HTTPException(status_code=403, detail="Access denied")

    return FileResponse(
        path=filepath,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint for monitoring."""
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        timestamp=datetime.now(UTC),
    )


@router.get("/scans")
async def list_recent_scans():
    """List recent scan jobs."""
    from app.database import get_session_factory
    from app.models import ScanJob

    SessionLocal = get_session_factory()
    session = SessionLocal()

    try:
        jobs = (
            session.query(ScanJob)
            .order_by(ScanJob.created_at.desc())
            .limit(20)
            .all()
        )

        return [
            {
                "scan_id": job.id,
                "year": job.year,
                "status": job.status,
                "total_stocks": job.total_stocks,
                "qualified_stocks": job.qualified_stocks,
                "report_path": job.report_path,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            }
            for job in jobs
        ]
    finally:
        session.close()
