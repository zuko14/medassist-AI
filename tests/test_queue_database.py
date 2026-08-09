"""Tests for queue/token database helpers."""

import pytest
from unittest.mock import MagicMock, patch

from app.database import check_in_appointment, call_next_patient, get_patient_queue_status


@pytest.mark.asyncio
async def test_check_in_appointment_assigns_next_token():
    mock_sb = MagicMock()
    # First select: appt lookup
    mock_appt = [{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-09"}]
    # Second select: max token query returns max 3
    mock_max = [{"token_number": 3}]
    # Third: update return
    mock_updated = [{"id": "appt-1", "token_number": 4, "queue_status": "waiting"}]

    mock_select = mock_sb.table.return_value.select.return_value
    # Make .eq chainable
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.side_effect = [
        MagicMock(data=mock_appt),
        MagicMock(data=mock_max),
    ]

    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=mock_updated)

    with patch("app.database.supabase", mock_sb):
        result = await check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 4
    assert result["queue_status"] == "waiting"


@pytest.mark.asyncio
async def test_check_in_appointment_first_token_of_day_is_1():
    mock_sb = MagicMock()
    mock_appt = [{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-09"}]
    mock_max = []
    mock_updated = [{"id": "appt-1", "token_number": 1, "queue_status": "waiting"}]

    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.side_effect = [
        MagicMock(data=mock_appt),
        MagicMock(data=mock_max),
    ]

    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=mock_updated)

    with patch("app.database.supabase", mock_sb):
        result = await check_in_appointment("clinic-1", "appt-1")

    assert result["token_number"] == 1


@pytest.mark.asyncio
async def test_get_patient_queue_status_not_checked_in():
    mock_sb = MagicMock()
    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.execute.return_value = MagicMock(
        data=[{"id": "appt-1", "token_number": None, "doctor_name": "Dr. Rao"}]
    )

    with patch("app.database.supabase", mock_sb):
        result = await get_patient_queue_status("clinic-1", "+919876543210", "2026-08-09")

    assert result["checked_in"] is False


@pytest.mark.asyncio
async def test_call_next_patient_advances_queue():
    mock_sb = MagicMock()
    mock_update = mock_sb.table.return_value.update.return_value
    mock_update.eq.return_value = mock_update
    mock_update.execute.return_value = MagicMock(data=[])

    mock_waiting = [{"id": "appt-2", "token_number": 2, "queue_status": "waiting"}]
    mock_select = mock_sb.table.return_value.select.return_value
    mock_select.eq.return_value = mock_select
    mock_select.order.return_value = mock_select
    mock_select.limit.return_value = mock_select
    mock_select.execute.return_value = MagicMock(data=mock_waiting)

    with patch("app.database.supabase", mock_sb):
        result = await call_next_patient("clinic-1", "Dr. Rao", "2026-08-09")

    assert result["id"] == "appt-2"
