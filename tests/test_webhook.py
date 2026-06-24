"""
Tests for the Telegram Webhook endpoint.

Tests the conversation state machine with simulated Telegram webhook requests.
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test environment variables before importing app
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_db.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token")

from app.main import app
from app.database import init_db, drop_db


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


def build_telegram_payload(chat_id: int, text: str) -> dict:
    """Helper to build a mock Telegram payload."""
    return {
        "update_id": 12345,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id, "type": "private"},
            "text": text
        }
    }


class TestTelegramWebhook:
    """Test the Telegram webhook conversation flow."""

    @patch("app.routers.webhook.telegram_service.send_message")
    def test_greeting_message(self, mock_send_message, client):
        """Test that 'hi' triggers the welcome message."""
        response = client.post(
            "/webhook/telegram",
            json=build_telegram_payload(111, "Hi")
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        
        # Verify the correct message was sent
        mock_send_message.assert_called_once()
        args, kwargs = mock_send_message.call_args
        assert args[0] == 111
        assert "Welcome to the <b>IPO Breakout Scanner</b>!" in args[1]

    @patch("app.routers.webhook.telegram_service.send_message")
    def test_invalid_year_input(self, mock_send_message, client):
        """Test that sending an invalid year returns an error."""
        # 1. Start state machine
        client.post("/webhook/telegram", json=build_telegram_payload(111, "Hi"))

        # 2. Send invalid year
        mock_send_message.reset_mock()
        client.post("/webhook/telegram", json=build_telegram_payload(111, "abcd"))

        mock_send_message.assert_called_once()
        assert "Invalid year format" in mock_send_message.call_args[0][1]

    @patch("app.routers.webhook.telegram_service.send_message")
    def test_out_of_range_year(self, mock_send_message, client):
        """Test that a year before 2000 is rejected."""
        client.post("/webhook/telegram", json=build_telegram_payload(111, "Hi"))
        
        mock_send_message.reset_mock()
        client.post("/webhook/telegram", json=build_telegram_payload(111, "1999"))

        mock_send_message.assert_called_once()
        assert "Year must be between 2000" in mock_send_message.call_args[0][1]


class TestScanEndpoints:
    """Test other scan-related endpoints."""

    def test_health_check(self, client):
        """Test the health check endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_list_scans(self, client):
        """Test listing scan jobs."""
        response = client.get("/scans")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
