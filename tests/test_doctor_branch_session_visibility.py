"""Evening shift invisible to patients (live, 2026-09-23).

Dr. Priya Sharma had morning 09-12 and evening 17-19 on her record, but her
doctor_branches row said session='morning' (set on the Branches page). The
slot picker filters by that session, so patients saw 6 slots a day instead of
10, and after 12:00 "today" vanished. The Doctors form, where shifts are
edited, never showed the session. It now does, and a session that leaves a
doctor no slots at all is refused on every write path.
"""

import importlib
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.admin import (
    AdminUser,
    DoctorBranchAssign,
    DoctorUpdate,
    _reject_empty_branch_session,
    update_doctor,
)

PRIYA = {
    "id": "doc-1",
    "name": "Dr. Priya Sharma",
    "available_days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
    "morning_start": "09:00:00", "morning_end": "12:00:00",
    "evening_start": "17:00:00", "evening_end": "19:00:00",
    "morning_slots": ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"],
    "evening_slots": ["17:00", "17:30", "18:00", "18:30"],
}
MORNING_ONLY_DOC = {**PRIYA, "evening_start": None, "evening_end": None, "evening_slots": []}


class _EmptyQuery:
    """Any PostgREST chain; every execute() returns no rows."""

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return MagicMock(data=[])


async def _slots(session, clinic_id):
    future = (date.today() + timedelta(days=3)).isoformat()
    fake = MagicMock()
    fake.table.side_effect = lambda _t: _EmptyQuery()

    async def _doc(*_a, **_k):
        return PRIYA

    # Resolved off the live module, as in test_booked_slot_exclusion: some test
    # modules swap app.database in sys.modules while being collected.
    db = importlib.import_module("app.database")
    with patch("app.database.supabase", fake), \
         patch("app.database.get_doctor_by_name", _doc), \
         patch.dict("app.database._holiday_cache", {}, clear=True):
        slots, reason = await db.get_available_slots(
            clinic_id, "Dr. Priya Sharma", future, branch_session=session
        )
    assert reason is None
    return slots


@pytest.mark.asyncio
async def test_branch_session_is_what_hid_the_evening_shift():
    assert len(await _slots("both", "c-both")) == 10
    morning = await _slots("morning", "c-morning")
    assert morning == PRIYA["morning_slots"]  # the 6 the patient saw


def test_session_naming_a_missing_shift_is_refused():
    with pytest.raises(HTTPException) as e:
        _reject_empty_branch_session(MORNING_ONLY_DOC, "evening")
    assert e.value.status_code == 422


def test_sessions_that_leave_slots_are_allowed():
    for session in ("both", "morning", "evening", None):
        _reject_empty_branch_session(PRIYA, session)
    _reject_empty_branch_session(MORNING_ONLY_DOC, "morning")


def test_session_values_are_validated():
    with pytest.raises(Exception):
        DoctorBranchAssign(doctor_id="doc-1", session="night")
    with pytest.raises(Exception):
        DoctorUpdate(branch_session="afternoon")


@pytest.mark.asyncio
async def test_update_refuses_empty_session_before_writing_anything():
    """Turning the evening shift off while the branch says 'evening only'."""
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = (
        MagicMock(data=[{**PRIYA, "clinic_id": "clinic-1"}])
    )
    body = DoctorUpdate(
        morning_start="09:00", morning_end="12:00",
        branch_id="11111111-1111-1111-1111-111111111111", branch_session="evening",
    )
    user = AdminUser("admin", role="clinic_admin", clinic_id="clinic-1", user_id="u1")
    with patch("app.routers.admin.supabase", sb), pytest.raises(HTTPException) as e:
        await update_doctor("doc-1", body, request=MagicMock(), clinic_id="clinic-1", user=user)
    assert e.value.status_code == 422
    sb.table.return_value.update.assert_not_called()
    sb.table.return_value.insert.assert_not_called()
    sb.table.return_value.delete.assert_not_called()
