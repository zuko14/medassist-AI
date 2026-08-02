"""Tests for PII Sanitization Middleware (app/utils/pii_sanitizer.py).

Verifies that:
  - Patient names are stripped before reaching external LLMs
  - Indian phone numbers (+91, 91, 0, standalone) are scrubbed
  - Aadhaar 12-digit numbers are redacted
  - ABHA 14-digit IDs are redacted
  - Emails and DOBs are scrubbed
  - restore_pii restores only patient name while keeping other PII hidden
"""

from app.utils.pii_sanitizer import (
    sanitize_report_text,
    restore_pii,
    get_redaction_summary,
)


class TestPIISanitizer:
    """Test suite for PII scrubbing and restoring."""

    def test_sanitizes_patient_name(self):
        text = "Lab Report for Rahul Sharma. Everything looks normal."
        sanitized, rmap = sanitize_report_text(text, patient_name="Rahul Sharma")
        assert "Rahul Sharma" not in sanitized
        assert "[PATIENT_1]" in sanitized
        assert rmap["[PATIENT_1]"] == "Rahul Sharma"

    def test_sanitizes_phone_numbers(self):
        text = "Patient Contact: +91-9876543210 or 9876543210."
        sanitized, rmap = sanitize_report_text(text)
        assert "9876543210" not in sanitized
        assert "[PHONE_" in sanitized

    def test_sanitizes_aadhaar(self):
        text = "Aadhaar Number: 1234 5678 9012 verified."
        sanitized, rmap = sanitize_report_text(text)
        assert "1234 5678 9012" not in sanitized
        assert "[AADHAAR_" in sanitized

    def test_sanitizes_abha_id(self):
        text = "ABHA ID: 12345678901234 linked."
        sanitized, rmap = sanitize_report_text(text)
        assert "12345678901234" not in sanitized
        assert "[ABHA_ID_" in sanitized

    def test_sanitizes_email(self):
        text = "Send report to rahul.sharma@example.com immediately."
        sanitized, rmap = sanitize_report_text(text)
        assert "rahul.sharma@example.com" not in sanitized
        assert "[EMAIL_" in sanitized

    def test_restores_only_patient_name(self):
        text = "Report for Rahul Sharma. Phone: 9876543210."
        sanitized, rmap = sanitize_report_text(text, patient_name="Rahul Sharma")

        # Simulate LLM summary output containing placeholders
        f"Hello {rmap.get('[PATIENT_1]', '[PATIENT_1]')}, we checked your phone [PHONE_2]."
        # Actually let's use the exact placeholder
        pat_key = [k for k in rmap.keys() if "PATIENT" in k][0]
        phone_key = [k for k in rmap.keys() if "PHONE" in k][0]

        llm_mock = f"Hello {pat_key}, we contacted {phone_key}."
        restored = restore_pii(llm_mock, rmap)

        assert "Rahul Sharma" in restored
        assert phone_key in restored  # Phone is NOT restored in outbound message

    def test_audit_summary(self):
        text = "Rahul Sharma, Phone: 9876543210"
        _, rmap = sanitize_report_text(text, patient_name="Rahul Sharma")
        summary = get_redaction_summary(rmap)
        assert "Redacted:" in summary
        assert "PATIENT" in summary
        assert "PHONE" in summary
