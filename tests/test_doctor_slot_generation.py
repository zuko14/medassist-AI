"""Tests for doctor slot auto-generation on create/update."""

import json

import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    create_doctor,
    update_doctor,
    DoctorCreate,
    DoctorUpdate,
    _apply_slot_config,
    _friendly_db_error,
)


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


@pytest.mark.asyncio
async def test_create_doctor_uses_real_clinic_admin_clinic_id():
    """A real clinic_admin (what every onboarded client actually is, unlike
    the bare MagicMock() used above) must write the doctor under their own
    clinic_id, never the 'pick the oldest clinic in the whole platform'
    fallback that resolve_clinic_id_for_write() only takes when clinic_id
    truly can't be resolved."""
    admin_user = AdminUser(
        username="clinic123abc",
        role="clinic_admin",
        clinic_id="11111111-1111-1111-1111-111111111111",
    )
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
    )

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Test"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=admin_user)

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["clinic_id"] == "11111111-1111-1111-1111-111111111111"
    assert mock_sb.table.call_args_list[0].args[0] == "doctors"


@pytest.mark.asyncio
async def test_create_doctor_surfaces_friendly_error_on_db_failure():
    admin_user = AdminUser(
        username="clinic123abc", role="clinic_admin", clinic_id="clinic-1"
    )
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
    )

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.side_effect = Exception(
        "insert or update on table \"doctors\" violates foreign key constraint"
    )

    with patch("app.routers.admin.supabase", mock_sb):
        with pytest.raises(HTTPException) as exc:
            await create_doctor(payload, clinic_id="default", user=admin_user)

    assert exc.value.status_code == 500
    assert "clinic" in exc.value.detail.lower()


def test_friendly_db_error_classifies_common_postgres_errors():
    assert "already exists" in _friendly_db_error(Exception("duplicate key value"), "x")
    assert "clinic" in _friendly_db_error(Exception("foreign key constraint"), "x")
    assert "valid" in _friendly_db_error(Exception("violates check constraint"), "x")
    assert _friendly_db_error(Exception("connection reset"), "fallback") == "fallback"


def test_apply_slot_config_output_is_json_serializable():
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
        evening_start="17:00",
        evening_end="19:00",
        slot_duration_minutes=30,
    )
    data = _apply_slot_config(payload.model_dump())

    json.dumps(data)  # must not raise TypeError
    assert data["morning_start"] == "09:00:00"
    assert data["evening_end"] == "19:00:00"


@pytest.mark.asyncio
async def test_create_doctor_morning_only_shift():
    """Verify that creating a doctor with morning-only shift sets morning_slots and clears evening_slots."""
    payload = DoctorCreate(
        name="Dr. Morning Only",
        specialization="General",
        department="OPD",
        morning_start="09:00",
        morning_end="11:00",
        slot_duration_minutes=30,
    )
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-morn", "name": "Dr. Morning Only"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=MagicMock())

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["morning_slots"] == ["09:00", "09:30", "10:00", "10:30"]
    assert inserted["evening_slots"] == []
    assert inserted["evening_start"] is None
    assert inserted["evening_end"] is None


@pytest.mark.asyncio
async def test_create_doctor_evening_only_shift():
    """Verify that creating a doctor with evening-only shift sets evening_slots and clears morning_slots."""
    payload = DoctorCreate(
        name="Dr. Evening Only",
        specialization="General",
        department="OPD",
        evening_start="17:00",
        evening_end="19:00",
        slot_duration_minutes=30,
    )
    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-eve", "name": "Dr. Evening Only"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=MagicMock())

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["evening_slots"] == ["17:00", "17:30", "18:00", "18:30"]
    assert inserted["morning_slots"] == []
    assert inserted["morning_start"] is None
    assert inserted["morning_end"] is None


@pytest.mark.asyncio
async def test_create_doctor_rejects_both_shifts_disabled():
    """Verify that attempting to create a doctor with no shifts enabled raises 422."""
    payload = DoctorCreate(
        name="Dr. No Shift",
        specialization="General",
        department="OPD",
    )
    with pytest.raises(HTTPException) as exc:
        await create_doctor(payload, clinic_id="default", user=MagicMock())
    assert exc.value.status_code == 422
    assert "at least one shift" in exc.value.detail.lower()

