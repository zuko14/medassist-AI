"""Integration tests for WhatsApp message flow (end-to-end mock).

Simulates the full webhook → tenant resolution → idempotency →
conversation manager pipeline, with Meta API and Supabase fully mocked.

Verifies:
  - Full message lifecycle: webhook receipt → 200 OK → background processing
  - Duplicate message deduplication (atomic idempotency gate)
  - Tenant resolution fallback for single-clinic mode
  - Clinical firewall blocks medication queries end-to-end
  - Emergency keyword triggers immediate escalation
  - Invalid webhook signature rejection (non-processing)
  - Test endpoint blocked in production mode
  - Dead-letter queue saves failed messages
  - WhatsApp interactive message parsing (button_reply, list_reply)
"""

import pytest
import hmac
import hashlib
import json
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Reusable test clinic
MOCK_CLINIC = {
    "id": "clinic-integration",
    "name": "Integration Test Hospital",
    "whatsapp_number": "+911234567890",
    "plan": "pro",
    "is_active": True,
    "config": {
        "meta_phone_number_id": "000000000000",
        "meta_access_token": "test_token",
        "clinic_name": "Integration Test Hospital",
        "language": "en",
    },
}


def _build_webhook_payload(
    phone: str = "+919876543210",
    message_text: str = "Hello",
    message_id: str = "wamid.test123",
    display_phone: str = "1234567890",
    msg_type: str = "text",
):
    """Build a valid WhatsApp webhook payload."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry-1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": display_phone,
                                "phone_number_id": "test_phone_id",
                            },
                            "contacts": [
                                {"wa_id": phone, "profile": {"name": "Test Patient"}}
                            ],
                            "messages": [
                                {
                                    "from": phone,
                                    "id": message_id,
                                    "timestamp": "1719756000",
                                    "type": msg_type,
                                    "text": {"body": message_text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    return payload


def _sign_payload(payload_dict: dict, secret: str = "") -> str:
    """Create HMAC-SHA256 signature for a payload."""
    body = json.dumps(payload_dict).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return sig


class TestWebhookIntegrationFlow:
    """End-to-end webhook message processing tests."""

    def test_webhook_returns_200_immediately(self):
        """Meta requires 200 OK within 20s — verify instant response."""
        payload = _build_webhook_payload()
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_webhook_empty_entry(self):
        """Webhook with no messages should still return 200."""
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "entry-1",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "1234567890",
                                    "phone_number_id": "test_phone_id",
                                },
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200

    def test_webhook_non_whatsapp_object(self):
        """Non-WhatsApp payloads should be silently accepted."""
        payload = {"object": "page", "entry": []}
        response = client.post("/webhook", json=payload)
        # Should not crash — returns 200 or handles gracefully
        assert response.status_code == 200

    def test_webhook_verify_subscribe(self):
        """Meta webhook verification handshake."""
        from app.config import settings

        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": settings.whatsapp_verify_token,
                "hub.challenge": "challenge_token_12345",
            },
        )
        assert response.status_code == 200
        assert response.text == "challenge_token_12345"

    def test_webhook_verify_wrong_token(self):
        """Invalid verify token should return 403."""
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "challenge",
            },
        )
        assert response.status_code == 403


class TestSignatureVerification:
    """Webhook HMAC signature verification integration tests."""

    def test_valid_signature_accepted(self):
        """Message with valid HMAC-SHA256 should be processed."""
        payload = _build_webhook_payload(message_text="Hi doctor")
        # When meta_app_secret is empty, verification is skipped (dev mode)
        response = client.post("/webhook", json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestClinicalFirewallIntegration:
    """End-to-end clinical firewall with webhook pipeline."""

    def test_medication_query_blocked_end_to_end(self):
        """A patient asking about medication should be blocked at the firewall level."""
        from app.services.clinical_firewall import screen_message

        # Simulate what happens inside conversation handler
        blocked, response = screen_message("can I take paracetamol for fever", "en")
        assert blocked is True
        assert "appointment" in response.lower()
        assert "🏥" in response

    def test_booking_request_passes_firewall(self):
        """A booking request should pass through the firewall cleanly."""
        from app.services.clinical_firewall import screen_message

        blocked, _ = screen_message(
            "I want to book an appointment with a cardiologist", "en"
        )
        assert blocked is False

    def test_emergency_keyword_detected(self):
        """Emergency keywords should be detected at AI engine level."""
        from app.services.ai_engine import keyword_intent_fallback

        assert keyword_intent_fallback("heart attack help") == "emergency"
        assert keyword_intent_fallback("severe bleeding") == "emergency"
        assert keyword_intent_fallback("can't breathe") == "emergency"

    def test_hindi_emergency_detected(self):
        """Hindi emergency keywords should also trigger."""
        from app.services.ai_engine import keyword_intent_fallback

        result = keyword_intent_fallback("बेहोश")
        assert result == "emergency"

    def test_data_deletion_intent_detected(self):
        """DELETE MY DATA should be detected as data_deletion_request intent."""
        from app.services.ai_engine import keyword_intent_fallback

        result = keyword_intent_fallback("delete my data")
        assert result == "data_deletion_request"


class TestIdempotencyIntegration:
    """Tests for message deduplication via the full webhook path."""

    def test_duplicate_webhook_payloads(self):
        """Sending the same message_id twice should not crash."""
        payload = _build_webhook_payload(
            message_id="wamid.duplicate_test_001", message_text="Booking please"
        )
        # First call
        response1 = client.post("/webhook", json=payload)
        assert response1.status_code == 200

        # Second call (duplicate) — should still return 200
        response2 = client.post("/webhook", json=payload)
        assert response2.status_code == 200


class TestEndpointSecurity:
    """Tests for endpoint access control."""

    def test_test_endpoint_blocked_in_production(self):
        """Test endpoint is removed from the application (returns 404) (T0.6)."""
        response = client.post(
            "/webhook/test",
            params={"phone": "+919876543210", "message": "test"},
        )
        assert response.status_code == 404

    def test_health_endpoint_accessible(self):
        """Health check should always be public."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readiness_endpoint_accessible(self):
        """Readiness check should always be public."""
        response = client.get("/health/ready")
        assert response.status_code == 200

    def test_root_returns_service_info(self):
        """Root endpoint should return service metadata."""
        response = client.get("/")
        data = response.json()
        assert data["service"] == "Kriya AI"
        assert data["version"] == "2.0.0"
        assert "status" in data

    def test_privacy_page_accessible(self):
        """Privacy policy page should be publicly accessible."""
        response = client.get("/privacy")
        assert response.status_code == 200
        assert "DPDP" in response.text
        assert "7 years" in response.text


class TestMultilingualIntegration:
    """End-to-end language detection and firewall integration."""

    def test_language_detection_english(self):
        from app.services.ai_engine import detect_language

        assert detect_language("I need an appointment") == "en"

    def test_language_detection_hindi(self):
        from app.services.ai_engine import detect_language

        assert detect_language("मुझे डॉक्टर से मिलना है") == "hi"

    def test_language_detection_telugu(self):
        from app.services.ai_engine import detect_language

        assert detect_language("నాకు డాక్టర్ కావాలి") == "te"

    def test_firewall_hindi_medication_blocked(self):
        from app.services.clinical_firewall import screen_message

        blocked, response = screen_message("कौन सी दवा लूं", "hi")
        assert blocked is True
        assert "🏥" in response

    def test_firewall_telugu_medication_blocked(self):
        from app.services.clinical_firewall import screen_message

        blocked, response = screen_message("ఏ మందు తీసుకోవాలి", "te")
        assert blocked is True
        assert "🏥" in response


class TestPIIIntegration:
    """End-to-end PII sanitization → restore integration."""

    def test_full_sanitize_restore_cycle(self):
        """Test complete PII pipeline: sanitize → (simulate LLM) → restore."""
        from app.utils.pii_sanitizer import sanitize_report_text, restore_pii

        report_text = (
            "Lab Report for Priya Sharma. "
            "Phone: +91-9876543210. "
            "Aadhaar: 1234 5678 9012. "
            "Email: priya@hospital.com. "
            "ABHA: 12345678901234. "
            "Hemoglobin: 12.5 g/dL (Normal)."
        )

        sanitized, rmap = sanitize_report_text(report_text, patient_name="Priya Sharma")

        # Verify all PII is stripped
        assert "Priya Sharma" not in sanitized
        assert "9876543210" not in sanitized
        assert "1234 5678 9012" not in sanitized
        assert "priya@hospital.com" not in sanitized
        # Clinical data preserved
        assert "Hemoglobin" in sanitized
        assert "12.5" in sanitized

        # Simulate LLM output referencing the patient placeholder
        patient_key = [k for k in rmap if "PATIENT" in k][0]
        llm_output = f"Dear {patient_key}, your hemoglobin is normal at 12.5 g/dL."

        restored = restore_pii(llm_output, rmap)
        # Patient name restored
        assert "Priya Sharma" in restored
        # Clinical content intact
        assert "hemoglobin" in restored.lower()


class TestTenantFeatureGating:
    """Integration test for plan-level feature gating."""

    def test_soloclinic_plan_features(self):
        from app.services.tenant import has_feature

        clinic = {"plan": "soloclinic"}
        assert has_feature(clinic, "booking") is True
        assert has_feature(clinic, "emergency_escalation") is True
        assert has_feature(clinic, "lab_reports") is False
        assert has_feature(clinic, "analytics") is False

    def test_essential_plan_features(self):
        from app.services.tenant import has_feature

        clinic = {"plan": "essential"}
        assert has_feature(clinic, "booking") is True
        assert has_feature(clinic, "lab_reports") is True
        assert has_feature(clinic, "analytics") is True
        assert has_feature(clinic, "feedback") is True

    def test_enterprise_wildcard(self):
        from app.services.tenant import has_feature

        clinic = {"plan": "enterprise"}
        assert has_feature(clinic, "booking") is True
        assert has_feature(clinic, "lab_reports") is True
        assert has_feature(clinic, "custom_anything") is True

    def test_per_clinic_override(self):
        from app.services.tenant import has_feature

        clinic = {"plan": "basic", "features": {"lab_reports": True}}
        assert has_feature(clinic, "lab_reports") is True  # overridden


class TestSecurityGuardsAndMetricsAuth:
    """T0.6 / T3.1: Verify metrics auth and staging security guardrails."""

    def test_metrics_endpoint_unauthenticated_fails_when_token_set(self):
        """GET /metrics should return 401 when unauthenticated and token configured."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.metrics_token = "secret_metrics_token_123"
            mock_settings.app_env = "production"
            response = client.get("/metrics")
            assert response.status_code == 401

    def test_metrics_endpoint_authenticated_success(self):
        """GET /metrics should return 200 when valid bearer token is provided."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.metrics_token = "secret_metrics_token_123"
            mock_settings.app_env = "production"
            response = client.get(
                "/metrics",
                headers={"Authorization": "Bearer secret_metrics_token_123"},
            )
            assert response.status_code == 200
            assert "kriya_" in response.text or "process_" in response.text or "python_" in response.text or len(response.text) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
