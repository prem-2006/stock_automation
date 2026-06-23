"""
Tests for the WhatsApp Webhook endpoint.

Tests the conversation state machine with simulated Twilio webhook requests.
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test environment variables before importing app
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.db")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "")

from app.main import app
from app.database import init_db, drop_db, get_engine


@pytest.fixture(autouse=True)
def setup_test_db():
    """Set up and tear down test database."""
    init_db()
    yield
    drop_db()
    # Clean up test db file
    if os.path.exists("test_db.db"):
        try:
            os.remove("test_db.db")
        except PermissionError:
            pass


@pytest.fixture
def client():
    """Create a FastAPI test client."""
    return TestClient(app)


class TestWhatsAppWebhook:
    """Test the WhatsApp webhook conversation flow."""

    def test_greeting_message(self, client):
        """Test that 'hi' triggers the welcome message."""
        response = client.post(
            "/webhook/whatsapp",
            data={
                "Body": "Hi",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_001",
            },
        )

        assert response.status_code == 200
        assert "application/xml" in response.headers["content-type"]
        assert "Enter IPO Year" in response.text
        assert "Welcome" in response.text

    def test_hello_variant(self, client):
        """Test that 'hello' also triggers the welcome message."""
        response = client.post(
            "/webhook/whatsapp",
            data={
                "Body": "hello",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_002",
            },
        )

        assert response.status_code == 200
        assert "Enter IPO Year" in response.text

    def test_invalid_year_input(self, client):
        """Test that an invalid year returns an error message."""
        # First, send greeting to set state to awaiting_year
        client.post(
            "/webhook/whatsapp",
            data={
                "Body": "Hi",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_003",
            },
        )

        # Send invalid year
        response = client.post(
            "/webhook/whatsapp",
            data={
                "Body": "abc",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_004",
            },
        )

        assert response.status_code == 200
        assert "valid year" in response.text.lower() or "doesn't look like" in response.text.lower()

    def test_out_of_range_year(self, client):
        """Test that a year outside valid range returns error."""
        # Set state to awaiting_year
        client.post(
            "/webhook/whatsapp",
            data={
                "Body": "Hi",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_005",
            },
        )

        # Send out-of-range year
        response = client.post(
            "/webhook/whatsapp",
            data={
                "Body": "1800",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_006",
            },
        )

        assert response.status_code == 200
        assert "valid year" in response.text.lower()

    def test_help_command(self, client):
        """Test that 'help' triggers the welcome message."""
        response = client.post(
            "/webhook/whatsapp",
            data={
                "Body": "help",
                "From": "whatsapp:+919876543210",
                "To": "whatsapp:+14155238886",
                "MessageSid": "test_sid_007",
            },
        )

        assert response.status_code == 200
        assert "Welcome" in response.text or "Enter IPO Year" in response.text


class TestScanEndpoints:
    """Test the REST scan API endpoints."""

    def test_health_check(self, client):
        """Test the health check endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"

    def test_root_endpoint(self, client):
        """Test the root endpoint returns application info."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["application"] == "IPO Breakout Stock Screener"
        assert "endpoints" in data

    def test_scan_invalid_year(self, client):
        """Test that an invalid year in scan request is rejected."""
        response = client.post(
            "/scan",
            json={"year": 1800},
        )
        assert response.status_code == 422  # Validation error

    def test_scan_missing_year(self, client):
        """Test that a missing year in scan request is rejected."""
        response = client.post(
            "/scan",
            json={},
        )
        assert response.status_code == 422

    def test_get_nonexistent_scan(self, client):
        """Test getting a non-existent scan returns 404."""
        response = client.get("/scan/nonexistent-id")
        assert response.status_code == 404

    def test_report_not_found(self, client):
        """Test downloading a non-existent report returns 404."""
        response = client.get("/reports/nonexistent.xlsx")
        assert response.status_code == 404

    def test_list_scans(self, client):
        """Test listing recent scans."""
        response = client.get("/scans")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
