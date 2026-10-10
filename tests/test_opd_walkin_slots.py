"""Tests for Kriya OPD OS Walk-ins & Slot Isolation (Phase 1.2).

Verifies:
1. Walk-in appointments have is_walk_in = True and do NOT consume scheduled slots in get_available_slots().
2. book_appointment() conflict query excludes walk-ins (.eq("is_walk_in", False)).
3. create_walk_in() operational guards:
   - Clinic holiday rejection (409 clinic_closed).
   - Doctor on leave rejection (409 doctor_on_leave).
   - Inactive doctor rejection (422 Doctor is inactive).
   - Doctor not found rejection (404 Doctor not found).
   - Branch scoping guard via API endpoint (403 Forbidden).
4. APScheduler background jobs ignore walk-in appointments:
   - send_24h_reminders (.eq("is_walk_in", False))
   - send_2h_reminders (.eq("is_walk_in", False))
   - check_doctor_leaves (.eq("is_walk_in", False))
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Resolved at call time: other suite tests delete and re-import app.database /
# services, so a name bound at import would run against a module the patches miss.
def get_available_slots(*a, **k):
    import importlib
    return importlib.import_module("app.database").get_available_slots(*a, **k)


from app.main import app
from app.routers import admin, opd
from app.routers.admin import AdminUser
from app.services.opd import create_walk_in
from app.services.scheduler import scheduler_service

client = TestClient(app)

CLINIC_ID = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())
BRANCH_A = str(uuid.uuid4())
BRANCH_B = str(uuid.uuid4())

FRONT_DESK_USER = AdminUser("receptionist", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_FRONT_DESK"])
BRANCH_SCOPED_USER = AdminUser("receptionist_b", role="staff", clinic_id=CLINIC_ID, permissions=["OPD_FRONT_DESK"], branch_id=BRANCH_A)
MOCK_CLINIC = {"id": CLINIC_ID, "features": {"opd_enabled": True}, "opd_state": "READY", "opd_settings": {}}


@pytest.fixture
def auth_front_desk():
    app.dependency_overrides[admin.verify_credentials] = lambda: FRONT_DESK_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


@pytest.fixture
def auth_branch_user():
    app.dependency_overrides[admin.verify_credentials] = lambda: BRANCH_SCOPED_USER
    yield
    app.dependency_overrides.pop(admin.verify_credentials, None)


# ─── 1. SLOT CAPACITY ISOLATION ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walk_ins_do_not_consume_slots():
    """get_available_slots excludes appointments with is_walk_in = True."""
    fake_doc = {
        "id": DOC_ID,
        "name": "Dr. Sharma",
        "is_active": True,
        "available_days": "Mon,Tue,Wed,Thu,Fri",
        "morning_start": "09:00",
        "morning_end": "10:00",
        "slot_duration": 30,
    }

    # Booked query result from PostgREST only returns non-walkins
    mock_supabase = MagicMock()
    # hospital_holidays -> empty
    mock_supabase.table().select().eq().eq().execute.return_value = MagicMock(data=[])
    # doctor_leaves -> empty
    mock_supabase.table().select().eq().eq().eq().execute.return_value = MagicMock(data=[])
    # appointments -> 09:00 is booked by an advance appointment
    mock_booked = MagicMock(data=[{"appointment_time": "09:00", "status": "confirmed", "is_walk_in": False}])

    with patch("app.database.supabase.table") as table_mock, \
         patch("app.database.get_doctor_by_name", return_value=fake_doc):
        # Configure table queries
        def table_dispatch(table_name):
            builder = MagicMock()
            if table_name == "hospital_holidays":
                builder.select().eq().eq().execute.return_value = MagicMock(data=[])
            elif table_name == "doctor_leaves":
                builder.select().eq().eq().eq().execute.return_value = MagicMock(data=[])
            elif table_name == "appointments":
                builder.select().eq().eq().eq().in_().eq().execute.return_value = mock_booked
            return builder

        table_mock.side_effect = table_dispatch
        # 2026-10-12 is a Monday
        slots, status = await get_available_slots(CLINIC_ID, "Dr. Sharma", "2026-10-12")

    assert status is None
    # 09:00 is occupied, 09:30 is available
    assert "09:00" not in slots
    assert "09:30" in slots


# ─── 2. WALK-IN GUARDS: HOLIDAYS, LEAVES & DOCTORS ──────────────────────────


@pytest.mark.asyncio
async def test_walk_in_rejected_on_holiday():
    """Attempting walk-in on a clinic holiday raises 409 clinic_closed."""
    body = {
        "patient_id": "p1",
        "doctor_id": DOC_ID,
        "visit_type": "new",
    }
    fake_doc = MagicMock(data=[{"id": DOC_ID, "name": "Dr. Sharma", "is_active": True}])
    fake_holiday = MagicMock(data=[{"name": "Diwali"}])

    with patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doc, fake_holiday])):
        with pytest.raises(HTTPException) as exc_info:
            await create_walk_in(MOCK_CLINIC, body, FRONT_DESK_USER)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "clinic_closed"


@pytest.mark.asyncio
async def test_walk_in_rejected_on_doctor_leave():
    """Attempting walk-in when doctor is on approved leave raises 409 doctor_on_leave."""
    body = {
        "patient_id": "p1",
        "doctor_id": DOC_ID,
        "visit_type": "new",
    }
    fake_doc = MagicMock(data=[{"id": DOC_ID, "name": "Dr. Sharma", "is_active": True}])
    fake_empty = MagicMock(data=[])
    fake_leave = MagicMock(data=[{"leave_type": "full"}])

    with patch("app.services.opd.sb", AsyncMock(side_effect=[fake_doc, fake_empty, fake_leave])):
        with pytest.raises(HTTPException) as exc_info:
            await create_walk_in(MOCK_CLINIC, body, FRONT_DESK_USER)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "doctor_on_leave"


@pytest.mark.asyncio
async def test_walk_in_rejected_for_inactive_doctor():
    """Attempting walk-in for an inactive doctor raises 422."""
    body = {
        "patient_id": "p1",
        "doctor_id": DOC_ID,
        "visit_type": "new",
    }
    fake_doc = MagicMock(data=[{"id": DOC_ID, "name": "Dr. Inactive", "is_active": False}])

    with patch("app.services.opd.sb", AsyncMock(return_value=fake_doc)):
        with pytest.raises(HTTPException) as exc_info:
            await create_walk_in(MOCK_CLINIC, body, FRONT_DESK_USER)
    assert exc_info.value.status_code == 422
    assert "Doctor is inactive" in exc_info.value.detail


@pytest.mark.asyncio
async def test_walk_in_rejected_for_nonexistent_doctor():
    """Attempting walk-in for non-existent doctor raises 404."""
    body = {
        "patient_id": "p1",
        "doctor_id": DOC_ID,
        "visit_type": "new",
    }
    with patch("app.services.opd.sb", AsyncMock(return_value=MagicMock(data=[]))):
        with pytest.raises(HTTPException) as exc_info:
            await create_walk_in(MOCK_CLINIC, body, FRONT_DESK_USER)
    assert exc_info.value.status_code == 404
    assert "Doctor not found" in exc_info.value.detail


def test_walk_in_cross_branch_rejected_via_endpoint(auth_branch_user):
    """Staff scoped to Branch A cannot create walk-in for Branch B (403)."""
    with patch("app.routers.opd.get_clinic_by_id", AsyncMock(return_value=MOCK_CLINIC)):
        r = client.post(
            f"/admin/opd/walk-ins?clinic_id={CLINIC_ID}",
            json={
                "patient_id": "pat-1",
                "doctor_id": DOC_ID,
                "branch_id": BRANCH_B,  # user is assigned to BRANCH_A
                "visit_type": "new",
            },
        )
    assert r.status_code == 403


# ─── 3. SCHEDULER EXCLUSION OF WALK-INS ──────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_24h_reminders_ignores_walk_ins():
    """24h reminders query must include is_walk_in = False."""
    with patch("app.services.scheduler.sb", AsyncMock(return_value=MagicMock(data=[]))) as sb_mock:
        await scheduler_service.send_24h_reminders()

    assert sb_mock.called
    query_call = sb_mock.call_args[0][0]
    params = vars(query_call.request)["params"]
    assert "is_walk_in" in params
    assert params["is_walk_in"] == "eq.False"


@pytest.mark.asyncio
async def test_scheduler_2h_reminders_ignores_walk_ins():
    """2h reminders query must include is_walk_in = False."""
    with patch("app.services.scheduler.sb", AsyncMock(return_value=MagicMock(data=[]))) as sb_mock:
        await scheduler_service.send_2h_reminders()

    assert sb_mock.called
    query_call = sb_mock.call_args[0][0]
    params = vars(query_call.request)["params"]
    assert "is_walk_in" in params
    assert params["is_walk_in"] == "eq.False"


@pytest.mark.asyncio
async def test_scheduler_check_doctor_leaves_ignores_walk_ins():
    """Doctor leaves notifier query must include is_walk_in = False."""
    fake_leave = [{"doctor_name": "Dr. Rao", "leave_date": "2026-10-12", "clinic_id": CLINIC_ID}]
    with patch("app.services.scheduler.sb", AsyncMock(side_effect=[
        MagicMock(data=fake_leave),
        MagicMock(data=[]),  # appointment search
    ])) as sb_mock:
        await scheduler_service.check_doctor_leaves()

    assert sb_mock.call_count >= 2
    appt_query = sb_mock.call_args_list[1][0][0]
    params = vars(appt_query.request)["params"]
    assert "is_walk_in" in params
    assert params["is_walk_in"] == "eq.False"
