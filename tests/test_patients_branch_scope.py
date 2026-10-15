"""D-22: the Patients list for branch-pinned staff (owner decision 2026-10-14: branch via appointments).

patients has no branch column, so a pinned staff member sees the patients who have
an appointment at their branch (or a branch-less one). Lab reports and prescriptions
carry no branch, so they never widen the list. Clinic admins and tenant-wide staff
see the whole clinic, as before. On a DB error a branch list fails closed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import database
from app.routers import admin
from app.routers.admin import AdminUser

CID = "6f1c2c56-0000-4000-8000-000000000001"
B1 = "11111111-1111-1111-1111-111111111111"
ROWS = {
    "patients": [
        {"phone": "911", "name": "Asha", "visit_count": 2},       # booked at B1
        {"phone": "922", "name": "Bala", "visit_count": 3},       # booked at another branch only
        {"phone": "933", "name": "Chitra", "visit_count": 0},     # lab report only (no branch)
        {"phone": "944", "name": "Devi", "visit_count": 0},       # named, never booked
    ],
    "lab_reports": [{"patient_phone": "933"}],
    "prescriptions": [],
}
APPTS_ALL = [{"patient_phone": "911"}, {"patient_phone": "922"}]
APPTS_B1 = [{"patient_phone": "911"}]


class _Q:
    def __init__(self, table, log):
        self.table, self.log = table, log

    def __getattr__(self, name):
        def chain(*a, **k):
            self.log.append((self.table, name, a))
            return self
        return chain


def _fake(fail=False):
    log = []
    sb_client = MagicMock()
    sb_client.table.side_effect = lambda t: _Q(t, log)

    async def _sb(q):
        if fail and q.table == "appointments":
            raise RuntimeError("db down")
        if q.table == "appointments":
            narrowed = any(c[0] == "appointments" and c[1] == "or_" for c in log)
            return MagicMock(data=APPTS_B1 if narrowed else APPTS_ALL)
        return MagicMock(data=ROWS.get(q.table, []))
    return sb_client, _sb, log


async def _names(**kw):
    client, sb_fn, log = _fake(kw.pop("fail", False))
    with patch.object(database, "supabase", client), patch.object(database, "sb", sb_fn):
        out = await database.get_genuine_patients(CID, **kw)
    return sorted(p["name"] for p in out), log


@pytest.mark.asyncio
async def test_whole_clinic_list_is_unchanged_without_a_branch():
    names, _ = await _names()
    assert names == ["Asha", "Bala", "Chitra", "Devi"]


@pytest.mark.asyncio
async def test_a_branch_list_holds_only_patients_booked_at_that_branch():
    names, log = await _names(branch_id=B1)
    assert names == ["Asha"]
    assert ("appointments", "or_", (f"branch_id.eq.{B1},branch_id.is.null",)) in log


@pytest.mark.asyncio
async def test_a_branch_list_fails_closed_on_a_db_error():
    names, _ = await _names(branch_id=B1, fail=True)
    assert names == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user, expected", [
    (AdminUser("desk", role="staff", clinic_id=CID, user_id="u1", branch_id=B1), B1),
    (AdminUser("desk", role="staff", clinic_id=CID, user_id="u2"), None),
    (AdminUser("boss", role="clinic_admin", clinic_id=CID, user_id="u3", branch_id=B1), None),
], ids=["pinned", "tenant-wide", "admin"])
async def test_patients_route_passes_the_pinned_branch(user, expected):
    fake = AsyncMock(return_value=[])
    with patch.object(admin, "get_genuine_patients", fake):
        await admin.get_patients(clinic_id=CID, user=user)
    fake.assert_awaited_once_with(CID, branch_id=expected)
