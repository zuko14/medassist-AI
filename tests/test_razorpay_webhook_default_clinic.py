"""Tests for Razorpay webhook endpoint with default clinic routing and UUID resilience."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.payment import payment_service


WEBHOOK_SECRET = "test_webhook_secret_789"


def _sign(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_razorpay_webhook_with_default_path():
    """Verify POST /webhooks/razorpay and /webhooks/razorpay/default successfully process payments."""
    mock_booking = {
        "id": "b-1234",
        "clinic_id": "11111111-1111-1111-1111-111111111111",
        "status": "pending_payment",
        "amount_paise": 50000,
        "patient_phone": "+919876543210",
        "doctor_name": "Dr. T Rajsekhar",
        "department": "Orthopaedics",
        "appointment_date": "2026-08-25",
        "appointment_time": "09:30",
        "booking_ref": "MC-2026-9999",
        "razorpay_payment_link_id": "plink_test999",
    }

    mock_clinic = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Test Hospital",
        "whatsapp_number": "+919876543210",
        "config": {
            "razorpay_webhook_secret": WEBHOOK_SECRET,
        },
    }

    payload = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_test999",
                    "amount": 50000,
                    "notes": {
                        "booking_id": "b-1234",
                        "booking_ref": "MC-2026-9999",
                    },
                }
            },
            "payment_link": {
                "entity": {
                    "id": "plink_test999",
                }
            }
        },
    }).encode()

    sig = _sign(payload, WEBHOOK_SECRET)

    with patch("app.services.tenant.supabase") as mock_tenant_sb, \
         patch("app.services.payment.supabase") as mock_pay_sb, \
         patch("app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock) as mock_send_text, \
         patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", new_callable=AsyncMock) as mock_send_buttons, \
         patch("app.services.conversation.conversation_manager.update_state", new_callable=AsyncMock) as mock_update_state:

        # Mock clinic lookup
        mock_clinic_table = MagicMock()
        mock_clinic_table.select.return_value.eq.return_value.neq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[mock_clinic])
        mock_clinic_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_clinic])
        mock_tenant_sb.table.return_value = mock_clinic_table

        # Mock appointment lookup & update in payment service
        mock_appt_table = MagicMock()
        # Idempotency query (3 .eq calls: payment_id, status, clinic_id) -> not yet confirmed
        mock_appt_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        # Scoped booking lookup (2 .eq calls: clinic_id, razorpay_payment_link_id) -> returns mock_booking
        mock_appt_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        mock_appt_table.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[mock_booking])
        mock_appt_table.select.return_value.execute.return_value = MagicMock(data=[mock_booking])
        # Update -> successful confirmation
        mock_appt_table.update.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[{**mock_booking, "status": "confirmed"}])
        mock_pay_sb.table.return_value = mock_appt_table

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Test POST /webhooks/razorpay/default
            resp_default = await ac.post(
                "/webhooks/razorpay/default",
                content=payload,
                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp_default.status_code == 200
            assert resp_default.json() == {"status": "ok"}
            mock_send_text.assert_called_once()
            assert "Payment Confirmed" in mock_send_text.call_args[0][2]
            assert "Dr. T Rajsekhar" in mock_send_text.call_args[0][2]

            mock_send_text.reset_mock()

            # 2. Test POST /webhooks/razorpay (no path param)
            resp_global = await ac.post(
                "/webhooks/razorpay",
                content=payload,
                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp_global.status_code == 200
            assert resp_global.json() == {"status": "ok"}
            mock_send_text.assert_called_once()


@pytest.mark.asyncio
async def test_get_clinic_by_id_none_or_default_resilience():
    """Verify get_clinic_by_id handles None, empty string, and 'default' without raising exceptions."""
    from app.services.tenant import get_clinic_by_id

    mock_clinic = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Fallback Clinic",
    }

    with patch("app.services.tenant.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_table.select.return_value.eq.return_value.neq.return_value.order.return_value.limit.return_value.execute.return_value = MagicMock(data=[mock_clinic])
        mock_sb.table.return_value = mock_table

        res_none = await get_clinic_by_id(None)
        assert res_none["id"] == "11111111-1111-1111-1111-111111111111"

        res_default = await get_clinic_by_id("default")
        assert res_default["id"] == "11111111-1111-1111-1111-111111111111"

        res_empty = await get_clinic_by_id("   ")
        assert res_empty["id"] == "11111111-1111-1111-1111-111111111111"
