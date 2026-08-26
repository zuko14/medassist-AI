"""Slot re-prompt after a booking collision.

Production report: a patient whose slot got taken between seeing it and
tapping it was re-offered *morning slots only* — every evening slot vanished.
get_available_slots() returns morning-then-evening, so the caller's slots[:3]
truncation sliced the evening session off before _show_slot_list ever saw it.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.conversation import ConversationManager

CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
MORNING = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]
EVENING = ["17:00", "17:30", "18:00", "18:30"]


def _context():
    return {
        "doctor_name": "Dr. Test",
        "appointment_date": "2026-09-01",
        "appointment_time": "09:00",
        "department": "General Medicine",
        "booking_name": "Patient",
    }


def _rows(mock_send_list):
    return [
        row
        for section in mock_send_list.call_args.kwargs["sections"]
        for row in section["rows"]
    ]


async def _run_collision(manager, payment_mode):
    """Drive _handle_confirming_booking down the slot_taken branch."""
    taken = {"success": False, "reason": "slot_taken"}
    with patch(
        "app.services.payment.resolve_payment_mode",
        return_value=(payment_mode, 100),
    ), patch(
        "app.services.payment.payment_service.create_booking_with_payment",
        new_callable=AsyncMock,
        return_value=taken,
    ), patch(
        "app.services.conversation.book_appointment",
        new_callable=AsyncMock,
        return_value=taken,
    ), patch(
        "app.services.conversation.get_available_slots",
        new_callable=AsyncMock,
        return_value=(MORNING + EVENING, None),
    ), patch.object(
        manager.whatsapp, "send_text", new_callable=AsyncMock
    ), patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_send_list, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ):
        await manager._handle_confirming_booking(
            CLINIC,
            "+919876543210",
            "yes",
            "confirm_booking",
            _context(),
            {"id": "p1", "name": "Patient"},
            "en",
        )
    return mock_send_list


@pytest.mark.asyncio
@pytest.mark.parametrize("payment_mode", ["full", "none"])
async def test_slot_taken_reprompt_still_offers_evening_slots(payment_mode):
    manager = ConversationManager()
    mock_send_list = await _run_collision(manager, payment_mode)

    assert mock_send_list.called, "patient was left with no alternative slots"
    ids = {row["id"] for row in _rows(mock_send_list)}
    assert "slot_17:00" in ids, f"evening session missing from re-prompt: {ids}"
    assert "slot_09:00" in ids


@pytest.mark.asyncio
async def test_slot_list_shows_how_many_slots_are_free():
    """Patient should see the count, not just the first few times."""
    manager = ConversationManager()
    with patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_send_list, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ):
        await manager._show_slot_list(
            CLINIC, "+919876543210", MORNING + EVENING, _context(), "en"
        )

    titles = " ".join(s["title"] for s in mock_send_list.call_args.kwargs["sections"])
    assert "6" in titles, f"morning count missing: {titles}"
    assert "4" in titles, f"evening count missing: {titles}"
    # WhatsApp hard-rejects a list with more than 10 rows total.
    assert len(_rows(mock_send_list)) <= 10


@pytest.mark.asyncio
async def test_single_session_slot_list_shows_count():
    manager = ConversationManager()
    with patch.object(
        manager.whatsapp, "send_interactive_list", new_callable=AsyncMock
    ) as mock_send_list, patch.object(
        manager, "update_state", new_callable=AsyncMock
    ):
        await manager._show_slot_list(
            CLINIC, "+919876543210", MORNING, _context(), "en"
        )

    sections = mock_send_list.call_args.kwargs["sections"]
    assert len(sections) == 1
    assert "6" in sections[0]["title"]
