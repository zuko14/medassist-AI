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
