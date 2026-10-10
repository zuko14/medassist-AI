"""OPD staff roles and branch scoping (2026-10-10 fixes).

1. doctor_logins: the checklist query must SELECT doctor_id, and only active
   logins count. It used to select id/role/permissions only, so the item could
   never pass and Go Live was blocked with every doctor linked.
2. DOCTOR / CASHIER logins are kept off the clinic-wide /admin data routes;
   a delegated grant re-opens its route; other staff roles are untouched.
3. Walk-in / arrival branch resolution (doctors has no branch_id column).
4. Branch-pinned reads are held to the caller's branch.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routers.admin import AdminUser, _enforce_opd_role_scope


def _req(method, path):
    return SimpleNamespace(method=method, url=SimpleNamespace(path=path))


def _staff(staff_role, perms, **kw):
    return AdminUser("u", role="staff", clinic_id="c1", permissions=perms, staff_role=staff_role, **kw)


# ─── 1. doctor_logins ─────────────────────────────────────────────────────────


def _checklist(admin_rows):
    from app.services import opd

    q = MagicMock()
    for m in ("table", "select", "eq", "limit", "is_", "in_", "order"):
        getattr(q, m).return_value = q
    doc = {"id": "d1", "is_active": True, "department": "GM", "morning_start": "09:00",
           "registration_number": "R1", "registration_council": "C", "consultation_fee": 300}
    sb = AsyncMock(side_effect=[MagicMock(data=[doc]), MagicMock(data=admin_rows)])
    clinic = {"id": "c1", "name": "A", "config": {"address": "x", "staff_phone": "1"}, "opd_settings": {}}
    with patch.object(opd, "sb", sb), patch.object(opd, "supabase", q), \
         patch.object(opd, "scoped_query", return_value=q):
        items = {i["key"]: i for i in asyncio.run(opd.setup_checklist(clinic))}
    return items, q


def test_doctor_logins_selects_doctor_id_and_passes_when_linked():
    items, q = _checklist([{"id": "a1", "role": "staff", "permissions": ["OPD_CLINICAL"],
                            "doctor_id": "d1", "is_active": True}])
    assert items["doctor_logins"]["done"] is True
    selected = [c.args[0] for c in q.select.call_args_list if "permissions" in c.args[0]]
    assert selected and "doctor_id" in selected[0] and "is_active" in selected[0]


def test_doctor_logins_ignores_deactivated_login():
    items, _ = _checklist([{"id": "a1", "role": "staff", "permissions": ["OPD_CLINICAL", "OPD_FRONT_DESK"],
                            "doctor_id": "d1", "is_active": False}])
    assert items["doctor_logins"]["done"] is False
    assert items["front_desk_permissions"]["done"] is False


# ─── 2. OPD role scope ────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/patients"), ("GET", "/admin/stats"), ("GET", "/admin/appointments"),
    ("GET", "/admin/bookings"), ("POST", "/admin/bookings/x/confirm"), ("GET", "/admin/lab-reports"),
    ("GET", "/admin/notifications/unread-count"), ("GET", "/fhir/Patient/9198"),
    ("DELETE", "/admin/appointments/a1"),
])
def test_doctor_login_denied_clinic_wide_data(method, path):
    with pytest.raises(HTTPException) as e:
        _enforce_opd_role_scope(_req(method, path), _staff("DOCTOR", ["OPD_CLINICAL"]))
    assert e.value.status_code == 403


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/me"), ("GET", "/admin/opd/workspace"), ("GET", "/admin/opd/queue"),
    ("GET", "/admin/doctors"), ("GET", "/admin/branches"), ("PUT", "/admin/change-password"),
])
def test_doctor_login_keeps_its_own_routes(method, path):
    u = _staff("DOCTOR", ["OPD_CLINICAL"])
    assert _enforce_opd_role_scope(_req(method, path), u) is u


def test_grant_reopens_route_and_other_roles_untouched():
    desk = _staff("CASHIER", ["OPD_BILLING", "OPD_FRONT_DESK"])
    assert _enforce_opd_role_scope(_req("GET", "/admin/appointments"), desk) is desk
    rep = _staff("DOCTOR", ["OPD_CLINICAL", "REPORTS_VIEW"])
    assert _enforce_opd_role_scope(_req("GET", "/admin/lab-reports"), rep) is rep
    for u in (_staff("STAFF", []), _staff("RECEPTIONIST", ["OPD_FRONT_DESK"]),
              AdminUser("adm", role="clinic_admin", clinic_id="c1")):
        assert _enforce_opd_role_scope(_req("GET", "/admin/patients"), u) is u


# ─── 3. visit branch resolution ───────────────────────────────────────────────


def _resolve(rows, actor_branch=None, explicit=None):
    from app.services import opd

    q = MagicMock()
    q.table.return_value = q
    q.select.return_value = q
    q.eq.return_value = q
    with patch.object(opd, "sb", AsyncMock(return_value=MagicMock(data=rows))), \
         patch.object(opd, "supabase", q):
        return asyncio.run(opd.resolve_visit_branch("d1", SimpleNamespace(branch_id=actor_branch), explicit))


def test_resolve_visit_branch_order():
    assert _resolve([{"branch_id": "B"}], explicit="X") == "X"
    assert _resolve([{"branch_id": "B"}], actor_branch="A") == "A"
    assert _resolve([{"branch_id": "B"}]) == "B"
    assert _resolve([{"branch_id": "B"}, {"branch_id": "C"}]) is None
    assert _resolve([]) is None


# ─── 4. branch-pinned reads ───────────────────────────────────────────────────


def test_read_branch_pins_staff():
    from app.routers.opd import _doctor_only, _read_branch

    pinned = _staff("CASHIER", ["OPD_BILLING"], branch_id="B1")
    assert _read_branch(pinned, None) == "B1"
    assert _read_branch(pinned, "B1") == "B1"
    with pytest.raises(HTTPException) as e:
        _read_branch(pinned, "B2")
    assert e.value.status_code == 403
    free = _staff("CASHIER", ["OPD_BILLING"])
    assert _read_branch(free, None) is None and _read_branch(free, "B2") == "B2"

    assert _doctor_only(_staff("DOCTOR", ["OPD_CLINICAL"], doctor_id="d1"))
    assert not _doctor_only(_staff("DOCTOR", ["OPD_CLINICAL", "OPD_FRONT_DESK"], doctor_id="d1"))
    assert not _doctor_only(_staff("STAFF", ["OPD_CLINICAL"]))
