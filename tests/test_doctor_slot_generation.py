"""Tests for doctor slot auto-generation on create/update."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import create_doctor, update_doctor, DoctorCreate, DoctorUpdate


@pytest.mark.asyncio
async def test_create_doctor_generates_morning_and_evening_slots():
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="11:00",
        evening_start="17:00",
        evening_end="18:00",
        slot_duration_minutes=30,
    )

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Test"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=MagicMock())

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["morning_slots"] == ["09:00", "09:30", "10:00", "10:30"]
    assert inserted["evening_slots"] == ["17:00", "17:30"]


@pytest.mark.asyncio
async def test_create_doctor_rejects_end_before_start():
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="11:00",
        morning_end="09:00",
    )

    with pytest.raises(HTTPException) as exc:
        await create_doctor(payload, clinic_id="default", user=MagicMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_update_doctor_regenerates_slots_when_timing_changed():
    payload = DoctorUpdate(morning_start="08:00", morning_end="09:00", slot_duration_minutes=15)

    mock_sb = MagicMock()
    mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await update_doctor("doc-1", payload, clinic_id="default", user=MagicMock())

    updated = mock_sb.table.return_value.update.call_args[0][0]
    assert updated["morning_slots"] == ["08:00", "08:15", "08:30", "08:45"]
