"""A patient who paid a link from a PHONE booking has no open WhatsApp window:
send_text returns False. The confirmation must then go as the approved
appointment_confirmation template, and a template refusal must alert the admin
instead of being recorded as delivered."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.payment import PaymentService

BOOKING = {"id": "b1", "clinic_id": "c1", "patient_phone": "+919876543210", "patient_name": "Ravi",
           "doctor_name": "Dr. Rao", "department": "Cardiology", "appointment_date": "2026-10-07",
           "appointment_time": "10:30", "amount_paise": 80000, "booking_ref": "KR-1",
           "booking_type": "consultation", "payment_id": "pay_1"}
CLINIC = {"id": "c1", "name": "ABC Hospitals", "config": {"admin_phone": "+919999988888"}}


async def _run(send_text_result, template_result):
    svc = PaymentService()
    send_text = AsyncMock(return_value=send_text_result)
    send_template = AsyncMock(return_value=template_result)
    with patch("app.services.tenant.get_clinic_by_id", new=AsyncMock(return_value=CLINIC)), \
         patch("app.database.get_patient_by_phone", new=AsyncMock(return_value={"language": "en"})), \
         patch("app.services.whatsapp.whatsapp_service.send_text", new=send_text), \
         patch("app.services.whatsapp.whatsapp_service.send_template", new=send_template), \
         patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", new=AsyncMock()), \
         patch("app.services.conversation.conversation_manager.update_state", new=AsyncMock()), \
         patch("app.services.payment.supabase", MagicMock()), \
         patch.object(svc, "_alert_admin", new=AsyncMock()) as alert, \
         patch("app.database.log_analytics_event", new=AsyncMock()):
        await svc._notify_payment_confirmed(dict(BOOKING))
    return send_text, send_template, alert


def _delivery_failed(alert):
    return any("Delivery Failed" in str(c) for c in alert.await_args_list)


@pytest.mark.asyncio
async def test_closed_window_uses_template_and_no_alert():
    _, send_template, alert = await _run(False, True)
    assert send_template.await_args.args[2] == "appointment_confirmation"
    params = send_template.await_args.kwargs["components"][0]["parameters"]
    assert [p["text"] for p in params][:2] == ["Dr. Rao", "Cardiology"]
    assert not _delivery_failed(alert)


@pytest.mark.asyncio
async def test_template_refused_alerts_admin():
    _, send_template, alert = await _run(False, False)
    send_template.assert_awaited_once()
    assert _delivery_failed(alert)


@pytest.mark.asyncio
async def test_open_window_never_sends_template():
    _, send_template, alert = await _run(True, True)
    send_template.assert_not_awaited()
    assert not _delivery_failed(alert)
