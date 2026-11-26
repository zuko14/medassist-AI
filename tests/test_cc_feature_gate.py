"""Continuous Care entitlement (P1; docs/continuous-care/TEST-PLAN.md §2, ADR-CC-01).

Off by default on every plan; the enterprise wildcard alone never enables it; needs OPD OS and a regular tenant;
/admin/me cc_* keys; the owner toggle (400 without OPD, 409 preview with enrolled patients → WIND_DOWN → OFF once
none is active); OPD cannot be disabled while CC is on; permissions and roles.
"""

import uuid

from app.routers.admin import AdminUser
from app.services.permissions import (CC_PERMISSIONS, PERMISSIONS, ROLE_PRESETS, STAFF_ROLES, holds_cc_permission,
                                      role_grants_beyond_authority, role_implied_grants)
from app.services.tenant import OPT_IN_FEATURES, care_access, care_eligible, has_feature
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)


def _c(plan="polyclinic", on=None, opd=True, **kw):
    feats = {"opd_enabled": True} if opd else {}
    if on is not None:
        feats["care_enabled"] = on
    return {"plan": plan, "account_type": "tenant", "features": feats, **kw}


def test_off_by_default_and_wildcard_never_enables():
    assert "care_enabled" in OPT_IN_FEATURES
    for plan in ("soloclinic", "polyclinic", "multispecialty", "enterprise", "diagstream", "dental", "pharmacy"):
        assert care_access(_c(plan)) is None, plan
    ent = _c("enterprise")
    assert has_feature(ent, "care_enabled") is True and care_access(ent) is None
    assert care_access(None) is None and not care_eligible(None)


def test_needs_opd_a_tenant_and_an_explicit_true():
    assert care_access(_c(on=True)) == "ACTIVE"
    assert care_access(_c(on=True, opd=False)) is None
    assert care_access({**_c(on=True), "account_type": "corporate_partner"}) is None
    assert not care_eligible({**_c(), "account_type": "corporate_partner"})
    assert care_access(_c(on="true")) is None and care_access({**_c(), "features": "junk"}) is None
    # WIND_DOWN: the owner switched it off while patients were enrolled
    assert care_access(_c(on=False, cc_state="WIND_DOWN")) == "WIND_DOWN"
    assert care_access(_c(on=False, cc_state="OFF")) is None
    assert care_access({**_c(on=False, cc_state="WIND_DOWN"), "account_type": "corporate_partner"}) is None


def _u(role, perms=(), staff_role=None, doctor_id=None):
    return AdminUser("u", role, "c1", str(uuid.uuid4()), list(perms), staff_role=staff_role, doctor_id=doctor_id)


def test_permissions_and_roles():
    assert set(CC_PERMISSIONS) == {"CC_VIEW", "CC_COORDINATE", "CC_APPROVE", "CC_ADMIN"} <= PERMISSIONS
    assert "CARE_COORDINATOR" in STAFF_ROLES and ROLE_PRESETS["CARE_COORDINATOR"] == ["CC_VIEW", "CC_COORDINATE"]
    admin = _u("clinic_admin")
    assert all(holds_cc_permission(admin, p) for p in ("CC_VIEW", "CC_COORDINATE", "CC_ADMIN"))
    assert not holds_cc_permission(admin, "CC_APPROVE")                     # no doctor row, no signing
    assert holds_cc_permission(_u("clinic_admin", doctor_id="d1"), "CC_APPROVE")
    assert not holds_cc_permission(_u("super_admin"), "CC_APPROVE")
    doc = _u("staff", staff_role="DOCTOR", doctor_id="d1")
    assert holds_cc_permission(doc, "CC_VIEW") and holds_cc_permission(doc, "CC_APPROVE")
    assert not holds_cc_permission(doc, "CC_COORDINATE") and not holds_cc_permission(doc, "CC_ADMIN")
    assert not holds_cc_permission(_u("staff", staff_role="DOCTOR"), "CC_APPROVE")      # doctor role, no doctor row
    coord = _u("staff", staff_role="CARE_COORDINATOR")
    assert holds_cc_permission(coord, "CC_COORDINATE") and not holds_cc_permission(coord, "CC_APPROVE")
    assert holds_cc_permission(_u("staff", staff_role="NURSE"), "CC_VIEW")
    assert not holds_cc_permission(_u("staff", staff_role="NURSE"), "CC_COORDINATE")
    for role in ("FRONT_DESK", "CASHIER", "LAB_TECH"):
        assert not any(holds_cc_permission(_u("staff", staff_role=role), p) for p in CC_PERMISSIONS), role
    assert holds_cc_permission(_u("staff", ["CC_ADMIN"], "FRONT_DESK"), "CC_ADMIN")
    # zero impact on staff creation: DOCTOR / NURSE logins are capped exactly as before migration 145
    for role in ("DOCTOR", "NURSE", "FRONT_DESK", "CASHIER"):
        assert not {p for p in role_implied_grants(role) if p.startswith("CC_")}, role
        assert not [g for g in role_grants_beyond_authority(role, _u("staff", ["STAFF_CREATE"])) if g.startswith("CC_")]
    assert {"CC_VIEW", "CC_COORDINATE"} <= role_implied_grants("CARE_COORDINATOR")


def test_owner_toggle_needs_opd_and_a_tenant(api):  # noqa: F811
    plain = _clinic(api.conn, opd=False)
    r = api.owner_toggle(plain, "care_enabled", True)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "care_requires_opd"
    partner = _clinic(api.conn)
    _q(api.conn, "UPDATE clinics SET account_type = 'corporate_partner' WHERE id = %s", partner)
    assert api.owner_toggle(partner, "care_enabled", True).status_code == 400
    assert _q(api.conn, "SELECT cc_state FROM clinics WHERE id = %s", partner) == "OFF"


def test_me_keys_and_off_on_off_without_enrollments(api):  # noqa: F811
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert (me["cc_access"], me["cc_state"], me["cc_perms"], me["cc_is_doctor_signer"]) == (None, "OFF", [], False)
    assert api.call("GET", "/admin/cc/setup", clinic).status_code == 403
    assert api.owner_toggle(clinic, "care_enabled", True).status_code == 200
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert me["cc_access"] == "ACTIVE" and me["cc_state"] == "ACTIVE"
    assert me["cc_perms"] == ["CC_ADMIN", "CC_COORDINATE", "CC_VIEW"] and me["cc_is_doctor_signer"] is False
    r = api.call("GET", "/admin/cc/setup", clinic)
    assert r.status_code == 200 and {i["key"] for i in r.json()["checklist"]} >= {"templates", "activation", "coordinator"}
    assert api.owner_toggle(clinic, "care_enabled", False).status_code == 200     # nothing enrolled: straight OFF
    assert _q(api.conn, "SELECT cc_state FROM clinics WHERE id = %s", clinic) == "OFF"
    assert api.call("GET", "/admin/cc/setup", clinic).status_code == 403


def test_wind_down_with_enrolled_patients(care):  # noqa: F811
    api, clinic = care.api, care.clinic
    e, _ = care.enroll()
    r = api.owner_toggle(clinic, "care_enabled", False)
    assert r.status_code == 409 and r.json()["detail"]["preview"]["active_enrollments"] == 1
    assert _q(api.conn, "SELECT cc_state FROM clinics WHERE id = %s", clinic) == "ACTIVE"
    # OPD cannot be switched off while care is on (care plans come from OPD prescriptions)
    r = api.owner_toggle(clinic, "opd_enabled", False)
    assert r.status_code == 409 and "Continuous Care" in r.json()["detail"]
    assert api.owner_toggle(clinic, "care_enabled", False, confirm=True).status_code == 200
    assert _q(api.conn, "SELECT cc_state FROM clinics WHERE id = %s", clinic) == "WIND_DOWN"
    # every enrolled person was ended with the one "programme ended" message attempt
    assert _q(api.conn, "SELECT status FROM cc_enrollments WHERE id = %s", e["id"]) == "ended_by_clinic"
    api.as_(care.u.admin)
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert me["cc_access"] == "WIND_DOWN"
    assert api.call("GET", "/admin/cc/cases", clinic).status_code == 200                 # open work still readable
    r = api.call("POST", "/admin/cc/activations", clinic, {"protocol_code": "htn"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "care_wind_down"     # no new work
    assert api.owner_toggle(clinic, "opd_enabled", False).status_code == 409
    import asyncio
    from app.services import cc
    assert asyncio.run(cc.finish_wind_down(clinic)) is True                             # nightly job: WIND_DOWN → OFF
    assert _q(api.conn, "SELECT cc_state FROM clinics WHERE id = %s", clinic) == "OFF"
    assert api.call("GET", "/admin/cc/cases", clinic).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM cc_enrollments WHERE clinic_id = %s", clinic) == 1   # nothing deleted


def test_doctor_sees_signer_flag(care):  # noqa: F811
    api = care.api
    api.as_(care.u.doctor)
    me = api.client.get("/admin/me", params={"clinic_id": care.clinic}).json()
    assert me["cc_is_doctor_signer"] is True and me["cc_perms"] == ["CC_APPROVE", "CC_VIEW"]
