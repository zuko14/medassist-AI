"""Tests for the cancellation appointment list — verifies only today/future bookings appear."""

import pytest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

CLINIC = {"id": "clinic-1", "name": "Test Clinic", "whatsapp_number": "919999999999"}
PHONE = "919876543210"
PATIENT = {"phone": PHONE, "name": "Test Patient"}

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
LAST_WEEK = (date.today() - timedelta(days=7)).isoformat()
LAST_MONTH = (date.today() - timedelta(days=30)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()
NEXT_WEEK = (date.today() + timedelta(days=7)).isoformat()


def _make_appt(appt_id, doctor, appt_date, appt_time="10:00", status="confirmed"):
    return {
        "id": appt_id,
        "clinic_id": CLINIC["id"],
        "patient_phone": PHONE,
        "doctor_name": doctor,
        "appointment_date": appt_date,
        "appointment_time": appt_time,
        "status": status,
    }


# ── Test: get_patient_appointments respects from_date ────────────────────────


@pytest.mark.asyncio
async def test_get_patient_appointments_filters_by_from_date():
    """Verify .gte('appointment_date', from_date) is applied when from_date is provided."""
    from app.database import get_patient_appointments

    mock_chain = MagicMock()
    # Build the chained query mock
    mock_chain.eq.return_value = mock_chain
    mock_chain.gte.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[
        _make_appt("a1", "Dr. Today", TODAY),
    ])

    with patch("app.database.scoped_query", return_value=mock_chain):
        result = await get_patient_appointments(
            CLINIC["id"], PHONE, status="confirmed", from_date=TODAY
        )

    assert len(result) == 1
    assert result[0]["id"] == "a1"
    # Verify .gte was called with the from_date
    mock_chain.gte.assert_called_once_with("appointment_date", TODAY)


@pytest.mark.asyncio
async def test_get_patient_appointments_no_from_date_skips_gte():
    """Verify .gte is NOT called when from_date is None (backward compat)."""
    from app.database import get_patient_appointments

    mock_chain = MagicMock()
    mock_chain.eq.return_value = mock_chain
    mock_chain.gte.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.execute.return_value = MagicMock(data=[
        _make_appt("a1", "Dr. Old", LAST_MONTH),
        _make_appt("a2", "Dr. Today", TODAY),
    ])

    with patch("app.database.scoped_query", return_value=mock_chain):
        result = await get_patient_appointments(
            CLINIC["id"], PHONE, status="confirmed"
        )

    assert len(result) == 2
    mock_chain.gte.assert_not_called()


# ── Test: _handle_cancel_request excludes past appointments ──────────────────


@pytest.mark.asyncio
async def test_cancel_list_excludes_past_appointments():
    """Past-date appointments must NOT appear in the cancellation list."""
    from app.services.conversation import ConversationManager

    svc = ConversationManager.__new__(ConversationManager)
    svc.whatsapp = AsyncMock()
    svc.update_state = AsyncMock()

    # Mock get_patient_appointments: confirmed returns today only, pending returns empty
    async def mock_get_appts(clinic_id, phone, status=None, from_date=None):
        if status == "confirmed" and from_date == TODAY:
            return [_make_appt("today-1", "Dr. Meena Patel", TODAY, "17:00")]
        if status == "pending_payment" and from_date == TODAY:
            return []
        return []

    with patch("app.database.get_patient_appointments", side_effect=mock_get_appts):
        await svc._handle_cancel_request(CLINIC, PHONE, PATIENT, "en")

    # The list should be sent
    svc.whatsapp.send_interactive_list.assert_called_once()
    call_kwargs = svc.whatsapp.send_interactive_list.call_args
    sections = call_kwargs[1].get("sections") or call_kwargs[0][4] if len(call_kwargs[0]) > 4 else call_kwargs[1]["sections"]
    rows = sections[0]["rows"]

    assert len(rows) == 1
    assert rows[0]["id"] == "cancel_today-1"
    assert "Dr. Meena Patel" in rows[0]["title"]
    assert "Today" in rows[0]["description"]


@pytest.mark.asyncio
async def test_cancel_list_includes_pending_payment():
    """Pending-payment bookings for today/future must appear in the cancellation list."""
    from app.services.conversation import ConversationManager

    svc = ConversationManager.__new__(ConversationManager)
    svc.whatsapp = AsyncMock()
    svc.update_state = AsyncMock()

    async def mock_get_appts(clinic_id, phone, status=None, from_date=None):
        if status == "confirmed" and from_date == TODAY:
            return [_make_appt("conf-1", "Dr. Sharma", TOMORROW, "09:30")]
        if status == "pending_payment" and from_date == TODAY:
            return [_make_appt("pend-1", "Dr. Meena Patel", TODAY, "17:00", "pending_payment")]
        return []

    with patch("app.database.get_patient_appointments", side_effect=mock_get_appts):
        await svc._handle_cancel_request(CLINIC, PHONE, PATIENT, "en")

    svc.whatsapp.send_interactive_list.assert_called_once()
    call_kwargs = svc.whatsapp.send_interactive_list.call_args
    sections = call_kwargs[1].get("sections") or call_kwargs[0][4] if len(call_kwargs[0]) > 4 else call_kwargs[1]["sections"]
    rows = sections[0]["rows"]

    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert "cancel_conf-1" in ids
    assert "cancel_pend-1" in ids
    # Pending payment should show status label
    pending_row = next(r for r in rows if r["id"] == "cancel_pend-1")
    assert "Pending Payment" in pending_row["description"]


@pytest.mark.asyncio
async def test_cancel_list_empty_shows_correct_message():
    """When no future appointments exist, the correct 'no upcoming' message is sent."""
    from app.services.conversation import ConversationManager

    svc = ConversationManager.__new__(ConversationManager)
    svc.whatsapp = AsyncMock()
    svc.update_state = AsyncMock()
    svc._send_main_menu = AsyncMock()

    async def mock_get_appts(clinic_id, phone, status=None, from_date=None):
        return []  # No future appointments

    with patch("app.database.get_patient_appointments", side_effect=mock_get_appts):
        await svc._handle_cancel_request(CLINIC, PHONE, PATIENT, "en")

    # Should NOT send the interactive list
    svc.whatsapp.send_interactive_list.assert_not_called()
    # Should send the "no upcoming" text
    svc.whatsapp.send_text.assert_called_once()
    sent_text = svc.whatsapp.send_text.call_args[0][2]
    assert "upcoming" in sent_text.lower()
    # Should show main menu
    svc._send_main_menu.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_list_future_appointments_appear():
    """Future-date confirmed appointments must appear in the cancellation list."""
    from app.services.conversation import ConversationManager

    svc = ConversationManager.__new__(ConversationManager)
    svc.whatsapp = AsyncMock()
    svc.update_state = AsyncMock()

    async def mock_get_appts(clinic_id, phone, status=None, from_date=None):
        if status == "confirmed" and from_date == TODAY:
            return [
                _make_appt("fut-1", "Dr. Reddy", TOMORROW, "11:00"),
                _make_appt("fut-2", "Dr. Kumar", NEXT_WEEK, "10:00"),
            ]
        if status == "pending_payment" and from_date == TODAY:
            return []
        return []

    with patch("app.database.get_patient_appointments", side_effect=mock_get_appts):
        await svc._handle_cancel_request(CLINIC, PHONE, PATIENT, "en")

    svc.whatsapp.send_interactive_list.assert_called_once()
    call_kwargs = svc.whatsapp.send_interactive_list.call_args
    sections = call_kwargs[1].get("sections") or call_kwargs[0][4] if len(call_kwargs[0]) > 4 else call_kwargs[1]["sections"]
    rows = sections[0]["rows"]

    assert len(rows) == 2
    assert rows[0]["id"] == "cancel_fut-1"
    assert rows[1]["id"] == "cancel_fut-2"
    # Future dates should show the actual date, NOT "Today"
    assert TOMORROW in rows[0]["description"]
    assert NEXT_WEEK in rows[1]["description"]
