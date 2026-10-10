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


# ─── 5. doctor consultation earnings ─────────────────────────────────────────


def test_allocate_consultation_rules():
    from app.services.opd_billing import allocate_consultation as a

    assert a(50000, 50000, 0, 50000) == {"fee_paise": 50000, "collected_paise": 50000, "outstanding_paise": 0}
    assert a(50000, 50000, 10000, 40000)["collected_paise"] == 40000      # discounted, paid in full
    assert a(50000, 100000, 10000, 45000)["collected_paise"] == 22500     # shared invoice, half paid
    assert a(50000, 50000, 0, 0)["outstanding_paise"] == 50000
    for args in [(333, 1000, 1, 500), (1, 3, 1, 1), (99999, 100000, 7, 12345)]:
        r = a(*args)
        assert 0 <= r["collected_paise"] <= r["fee_paise"] <= args[0]
        assert r["collected_paise"] + r["outstanding_paise"] == r["fee_paise"]


def test_get_doctor_earnings_sums_only_this_doctors_consultations():
    import datetime as dt

    from app.services import opd_billing

    q = MagicMock()
    for m in ("select", "eq", "in_", "gte", "lte", "order", "range", "limit"):
        getattr(q, m).return_value = q
    invoices = [
        {"id": "i1", "invoice_number": "INV-1", "status": "paid", "issued_at": "2026-10-10T05:00:00+00:00",
         "subtotal_paise": 50000, "discount_paise": 0, "paid_paise": 50000, "patient_snapshot": {"name": "A"}},
        {"id": "i2", "invoice_number": "INV-2", "status": "issued", "issued_at": "2026-10-10T06:00:00+00:00",
         "subtotal_paise": 80000, "discount_paise": 0, "paid_paise": 0, "patient_snapshot": {"name": "B"}},
        {"id": "i3", "invoice_number": "INV-3", "status": "paid", "issued_at": "2026-10-10T07:00:00+00:00",
         "subtotal_paise": 30000, "discount_paise": 0, "paid_paise": 30000, "patient_snapshot": {"name": "C"}},
    ]
    items = [{"invoice_id": "i1", "line_total_paise": 50000}, {"invoice_id": "i2", "line_total_paise": 50000}]
    sb = AsyncMock(side_effect=[MagicMock(data=[{"id": "d1", "name": "Dr X", "department": "GM"}]),
                                MagicMock(data=invoices), MagicMock(data=items)])
    with patch.object(opd_billing, "sb", sb), patch.object(opd_billing, "scoped_query", return_value=q):
        out = asyncio.run(opd_billing.get_doctor_earnings("c1", "d1", dt.date(2026, 10, 10), dt.date(2026, 10, 10)))
    assert out["totals"] == {"visits": 2, "fee_paise": 100000, "collected_paise": 50000, "outstanding_paise": 50000}
    assert [r["invoice_number"] for r in out["rows"]] == ["INV-1", "INV-2"]   # i3 is another doctor's
    assert out["rows"][1]["shared_invoice"] is True
    eqs = [c.args for c in q.eq.call_args_list]
    assert ("doctor_id", "d1") in eqs and ("item_type", "consultation") in eqs
    statuses = [c.args for c in q.in_.call_args_list if c.args[0] == "status"]
    assert statuses == [("status", ["issued", "partially_paid", "paid"])]   # drafts and voids excluded


def test_get_doctor_earnings_rejects_bad_ranges():
    import datetime as dt

    from app.services import opd_billing

    with pytest.raises(HTTPException) as e:
        asyncio.run(opd_billing.get_doctor_earnings("c1", "d1", dt.date(2026, 10, 10), dt.date(2026, 10, 9)))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(opd_billing.get_doctor_earnings("c1", "d1", dt.date(2026, 1, 1), dt.date(2026, 10, 9)))
    assert e.value.status_code == 422


@pytest.mark.parametrize("user,doctor_param,expect", [
    (dict(staff_role="DOCTOR", perms=["OPD_CLINICAL"], doctor_id="d1"), "d2", ("d1", None)),   # own only
    (dict(staff_role="CASHIER", perms=["OPD_BILLING"]), "d2", ("d2", None)),
    (dict(staff_role="CASHIER", perms=["OPD_BILLING"], branch_id="B1"), "d2", ("d2", "B1")),
    (dict(staff_role="CASHIER", perms=["OPD_BILLING"]), None, 422),
    (dict(staff_role="STAFF", perms=["OPD_CLINICAL"]), "d2", 403),                            # no doctor link
])
def test_my_earnings_route_scoping(user, doctor_param, expect):
    from app.routers import opd as opd_router

    u = _staff(user["staff_role"], user["perms"], doctor_id=user.get("doctor_id"), branch_id=user.get("branch_id"))
    earn = AsyncMock(return_value={"ok": True})
    with patch.object(opd_router, "_opd_scope", AsyncMock(return_value=("c1", {"id": "c1"}))), \
         patch.object(opd_router, "get_doctor_earnings", earn):
        call = opd_router.get_my_earnings(from_date=None, to_date=None, doctor_id=doctor_param, clinic_id="c1", user=u)
        if isinstance(expect, int):
            with pytest.raises(HTTPException) as e:
                asyncio.run(call)
            assert e.value.status_code == expect
        else:
            asyncio.run(call)
            args, kwargs = earn.call_args
            assert (args[1], kwargs["branch_id"]) == expect


# ─── 6. branches checklist item reads the multi_branch plan feature ─────────


def _branches_item(plan, features, branch_rows):
    from app.services import opd

    q = MagicMock()
    for m in ("table", "select", "eq", "limit", "is_", "in_", "order"):
        getattr(q, m).return_value = q
    doc = {"id": "d1", "is_active": True}
    responses = [MagicMock(data=[doc])]
    if branch_rows is not None:
        responses.append(MagicMock(data=branch_rows))
    responses.append(MagicMock(data=[]))  # clinic_admins
    clinic = {"id": "c1", "name": "A", "plan": plan, "features": features, "opd_settings": {}}
    with patch.object(opd, "sb", AsyncMock(side_effect=responses)), patch.object(opd, "supabase", q), \
         patch.object(opd, "scoped_query", return_value=q):
        return {i["key"]: i for i in asyncio.run(opd.setup_checklist(clinic))}["branches"]["done"]


def test_branches_check_uses_multi_branch_feature():
    # Feature off: never queried, always done.
    assert _branches_item("soloclinic", {}, None) is True
    # Feature on (override or enterprise wildcard), single site with no branch rows: done.
    assert _branches_item("soloclinic", {"multi_branch": True}, []) is True
    assert _branches_item("enterprise", {}, []) is True
    # Branches exist: at least one must be active.
    assert _branches_item("enterprise", {}, [{"id": "b1", "is_active": False}, {"id": "b2", "is_active": True}]) is True
    assert _branches_item("enterprise", {}, [{"id": "b1", "is_active": False}]) is False
    assert _branches_item("soloclinic", {"multi_branch": True}, [{"id": "b1", "is_active": False}]) is False
