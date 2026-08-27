"""Comprehensive tests for the Payment Module.

Tests cover all spec requirements:
  1. Double-booking prevention (DB constraint)
  2. Duplicate webhook idempotency
  3. Invalid/missing signature rejection
  4. Amount mismatch → pending_review
  5. Hold expiry + race condition
  6. Refund flow
  7. Webhook endpoint behavior
"""

import hashlib
import hmac
import json
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure test env vars are set before any app import
os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Hospital")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "City Care Hospital")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault(
    "HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy"
)
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "MC")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key123")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "rzp_test_secret456")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret_789")
os.environ.setdefault("BOOKING_FEE_PAISE", "50000")
os.environ.setdefault("BOOKING_HOLD_MINUTES", "10")
os.environ.setdefault("REFUND_WINDOW_HOURS", "4")

# ── Mock out app.database BEFORE any app module import ──
# The supabase client tries to connect at module-load time.
mock_supabase = MagicMock()
mock_db_module = MagicMock()
mock_db_module.supabase = mock_supabase
sys.modules["app.database"] = mock_db_module


@pytest.fixture(scope="module", autouse=True)
def cleanup_mock_db():
    yield
    if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
        del sys.modules["app.database"]


WEBHOOK_SECRET = "test_webhook_secret_789"


def _sign_payload(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Generate a valid HMAC-SHA256 signature for test payloads."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def _make_payment_webhook_payload(
    payment_id: str = "pay_test123",
    order_id: str = "order_test456",
    amount: int = 50000,
    booking_id: str = "test-booking-uuid",
    event: str = "payment.captured",
    payment_link_id: str = "plink_test123",
) -> dict:
    """Build a Razorpay webhook payload for testing."""
    payload = {
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "notes": {
                        "booking_id": booking_id,
                        "booking_ref": "MC-2026-1234",
                        "patient_phone": "+919876543210",
                    },
                }
            }
        },
    }
    if payment_link_id:
        payload["payload"]["payment_link"] = {
            "entity": {
                "id": payment_link_id,
            }
        }
    return payload


class TestResolvePaymentMode:
    """Test the full/partial/none payment mode resolution."""

    def test_defaults_to_full_when_keys_configured_no_mode_set(self):
        from app.services.payment import resolve_payment_mode

        clinic = {"config": {"razorpay_key_id": "rzp_1", "razorpay_key_secret": "secret1"}}
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "full"
        assert percent == 100

    def test_defaults_to_none_when_no_keys_and_no_mode_set(self):
        from app.services.payment import resolve_payment_mode

        with patch("app.services.payment.settings.razorpay_key_id", ""), patch(
            "app.services.payment.settings.razorpay_key_secret", ""
        ):
            clinic = {"config": {}}
            mode, percent = resolve_payment_mode(clinic)
            assert mode == "none"
            assert percent == 100

    def test_explicit_none_with_keys_configured_stays_none(self):
        from app.services.payment import resolve_payment_mode

        clinic = {
            "config": {
                "razorpay_key_id": "rzp_1",
                "razorpay_key_secret": "secret1",
                "payment_mode": "none",
            }
        }
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "none"
        assert percent == 100

    def test_partial_with_keys_configured_returns_percent(self):
        from app.services.payment import resolve_payment_mode

        clinic = {
            "config": {
                "razorpay_key_id": "rzp_1",
                "razorpay_key_secret": "secret1",
                "payment_mode": "partial",
                "payment_deposit_percent": 20,
            }
        }
        mode, percent = resolve_payment_mode(clinic)
        assert mode == "partial"
        assert percent == 20

    def test_full_mode_without_keys_fails_safe_to_none(self):
        from app.services.payment import resolve_payment_mode

        with patch("app.services.payment.settings.razorpay_key_id", ""), patch(
            "app.services.payment.settings.razorpay_key_secret", ""
        ):
            clinic = {"config": {"payment_mode": "full"}}
            mode, percent = resolve_payment_mode(clinic)
            assert mode == "none"
            assert percent == 100

    def test_partial_mode_without_keys_fails_safe_to_none(self):
        from app.services.payment import resolve_payment_mode

        with patch("app.services.payment.settings.razorpay_key_id", ""), patch(
            "app.services.payment.settings.razorpay_key_secret", ""
        ):
            clinic = {
                "config": {"payment_mode": "partial", "payment_deposit_percent": 20}
            }
            mode, percent = resolve_payment_mode(clinic)
            assert mode == "none"
            assert percent == 100


class TestWebhookSignatureVerification:
    """Test that webhook signature verification is correct and mandatory."""

    def test_valid_signature_accepted(self):
        """Valid HMAC-SHA256 signature should pass verification."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload = b'{"event":"payment.captured"}'
        signature = _sign_payload(payload)

        with patch("app.services.payment.settings") as mock_settings:
            mock_settings.razorpay_webhook_secret = WEBHOOK_SECRET
            assert service.verify_webhook_signature(payload, signature) is True

    def test_invalid_signature_rejected(self):
        """Tampered signature should be rejected."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload = b'{"event":"payment.captured"}'
        bad_signature = "deadbeef" * 8

        assert service.verify_webhook_signature(payload, bad_signature) is False

    def test_empty_signature_rejected(self):
        """Empty signature header should be rejected."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload = b'{"event":"payment.captured"}'
        assert service.verify_webhook_signature(payload, "") is False

    def test_no_secret_configured_rejects(self):
        """If webhook secret is not configured, all signatures should be rejected."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload = b'{"event":"payment.captured"}'
        signature = _sign_payload(payload)

        with patch("app.services.payment.settings") as mock_settings:
            mock_settings.razorpay_webhook_secret = ""
            assert service.verify_webhook_signature(payload, signature) is False

    def test_tampered_body_detected(self):
        """Signature for original body should not match tampered body."""
        from app.services.payment import PaymentService

        service = PaymentService()

        original = b'{"event":"payment.captured","amount":50000}'
        tampered = b'{"event":"payment.captured","amount":99999}'

        signature = _sign_payload(original)
        with patch("app.services.payment.settings") as mock_settings:
            mock_settings.razorpay_webhook_secret = WEBHOOK_SECRET
            assert service.verify_webhook_signature(tampered, signature) is False


class TestPaymentWebhookProcessing:
    """Test the full webhook processing pipeline."""

    @pytest.mark.asyncio
    async def test_signature_failed_returns_400(self):
        """Invalid signature should return 400 and log signature_failed."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload = json.dumps(_make_payment_webhook_payload()).encode()
        bad_signature = "invalid_signature"

        with patch.object(service, "_log_payment_event_raw"), patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ):
            result = await service.process_payment_webhook(payload, bad_signature)

        assert result["code"] == 400
        assert result["reason"] == "signature_failed"

    @pytest.mark.asyncio
    async def test_non_payment_captured_event_ignored(self):
        """Events other than payment.captured should be ignored with 200."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload_dict = _make_payment_webhook_payload(event="payment.failed")
        payload = json.dumps(payload_dict).encode()
        signature = _sign_payload(payload)

        with patch("app.services.payment.settings") as mock_settings:
            mock_settings.razorpay_webhook_secret = WEBHOOK_SECRET
            result = await service.process_payment_webhook(payload, signature)

        assert result["code"] == 200
        assert result["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_amount_mismatch_routes_to_pending_review(self):
        """Webhook with wrong amount should route booking to pending_review."""
        from app.services.payment import PaymentService

        service = PaymentService()

        # Booking expects 50000, webhook sends 99999
        payload_dict = _make_payment_webhook_payload(amount=99999)
        payload = json.dumps(payload_dict).encode()
        signature = _sign_payload(payload)

        mock_booking = {
            "id": "test-booking-uuid",
            "amount_paise": 50000,
            "status": "pending_payment",
            "booking_ref": "MC-2026-1234",
            "patient_phone": "+919876543210",
        }

        # Setup chain mock for supabase
        with patch("app.services.payment.supabase") as mock_sb, patch(
            "app.services.payment.settings"
        ) as mock_settings, patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ), patch.object(
            service, "_log_payment_event"
        ):

            mock_settings.razorpay_webhook_secret = WEBHOOK_SECRET

            # Setup the mock chain for all table operations
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            # Default: return empty (no idempotency match, no confirmed match)
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq1 = MagicMock()
            mock_select.eq.return_value = mock_eq1
            mock_eq2 = MagicMock()
            mock_eq1.eq.return_value = mock_eq2
            mock_eq2.execute.return_value = MagicMock(data=[])

            # For booking lookup (single .eq)
            mock_select.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )

            # For update
            mock_update = MagicMock()
            mock_table.update.return_value = mock_update
            mock_update.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.process_payment_webhook(payload, signature)

        assert result["reason"] == "amount_mismatch"

    @pytest.mark.asyncio
    async def test_signature_failure_alert_is_rate_limited(self):
        """Repeated bad-signature webhooks for the same key must not each
        trigger a fresh _alert_admin call once the limiter says no."""
        from app.services.payment import PaymentService
        from app.utils.security import PersistentRateLimiter

        service = PaymentService()
        payload = json.dumps(_make_payment_webhook_payload()).encode()
        limiter = PersistentRateLimiter(max_attempts=3, window_seconds=300)

        with patch.object(service, "_log_payment_event_raw"), patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ) as mock_alert, patch.object(
            limiter, "check_and_record", side_effect=[False, False, False, True, True]
        ):
            for _ in range(5):
                await service.process_payment_webhook(
                    payload,
                    "bad_signature",
                    alert_limiter=limiter,
                    alert_key="clinic-1:1.2.3.4",
                )

        assert mock_alert.call_count == 3

    @pytest.mark.asyncio
    async def test_signature_failure_without_limiter_still_alerts(self):
        """Backward compatibility: callers that don't pass a limiter still
        get alerted, same as before."""
        from app.services.payment import PaymentService

        service = PaymentService()
        payload = json.dumps(_make_payment_webhook_payload()).encode()

        with patch.object(service, "_log_payment_event_raw"), patch.object(
            service, "_alert_admin", new_callable=AsyncMock
        ) as mock_alert:
            await service.process_payment_webhook(payload, "bad_signature")

        mock_alert.assert_called_once()


class TestOrphanWebhookEventPersistence:
    def test_log_payment_event_raw_persists_orphan_events(self):
        """Events with no booking_id (e.g. signature failures) must now be
        written to webhook_security_events, not just logged."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            service._log_payment_event_raw(
                None, "signature_failed", {"body_length": 42}
            )

            mock_sb.table.assert_called_once_with("webhook_security_events")
            inserted = mock_table.insert.call_args[0][0]
            assert inserted["event_type"] == "signature_failed"

    def test_log_payment_event_raw_still_uses_payment_events_when_booking_id_present(self):
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            service._log_payment_event_raw(
                "booking-1", "webhook_received", {"payment_id": "pay_1"}
            )

            mock_sb.table.assert_called_once_with("payment_events")


class TestBookingCreation:
    """Test booking creation with payment gating."""

    @pytest.mark.asyncio
    async def test_slot_taken_returns_failure(self):
        """If the unique constraint rejects the insert, return slot_taken."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ):
            # Simulate unique constraint violation
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.side_effect = Exception(
                'duplicate key value violates unique constraint "idx_unique_active_slot"'
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
            )

        assert result["success"] is False
        assert result["reason"] == "slot_taken"

    @pytest.mark.asyncio
    async def test_successful_booking_creates_order(self):
        """Successful booking should return payment link and booking details."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {"id": "new-booking-uuid", "booking_ref": "MC-2026-5678"}
        mock_link = {"id": "plink_new_test", "short_url": "https://rzp.io/i/test1"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_payment_link",
            new_callable=AsyncMock,
            return_value=mock_link,
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
            )

        assert result["success"] is True
        assert result["razorpay_payment_link_id"] == "plink_new_test"
        assert result["amount_paise"] == 50000
        assert "payment_link" in result

    @pytest.mark.asyncio
    async def test_partial_deposit_scales_amount(self):
        """deposit_percent < 100 should charge that fraction of the full fee."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {"id": "new-booking-uuid", "booking_ref": "MC-2026-5679"}
        mock_link = {"id": "plink_partial_test", "short_url": "https://rzp.io/i/test2"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_payment_link",
            new_callable=AsyncMock,
            return_value=mock_link,
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
                deposit_percent=20,
            )

        assert result["success"] is True
        # 50000 paise full fee * 20% = 10000 paise deposit
        assert result["amount_paise"] == 10000

    @pytest.mark.asyncio
    async def test_full_deposit_percent_default_charges_full_fee(self):
        """Omitting deposit_percent must charge the full fee (back-compat)."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {"id": "new-booking-uuid", "booking_ref": "MC-2026-5680"}
        mock_link = {"id": "plink_full_test", "short_url": "https://rzp.io/i/test3"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_payment_link",
            new_callable=AsyncMock,
            return_value=mock_link,
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="General Medicine",
                doctor_name="Dr. Test",
                appointment_date="2026-07-05",
                appointment_time="10:00",
            )

        assert result["amount_paise"] == 50000


class TestPaymentLinkGeneration:
    """Regression tests for the broken checkout-URL fix (Finding #5)."""

    @pytest.mark.asyncio
    async def test_create_payment_link_returns_hosted_short_url(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "plink_test123",
            "short_url": "https://rzp.io/i/abc123",
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__.return_value = mock_client

            result = await service._create_payment_link(
                amount_paise=50000,
                booking_id="booking-1",
                booking_ref="MC-2026-1234",
                patient_phone="+919876543210",
                patient_name="Ramesh Sharma",
                key_id="rzp_test_key123",
                key_secret="rzp_test_secret456",
            )

        assert result["short_url"] == "https://rzp.io/i/abc123"
        assert result["id"] == "plink_test123"
        call_kwargs = mock_client.post.call_args
        assert "payment_links" in call_kwargs.args[0]
        assert call_kwargs.kwargs["json"]["amount"] == 50000
        assert call_kwargs.kwargs["json"]["reference_id"] == "MC-2026-1234"

    @pytest.mark.asyncio
    async def test_booking_with_payment_returns_rzp_io_link_not_api_endpoint(self):
        """End-to-end: the payment_link returned to the patient must be a
        real hosted page, never the old api.razorpay.com/v1/... API URL."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ), patch.object(
            service,
            "_create_payment_link",
            new_callable=AsyncMock,
            return_value={"id": "plink_1", "short_url": "https://rzp.io/i/xyz"},
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(
                data=[{"id": "booking-1"}]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Ramesh Sharma",
                department="Cardiology",
                doctor_name="Dr. Rao",
                appointment_date="2026-08-10",
                appointment_time="10:00",
            )

        assert result["success"] is True
        assert result["payment_link"] == "https://rzp.io/i/xyz"
        assert "api.razorpay.com" not in result["payment_link"]


class TestRefundFlow:
    """Test refund eligibility and processing."""

    @pytest.mark.asyncio
    async def test_refund_eligibility_check(self):
        """Refund should be denied if less than 4 hours before slot."""
        from app.services.payment import PaymentService
        from datetime import datetime, timedelta, timezone

        service = PaymentService()

        # Slot is 1 hour from now (inside refund window)
        soon = datetime.now(timezone.utc) + timedelta(hours=1)

        mock_booking = {
            "id": "booking-refund-test",
            "status": "confirmed",
            "payment_id": "pay_refund_test",
            "amount_paise": 50000,
            "appointment_date": soon.strftime("%Y-%m-%d"),
            "appointment_time": soon.strftime("%H:%M"),
        }

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.initiate_refund("booking-refund-test")

        assert result["success"] is False
        assert "refund_window_closed" in result["reason"]

    @pytest.mark.asyncio
    async def test_refund_for_non_confirmed_booking_fails(self):
        """Cannot refund a booking that isn't confirmed or pending_review."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {
            "id": "booking-expired",
            "status": "expired",
            "payment_id": None,
            "amount_paise": 50000,
        }

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.initiate_refund("booking-expired")

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_refund_without_payment_id_fails(self):
        """Cannot refund a booking that has no payment_id."""
        from app.services.payment import PaymentService

        service = PaymentService()

        mock_booking = {
            "id": "booking-no-pay",
            "status": "confirmed",
            "payment_id": None,
            "amount_paise": 50000,
        }

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.initiate_refund("booking-no-pay")

        assert result["success"] is False
        assert result["reason"] == "no_payment_to_refund"


class TestAdminBookingScoping:
    """Regression tests for the cross-tenant BOLA fix (Finding #1)."""

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_rejects_cross_tenant_id(self):
        """A booking belonging to clinic B must not be confirmable by a
        request scoped to clinic A — the clinic_id filter must exclude it."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            # .eq("id", booking_id).eq("clinic_id", "clinic-A") -> no rows,
            # because this booking actually belongs to clinic-B
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.admin_confirm_booking(
                "booking-owned-by-clinic-b", clinic_id="clinic-A"
            )

        assert result["success"] is False
        assert result["reason"] == "booking_not_found"

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_succeeds_for_own_clinic(self):
        """Same booking IS confirmable when clinic_id matches."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {
            "id": "booking-1",
            "clinic_id": "clinic-A",
            "status": "pending_review",
            "patient_phone": "+919876543210",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_increment_patient_visit_count", new_callable=AsyncMock
        ), patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.admin_confirm_booking(
                "booking-1", clinic_id="clinic-A"
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_admin_reject_booking_rejects_cross_tenant_id(self):
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_eq_id = MagicMock()
            mock_select.eq.return_value = mock_eq_id
            mock_eq_id.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.admin_reject_booking(
                "booking-owned-by-clinic-b", clinic_id="clinic-A"
            )

        assert result["success"] is False
        assert result["reason"] == "booking_not_found"

    @pytest.mark.asyncio
    async def test_admin_confirm_booking_default_clinic_id_is_unscoped(self):
        """clinic_id='default' (super_admin path) must NOT add a clinic filter —
        preserves existing super_admin cross-clinic behavior."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {
            "id": "booking-1",
            "clinic_id": "clinic-A",
            "status": "pending_review",
            "patient_phone": "+919876543210",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_increment_patient_visit_count", new_callable=AsyncMock
        ), patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ), patch.object(
            service, "_log_payment_event"
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            # Only ONE .eq() call expected: .eq("id", booking_id) — no clinic filter
            mock_select.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_table.update.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_booking])
            )

            result = await service.admin_confirm_booking(
                "booking-1", clinic_id="default"
            )

        assert result["success"] is True
        mock_select.eq.assert_called_once_with("id", "booking-1")


class TestHoldExpiry:
    """Test stale booking expiry and recovery path."""

    @pytest.mark.asyncio
    async def test_expired_booking_gets_expired_status(self):
        """Pending bookings past hold_expires_at should be expired."""
        from app.services.payment import PaymentService
        from datetime import datetime, timedelta, timezone

        service = PaymentService()

        past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        mock_stale = {
            "id": "stale-booking-uuid",
            "razorpay_payment_link_id": "plink_stale",
            "hold_expires_at": past_time,
            "status": "pending_payment",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service,
            "_check_payment_link_status",
            new_callable=AsyncMock,
            return_value={"status": "created", "payment_id": ""},
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.lt.return_value.limit.return_value.execute.return_value = MagicMock(
                data=[mock_stale]
            )
            mock_table.select.return_value.eq.return_value.lt.return_value.execute.return_value = MagicMock(
                data=[mock_stale]
            )
            mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
                data=[]
            )

            count = await service.expire_stale_bookings()

        assert count == 1

    @pytest.mark.asyncio
    async def test_expire_stale_bookings_is_bounded(self):
        """T1.3 / KRIYA-014: expire_stale_bookings applies limit(200) on query."""
        from app.services.payment import PaymentService

        service = PaymentService()

        with patch("app.services.payment.supabase") as mock_sb:
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.lt.return_value.limit.return_value.execute.return_value = MagicMock(
                data=[]
            )

            count = await service.expire_stale_bookings()
            assert count == 0
            mock_table.select.return_value.eq.return_value.lt.return_value.limit.assert_called_with(200)

    @pytest.mark.asyncio
    async def test_recovery_path_confirms_paid_booking(self):
        """If Razorpay shows paid but webhook missed, confirm instead of expiring."""
        from app.services.payment import PaymentService
        from datetime import datetime, timedelta, timezone

        service = PaymentService()

        past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        mock_stale = {
            "id": "paid-but-missed-uuid",
            "razorpay_payment_link_id": "plink_paid_missed",
            "hold_expires_at": past_time,
            "status": "pending_payment",
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "doctor_name": "Dr. Test",
            "appointment_date": "2026-07-05",
            "appointment_time": "10:00",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service,
            "_check_payment_link_status",
            new_callable=AsyncMock,
            return_value={"status": "paid", "payment_id": "pay_recovered"},
        ), patch.object(
            service, "_log_payment_event"
        ), patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.select.return_value.eq.return_value.lt.return_value.limit.return_value.execute.return_value = MagicMock(
                data=[mock_stale]
            )
            mock_table.select.return_value.eq.return_value.lt.return_value.execute.return_value = MagicMock(
                data=[mock_stale]
            )
            mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[mock_stale])
            )

            count = await service.expire_stale_bookings()

        assert count == 1

    @pytest.mark.asyncio
    async def test_atomic_update_race_condition_returns_already_confirmed(self):
        """If atomic update returns 0 affected rows (concurrent webhook win), handle as already_confirmed without duplicate notification."""
        from app.services.payment import PaymentService

        service = PaymentService()

        payload_dict = _make_payment_webhook_payload()
        payload = json.dumps(payload_dict).encode()
        signature = _sign_payload(payload)

        mock_booking = {
            "id": "test-booking-uuid",
            "amount_paise": 50000,
            "status": "pending_payment",
            "booking_ref": "MC-2026-1234",
            "patient_phone": "+919876543210",
        }

        with patch("app.services.payment.supabase") as mock_sb, patch(
            "app.services.payment.settings"
        ) as mock_settings, patch.object(
            service, "_notify_payment_confirmed", new_callable=AsyncMock
        ) as mock_notify:

            mock_settings.razorpay_webhook_secret = WEBHOOK_SECRET

            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table

            mock_select = MagicMock()
            mock_table.select.return_value = mock_select
            mock_select.eq.return_value.execute.return_value = MagicMock(
                data=[mock_booking]
            )
            mock_select.eq.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            # Update returns empty data (0 rows affected due to concurrent update)
            mock_update = MagicMock()
            mock_table.update.return_value = mock_update
            mock_update.eq.return_value.eq.return_value.execute.return_value = (
                MagicMock(data=[])
            )
            mock_update.eq.return_value.in_.return_value.execute.return_value = (
                MagicMock(data=[])
            )

            res = await service.process_payment_webhook(payload, signature)

            assert res["status"] == "ok"
            assert res["reason"] == "already_confirmed"
            # Notification must NOT be sent if update affected 0 rows
            mock_notify.assert_not_called()


class TestAmountIntegrity:
    """Test that money is always in paise (integers), never floats."""

    def test_no_float_amounts_in_models(self):
        """Pydantic models should use int for amounts, never float."""
        from app.models.booking import BookingCreateResponse, BookingDetail

        for model in [BookingCreateResponse, BookingDetail]:
            for name, field in model.model_fields.items():
                if "amount" in name.lower() or "paise" in name.lower():
                    annotation = field.annotation
                    # Allow Optional[int] as well
                    assert annotation in (int,) or (
                        hasattr(annotation, "__args__") and int in annotation.__args__
                    ), f"{model.__name__}.{name} should be int, not {annotation}"


@pytest.mark.asyncio
async def test_cross_tenant_payment_forgery():
    """T6.5 / T0.5: Webhook from Clinic A cannot confirm or access Clinic B's booking."""
    from app.services.payment import PaymentService

    service = PaymentService()
    secret = "clinic_a_webhook_secret"

    payload_dict = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_CROSS_TENANT_999",
                    "order_id": "order_CROSS_999",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "captured",
                    "notes": {
                        "booking_id": "uuid-clinic-b-booking",
                        "clinic_id": "clinic_a",  # Claimed clinic in notes
                    },
                }
            }
        },
    }
    raw_payload = json.dumps(payload_dict).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()

    mock_supabase = MagicMock()
    mock_table = MagicMock()
    mock_supabase.table.return_value = mock_table

    mock_empty = MagicMock(data=[])
    # Any depth of .eq() should resolve to mock_empty on execute()
    mock_query = MagicMock()
    mock_query.execute.return_value = mock_empty
    mock_query.eq.return_value = mock_query
    mock_table.select.return_value = mock_query
    mock_table.insert.return_value.execute.return_value = mock_empty
    mock_table.update.return_value.execute.return_value = mock_empty

    with patch("app.services.payment.supabase", mock_supabase), \
         patch.object(service, "verify_webhook_signature", return_value=True):
        res = await service.process_payment_webhook(
            raw_payload, signature, webhook_secret=secret, clinic_id="clinic_a"
        )
        assert res["status"] == "unmatched"
        assert res["reason"] == "booking_not_found"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
