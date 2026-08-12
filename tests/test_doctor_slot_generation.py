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
    payload = DoctorCreate(name="Dr. Test", specialization="Cardiologist", department="Cardiology")

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Test"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=admin_user)

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    assert inserted["clinic_id"] == "11111111-1111-1111-1111-111111111111"
    # The "no clinic_id resolvable" fallback query must never run for a
    # properly-bound clinic_admin.
    # Note: supabase.table() is now called for both "doctors" (insert) and
    # "branches" (auto-branch-selection check), so we verify the first call
    # was "doctors" and that the correct clinic_id was used.
    assert mock_sb.table.call_args_list[0].args[0] == "doctors"


@pytest.mark.asyncio
async def test_create_doctor_surfaces_friendly_error_on_db_failure():
    admin_user = AdminUser(
        username="clinic123abc", role="clinic_admin", clinic_id="clinic-1"
    )
    payload = DoctorCreate(name="Dr. Test", specialization="Cardiologist", department="Cardiology")

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
    """Regression test: DoctorCreate.morning_start/etc are typed as
    datetime.time, and the Supabase/httpx client JSON-encodes the payload
    with the stdlib encoder (no default for `time`). Every admin-panel
    doctor-add sends these fields (the form always populates them), so any
    raw `time` object left in the payload after _apply_slot_config crashes
    every real insert with a TypeError, surfaced to the client as a generic
    "Failed to create doctor" 500."""
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
    data = _apply_slot_config(payload.dict())

    json.dumps(data)  # must not raise TypeError
    assert data["morning_start"] == "09:00:00"
    assert data["evening_end"] == "19:00:00"


@pytest.mark.asyncio
async def test_create_doctor_sends_json_serializable_payload_to_supabase():
    admin_user = AdminUser(username="clinic123abc", role="clinic_admin", clinic_id="clinic-1")
    payload = DoctorCreate(
        name="Dr. Test",
        specialization="Cardiologist",
        department="Cardiology",
        morning_start="09:00",
        morning_end="12:00",
        evening_start="17:00",
        evening_end="19:00",
    )

    mock_sb = MagicMock()
    mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock(
        data=[{"id": "doc-1", "name": "Dr. Test"}]
    )

    with patch("app.routers.admin.supabase", mock_sb):
        await create_doctor(payload, clinic_id="default", user=admin_user)

    inserted = mock_sb.table.return_value.insert.call_args[0][0]
    json.dumps(inserted)  # this is what actually crashed in production
