"""Test suite for payment confirmation delivery to patients and admin dashboard synchronization."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.payment import PaymentService


@pytest.mark.asyncio
async def test_notify_payment_confirmed_sends_multilingual_whatsapp():
    """Verify payment confirmation translates properly for patient language (Hindi, Telugu, English)."""
    service = PaymentService()

    booking = {
        "id": "book-123",
        "clinic_id": "clinic-test-uuid",
        "patient_phone": "+919876543210",
        "patient_name": "Ravi Kumar",
        "department": "Cardiology",
        "doctor_name": "Dr. Naidu",
        "appointment_date": "2026-09-01",
        "appointment_time": "10:30",
        "amount_paise": 50000,
        "booking_ref": "KRI-9988",
        "payment_id": "pay_test_123",
    }

    mock_clinic = {
        "id": "clinic-test-uuid",
        "name": "Kriya Heart Care",
        "whatsapp_number": "+919490386668",
        "config": {"admin_phone": "+919999988888"},
    }

    # Test Telugu language
    with patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=mock_clinic)), \
         patch("app.database.get_patient_by_phone", new=AsyncMock(return_value={"language": "te"})), \
         patch("app.services.whatsapp.whatsapp_service.send_text", new=AsyncMock()) as mock_send_text, \
         patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", new=AsyncMock()) as mock_send_btns, \
         patch("app.services.conversation.conversation_manager.update_state", new=AsyncMock()) as mock_update_state, \
         patch("app.services.payment.supabase") as mock_sb, \
         patch("app.database.log_analytics_event", new=AsyncMock()) as mock_analytics:

        mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{"id": "notif-1"}])

        await service._notify_payment_confirmed(booking)

        # Check that patient received Telugu confirmation
        assert "చెల్లింపు నిర్ధారించబడింది" in mock_send_text.call_args_list[0][0][2]
        assert "KRI-9988" in mock_send_text.call_args_list[0][0][2]
        assert "₹500" in mock_send_text.call_args_list[0][0][2]

        # Check admin alert was sent
        admin_calls = [c for c in mock_send_text.call_args_list if c[0][1] == "+919999988888"]
        assert len(admin_calls) >= 1
        assert "KRI-9988" in admin_calls[0][0][2]

        # Check interactive buttons sent
        mock_send_btns.assert_called_once()

        # Check conversation state updated to main_menu
        mock_update_state.assert_called_once_with(mock_clinic, "+919876543210", "main_menu")

        # Check analytics event logged
        mock_analytics.assert_called_once_with(
            "clinic-test-uuid", "+919876543210", "appointment_booked", department="Cardiology"
        )


@pytest.mark.asyncio
async def test_notify_payment_confirmed_retries_on_failure():
    """Verify that a temporary network failure on first send triggers a retry."""
    service = PaymentService()

    booking = {
        "id": "book-456",
        "clinic_id": "clinic-test-uuid",
        "patient_phone": "+919876543210",
        "patient_name": "Ravi Kumar",
        "department": "General Medicine",
        "doctor_name": "Dr. Sharma",
        "appointment_date": "2026-09-02",
        "appointment_time": "11:00",
        "amount_paise": 40000,
        "booking_ref": "KRI-4455",
        "payment_id": "pay_test_456",
    }

    mock_clinic = {
        "id": "clinic-test-uuid",
        "name": "Kriya Health",
        "whatsapp_number": "+919490386668",
        "config": {"admin_phone": "+919999988888"},
    }

    # Simulate 1 failure then 1 success
    send_text_mock = AsyncMock(side_effect=[Exception("Meta network timeout"), None, None])

    with patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=mock_clinic)), \
         patch("app.database.get_patient_by_phone", new=AsyncMock(return_value={"language": "en"})), \
         patch("app.services.whatsapp.whatsapp_service.send_text", new=send_text_mock), \
         patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", new=AsyncMock()), \
         patch("app.services.conversation.conversation_manager.update_state", new=AsyncMock()), \
         patch("app.services.payment.supabase") as mock_sb, \
         patch("app.database.log_analytics_event", new=AsyncMock()):

        mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{"id": "notif-1"}])

        await service._notify_payment_confirmed(booking)

        # send_text should have been called 2 times for patient, and 1 for admin
        patient_calls = [c for c in send_text_mock.call_args_list if c[0][1] == "+919876543210"]
        assert len(patient_calls) == 2


@pytest.mark.asyncio
async def test_notify_payment_confirmed_escalates_to_admin_if_patient_send_fails():
    """Verify that if patient WhatsApp message fails after all retries, admin is alerted."""
    service = PaymentService()

    booking = {
        "id": "book-789",
        "clinic_id": "clinic-test-uuid",
        "patient_phone": "+919876543210",
        "patient_name": "Ravi Kumar",
        "department": "General Medicine",
        "doctor_name": "Dr. Sharma",
        "appointment_date": "2026-09-02",
        "appointment_time": "11:00",
        "amount_paise": 40000,
        "booking_ref": "KRI-7788",
        "payment_id": "pay_test_789",
    }

    mock_clinic = {
        "id": "clinic-test-uuid",
        "name": "Kriya Health",
        "whatsapp_number": "+919490386668",
        "config": {"admin_phone": "+919999988888"},
    }

    # Simulate persistent failure for patient phone
    async def mock_send_side_effect(clinic, phone, msg, **kwargs):
        if phone == "+919876543210":
            raise Exception("Persistent Meta API error: user blocked business")
        return None

    send_text_mock = AsyncMock(side_effect=mock_send_side_effect)

    with patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=mock_clinic)), \
         patch("app.database.get_patient_by_phone", new=AsyncMock(return_value={"language": "en"})), \
         patch("app.services.whatsapp.whatsapp_service.send_text", new=send_text_mock), \
         patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", new=AsyncMock()), \
         patch("app.services.conversation.conversation_manager.update_state", new=AsyncMock()), \
         patch("app.services.payment.supabase") as mock_sb, \
         patch("app.database.log_analytics_event", new=AsyncMock()):

        mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[{"id": "notif-1"}])

        await service._notify_payment_confirmed(booking)

        # Check that admin received an alert about the delivery failure
        admin_calls = [c for c in send_text_mock.call_args_list if c[0][1] == "+919999988888"]
        assert len(admin_calls) >= 1
        assert any("Delivery Failed" in c[0][2] for c in admin_calls)


@pytest.mark.asyncio
async def test_webhook_step9_safety_returns_200_even_if_notification_fails():
    """Verify that Step 9 failures in process_payment_webhook do not crash the webhook response."""
    service = PaymentService()

    raw_body = b'{"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_step9_test", "amount": 50000, "notes": {"booking_id": "book-step9"}}}}}'
    signature = "valid_sig"

    mock_booking = {
        "id": "book-step9",
        "clinic_id": "clinic-test-uuid",
        "amount_paise": 50000,
        "status": "pending_payment",
        "patient_phone": "+919876543210",
        "booking_ref": "KRI-1111",
    }

    with patch.object(service, "verify_webhook_signature", return_value=True), \
         patch("app.services.payment.supabase") as mock_sb, \
         patch.object(service, "_increment_patient_visit_count", new=AsyncMock(side_effect=Exception("DB visit count down"))), \
         patch.object(service, "_notify_payment_confirmed", new=AsyncMock(side_effect=Exception("Notification service crash"))):

        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table

        # Idempotency query -> empty
        mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        # Booking query -> mock_booking
        mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        # Update status -> confirmed
        mock_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        mock_table.insert.return_value.execute.return_value = MagicMock(data=[{}])

        result = await service.process_payment_webhook(
            raw_body=raw_body,
            signature=signature,
            webhook_secret="sec",
            clinic_id="clinic-test-uuid",
        )

        assert result["status"] == "ok"
        assert result["code"] == 200
