"""Diagnostic OS foundation (P1: DX-03 … DX-09).

Pure rules: entitlement by plan (ADR-DX-02), DX permissions and roles
(SECURITY §3), confined logins. Then real HTTP routes on real Postgres
(tests/ipd_pg_bridge.py): owner enable / wind-down / re-enable (ADR-DX-14),
setup, settings CAS, sites with branch and tenant scope, go-live, /admin/me,
LIS / G1 worklist widened for DX-only labs, and the purge order on deletion.
"""

import json
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers.admin import AdminUser, _enforce_dx_confined_scope
from app.services import dx as dx_service
from app.services.permissions import (
    PERMISSIONS,
    ROLE_PRESETS,
    STAFF_ROLES,
    holds_dx_permission,
    role_grants_beyond_authority,
)
from app.services.tenant import OPT_IN_FEATURES, dx_access, dx_eligible, has_feature
from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)

DX_PERMS = ("DX_READ", "DX_REGISTER", "DX_COLLECT", "DX_BILLING", "DX_RECEIVE", "DX_VERIFY", "DX_DISPATCH",
            "DX_LOGISTICS", "DX_CATALOGUE", "DX_CATALOGUE_APPROVE", "DX_B2B_MANAGE", "DX_REPORTS_MIS", "DX_QC",
            "DX_ADMIN")


def _c(plan, on=None, **kw):
    feats = {} if on is None else {"dx_enabled": on}
    return {"plan": plan, "account_type": "tenant", "features": feats, **kw}


# ── entitlement (pure) ───────────────────────────────────────────────────────

def test_dx_off_by_default():
    assert "dx_enabled" in OPT_IN_FEATURES
    for plan in ("diagstream", "diagbooking", "polyclinic", "enterprise"):
        assert dx_access(_c(plan)) is None, plan
    assert dx_access(None) is None and dx_eligible(None) is False


def test_dx_eligibility_follows_the_plans_own_lab_features():
    eligible = ("diagstream", "diagbooking", "essential", "polyclinic", "multispecialty", "womenchild", "ivf",
                "enterprise")
    for plan in eligible:
        assert dx_eligible(_c(plan)), plan
        assert dx_access(_c(plan, True)) == "ACTIVE", plan
    for plan in ("soloclinic", "derma", "eye", "dental"):
        assert not dx_eligible(_c(plan)), plan
        assert dx_access(_c(plan, True)) is None, plan          # a stray flag on an ineligible plan does nothing


def test_dx_enterprise_wildcard_does_not_enable():
    ent = _c("enterprise")
    assert has_feature(ent, "dx_enabled") is True                 # the wildcard answers yes to everything …
    assert dx_access(ent) is None                                 # … and DX ignores it
    assert dx_access(_c("enterprise", False)) is None


def test_dx_partner_account_refused():
    partner = {**_c("diagstream", True), "account_type": "corporate_partner"}
    assert not dx_eligible(partner) and dx_access(partner) is None


def test_dx_does_not_need_opd():
    lab = _c("diagstream", True)
    assert not has_feature(lab, "booking")
    assert dx_access(lab) == "ACTIVE"


def test_wind_down_survives_the_flag_being_cleared():
    assert dx_access(_c("diagstream", False, dx_state="WIND_DOWN")) == "WIND_DOWN"
    assert dx_access(_c("diagstream", None, dx_state="OFF")) is None
    assert dx_access({**_c("diagstream", True), "features": "not-a-dict"}) is None


# ── permissions (pure) ───────────────────────────────────────────────────────

def _staff(role, perms=(), branch=None):
    return AdminUser("u", "staff", "c1", str(uuid.uuid4()), list(perms), branch_id=branch, staff_role=role)


def test_dx_permissions_and_roles_exist():
    assert set(DX_PERMS) <= PERMISSIONS
    new_roles = ("LAB_SUPERVISOR", "LOGISTICS_RUNNER", "PROCESSING_ADMIN", "B2B_MANAGER", "B2B_PARTNER")
    assert set(new_roles) <= STAFF_ROLES
    for role in new_roles:
        assert role in ROLE_PRESETS and set(ROLE_PRESETS[role]) <= PERMISSIONS, role
    assert ROLE_PRESETS["B2B_PARTNER"] == []
    assert "DX_BILLING" not in ROLE_PRESETS["PROCESSING_ADMIN"] and "DX_B2B_MANAGE" not in ROLE_PRESETS["PROCESSING_ADMIN"]


def test_existing_logins_gain_only_the_listed_implied_grants():
    implied = {"DIAGNOSTIC_OPERATOR": {"DX_READ"}, "LAB_OPERATOR": {"DX_READ", "DX_RECEIVE"},
               "PATHOLOGIST": {"DX_READ", "DX_VERIFY"}}
    for role in ("FRONT_DESK", "RECEPTIONIST", "CASHIER", "DOCTOR", "NURSE", "PHLEBOTOMIST", "STAFF",
                 "DIAGNOSTIC_OPERATOR", "LAB_OPERATOR", "PATHOLOGIST"):
        got = {p for p in DX_PERMS if holds_dx_permission(_staff(role), p)}
        assert got == implied.get(role, set()), role
    # a stored grant works for any role; admins hold everything
    assert holds_dx_permission(_staff("FRONT_DESK", ["DX_REGISTER"]), "DX_REGISTER")
    assert all(holds_dx_permission(AdminUser("a", "clinic_admin", "c1"), p) for p in DX_PERMS)


def test_new_roles_imply_their_presets():
    for role in ("LAB_SUPERVISOR", "LOGISTICS_RUNNER", "PROCESSING_ADMIN", "B2B_MANAGER"):
        for p in (g for g in ROLE_PRESETS[role] if g.startswith("DX_")):
            assert holds_dx_permission(_staff(role), p), (role, p)
    assert not holds_dx_permission(_staff("LOGISTICS_RUNNER"), "DX_ADMIN")
    assert not holds_dx_permission(_staff("B2B_PARTNER"), "DX_READ")


def test_staff_grant_cap_covers_new_roles_but_not_existing_lab_roles():
    granter = _staff("BRANCH_MANAGER", ["STAFF_CREATE", "LAB_ENTRY"])
    # a granter without DX_ADMIN cannot mint a processing admin by picking the role …
    assert {"DX_ADMIN", "DX_DISPATCH"} <= set(role_grants_beyond_authority("PROCESSING_ADMIN", granter))
    # … but creating a lab technician on a clinic that never enabled DX works exactly as before
    assert role_grants_beyond_authority("LAB_OPERATOR", granter) == []


# ── confined logins (pure) ───────────────────────────────────────────────────

def _req(method, path):
    return Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""})


def test_logistics_runner_and_partner_are_confined():
    runner, partner = _staff("LOGISTICS_RUNNER"), _staff("B2B_PARTNER")
    for user, ok in ((runner, "/admin/dx/logistics/batches"), (partner, "/admin/dx/partner/visits")):
        assert _enforce_dx_confined_scope(_req("GET", ok), user) is user
        assert _enforce_dx_confined_scope(_req("GET", "/admin/me"), user) is user
        assert _enforce_dx_confined_scope(_req("PUT", "/admin/change-password"), user) is user
        for method, path in (("GET", "/admin/patients"), ("GET", "/admin/dx/setup"), ("GET", "/fhir/Patient/x"),
                             ("POST", "/admin/me")):
            with pytest.raises(HTTPException) as e:
                _enforce_dx_confined_scope(_req(method, path), user)
            assert e.value.status_code == 403, (user.staff_role, path)
    with pytest.raises(HTTPException):
        _enforce_dx_confined_scope(_req("GET", "/admin/dx/logistics"), partner)
    tech = _staff("LAB_OPERATOR")
    assert _enforce_dx_confined_scope(_req("GET", "/admin/patients"), tech) is tech


# ── settings validation (pure) ───────────────────────────────────────────────

def test_settings_defaults_and_validation():
    s = dx_service.get_dx_settings({"dx_settings": {}})
    assert (s["approval_levels"], s["collect_before_payment"], s["auto_release"], s["sample_seq_floor"],
            s["accession_prefix"], s["version"]) == (2, False, "when_complete", 1, "LAB-", 0)
    for bad in ({"approval_levels": 1}, {"accession_prefix": "lab prefix!"}, {"extra_labels": 9},
                {"unknown_key": True}, {"delivery_owner": {"b1": "email"}}):
        with pytest.raises(HTTPException) as e:
            dx_service.validate_settings({}, bad)
        assert e.value.status_code == 422, bad
    with pytest.raises(HTTPException) as e:
        dx_service.validate_settings({"went_live_at": "2026-11-01T00:00:00Z", "sample_seq_floor": 16600},
                                     {"sample_seq_floor": 100})
    assert e.value.status_code == 409
    assert dx_service._site_code("Banjara Hills", set()) == "BANJ"
    assert dx_service._site_code("Banjara Hills", {"BANJ"}) == "BANJ2"
    assert dx_service._site_code("", set()) == "SITE"


# ── HTTP + Postgres ──────────────────────────────────────────────────────────

def _lab(conn, plan="diagstream"):
    return str(_q(conn, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('DX Lab', %s, %s, true) "
                        "RETURNING id", "+9176" + uuid.uuid4().hex[:8], plan))


def _branch(conn, clinic, name):
    return str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, %s) RETURNING id", clinic, name))


@pytest.fixture
def lab(api):  # noqa: F811
    clinic = _lab(api.conn)
    b1, b2 = _branch(api.conn, clinic, "Banjara Hills"), _branch(api.conn, clinic, "Kukatpally")
    U, uid = api.U, lambda: str(uuid.uuid4())
    return {"clinic": clinic, "b1": b1, "b2": b2,
            "admin": U("admin", "clinic_admin", clinic, uid()),
            "tech": U("tech", "staff", clinic, uid(), ["ORDERS_READ", "ORDERS_COLLECT"], staff_role="LAB_OPERATOR"),
            "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK"),
            "padmin": U("padmin", "staff", clinic, uid(), [], staff_role="PROCESSING_ADMIN", branch_id=b2)}


def test_module_is_off_until_the_owner_enables_it(api, lab):  # noqa: F811
    api.as_(lab["admin"])
    r = api.call("GET", "/admin/dx/setup", lab["clinic"])
    assert r.status_code == 403 and "not enabled" in r.text
    me = api.call("GET", "/admin/me", lab["clinic"]).json()
    assert (me["dx_access"], me["dx_state"], me["dx_site_ids"]) == (None, "OFF", None)


def test_ineligible_plan_and_partner_are_refused_by_the_owner_toggle(api):  # noqa: F811
    solo = _lab(api.conn, "soloclinic")
    assert api.owner_toggle(solo, "dx_enabled", True).status_code == 400
    partner = _lab(api.conn)
    _q(api.conn, "UPDATE clinics SET account_type = 'corporate_partner' WHERE id = %s", partner)
    assert api.owner_toggle(partner, "dx_enabled", True).status_code == 400
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", solo) == "OFF"


def test_enable_provisions_sites_and_opens_setup(api, lab):  # noqa: F811
    r = api.owner_toggle(lab["clinic"], "dx_enabled", True)
    assert r.status_code == 200, r.text
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", lab["clinic"]) == "ACTIVE"
    setup = api.as_(lab["admin"]).call("GET", "/admin/dx/setup", lab["clinic"])
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["access"] == "ACTIVE" and body["settings"]["approval_levels"] == 2
    assert sorted(s["code"] for s in body["sites"]) == ["BANJ", "KUKA"]
    assert {c["key"]: c["done"] for c in body["checklist"]}["sites"] is True
    # re-enabling never duplicates or overwrites sites
    api.as_(lab["admin"]).call("PUT", f"/admin/dx/sites/{body['sites'][0]['id']}", lab["clinic"],
                               {"display_name": "Banjara Hub"})
    assert api.owner_toggle(lab["clinic"], "dx_enabled", True).status_code == 200
    assert _q(api.conn, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", lab["clinic"]) == 2
    assert _q(api.conn, "SELECT count(*) FROM dx_sites WHERE display_name = 'Banjara Hub' AND clinic_id = %s",
              lab["clinic"]) == 1
    # an OPD-less lab: OPD routes stay closed, DX is open
    assert api.call("GET", "/admin/opd/setup", lab["clinic"]).status_code in (403, 404)


def test_every_route_checks_permission_first(api, lab):  # noqa: F811
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    api.as_(lab["desk"])
    for method, path, body in (("GET", "/admin/dx/setup", None), ("GET", "/admin/dx/sites", None),
                               ("PATCH", "/admin/dx/settings", {"version": 0}),
                               ("POST", "/admin/dx/go-live", {"version": 0})):
        r = api.call(method, path, lab["clinic"], body)
        assert r.status_code == 403 and "Missing permission" in r.text, (path, r.text)
    # implied DX_READ: the technician reads setup but cannot change settings
    api.as_(lab["tech"])
    assert api.call("GET", "/admin/dx/setup", lab["clinic"]).status_code == 200
    assert api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 0}).status_code == 403


def test_settings_cas_and_go_live(api, lab, monkeypatch):  # noqa: F811
    from app.routers import dx as dx_router
    audits = []

    async def _spy(**kw):
        audits.append(kw["action"])
    monkeypatch.setattr(dx_router, "log_admin_action", _spy)
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    api.as_(lab["admin"])
    r = api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 0, "approval_levels": 3,
                                                                "accession_prefix": "BH-"})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert (s["approval_levels"], s["accession_prefix"], s["version"]) == (3, "BH-", 1)
    stale = api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 0, "approval_levels": 2})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "dx_settings_conflict"
    assert api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 1, "approval_levels": 4}).status_code == 422
    assert api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 1, "went_live_at": "x"}).status_code == 422
    live = api.call("POST", "/admin/dx/go-live", lab["clinic"], {"version": 1})
    assert live.status_code == 200, live.text
    assert live.json()["settings"]["went_live_at"] and live.json()["settings"]["version"] == 2
    assert api.call("POST", "/admin/dx/go-live", lab["clinic"], {"version": 2}).status_code == 409
    assert audits == ["dx_settings_update", "dx_go_live"]      # refused writes are not audited as changes


def test_go_live_needs_a_processing_site(api, lab):  # noqa: F811
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    _q(api.conn, "UPDATE dx_sites SET kind = 'collection' WHERE clinic_id = %s", lab["clinic"])
    r = api.as_(lab["admin"]).call("POST", "/admin/dx/go-live", lab["clinic"], {"version": 0})
    assert r.status_code == 409 and "processing site" in r.text


def test_sites_are_branch_and_tenant_scoped(api, lab):  # noqa: F811
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    _q(api.conn, "DELETE FROM dx_sites WHERE clinic_id = %s AND branch_id = %s", lab["clinic"], lab["b2"])
    other = _lab(api.conn)
    their_branch = _branch(api.conn, other, "Theirs")
    api.as_(lab["admin"])
    r = api.call("POST", "/admin/dx/sites", lab["clinic"], {"branch_id": their_branch, "code": "XX",
                                                           "display_name": "Leak"})
    assert r.status_code == 404
    dup = api.call("POST", "/admin/dx/sites", lab["clinic"], {"branch_id": lab["b2"], "code": "BANJ",
                                                             "display_name": "Dup code"})
    assert dup.status_code == 409 and "code" in dup.text
    # a branch-pinned processing admin sees and edits only its own branch's site
    api.as_(lab["padmin"])
    made = api.call("POST", "/admin/dx/sites", lab["clinic"], {"branch_id": lab["b2"], "code": "KPHB",
                                                              "display_name": "Kukatpally lab", "kind": "processing",
                                                              "nabl_certificate_no": "MC-1234"})
    assert made.status_code == 201, made.text
    assert [s["code"] for s in api.call("GET", "/admin/dx/sites", lab["clinic"]).json()["sites"]] == ["KPHB"]
    banj = _q(api.conn, "SELECT id::text FROM dx_sites WHERE clinic_id = %s AND code = 'BANJ'", lab["clinic"])
    assert api.call("PUT", f"/admin/dx/sites/{banj}", lab["clinic"], {"display_name": "Nope"}).status_code == 403
    assert api.call("PATCH", "/admin/dx/settings", lab["clinic"], {"version": 0}).status_code == 403
    me = api.call("GET", "/admin/me", lab["clinic"]).json()
    assert me["dx_access"] == "ACTIVE" and me["dx_site_ids"] == [made.json()["id"]]
    # null never reaches a NOT NULL column; clearing an optional field works
    api.as_(lab["admin"])
    r = api.call("PUT", f"/admin/dx/sites/{made.json()['id']}", lab["clinic"],
                 {"display_name": None, "nabl_certificate_no": None})
    assert r.status_code == 200 and r.json()["display_name"] == "Kukatpally lab"
    assert r.json()["nabl_certificate_no"] is None
    # another tenant's admin cannot read this clinic's sites
    api.as_(api.U("admin2", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", "/admin/dx/sites", lab["clinic"]).status_code in (403, 404)


def test_owner_disable_and_wind_down_and_reenable(api, lab, monkeypatch):  # noqa: F811
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    # nothing in process: straight to OFF, routes close, data stays
    r = api.owner_toggle(lab["clinic"], "dx_enabled", False)
    assert r.status_code == 200 and _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", lab["clinic"]) == "OFF"
    assert api.as_(lab["admin"]).call("GET", "/admin/dx/setup", lab["clinic"]).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", lab["clinic"]) == 2
    # work in process: 409 with the preview, then confirm -> WIND_DOWN
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    busy = {"visits_in_process": 3, "samples_in_transit": 1, "unreleased_results": 2, "blocking": True}

    async def _busy(clinic_id):
        return busy
    monkeypatch.setattr(dx_service, "deactivation_preview", _busy)
    r = api.owner_toggle(lab["clinic"], "dx_enabled", False)
    assert r.status_code == 409 and r.json()["detail"]["preview"]["visits_in_process"] == 3
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", lab["clinic"]) == "ACTIVE"
    preview = api.client.get(f"/platform/clinics/{lab['clinic']}/dx/deactivation-preview").json()
    assert preview["blocking"] and preview["state"] == "ACTIVE" and preview["eligible"]
    assert api.owner_toggle(lab["clinic"], "dx_enabled", False, confirm=True).status_code == 200
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", lab["clinic"]) == "WIND_DOWN"
    # wind-down: reads work, nothing new starts
    api.as_(lab["admin"])
    assert api.call("GET", "/admin/dx/setup", lab["clinic"]).json()["access"] == "WIND_DOWN"
    r = api.call("POST", "/admin/dx/sites", lab["clinic"], {"branch_id": lab["b1"], "code": "NEW",
                                                           "display_name": "New site"})
    assert r.status_code == 409 and "switched off" in r.text
    assert api.call("POST", "/admin/dx/go-live", lab["clinic"], {"version": 0}).status_code == 409
    # re-enable: ACTIVE with data intact
    assert api.owner_toggle(lab["clinic"], "dx_enabled", True).status_code == 200
    assert _q(api.conn, "SELECT dx_state FROM clinics WHERE id = %s", lab["clinic"]) == "ACTIVE"
    assert _q(api.conn, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", lab["clinic"]) == 2


def test_lis_and_worklist_widened_for_a_dx_only_lab(api, lab):  # noqa: F811
    api.as_(lab["tech"])
    for path in ("/admin/lis/results", "/admin/diagnostic/worklist"):
        r = api.call("GET", path, lab["clinic"])
        assert r.status_code == 403 and "Diagnostic OS" in r.text, path     # no OPD, IPD or DX: refused
    api.owner_toggle(lab["clinic"], "dx_enabled", True)
    for path in ("/admin/lis/results", "/admin/diagnostic/worklist"):
        r = api.call("GET", path, lab["clinic"])
        assert r.status_code == 200, (path, r.text)


def test_opd_clinic_reaches_lis_unchanged(api):  # noqa: F811
    clinic = _clinic(api.conn)          # polyclinic with OPD, DX never enabled
    tech = api.U("tech", "staff", clinic, str(uuid.uuid4()), [], staff_role="LAB_OPERATOR")
    assert api.as_(tech).call("GET", "/admin/lis/results", clinic).status_code == 200
    assert api.call("GET", "/admin/dx/setup", clinic).status_code == 403


def test_clinic_deletion_purges_dx_before_opd(api, monkeypatch):  # noqa: F811
    from app.database import supabase
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    _branch(api.conn, clinic, "Main")
    assert api.owner_toggle(clinic, "dx_enabled", True).status_code == 200
    admin = api.U("admin", "clinic_admin", clinic, str(uuid.uuid4()))
    walk = api.as_(admin).call("POST", "/admin/opd/walk-ins", clinic,
                               {"patient_id": _patient(api.conn, clinic), "doctor_id": doctor, "skip_vitals": True})
    assert walk.status_code == 201, walk.text
    dr = api.U("drmeena", "staff", clinic, str(uuid.uuid4()), ["OPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor)
    enc = api.as_(dr).call("GET", f"/admin/opd/encounters/by-appointment/{walk.json()['appointment_id']}", clinic)
    assert enc.status_code == 200, enc.text           # an OPD record now exists: deletion takes the purge path
    calls, real_rpc = [], supabase.rpc
    monkeypatch.setattr(supabase, "rpc", lambda name, params=None, **kw: (calls.append(name),
                                                                           real_rpc(name, params, **kw))[1])
    try:
        r = api.client.delete(f"/platform/clinics/{clinic}", params={"confirm_opd_purge": True})
        assert r.status_code == 200, r.text
        purges = [c for c in calls if c.endswith("_purge_clinic")]
        assert purges[0] == "dx_purge_clinic" and purges[1] in ("opd_purge_clinic", "ipd_purge_clinic"), purges
        assert _q(api.conn, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", clinic) == 0
    finally:   # an open walk-in would block 103_down in the shared test database
        _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def test_settings_rpc_payload_is_json_object():
    merged = dx_service.validate_settings({}, {"approval_levels": 3})
    assert json.loads(json.dumps(merged))["approval_levels"] == 3
