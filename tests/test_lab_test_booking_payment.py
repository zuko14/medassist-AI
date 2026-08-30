"""Tests for booking_type='lab_test' support in PaymentService."""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
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

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]


class TestLabTestBookingCreation:
    @pytest.mark.asyncio
    async def test_lab_test_booking_uses_lab_test_fee_not_doctor_fee(self):
        """booking_type='lab_test' must price from lab_tests.price_paise, not doctors.consultation_fee."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "lab-booking-uuid", "booking_ref": "MC-2026-9001"}
        mock_link = {"id": "plink_lab_test", "short_url": "https://rzp.io/i/labtest1"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock, return_value=80000
        ) as mock_fee, patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ) as mock_doctor_fee, patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

            result = await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="Lab Test",
                doctor_name=None,
                appointment_date="2026-07-05",
                appointment_time=None,
                booking_type="lab_test",
                lab_test_id="test-uuid-1",
                lab_test_name="Complete Blood Count",
            )

        assert result["success"] is True
        assert result["amount_paise"] == 80000
        mock_fee.assert_called_once_with("test-clinic", "test-uuid-1")
        mock_doctor_fee.assert_not_called()

    @pytest.mark.asyncio
    async def test_lab_test_booking_data_includes_lab_test_fields(self):
        """The inserted row must carry booking_type/lab_test_id/lab_test_name."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "lab-booking-uuid-2", "booking_ref": "MC-2026-9002"}
        mock_link = {"id": "plink_lab_test2", "short_url": "https://rzp.io/i/labtest2"}

        with patch("app.services.payment.supabase") as mock_sb, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock, return_value=30000
        ), patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

            await service.create_booking_with_payment(
                clinic_id="test-clinic",
                patient_phone="+919876543210",
                patient_name="Test Patient",
                department="Lab Test",
                doctor_name=None,
                appointment_date="2026-07-05",
                appointment_time=None,
                booking_type="lab_test",
                lab_test_id="test-uuid-2",
                lab_test_name="Lipid Profile",
            )

            inserted = mock_table.insert.call_args[0][0]
            assert inserted["booking_type"] == "lab_test"
            assert inserted["lab_test_id"] == "test-uuid-2"
            assert inserted["lab_test_name"] == "Lipid Profile"
            assert inserted["doctor_name"] is None
            assert inserted["appointment_time"] is None

    @pytest.mark.asyncio
    async def test_consultation_booking_still_prices_from_doctor_fee(self):
        """Regression: omitting booking_type must keep pricing from the doctor's fee."""
        from app.services.payment import PaymentService

        service = PaymentService()
        mock_booking = {"id": "consult-booking-uuid", "booking_ref": "MC-2026-9003"}
        mock_link = {"id": "plink_consult", "short_url": "https://rzp.io/i/consult1"}

        # get_doctor_by_name must be patched: a consultation booking now
        # requires a resolved doctor_id before the INSERT, because migration
        # 064 keys the slot uniqueness index on doctor_id and a NULL there is
        # an unguarded slot (KA-P0-01). Without this the test made a real
        # network call, got None, and exercised exactly the row shape the
        # fix forbids.
        with patch("app.services.payment.supabase") as mock_sb, patch(
            "app.database.get_doctor_by_name",
            new_callable=AsyncMock,
            return_value={"id": "33333333-4444-5555-6666-777777777777",
                          "name": "Dr. Test", "consultation_fee": 500},
        ), patch.object(
            service, "_get_doctor_fee_paise", new_callable=AsyncMock, return_value=50000
        ) as mock_doctor_fee, patch.object(
            service, "_get_lab_test_fee_paise", new_callable=AsyncMock
        ) as mock_lab_fee, patch.object(
            service, "_create_payment_link", new_callable=AsyncMock, return_value=mock_link
        ), patch.object(service, "_log_payment_event"):
            mock_table = MagicMock()
            mock_sb.table.return_value = mock_table
            mock_table.insert.return_value.execute.return_value = MagicMock(data=[mock_booking])
            mock_table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

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
        mock_doctor_fee.assert_called_once()
        mock_lab_fee.assert_not_called()


class TestNotifyPaymentConfirmedLabTestCopy:
    @pytest.mark.asyncio
    async def test_lab_test_booking_gets_test_copy_not_doctor_copy(self):
        from app.services.payment import PaymentService

        service = PaymentService()
        booking = {
            "clinic_id": "test-clinic",
            "patient_phone": "+919876543210",
            "booking_ref": "MC-2026-9001",
            "booking_type": "lab_test",
            "lab_test_name": "Complete Blood Count",
            "appointment_date": "2026-07-05",
            "amount_paise": 80000,
            "branch_id": None,
        }

        with patch("app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock) as mock_send, patch(
            "app.services.tenant.get_clinic_by_id", new_callable=AsyncMock, return_value={"name": "Accumax Diagnostics", "config": {}}
        ):
            await service._notify_payment_confirmed(booking)

        sent_text = mock_send.call_args[0][2]
        assert "Complete Blood Count" in sent_text
        assert "Doctor" not in sent_text
