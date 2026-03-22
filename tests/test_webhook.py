"""Tests for webhook handler."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestWebhook:
    """Test webhook endpoints."""

    def test_health_check(self):
        """Test health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_readiness_check(self):
        """Test readiness endpoint."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data

    def test_root_endpoint(self):
        """Test root endpoint."""
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["service"] == "MediAssist AI"

    def test_webhook_verify(self):
        """Test webhook verification."""
        from app.config import settings

        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "test_challenge"
            }
        )
        assert response.status_code == 200
        assert response.text == "test_challenge"

    def test_webhook_verify_invalid_token(self):
        """Test webhook verification with invalid token."""
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "invalid_token",
                "hub.challenge": "test_challenge"
            }
        )
        assert response.status_code == 403

    def test_webhook_receive(self):
        """Test webhook receive."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "test_entry_id",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "1234567890",
                            "phone_number_id": "test_phone_id"
                        },
                        "contacts": [{
                            "wa_id": "+919876543210",
                            "profile": {"name": "Test User"}
                        }],
                        "messages": [{
                            "from": "+919876543210",
                            "id": "test_msg_id",
                            "timestamp": "1234567890",
                            "type": "text",
                            "text": {"body": "Hello"}
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }

        response = client.post("/webhook", json=payload)
        assert response.status_code == 200


class TestValidators:
    """Test validation utilities."""

    def test_phone_normalization(self):
        """Test phone number normalization."""
        from app.utils.validators import normalize_phone

        assert normalize_phone("9876543210") == "+919876543210"
        assert normalize_phone("+919876543210") == "+919876543210"
        assert normalize_phone("+1-987-654-3210") == "+19876543210"

    def test_phone_validation(self):
        """Test phone validation."""
        from app.utils.validators import validate_phone

        assert validate_phone("9876543210") is True
        assert validate_phone("+919876543210") is True
        assert validate_phone("123") is False

    def test_name_validation(self):
        """Test name validation."""
        from app.utils.validators import validate_name

        valid, error = validate_name("Ravi Kumar")
        assert valid is True
        assert error is None

        valid, error = validate_name("R")
        assert valid is False
        assert error is not None


class TestHelpers:
    """Test helper utilities."""

    def test_booking_reference_generation(self):
        """Test booking reference generation."""
        from app.utils.helpers import generate_booking_reference

        ref = generate_booking_reference()
        assert len(ref) == 8  # 2 char prefix + 6 char code
        assert ref[:2] == "MC"  # Default prefix

    def test_date_formatting(self):
        """Test date formatting."""
        from app.utils.helpers import format_date

        result = format_date("2026-03-17")
        assert "17" in result
        assert "Mar" in result

    def test_truncate_text(self):
        """Test text truncation."""
        from app.utils.helpers import truncate_text

        long_text = "a" * 100
        result = truncate_text(long_text, 20)
        assert len(result) <= 23  # 20 + "..."
        assert result.endswith("...")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
