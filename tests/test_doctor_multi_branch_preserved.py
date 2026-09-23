"""Saving a multi-branch doctor in the Doctors tab wiped their other branches.

update_doctor wrote a branch by deleting every doctor_branches row for the
doctor and inserting the one in the dropdown. For a doctor at two branches,
editing the fee dropped one of them. The form now omits branch fields for
such a doctor, and the endpoint refuses (400, before any write) a branch
change that would drop assignments.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.admin import AdminUser, DoctorUpdate, update_doctor

KUKATPALLY = "11111111-1111-1111-1111-111111111111"
MADHAPUR = "22222222-2222-2222-2222-222222222222"
DOCTOR = {
    "id": "doc-1", "clinic_id": "clinic-1", "name": "Dr. Priya Sharma",
    "consultation_fee": 300,
    "morning_start": "09:00:00", "morning_end": "12:00:00",
    "evening_start": "17:00:00", "evening_end": "19:00:00",
    "morning_slots": ["09:00", "09:30"], "evening_slots": ["17:00", "17:30"],
}
TWO_BRANCHES = [
    {"branch_id": KUKATPALLY, "session": "morning"},
    {"branch_id": MADHAPUR, "session": "evening"},
]


class _Table:
    """One PostgREST table; records writes, answers reads from `rows`."""

    def __init__(self, name, rows, writes):
        self.name, self.rows, self.writes = name, rows, writes

    def __getattr__(self, op):
        def chain(*args, **kwargs):
            if op in ("update", "insert", "delete", "upsert"):
                self.writes.append((self.name, op, args))
            return self
        return chain

    def execute(self):
        return MagicMock(data=self.rows.get(self.name, []))


def _client(rows):
    writes = []
    client = MagicMock()
    client.table.side_effect = lambda name: _Table(name, rows, writes)
    return client, writes


USER = AdminUser("admin", role="clinic_admin", clinic_id="clinic-1", user_id="u1")


async def _update(body, rows):
    client, writes = _client(rows)
    with patch("app.routers.admin.supabase", client), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock), \
         patch("app.routers.admin.invalidate_doctor_cache"):
        result = await update_doctor("doc-1", body, request=MagicMock(), clinic_id="clinic-1", user=USER)
    return result, writes


def _branch_writes(writes):
    return [w for w in writes if w[0] == "doctor_branches"]


@pytest.mark.asyncio
async def test_fee_edit_without_branch_fields_leaves_all_branches():
    """What the fixed form sends for a multi-branch doctor."""
    rows = {"doctors": [DOCTOR], "doctor_branches": TWO_BRANCHES}
    _, writes = await _update(DoctorUpdate(consultation_fee=500), rows)
    assert ("doctors", "update", ({"consultation_fee": 500},)) in writes
    assert _branch_writes(writes) == []


@pytest.mark.asyncio
async def test_resending_an_existing_assignment_unchanged_is_a_no_op():
    """An older panel still pre-selects the first branch; that must not wipe."""
    rows = {"doctors": [DOCTOR], "doctor_branches": TWO_BRANCHES}
    _, writes = await _update(
        DoctorUpdate(consultation_fee=500, branch_id=KUKATPALLY), rows
    )
    assert _branch_writes(writes) == []
    assert any(w[:2] == ("doctors", "update") for w in writes)


@pytest.mark.parametrize("body", [
    {"branch_id": KUKATPALLY, "branch_session": "both"},   # session change
    {"branch_id": "33333333-3333-3333-3333-333333333333"},  # a different branch
    {"branch_id": ""},                                      # unassign all
])
@pytest.mark.asyncio
async def test_branch_change_for_multi_branch_doctor_is_refused_before_any_write(body):
    rows = {"doctors": [DOCTOR], "doctor_branches": TWO_BRANCHES}
    client, writes = _client(rows)
    with patch("app.routers.admin.supabase", client), pytest.raises(HTTPException) as e:
        await update_doctor(
            "doc-1", DoctorUpdate(consultation_fee=500, **body),
            request=MagicMock(), clinic_id="clinic-1", user=USER,
        )
    assert e.value.status_code == 400
    assert "Branches tab" in e.value.detail
    assert writes == []  # not even the fee


@pytest.mark.asyncio
async def test_single_branch_doctor_can_still_move_branch():
    """The existing single-branch behaviour is unchanged."""
    rows = {
        "doctors": [DOCTOR],
        "doctor_branches": [{"branch_id": KUKATPALLY, "session": "both"}],
        "branches": [{"id": MADHAPUR}],
    }
    _, writes = await _update(DoctorUpdate(branch_id=MADHAPUR, branch_session="both"), rows)
    ops = [w[1] for w in _branch_writes(writes)]
    assert ops == ["delete", "insert"]
    assert _branch_writes(writes)[1][2][0]["branch_id"] == MADHAPUR
