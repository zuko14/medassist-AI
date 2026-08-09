"""Tests for admin queue endpoints."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    check_in_appointment_endpoint,
    call_next_patient_endpoint,
)


@pytest.mark.asyncio
async def test_check_in_endpoint_assigns_token():
    user = AdminUser(username="admin", role="super_admin", clinic_id="c1")
    with patch(
        "app.routers.admin.check_in_appointment",
        new_callable=AsyncMock,
        return_value={"id": "appt-1", "token_number": 5, "queue_status": "waiting"},
    ), patch(
        "app.routers.admin.supabase"
    ) as mock_sb:
        mock_sb.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value.data = [
            {"id": "c1", "name": "Default"}
        ]
        result = await check_in_appointment_endpoint(
            appointment_id="appt-1",
            clinic_id="default",
            user=user,
        )

    assert result["token_number"] == 5
    assert result["queue_status"] == "waiting"


@pytest.mark.asyncio
async def test_check_in_notifies_patient_of_token_over_whatsapp():
    """Front desk check-in must push the token to the patient, not just show
    it in the admin UI — patients shouldn't have to text 'queue status' to
    learn their own OPD token number."""
    user = AdminUser(username="admin", role="super_admin", clinic_id="c1")
    queue_status = {
        "checked_in": True,
        "token_number": 5,
        "doctor_name": "Dr. Rao",
        "currently_serving": 3,
        "patients_ahead": 2,
    }
    with patch(
        "app.routers.admin.check_in_appointment",
        new_callable=AsyncMock,
        return_value={
            "id": "appt-1",
            "token_number": 5,
            "queue_status": "waiting",
            "patient_phone": "+919876543210",
        },
    ), patch(
        "app.routers.admin.get_patient_queue_status",
        new_callable=AsyncMock,
        return_value=queue_status,
    ), patch(
        "app.routers.admin.get_clinic_by_id",
        new_callable=AsyncMock,
        return_value={"id": "c1", "name": "Default"},
    ), patch(
        "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
    ) as mock_send:
        await check_in_appointment_endpoint(
            appointment_id="appt-1",
            clinic_id="default",
            user=user,
        )

    mock_send.assert_awaited_once()
    args, _ = mock_send.call_args
    assert args[1] == "+919876543210"
    assert "5" in args[2]


@pytest.mark.asyncio
async def test_call_next_endpoint_advances_queue():
    user = AdminUser(username="admin", role="super_admin", clinic_id="c1")
    with patch(
        "app.routers.admin.call_next_patient",
        new_callable=AsyncMock,
        return_value={"id": "appt-2", "token_number": 2, "queue_status": "in_consultation"},
    ), patch(
        "app.routers.admin.supabase"
    ) as mock_sb:
        mock_sb.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value.data = [
            {"id": "c1", "name": "Default"}
        ]
        result = await call_next_patient_endpoint(
            doctor_name="Dr. Rao",
            clinic_id="default",
            user=user,
        )

    assert result["token_number"] == 2
    assert result["queue_status"] == "in_consultation"


@pytest.mark.asyncio
async def test_call_next_notifies_patient_of_their_turn_over_whatsapp():
    """Advancing the queue must push a 'your turn now' WhatsApp message to the
    newly-called patient, not just update the admin UI — mirrors the check-in
    notification fix."""
    user = AdminUser(username="admin", role="super_admin", clinic_id="c1")
    with patch(
        "app.routers.admin.call_next_patient",
        new_callable=AsyncMock,
        return_value={
            "id": "appt-2",
            "token_number": 2,
            "queue_status": "in_consultation",
            "patient_phone": "+919876543210",
            "doctor_name": "Dr. Rao",
            "clinic_id": "c1",
        },
    ), patch(
        "app.routers.admin.get_clinic_by_id",
        new_callable=AsyncMock,
        return_value={"id": "c1", "name": "Default"},
    ), patch(
        "app.services.whatsapp.whatsapp_service.send_text", new_callable=AsyncMock
    ) as mock_send:
        await call_next_patient_endpoint(
            doctor_name="Dr. Rao",
            clinic_id="default",
            user=user,
        )

    mock_send.assert_awaited_once()
    args, _ = mock_send.call_args
    assert args[1] == "+919876543210"
    assert "2" in args[2]


@pytest.mark.asyncio
async def test_check_in_appointment_retries_on_token_conflict():
    """If the UNIQUE index rejects the first token due to a concurrent
    check-in, check_in_appointment must retry with the next number instead
    of returning None or raising."""
    from app.database import check_in_appointment

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table

        mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-10"}]
        )

        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.return_value = MagicMock(data=[])

        update_chain = mock_table.update.return_value.eq.return_value.eq.return_value
        update_chain.execute.side_effect = [
            Exception("duplicate key value violates unique constraint idx_unique_queue_token"),
            MagicMock(data=[{"id": "appt-1", "token_number": 2, "queue_status": "waiting"}]),
        ]

        result = await check_in_appointment("clinic-1", "appt-1")

    assert result is not None
    assert result["token_number"] == 2
    assert update_chain.execute.call_count == 2


@pytest.mark.asyncio
async def test_check_in_appointment_gives_up_after_max_retries():
    """After exhausting retries, return None instead of raising."""
    from app.database import check_in_appointment

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table
        mock_table.select.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"doctor_name": "Dr. Rao", "appointment_date": "2026-08-10"}]
        )
        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.return_value = MagicMock(data=[])

        update_chain = mock_table.update.return_value.eq.return_value.eq.return_value
        update_chain.execute.side_effect = Exception(
            "duplicate key value violates unique constraint idx_unique_queue_token"
        )

        result = await check_in_appointment("clinic-1", "appt-1")

    assert result is None


@pytest.mark.asyncio
async def test_call_next_patient_retries_if_candidate_already_claimed():
    """If a concurrent call already claimed the first candidate (guarded
    UPDATE affects 0 rows), retry with the next waiting patient."""
    from app.database import call_next_patient

    with patch("app.database.supabase") as mock_sb:
        mock_table = MagicMock()
        mock_sb.table.return_value = mock_table

        mock_table.update.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value = MagicMock(data=[])

        select_chain = mock_table.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        select_chain.execute.side_effect = [
            MagicMock(data=[{"id": "appt-1", "token_number": 1}]),
            MagicMock(data=[{"id": "appt-2", "token_number": 2}]),
        ]

        claim_chain = mock_table.update.return_value.eq.return_value.eq.return_value.eq.return_value
        claim_chain.execute.side_effect = [
            MagicMock(data=[]),
            MagicMock(data=[{"id": "appt-2", "queue_status": "in_consultation"}]),
        ]

        result = await call_next_patient("clinic-1", "Dr. Rao", "2026-08-10")

    assert result is not None
    assert result["id"] == "appt-2"

