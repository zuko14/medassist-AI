"""Revenue Guard entitlement (RI-02): owner opt-in, OFF by default, never via the enterprise wildcard, needs a billing
module; permissions; /admin/me additive keys; the owner toggle; the Ketu action's gate."""

import uuid

from app.routers.admin import AdminUser
from app.services.permissions import (PERMISSIONS, RI_PERMISSIONS, holds_ri_permission, role_grants_beyond_authority,
                                      role_implied_grants)
from app.services.tenant import OPT_IN_FEATURES, has_feature, ri_access, ri_eligible, ri_modules
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)


def _c(plan="polyclinic", on=None, opd=True, **kw):
    feats = {"opd_enabled": True} if opd else {}
    if on is not None:
        feats["revenue_guard"] = on
    return {"plan": plan, "account_type": "tenant", "features": feats, **kw}


def test_off_by_default_and_wildcard_never_enables():
    assert "revenue_guard" in OPT_IN_FEATURES
    for plan in ("soloclinic", "polyclinic", "multispecialty", "enterprise", "diagstream"):
        assert ri_access(_c(plan)) is False, plan
    ent = _c("enterprise")
    assert has_feature(ent, "revenue_guard") is True and ri_access(ent) is False
    assert ri_access(None) is False and ri_eligible(None) is False


def test_needs_a_billing_module_and_a_tenant():
    assert ri_access(_c(on=True)) is True and ri_modules(_c(on=True)) == ["opd"]
    assert ri_access(_c(on=True, opd=False)) is False                    # nothing to read
    assert ri_access({**_c(on=True), "account_type": "corporate_partner"}) is False
    assert ri_access({**_c(on=True), "features": "junk"}) is False
    full = _c(on=True)
    full["features"].update(ipd_enabled=True, pharmacy_enabled=True)
    assert ri_modules(full) == ["opd", "ipd", "ot", "rx"]


def _u(role, perms=(), staff_role=None):
    return AdminUser("u", role, "c1", str(uuid.uuid4()), list(perms), staff_role=staff_role)


def test_permissions_and_roles():
    assert set(RI_PERMISSIONS) == {"RI_VIEW", "RI_RESOLVE"} <= PERMISSIONS
    assert holds_ri_permission(_u("clinic_admin"), "RI_RESOLVE")
    assert holds_ri_permission(_u("staff", staff_role="CASHIER"), "RI_VIEW")
    assert not holds_ri_permission(_u("staff", staff_role="CASHIER"), "RI_RESOLVE")
    assert holds_ri_permission(_u("staff", staff_role="BRANCH_MANAGER"), "RI_RESOLVE")
    assert not holds_ri_permission(_u("staff", staff_role="DOCTOR"), "RI_VIEW")
    assert holds_ri_permission(_u("staff", ["RI_VIEW"], "FRONT_DESK"), "RI_VIEW")
    # zero impact: a staff granter creating a CASHIER / BRANCH_MANAGER is capped exactly as before 143
    for role in ("CASHIER", "BRANCH_MANAGER"):
        assert not {"RI_VIEW", "RI_RESOLVE"} & role_implied_grants(role)
        assert not [g for g in role_grants_beyond_authority(role, _u("staff", ["STAFF_CREATE"])) if g.startswith("RI_")]


def test_owner_toggle_me_keys_and_ketu_gate(api):  # noqa: F811
    from app.assistant.registry import REGISTRY
    plain = _clinic(api.conn, opd=False)
    r = api.owner_toggle(plain, "revenue_guard", True)
    assert r.status_code == 400 and "enable one of them" in r.json()["detail"]
    clinic = _clinic(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert me["ri_access"] is False and me["ri_perms"] == []
    assert api.call("GET", "/admin/ri/summary", clinic).status_code == 403
    assert api.owner_toggle(clinic, "revenue_guard", True).status_code == 200
    me = api.client.get("/admin/me", params={"clinic_id": clinic}).json()
    assert me["ri_access"] is True and me["ri_perms"] == ["RI_RESOLVE", "RI_VIEW"]
    assert api.call("GET", "/admin/ri/summary", clinic).status_code == 200
    spec = REGISTRY["ri.yesterday"]
    assert spec.risk == "R0" and spec.endpoint == ("GET", "/admin/ri/summary") and spec.page == "riguard"
    assert spec.feature(_c(on=True)) and not spec.feature(_c("enterprise"))
    assert api.owner_toggle(clinic, "revenue_guard", False).status_code == 200      # off: no wind-down, just off
    assert api.call("GET", "/admin/ri/summary", clinic).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM ri_findings WHERE clinic_id = %s", clinic) == 0
