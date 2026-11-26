"""/admin/ri route matrix (docs/revenue-integrity/SPEC.md §6–§7).

1. The route set is pinned: a new /admin/ri route is added here (and to the role matrix) in the same commit.
2. Every /admin/ri route is in both global security matrices (they discover routes from the app).
3. Role matrix on real Postgres: who may read, dismiss, re-run and change settings; branch pin; tenant scope.
"""

import uuid

import pytest

from app.main import app
from tests.test_admin_super_admin_scope_matrix import ROUTES as SCOPE_ROUTES
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_phase2_route_adversarial_matrix import ALL_ADMIN_ROUTES

RI_ROUTES = {
    ("GET", "/admin/ri/summary"), ("GET", "/admin/ri/findings"), ("POST", "/admin/ri/findings/{finding_id}/dismiss"),
    ("POST", "/admin/ri/run"), ("PATCH", "/admin/ri/settings"),
}


def _app_ri_routes() -> set[tuple[str, str]]:
    flat = []
    for r in app.routes:
        flat.extend(getattr(getattr(r, "original_router", None), "routes", None) or [r])
    return {(m, r.path) for r in flat if getattr(r, "path", "").startswith("/admin/ri/")
            for m in (getattr(r, "methods", None) or set()) if m not in ("HEAD", "OPTIONS")}


def test_route_set_is_pinned():
    assert _app_ri_routes() == RI_ROUTES


def test_every_ri_route_is_in_both_security_matrices():
    assert {r for r in ALL_ADMIN_ROUTES if r[1].startswith("/admin/ri/")} == RI_ROUTES
    assert {(m, p) for m, p in SCOPE_ROUTES if p.startswith("/admin/ri/")} == RI_ROUTES


def _finding(conn, clinic, branch=None):
    return str(_q(conn, """INSERT INTO ri_findings (clinic_id, rule_code, kind, source_table, source_id, branch_id,
                             service_date, amount_paise, first_seen_on, last_seen_on)
                           VALUES (%s, 'order_done_not_billed', 'leakage', 'opd_orders', %s, %s, CURRENT_DATE, 35000,
                                   CURRENT_DATE, CURRENT_DATE) RETURNING id""", clinic, str(uuid.uuid4()), branch))


@pytest.fixture
def ri_clinic(api):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "revenue_guard", True).status_code == 200
    return clinic


# (role, stored grants) -> read summary, list, dismiss, re-run, settings
MATRIX = {
    ("clinic_admin", None, ()): (200, 200, 200, 200, 200),
    ("staff", "BRANCH_MANAGER", ()): (200, 200, 200, 200, 403),     # settings are clinic-admin only
    ("staff", "CASHIER", ()): (200, 200, 403, 403, 403),
    ("staff", "FRONT_DESK", ("RI_VIEW",)): (200, 200, 403, 403, 403),
    ("staff", "FRONT_DESK", ()): (403, 403, 403, 403, 403),
    ("staff", "DOCTOR", ("OPD_CLINICAL",)): (403, 403, 403, 403, 403),
    ("staff", "NURSE", ("IPD_NURSING",)): (403, 403, 403, 403, 403),
}


@pytest.mark.parametrize("role,staff_role,grants", sorted(MATRIX, key=str))
def test_role_matrix(api, ri_clinic, role, staff_role, grants):  # noqa: F811
    f = _finding(api.conn, ri_clinic)
    api.as_(api.U("u", role, ri_clinic, str(uuid.uuid4()), list(grants), staff_role=staff_role))
    got = (api.call("GET", "/admin/ri/summary", ri_clinic).status_code,
           api.call("GET", "/admin/ri/findings", ri_clinic).status_code,
           api.call("POST", f"/admin/ri/findings/{f}/dismiss", ri_clinic, {"reason": "Charity patient"}).status_code,
           api.call("POST", "/admin/ri/run", ri_clinic, {}).status_code,
           api.call("PATCH", "/admin/ri/settings", ri_clinic, {"review_free_days": 7}).status_code)
    assert got == MATRIX[(role, staff_role, grants)]


def test_dismiss_rules_audit_and_x14_visibility(api, ri_clinic, monkeypatch):  # noqa: F811
    import app.routers.revenue_guard as route
    audits = []

    async def _log(**kw):
        audits.append((kw["action"], kw["resource_id"], kw["details"]))
    monkeypatch.setattr(route, "log_admin_action", _log)
    f = _finding(api.conn, ri_clinic)
    api.as_(api.U("admin", "clinic_admin", ri_clinic, str(uuid.uuid4())))
    assert api.call("POST", f"/admin/ri/findings/{f}/dismiss", ri_clinic, {"reason": "no"}).status_code == 422
    assert api.call("POST", f"/admin/ri/findings/{f}/dismiss", ri_clinic, {"reason": "Waived", "x": 1}).status_code == 422
    assert api.call("POST", f"/admin/ri/findings/{f}/dismiss", ri_clinic, {"reason": "Waived by MD"}).status_code == 200
    assert api.call("POST", f"/admin/ri/findings/{f}/dismiss", ri_clinic, {"reason": "Waived by MD"}).status_code == 409
    s = api.call("GET", "/admin/ri/summary", ri_clinic).json()
    assert (s["dismissed_paise"], s["dismissed_count"]) == (35000, 1)            # X14: dismissals are shown in ₹
    owner = api.client.get(f"/platform/clinics/{ri_clinic}/ri/value").json()
    assert owner["access"] is True and owner["dismissed_paise"] == 35000
    assert audits == [("ri_dismiss", f, {"rule": "order_done_not_billed", "amount_paise": 35000, "reason": "Waived by MD"})]
    lst = api.call("GET", "/admin/ri/findings", ri_clinic, status="dismissed").json()
    assert lst["total"] == 1 and lst["findings"][0]["dismiss_reason"] == "Waived by MD"
    assert api.call("GET", "/admin/ri/findings", ri_clinic, rule="made_up").status_code == 422


def test_branch_pinned_login_sees_its_branch_only(api, ri_clinic):  # noqa: F811
    b1 = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", ri_clinic))
    b2 = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", ri_clinic))
    mine, theirs = _finding(api.conn, ri_clinic, b1), _finding(api.conn, ri_clinic, b2)
    api.as_(api.U("bm", "staff", ri_clinic, str(uuid.uuid4()), [], staff_role="BRANCH_MANAGER", branch_id=b1))
    ids = [f["id"] for f in api.call("GET", "/admin/ri/findings", ri_clinic, branch_id=b2).json()["findings"]]
    assert ids == [mine]                                       # asking for another branch still answers its own
    assert api.call("POST", f"/admin/ri/findings/{theirs}/dismiss", ri_clinic, {"reason": "Not ours"}).status_code == 404
    assert api.call("GET", "/admin/ri/summary", ri_clinic).json()["by_kind"]["leakage"]["count"] == 1


def test_cross_tenant_is_refused(api, ri_clinic):  # noqa: F811
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "revenue_guard", True).status_code == 200
    f = _finding(api.conn, ri_clinic)
    api.as_(api.U("admin", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", "/admin/ri/findings", ri_clinic).status_code == 403
    assert api.call("POST", f"/admin/ri/findings/{f}/dismiss", other, {"reason": "Not mine at all"}).status_code == 404


def test_settings_patch_is_bounded(api, ri_clinic):  # noqa: F811
    api.as_(api.U("admin", "clinic_admin", ri_clinic, str(uuid.uuid4())))
    assert api.call("PATCH", "/admin/ri/settings", ri_clinic, {"review_free_days": 31}).status_code == 422
    assert api.call("PATCH", "/admin/ri/settings", ri_clinic, {"made_up": 1}).status_code == 422
    r = api.call("PATCH", "/admin/ri/settings", ri_clinic, {"min_opd_invoices_30d": 5, "exempt_reasons": ["Staff"]})
    assert r.status_code == 200 and r.json()["settings"]["min_opd_invoices_30d"] == 5
    assert api.call("GET", "/admin/ri/summary", ri_clinic).json()["settings"]["exempt_reasons"] == ["staff"]
