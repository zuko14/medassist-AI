"""Booked slots must never be offered again.

Postgres returns a TIME column as 'HH:MM:SS'. Generated slots are 'HH:MM'.
The exclusion set compared the two raw, so it matched nothing and every
booked slot stayed on offer — the patient only found out after tapping it
and hitting the DB uniqueness guard.
"""

import importlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

DOCTOR = {
    "name": "Dr. Test",
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
    "morning_slots": ["09:00", "09:30", "10:00"],
    "evening_slots": ["17:00", "17:30"],
}
FUTURE = "2027-03-15"



@pytest.mark.asyncio
async def test_slot_booked_as_hh_mm_ss_is_not_offered_again():
    """Postgres hands back '09:30:00' — the 09:30 slot must still disappear."""
    slots, reason = await get_available_slots_with(
        [{"appointment_time": "09:30:00", "status": "confirmed"}]
    )
    assert reason is None
    assert "09:30" not in slots, f"booked slot still on offer: {slots}"
    assert "09:00" in slots and "17:00" in slots


@pytest.mark.asyncio
async def test_slot_booked_as_hh_mm_is_not_offered_again():
    slots, _ = await get_available_slots_with(
        [{"appointment_time": "17:00", "status": "confirmed"}]
    )
    assert "17:00" not in slots
    assert "17:30" in slots


@pytest.mark.asyncio
async def test_fully_booked_day_returns_no_slots():
    slots, _ = await get_available_slots_with(
        [
            {"appointment_time": f"{t}:00", "status": "confirmed"}
            for t in ["09:00", "09:30", "10:00", "17:00", "17:30"]
        ]
    )
    assert slots == []


@pytest.mark.asyncio
async def test_expired_payment_hold_frees_the_slot():
    slots, _ = await get_available_slots_with(
        [
            {
                "appointment_time": "09:00:00",
                "status": "pending_payment",
                "hold_expires_at": "2020-01-01T00:00:00+00:00",
            }
        ]
    )
    assert "09:00" in slots


async def get_available_slots_with(booked_rows):
    db = importlib.import_module("app.database")

    def fake_table(name):
        tbl = MagicMock()
        tbl.select.return_value = tbl
        tbl.eq.return_value = tbl
        tbl.in_.return_value = tbl
        tbl.execute.return_value = MagicMock(
            data=booked_rows if name == "appointments" else []
        )
        return tbl

    with patch("app.database.supabase") as sb, patch(
        "app.database.get_doctor_by_name", new_callable=AsyncMock, return_value=DOCTOR
    ), patch.dict("app.database._holiday_cache", {}, clear=True):
        sb.table.side_effect = fake_table
        # Resolved off the live module: several test modules reload app.database,
        # which would leave a module-level import bound to a stale, unpatched copy.
        return await db.get_available_slots("clinic-1", "Dr. Test", FUTURE)
