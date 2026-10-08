"""Home collection: custom visit windows and phlebotomist leave (migration 101)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import home_collection as hc

NOW = datetime(2026, 10, 8, 6, 0, tzinfo=hc.IST)   # before any slot, so none are dropped for lead time
LAB_WINDOW = {"start": "07:00", "end": "11:00"}


def test_normalize_windows_keeps_valid_sorted_non_overlapping_max_three():
    raw = [
        {"start": "16:00", "end": "19:00"},
        {"start": "07:00", "end": "11:00"},
        {"start": "10:00", "end": "12:00"},   # overlaps 07-11
        {"start": "12:00", "end": "12:00"},   # empty
        {"start": "25:00", "end": "26:00"},   # not a time
        "junk",
        {"start": "12:00", "end": "14:00"},
        {"start": "20:00", "end": "21:00"},   # a 4th valid window
    ]
    assert hc.normalize_windows(raw) == [
        {"start": "07:00", "end": "11:00"}, {"start": "12:00", "end": "14:00"}, {"start": "16:00", "end": "19:00"},
    ]
    assert hc.normalize_windows(None) == []
    assert hc.normalize_settings({"windows": "07-11"})["windows"] == []


def test_no_custom_windows_keeps_the_lab_collection_window():
    s = hc.normalize_settings({"enabled": True, "slot_minutes": 60})
    assert s["windows"] == []
    assert hc.slots_for(LAB_WINDOW, s, "2026-10-09", NOW) == [
        "07:00-08:00", "08:00-09:00", "09:00-10:00", "10:00-11:00"]


def test_custom_windows_replace_the_lab_window():
    s = hc.normalize_settings({"slot_minutes": 60, "windows": [{"start": "16:00", "end": "17:30"},
                                                               {"start": "07:00", "end": "09:00"}]})
    assert hc.slots_for(LAB_WINDOW, s, "2026-10-09", NOW) == [
        "07:00-08:00", "08:00-09:00", "16:00-17:00", "17:00-17:30"]


def test_custom_windows_still_respect_same_day_notice():
    s = hc.normalize_settings({"slot_minutes": 60, "lead_minutes": 60, "windows": [{"start": "12:00", "end": "16:00"}]})
    at_noon = datetime(2026, 10, 8, 12, 30, tzinfo=hc.IST)
    assert hc.slots_for(LAB_WINDOW, s, "2026-10-08", at_noon) == ["14:00-15:00", "15:00-16:00"]


def test_upcoming_off_dates_drops_past_bad_and_duplicate_dates():
    assert hc.upcoming_off_dates(["2026-10-09", "2026-10-07", "x", "2026-10-09", "2026-10-08"], "2026-10-08") == [
        "2026-10-08", "2026-10-09"]


def test_phlebotomist_on_leave_is_never_picked():
    appt = {"appointment_date": "2026-10-09", "collection_slot": "07:00-08:00", "branch_id": None}
    asha = {"id": "a", "full_name": "Asha", "is_active": True, "off_dates": ["2026-10-09"]}
    ravi = {"id": "r", "full_name": "Ravi", "is_active": True, "off_dates": []}
    assert hc.pick_phlebotomist(appt, [asha, ravi], [])["id"] == "r"
    assert hc.pick_phlebotomist(appt, [asha], []) is None
    # Leave on another day does not matter.
    assert hc.pick_phlebotomist({**appt, "appointment_date": "2026-10-10"}, [asha], [])["id"] == "a"


@pytest.mark.asyncio
async def test_no_slots_when_everyone_serving_the_branch_is_off():
    phlebs = [
        {"id": "a", "branch_id": "b1", "is_active": True, "off_dates": ["2026-10-09"]},
        {"id": "f", "branch_id": None, "is_active": True, "off_dates": ["2026-10-09"]},
        {"id": "o", "branch_id": "b2", "is_active": True, "off_dates": []},   # other branch: does not help b1
    ]
    s = hc.normalize_settings({"slot_minutes": 60})
    with patch.object(hc, "list_phlebotomists", new=AsyncMock(return_value=phlebs)):
        assert await hc.open_slots("c1", LAB_WINDOW, s, "2026-10-09", "b1", NOW) == []
        assert await hc.open_slots("c1", LAB_WINDOW, s, "2026-10-09", "b2", NOW) != []
        assert await hc.open_slots("c1", LAB_WINDOW, s, "2026-10-10", "b1", NOW) != []


@pytest.mark.asyncio
async def test_slots_still_offered_with_no_phlebotomist_or_on_read_error():
    s = hc.normalize_settings({"slot_minutes": 60})
    with patch.object(hc, "list_phlebotomists", new=AsyncMock(return_value=[])):
        assert await hc.open_slots("c1", LAB_WINDOW, s, "2026-10-09", None, NOW) != []
    with patch.object(hc, "list_phlebotomists", new=AsyncMock(side_effect=RuntimeError("db down"))):
        assert await hc.open_slots("c1", LAB_WINDOW, s, "2026-10-09", None, NOW) != []


# ═══════ routes ═══════

import uuid  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routers import home_collection as router_mod  # noqa: E402
from app.routers.admin import AdminUser, _enforce_phlebotomist_scope  # noqa: E402

client = TestClient(app)
CLINIC = "11111111-2222-3333-4444-555555555555"
CLINIC_ROW = {"id": CLINIC, "plan": "diagbooking", "config": {}}
PID = "aaaaaaaa-0000-0000-0000-000000000001"
ADMIN = AdminUser(username="a", role="clinic_admin", clinic_id=CLINIC, user_id="a1")


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[router_mod.verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(router_mod.verify_credentials, None)


def _future(n):
    from datetime import timedelta
    return (datetime.now(hc.IST).date() + timedelta(days=n)).isoformat()


def test_marking_leave_saves_dates_and_hands_over_only_new_days(as_user):
    as_user(ADMIN)
    d1, d2 = _future(1), _future(2)
    phleb = {"id": PID, "full_name": "Asha", "branch_id": None, "is_active": True, "off_dates": [d1]}
    sb = AsyncMock(return_value=MagicMock(data=[{}]))
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[phleb])), \
         patch.object(router_mod, "sb", sb), \
         patch.object(hc, "release_visits_of", AsyncMock(return_value=2)) as release, \
         patch.object(router_mod, "log_admin_action", AsyncMock()):
        r = client.put(f"/admin/home-collection/phlebotomists/{PID}/off-dates",
                       json={"dates": [d2, d1, "2020-01-01"]}, params={"clinic_id": CLINIC})
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "off_dates": [d1, d2], "released_visits": 2}
    release.assert_awaited_once_with(CLINIC, PID, [d2])          # d1 was already leave


def test_leave_rejects_malformed_dates_and_unknown_phlebotomists(as_user):
    as_user(ADMIN)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[{"id": PID, "branch_id": None}])):
        bad = client.put(f"/admin/home-collection/phlebotomists/{PID}/off-dates",
                         json={"dates": ["9/10/2026"]}, params={"clinic_id": CLINIC})
        other = client.put(f"/admin/home-collection/phlebotomists/{uuid.uuid4()}/off-dates",
                           json={"dates": []}, params={"clinic_id": CLINIC})
    assert bad.status_code == 422
    assert other.status_code == 404


def test_branch_staff_cannot_set_leave_for_another_branch(as_user):
    as_user(AdminUser(username="m", role="staff", clinic_id=CLINIC, user_id="m1", branch_id="b1",
                      permissions=["HOME_COLLECTION_MANAGE"]))
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[{"id": PID, "branch_id": "b2"}])):
        r = client.put(f"/admin/home-collection/phlebotomists/{PID}/off-dates",
                       json={"dates": []}, params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_a_phlebotomist_login_cannot_set_leave():
    phleb = AdminUser(username="ph1", role="staff", clinic_id=CLINIC, user_id="p1", staff_role="PHLEBOTOMIST")
    req = MagicMock()
    req.method, req.url.path = "PUT", f"/admin/home-collection/phlebotomists/{PID}/off-dates"
    with pytest.raises(Exception) as e:
        _enforce_phlebotomist_scope(req, phleb)
    assert getattr(e.value, "status_code", None) == 403


def test_assigning_a_phlebotomist_on_leave_is_refused(as_user):
    as_user(ADMIN)
    day = _future(1)
    visit = {"id": PID, "clinic_id": CLINIC, "status": "confirmed", "collection_status": "unassigned",
             "phlebotomist_id": None, "branch_id": None, "appointment_date": day}
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(hc, "get_visit", AsyncMock(return_value=visit)), \
         patch.object(hc, "list_phlebotomists", AsyncMock(return_value=[{"id": "p9", "is_active": True, "off_dates": [day]}])), \
         patch.object(hc, "assign", AsyncMock()) as assign:
        r = client.post(f"/admin/home-collection/visits/{PID}/assign", json={"phlebotomist_id": "p9"},
                        params={"clinic_id": CLINIC})
    assert r.status_code == 422
    assign.assert_not_awaited()


@pytest.mark.parametrize("windows", [
    [{"start": "11:00", "end": "07:00"}],
    [{"start": "07:00", "end": "11:00"}, {"start": "10:00", "end": "12:00"}],
    [{"start": "7", "end": "11"}],
])
def test_settings_refuse_bad_or_overlapping_windows(as_user, windows):
    as_user(ADMIN)
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))):
        r = client.put("/admin/home-collection/settings", json={"enabled": True, "windows": windows},
                       params={"clinic_id": CLINIC})
    assert r.status_code == 422


def test_settings_save_custom_windows(as_user):
    as_user(ADMIN)
    sb = AsyncMock(return_value=MagicMock(data=[{"config": {}, "whatsapp_number": None, "phone_number_id": None}]))
    with patch.object(router_mod, "_scope", AsyncMock(return_value=(CLINIC, CLINIC_ROW))), \
         patch.object(router_mod, "sb", sb), patch.object(router_mod, "log_admin_action", AsyncMock()), \
         patch.object(router_mod, "invalidate_tenant_cache"):
        r = client.put("/admin/home-collection/settings",
                       json={"enabled": True, "windows": [{"start": "16:00", "end": "19:00"}, {"start": "07:00", "end": "10:00"}]},
                       params={"clinic_id": CLINIC})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["windows"] == [{"start": "07:00", "end": "10:00"}, {"start": "16:00", "end": "19:00"}]


# ═══════ schema (migration 101) ═══════


def test_off_dates_column_defaults_empty_and_stores_dates(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'diagbooking', true) "
                "RETURNING id", ("Leave " + uuid.uuid4().hex[:6], "x-" + uuid.uuid4().hex))
    cid = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, permissions, is_active) "
        "VALUES (%s, %s, 'x', 'staff', 'PHLEBOTOMIST', '{}', true) RETURNING id, off_dates",
        (cid, "ph_" + uuid.uuid4().hex[:8]))
    pid, off = cur.fetchone()
    assert off == []
    cur.execute("UPDATE clinic_admins SET off_dates = %s::date[] WHERE id = %s RETURNING off_dates",
                (["2026-10-09", "2026-10-10"], pid))
    assert [d.isoformat() for d in cur.fetchone()[0]] == ["2026-10-09", "2026-10-10"]
    cur.execute("DELETE FROM clinics WHERE id = %s", (cid,))
