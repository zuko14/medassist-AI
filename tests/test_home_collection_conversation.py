"""Home sample collection through the real ConversationManager, webhook parser
and payment confirmation (migration 097). The step logic itself is covered in
tests/test_home_collection.py; this file checks the hooks that connect it."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation import ConversationManager
from app.services import home_collection_flow as flow

CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111", "name": "Accumax", "plan": "diagbooking"}
PHONE = "+919999999999"
PATIENT = {"id": "patient-1", "name": "Ravi Kumar", "language": "en"}


def _home_ready_context():
    return {
        "lab_test_id": "test-0001", "lab_test_name": "CBC", "lab_test_price_paise": 40000,
        "lab_collection_date": "2026-10-05", "lab_step": "hc_confirm",
        "hc_available": True, "hc_mode": "home", "hc_ready": True, "hc_slot": "07:00-08:00",
        "hc_patient_name": "Ravi Kumar", "hc_lat": 17.72, "hc_lng": 83.30,
        "hc_address": "Flat 302, MVP Colony", "hc_pin": "Sai Residency",
        "hc_contact": "+919848022338", "hc_fee_paise": 10000,
    }


@pytest.mark.asyncio
async def test_date_tap_asks_home_or_centre_when_offered():
    manager = ConversationManager()
    ctx = {"lab_test_id": "t1", "hc_available": True}
    with patch("app.database.get_lab_collection_window", AsyncMock(return_value={"start": "07:00", "end": "11:00"})), \
         patch.object(manager, "_next_collection_dates", return_value=["2026-10-05"]), \
         patch.object(manager.whatsapp, "send_interactive_buttons", AsyncMock()) as buttons, \
         patch.object(manager, "_ask_lab_test_patient", AsyncMock()) as who, \
         patch.object(manager, "update_state", AsyncMock()):
        await manager._handle_confirming_collection_date(
            CLINIC, PHONE, "", "button_click", ctx, PATIENT, "en", {"id": "labdate_2026-10-05"})
    who.assert_not_awaited()
    assert [b["id"] for b in buttons.call_args.kwargs["buttons"]] == ["hcmode_home", "hcmode_centre"]


@pytest.mark.asyncio
async def test_date_tap_is_unchanged_when_home_collection_is_not_offered():
    manager = ConversationManager()
    ctx = {"lab_test_id": "t1", "hc_available": False}
    with patch("app.database.get_lab_collection_window", AsyncMock(return_value={})), \
         patch.object(manager, "_next_collection_dates", return_value=["2026-10-05"]), \
         patch.object(manager, "_ask_lab_test_patient", AsyncMock()) as who:
        await manager._handle_confirming_collection_date(
            CLINIC, PHONE, "", "button_click", ctx, PATIENT, "en", {"id": "labdate_2026-10-05"})
    who.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_parks_a_home_booking_until_the_address_is_known():
    manager = ConversationManager()
    ctx = {**_home_ready_context(), "hc_ready": None, "hc_lat": None}
    with patch("app.services.payment.payment_service.create_booking_with_payment", AsyncMock()) as paid, \
         patch("app.services.conversation.book_appointment", AsyncMock()) as free, \
         patch.object(flow, "_ask_location", AsyncMock()) as ask:
        await manager._finalize_lab_booking(CLINIC, PHONE, ctx, PATIENT, "en", "2026-10-05", "Ravi Kumar")
    paid.assert_not_awaited()
    free.assert_not_awaited()
    ask.assert_awaited_once()
    assert ctx["hc_patient_name"] == "Ravi Kumar"


@pytest.mark.asyncio
async def test_counter_payment_centre_books_home_visit_and_assigns_it():
    manager = ConversationManager()
    booked = {"success": True, "appointment": {"id": "appt-1", "booking_ref": "AD-1"}}
    with patch("app.services.payment.resolve_payment_mode", return_value=("none", 100)), \
         patch("app.services.conversation.book_appointment", AsyncMock(return_value=booked)) as book, \
         patch.object(manager.whatsapp, "send_text", AsyncMock()) as send, \
         patch.object(manager, "_send_main_menu", AsyncMock()), \
         patch.object(manager, "update_state", AsyncMock()), \
         patch("app.services.home_collection.auto_assign", AsyncMock()) as assign:
        await manager._finalize_lab_booking(
            CLINIC, PHONE, _home_ready_context(), PATIENT, "en", "2026-10-05", "Ravi Kumar")
    row = book.call_args.args[1]
    assert row["collection_mode"] == "home" and row["collection_slot"] == "07:00-08:00"
    assert row["collection_lat"] == 17.72 and row["collection_contact_phone"] == "+919848022338"
    assert row["amount_paise"] == 50000  # test + home fee
    text = send.call_args.args[2]
    assert "07:00-08:00" in text and "Flat 302" in text and "₹500" in text
    assign.assert_awaited_once_with("clinic-1", "appt-1", alert_if_none=True)


@pytest.mark.asyncio
async def test_online_payment_centre_passes_the_visit_to_payment():
    manager = ConversationManager()
    paid = {"success": True, "booking_id": "b1", "booking_ref": "AD-2", "payment_link": "https://rzp.io/x",
            "amount_paise": 50000, "hold_expires_at": "2026-10-05T01:00:00Z"}
    with patch("app.services.payment.resolve_payment_mode", return_value=("full", 100)), \
         patch("app.services.payment.payment_service.create_booking_with_payment", AsyncMock(return_value=paid)) as create, \
         patch.object(manager.whatsapp, "send_text", AsyncMock()) as send, \
         patch.object(manager, "update_state", AsyncMock()) as state:
        await manager._finalize_lab_booking(
            CLINIC, PHONE, _home_ready_context(), PATIENT, "en", "2026-10-05", "Ravi Kumar")
    home = create.call_args.kwargs["home_collection"]
    assert home["collection_mode"] == "home" and home["home_collection_fee_paise"] == 10000
    assert "Home collection" in send.call_args.args[2]
    assert state.call_args.args[2] == "awaiting_payment"
    # The parked visit details are cleared once the booking exists.
    assert state.call_args.args[3]["hc_address"] is None


@pytest.mark.asyncio
async def test_a_centre_visit_never_sends_visit_columns_to_payment():
    manager = ConversationManager()
    ctx = {**_home_ready_context(), "hc_mode": "centre"}
    paid = {"success": True, "booking_id": "b1", "booking_ref": "AD-3", "payment_link": "u",
            "amount_paise": 40000, "hold_expires_at": "x"}
    with patch("app.services.payment.resolve_payment_mode", return_value=("full", 100)), \
         patch("app.services.payment.payment_service.create_booking_with_payment", AsyncMock(return_value=paid)) as create, \
         patch.object(manager.whatsapp, "send_text", AsyncMock()), \
         patch.object(manager, "update_state", AsyncMock()):
        await manager._finalize_lab_booking(CLINIC, PHONE, ctx, PATIENT, "en", "2026-10-05", "Ravi Kumar")
    assert create.call_args.kwargs["home_collection"] is None


# ── location pins reach the step that asked for them, and only that one ──


def _locked_handler_patches(state, context):
    session = {"state": state, "context": context}
    return [
        patch("app.services.conversation.get_or_create_conversation", AsyncMock(return_value=session)),
        patch("app.services.conversation.get_patient_by_phone", AsyncMock(return_value=PATIENT)),
        patch("app.services.conversation.update_conversation", AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_a_location_pin_is_routed_to_the_home_collection_step():
    manager = ConversationManager()
    ctx = {"lab_step": "hc_location", "hc_mode": "home"}
    pin = {"type": "location", "latitude": 17.72, "longitude": 83.30}
    patches = _locked_handler_patches("confirming_collection_date", ctx)
    for p in patches:
        p.start()
    try:
        with patch.object(manager, "_handle_confirming_collection_date", AsyncMock()) as handler, \
             patch.object(manager.whatsapp, "send_text", AsyncMock()) as send:
            await manager._handle_message_locked(CLINIC, PHONE, "", "location", "wamid.1", pin)
    finally:
        for p in patches:
            p.stop()
    handler.assert_awaited_once()
    assert handler.call_args.args[-1] == pin
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_location_pin_elsewhere_keeps_the_unsupported_reply():
    manager = ConversationManager()
    patches = _locked_handler_patches("main_menu", {})
    for p in patches:
        p.start()
    try:
        with patch.object(manager, "_handle_confirming_collection_date", AsyncMock()) as handler, \
             patch.object(manager.whatsapp, "send_text", AsyncMock()) as send:
            await manager._handle_message_locked(
                CLINIC, PHONE, "", "location", "wamid.2", {"type": "location", "latitude": 1, "longitude": 2})
    finally:
        for p in patches:
            p.stop()
    handler.assert_not_awaited()
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_parses_a_location_pin_without_logging_the_address():
    from app.models.message import WhatsAppMessage
    from app.routers import webhook

    msg = WhatsAppMessage(**{
        "from": "919999999999", "id": "wamid.L", "timestamp": "1", "type": "location",
        "location": {"latitude": 17.72, "longitude": 83.30, "name": "Home", "address": "Flat 302"},
    })
    with patch.object(webhook, "is_callmedex_number", AsyncMock(return_value=False)), \
         patch.object(webhook, "resolve_tenant", AsyncMock(return_value=CLINIC)), \
         patch.object(webhook.message_queue, "acquire", AsyncMock(return_value=True)), \
         patch.object(webhook.message_queue, "claim_message", AsyncMock()), \
         patch.object(webhook.whatsapp_service, "mark_as_read", AsyncMock()), \
         patch.object(webhook.conversation_manager, "handle_message", AsyncMock()) as handle:
        await webhook.process_message(msg, "+911111111111", "pnid")
    kwargs = handle.call_args.kwargs
    assert kwargs["message_type"] == "location" and kwargs["message"] == ""
    assert kwargs["interactive_data"]["latitude"] == 17.72
    assert kwargs["interactive_data"]["address"] == "Flat 302"


# ── payment confirmation ──


def _confirm_patches(booking_row=None):
    full = MagicMock(data=[booking_row] if booking_row else [])
    return [
        patch("app.services.tenant.get_clinic_by_id", AsyncMock(return_value={**CLINIC, "config": {}})),
        patch("app.database.get_patient_by_phone", AsyncMock(return_value=PATIENT)),
        patch("app.services.payment.sb", AsyncMock(return_value=full)),
        patch("app.services.whatsapp.whatsapp_service.send_text", AsyncMock()),
        patch("app.services.whatsapp.whatsapp_service.send_interactive_buttons", AsyncMock()),
        patch("app.services.conversation.conversation_manager.update_state", AsyncMock()),
        patch("app.services.home_collection.auto_assign", AsyncMock()),
    ]


HOME_BOOKING = {
    "id": "b1", "clinic_id": "clinic-1", "booking_type": "lab_test", "collection_mode": "home",
    "lab_test_name": "CBC", "patient_name": "Ravi", "patient_phone": PHONE, "appointment_date": "2026-10-05",
    "collection_slot": "07:00-08:00", "collection_address": "Flat 302", "amount_paise": 50000,
    "booking_ref": "AD-9", "branch_id": "br-1",
}


@pytest.mark.asyncio
async def test_paid_home_booking_gets_visit_copy_and_is_assigned():
    from app.services.payment import PaymentService
    from app.services.whatsapp import whatsapp_service
    from app.services import home_collection

    patches = _confirm_patches()
    for p in patches:
        p.start()
    try:
        with patch.object(PaymentService, "_alert_admin", AsyncMock()), \
             patch.object(PaymentService, "_log_payment_event", AsyncMock()), \
             patch("app.services.tenant.get_branch_by_id", AsyncMock()) as branch:
            await PaymentService()._notify_payment_confirmed(dict(HOME_BOOKING))
            text = whatsapp_service.send_text.call_args_list[0].args[2]
            assign = home_collection.auto_assign
    finally:
        for p in patches:
            p.stop()
    assert "Home Sample Collection" in text and "07:00-08:00" in text and "Flat 302" in text
    assert "Please arrive" not in text
    branch.assert_not_awaited()  # the centre's address is not sent for a home visit
    assign.assert_awaited_once_with("clinic-1", "b1", alert_if_none=True)


@pytest.mark.asyncio
async def test_a_partial_booking_dict_is_reloaded_before_confirmation():
    """The fast-poll path passes a row without booking_type; a lab test used to
    be announced with doctor copy from it."""
    from app.services.payment import PaymentService
    from app.services.whatsapp import whatsapp_service

    partial = {k: HOME_BOOKING[k] for k in ("id", "clinic_id", "patient_phone", "booking_ref", "amount_paise",
                                              "appointment_date")}
    patches = _confirm_patches(booking_row=HOME_BOOKING)
    for p in patches:
        p.start()
    try:
        with patch.object(PaymentService, "_alert_admin", AsyncMock()), \
             patch.object(PaymentService, "_log_payment_event", AsyncMock()):
            await PaymentService()._notify_payment_confirmed(partial)
            text = whatsapp_service.send_text.call_args_list[0].args[2]
    finally:
        for p in patches:
            p.stop()
    assert "Home Sample Collection" in text and "Doctor" not in text
