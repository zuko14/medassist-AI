"""A treatment booking is an ordinary consultation with a tag: same payment,
same slot guard, same refunds. Bookings without a treatment are unchanged."""

import sys
if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow
from app.services.conversation import ConversationManager
from app.services.payment import PaymentService

PHONE = "+919000000001"
T1 = "22222222-2222-2222-2222-222222222222"
D1 = "11111111-1111-1111-1111-111111111111"


async def _paid_booking(**extra):
    service = PaymentService()
    fake_supabase = MagicMock()
    results = [
        MagicMock(data=[{"consultation_fee": 500}]),        # _get_doctor_fee_paise
        MagicMock(data=[{"id": "b1", "booking_ref": "MC1"}]),  # insert
        MagicMock(data=[]),                                  # store payment link id
    ]
    with patch("app.services.payment.supabase", fake_supabase), \
         patch("app.services.payment.sb", AsyncMock(side_effect=results)), \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test_x", "secret", None)), \
         patch.object(service, "_create_payment_link", AsyncMock(return_value={"id": "plink_1", "short_url": "https://rzp.io/x"})), \
         patch.object(service, "_log_payment_event", AsyncMock()):
        result = await service.create_booking_with_payment(
            clinic_id="c1", patient_phone=PHONE, patient_name="Asha Rao", department="Dermatology",
            doctor_name="Dr. Mehta", appointment_date="2026-10-01", appointment_time="10:00",
            clinic={"id": "c1", "plan": "derma"}, doctor_id=D1, **extra,
        )
    return result, fake_supabase.table.return_value.insert.call_args.args[0]


@pytest.mark.asyncio
async def test_paid_treatment_booking_is_a_tagged_consultation_priced_from_the_doctor():
    result, row = await _paid_booking(treatment_id=T1, treatment_name="Hair PRP Therapy")
    assert result["success"] is True
    assert row["booking_type"] == "consultation"
    assert row["doctor_id"] == D1
    assert row["amount_paise"] == 50000
    assert row["treatment_id"] == T1 and row["treatment_name"] == "Hair PRP Therapy"


@pytest.mark.asyncio
async def test_paid_booking_without_treatment_has_no_new_keys():
    _, row = await _paid_booking()
    assert "treatment_id" not in row and "treatment_name" not in row


@pytest.mark.asyncio
async def test_treatment_is_never_attached_to_a_lab_test_booking():
    service = PaymentService()
    fake_supabase = MagicMock()
    results = [MagicMock(data=[{"id": "b2", "booking_ref": "MC2"}]), MagicMock(data=[])]
    with patch("app.services.payment.supabase", fake_supabase), \
         patch("app.services.payment.sb", AsyncMock(side_effect=results)), \
         patch("app.services.payment.get_razorpay_creds", return_value=("rzp_test_x", "secret", None)), \
         patch.object(service, "_get_lab_test_fee_paise", AsyncMock(return_value=30000)), \
         patch.object(service, "_create_payment_link", AsyncMock(return_value={"id": "plink_2", "short_url": "https://rzp.io/y"})), \
         patch.object(service, "_log_payment_event", AsyncMock()):
        await service.create_booking_with_payment(
            clinic_id="c1", patient_phone=PHONE, patient_name="Asha Rao", department="Lab Test",
            doctor_name=None, appointment_date="2026-10-01", appointment_time=None,
            clinic={"id": "c1", "plan": "ivf"}, booking_type="lab_test", lab_test_id="lt1",
            lab_test_name="AMH", treatment_id=T1, treatment_name="IVF",
        )
    row = fake_supabase.table.return_value.insert.call_args.args[0]
    assert row["booking_type"] == "lab_test" and "treatment_id" not in row


def _confirm_context(**extra):
    return {"doctor_name": "Dr. Mehta", "doctor_id": D1, "department": "Dermatology",
            "appointment_date": "2026-10-01", "appointment_time": "10:00",
            "booking_name": "Asha Rao", **extra}


async def _direct_confirm(context, treatment_row):
    m = ConversationManager()
    m.whatsapp = MagicMock(send_text=AsyncMock(), send_interactive_buttons=AsyncMock(), send_interactive_list=AsyncMock())
    booked = AsyncMock(return_value={"success": False, "reason": "error"})
    with patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)), \
         patch("app.database.get_doctor_by_name", AsyncMock(return_value={"is_active": True})), \
         patch.object(specialty_flow, "get_treatment_by_id", AsyncMock(return_value=treatment_row)), \
         patch("app.services.conversation.book_appointment", booked), \
         patch.object(m, "update_state", AsyncMock()), \
         patch.object(m, "_send_main_menu", AsyncMock()):
        await m._handle_confirming_booking({"id": "c1", "whatsapp_number": "+911"}, PHONE, "Confirm",
                                           "confirm_booking", context, {"id": "p1"}, "en")
    return booked.await_args.args[1]


@pytest.mark.asyncio
async def test_unpaid_treatment_booking_carries_the_tag():
    data = await _direct_confirm(_confirm_context(treatment_id=T1, treatment_name="Chemical Peel"), {"id": T1})
    assert data["treatment_id"] == T1 and data["treatment_name"] == "Chemical Peel"
    assert "booking_type" not in data or data["booking_type"] == "consultation"


@pytest.mark.asyncio
async def test_treatment_deleted_mid_booking_books_the_consultation_untagged():
    data = await _direct_confirm(_confirm_context(treatment_id=T1, treatment_name="Chemical Peel"), None)
    assert "treatment_id" not in data


@pytest.mark.asyncio
async def test_unpaid_booking_without_treatment_is_unchanged():
    data = await _direct_confirm(_confirm_context(), None)
    assert "treatment_id" not in data and "treatment_name" not in data


@pytest.mark.asyncio
async def test_confirmation_screen_names_the_treatment_only_when_present():
    for ctx, expected in ((_confirm_context(treatment_name="Root Canal Treatment"), True), (_confirm_context(), False)):
        m = ConversationManager()
        m.whatsapp = MagicMock(send_interactive_buttons=AsyncMock())
        with patch.object(m, "update_state", AsyncMock()):
            await m._show_booking_confirmation({"id": "c1"}, PHONE, ctx, "en")
        body = m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]
        assert ("Root Canal Treatment" in body) is expected


def test_insights_and_payments_read_the_treatment_name():
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    analytics = (repo / "app" / "services" / "analytics.py").read_text(encoding="utf-8")
    admin = (repo / "app" / "routers" / "admin.py").read_text(encoding="utf-8")
    assert "lab_test_name,treatment_name,amount_paise" in analytics
    assert "booking_type, lab_test_id, lab_test_name, treatment_id, treatment_name, " in admin


def test_insights_service_mix_uses_treatment_names():
    from app.services import analytics

    source = analytics.__file__
    text = open(source, encoding="utf-8").read()
    block = text.split('if booking_type == "lab_test":\n            service =')[1][:400]
    assert 'a.get("treatment_name")' in block
