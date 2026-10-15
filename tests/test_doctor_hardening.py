"""D-34 (KK-P4-11 hardening audit): doctor endpoints.

(a) PUT /admin/doctors/{id} regenerated both shifts from what it was sent: an update with only the
    evening times blanked the morning shift, and slots were regenerated at a 30-minute default unless
    the duration was sent. Now a shift (and the duration) that the request does not mention keeps its
    stored values; a shift the request clears explicitly is still cleared.
(b) consultation_fee had no bounds; name / specialization / department had no length limit.
(c) POST /admin/branches/{id}/doctors did not check doctor_id was a UUID (generic 500).
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.routers.admin import AdminUser, DoctorBranchAssign, DoctorCreate, DoctorUpdate, update_doctor

CLINIC = "22222222-2222-2222-2222-222222222222"
ADMIN = AdminUser(username="tester", role="clinic_admin", clinic_id=CLINIC)
STORED = {"id": "doc-1", "name": "Dr. Anitha", "morning_start": "09:00:00", "morning_end": "10:00:00",
          "evening_start": "17:00:00", "evening_end": "18:00:00", "slot_duration_minutes": 15,
          "morning_slots": ["09:00", "09:15", "09:30", "09:45"], "evening_slots": ["17:00", "17:15", "17:30", "17:45"]}


async def _update(payload: DoctorUpdate) -> dict:
    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = \
        MagicMock(data=[dict(STORED)])
    mock_sb.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = \
        MagicMock(data=[{"id": "doc-1"}])
    with patch("app.routers.admin.supabase", mock_sb):
        await update_doctor("doc-1", payload, clinic_id="default", user=ADMIN)
    return mock_sb.table.return_value.update.call_args[0][0]


@pytest.mark.asyncio
async def test_an_evening_only_update_keeps_the_morning_and_the_duration():
    sent = await _update(DoctorUpdate(evening_start="18:00", evening_end="19:00"))
    assert sent["morning_slots"] == ["09:00", "09:15", "09:30", "09:45"]
    assert sent["evening_slots"] == ["18:00", "18:15", "18:30", "18:45"]


@pytest.mark.asyncio
async def test_a_shift_cleared_explicitly_is_still_cleared():
    sent = await _update(DoctorUpdate(morning_start=None, morning_end=None))
    assert sent["morning_slots"] == [] and sent["morning_start"] is None
    assert sent["evening_slots"] == ["17:00", "17:15", "17:30", "17:45"]


@pytest.mark.asyncio
async def test_a_sent_duration_still_wins():
    sent = await _update(DoctorUpdate(evening_start="18:00", evening_end="19:00", slot_duration_minutes=30))
    assert sent["evening_slots"] == ["18:00", "18:30"] and sent["morning_slots"] == ["09:00", "09:30"]


_BASE = dict(name="Dr. Test", specialization="Cardiology", department="Cardiology")


@pytest.mark.parametrize("fee", [-1, 100_001])
def test_fee_out_of_bounds_is_422(fee):
    with pytest.raises(ValidationError):
        DoctorCreate(**_BASE, consultation_fee=fee)
    with pytest.raises(ValidationError):
        DoctorUpdate(consultation_fee=fee)


def test_fee_bounds_inclusive():
    assert DoctorCreate(**_BASE, consultation_fee=0).consultation_fee == 0
    assert DoctorUpdate(consultation_fee=100_000).consultation_fee == 100_000


@pytest.mark.parametrize("field", ["name", "specialization", "department"])
def test_text_fields_are_bounded(field):
    with pytest.raises(ValidationError):
        DoctorCreate(**{**_BASE, field: "x" * 121})
    with pytest.raises(ValidationError):
        DoctorUpdate(**{field: "x" * 121})
    assert getattr(DoctorCreate(**{**_BASE, field: "x" * 120}), field) == "x" * 120


def test_branch_assign_needs_a_uuid():
    with pytest.raises(ValidationError):
        DoctorBranchAssign(doctor_id="not-a-uuid")
    assert DoctorBranchAssign(doctor_id="33333333-3333-3333-3333-333333333333").session == "both"
