"""Pharmacy OS foundation (P1: RX-03 … RX-08).

Pure rules: entitlement (ADR-RX-09), RX permissions and roles (SECURITY §2–§3),
the staff grant cap. Then real HTTP routes on real Postgres (tests/ipd_pg_bridge.py):
owner enable / wind-down / re-enable, setup, settings CAS, stores with branch and
tenant scope, /admin/me, the OPD-disable guard and the purge order on deletion.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.routers.admin import AdminUser
from app.services import rx as rx_service
from app.services.permissions import (
    PERMISSIONS,
    ROLE_PRESETS,
    RX_PERMISSIONS,
    STAFF_ROLES,
    allowed_store_ids,
    holds_rx_permission,
    role_grants_beyond_authority,
)
from app.services.tenant import OPT_IN_FEATURES, has_feature, rx_access, rx_eligible
from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)

RX_PERMS = ("RX_READ", "RX_DISPENSE", "RX_PURCHASE", "RX_ADJUST", "RX_TRANSFER", "RX_AUDIT", "RX_REGISTERS",
            "RX_REFILLS", "RX_REPORTS", "RX_STORE_MANAGE", "RX_CATALOGUE", "RX_CONTROLLED", "RX_ADMIN")


def _c(plan="polyclinic", on=None, opd=True, **kw):
    feats = {"opd_enabled": True} if opd else {}
    if on is not None:
        feats["pharmacy_enabled"] = on
    return {"plan": plan, "account_type": "tenant", "features": feats, **kw}


# ── entitlement (pure) ───────────────────────────────────────────────────────

def test_rx_off_by_default():
    assert "pharmacy_enabled" in OPT_IN_FEATURES
    for plan in ("polyclinic", "multispecialty", "enterprise", "diagstream"):
        assert rx_access(_c(plan)) is None, plan
    assert rx_access(None) is None and rx_eligible(None) is False


def test_rx_needs_opd():
    assert rx_access(_c(on=True)) == "ACTIVE"
    assert rx_access(_c(on=True, opd=False)) is None              # a stray flag without OPD does nothing
    assert not rx_eligible(_c(opd=False))


def test_rx_enterprise_wildcard_does_not_enable():
    ent = _c("enterprise")
    assert has_feature(ent, "pharmacy_enabled") is True           # the wildcard answers yes to everything …
    assert rx_access(ent) is None                                 # … and Pharmacy OS ignores it
    assert rx_access(_c("enterprise", False)) is None


def test_rx_partner_account_refused():
    partner = {**_c(on=True), "account_type": "corporate_partner"}
    assert not rx_eligible(partner) and rx_access(partner) is None


def test_wind_down_survives_the_flag_being_cleared():
    assert rx_access(_c(on=False, rx_state="WIND_DOWN")) == "WIND_DOWN"
    assert rx_access(_c(on=None, rx_state="OFF")) is None
    assert rx_access({**_c(on=True), "features": "not-a-dict"}) is None


# ── permissions (pure) ───────────────────────────────────────────────────────

def _staff(role, perms=(), branch=None):
    return AdminUser("u", "staff", "c1", str(uuid.uuid4()), list(perms), branch_id=branch, staff_role=role)


def test_rx_permissions_and_roles_exist():
    assert set(RX_PERMS) <= PERMISSIONS and set(RX_PERMISSIONS) == set(RX_PERMS)
    for role in ("PHARMACIST", "PHARMACY_MANAGER", "STORE_KEEPER"):
        assert role in STAFF_ROLES and role in ROLE_PRESETS and set(ROLE_PRESETS[role]) <= PERMISSIONS, role
    assert set(ROLE_PRESETS["PHARMACY_MANAGER"]) == set(RX_PERMS)
    # narcotic custody is a deliberate grant: no preset below the manager carries it
    assert "RX_CONTROLLED" not in ROLE_PRESETS["PHARMACIST"] + ROLE_PRESETS["STORE_KEEPER"]
    assert "RX_DISPENSE" not in ROLE_PRESETS["STORE_KEEPER"] and "RX_PURCHASE" not in ROLE_PRESETS["PHARMACIST"]


def test_existing_logins_gain_only_the_listed_implied_grants():
    implied = {"PHARMACY_OPERATOR": {"RX_READ", "RX_DISPENSE"}}
    for role in ("FRONT_DESK", "RECEPTIONIST", "CASHIER", "DOCTOR", "NURSE", "PHLEBOTOMIST", "STAFF", "LAB_OPERATOR",
                 "PATHOLOGIST", "BRANCH_MANAGER", "PHARMACY_OPERATOR"):
        got = {p for p in RX_PERMS if holds_rx_permission(_staff(role), p)}
        assert got == implied.get(role, set()), role
    assert holds_rx_permission(_staff("FRONT_DESK", ["RX_READ"]), "RX_READ")       # a stored grant works
    assert all(holds_rx_permission(AdminUser("a", "clinic_admin", "c1"), p) for p in RX_PERMS)


def test_new_roles_imply_their_presets():
    for role in ("PHARMACIST", "PHARMACY_MANAGER", "STORE_KEEPER"):
        for p in ROLE_PRESETS[role]:
            assert holds_rx_permission(_staff(role), p), (role, p)
    assert not holds_rx_permission(_staff("PHARMACIST"), "RX_CONTROLLED")
    assert not holds_rx_permission(_staff("STORE_KEEPER"), "RX_DISPENSE")


def test_staff_grant_cap_covers_new_roles_but_not_the_existing_pharmacy_operator():
    granter = _staff("BRANCH_MANAGER", ["STAFF_CREATE"])
    # a granter without RX_ADMIN cannot mint a pharmacy manager by picking the role …
    assert {"RX_ADMIN", "RX_CONTROLLED"} <= set(role_grants_beyond_authority("PHARMACY_MANAGER", granter))
    assert "RX_DISPENSE" in role_grants_beyond_authority("PHARMACIST", granter)
    # … but creating a pharmacy operator works exactly as before migration 134
    assert role_grants_beyond_authority("PHARMACY_OPERATOR", granter) == []


def test_store_scope():
    stores = [{"id": "s1", "branch_id": "b1"}, {"id": "s2", "branch_id": "b2"}, {"id": "s3", "branch_id": None}]
    assert allowed_store_ids(AdminUser("a", "clinic_admin", "c1"), stores) is None
    assert allowed_store_ids(_staff("PHARMACIST"), stores) is None                 # tenant-wide staff
    assert allowed_store_ids(_staff("PHARMACIST", branch="b1"), stores) == {"s1"}


def test_settings_defaults_and_validation():
    s = rx_service.get_rx_settings({"rx_settings": {}})
    assert (s["max_discount_bps"], s["near_expiry_days"], s["return_days"], s["ipd_price_basis"], s["version"]) == (
        1000, 90, 7, "mrp", 0)
    for bad in ({"max_discount_bps": 20000}, {"near_expiry_days": 2}, {"receipt_format": "a4"},
                {"unknown_key": True}, {"ipd_price_basis": "free"}):
        with pytest.raises(HTTPException) as e:
            rx_service.validate_settings({}, bad)
        assert e.value.status_code == 422, bad


# ── HTTP + Postgres ──────────────────────────────────────────────────────────

def _branch(conn, clinic, name):
    return str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, %s) RETURNING id", clinic, name))


def _ward(conn, clinic, branch=None, code="W3"):
    return str(_q(conn, "INSERT INTO ipd_wards (clinic_id, branch_id, code, name, ward_type) "
                        "VALUES (%s, %s, %s, 'Ward 3', 'general') RETURNING id", clinic, branch, code))


@pytest.fixture
def hosp(api):  # noqa: F811
    clinic = _clinic(api.conn)                    # polyclinic with OPD on, pharmacy never enabled
    b1, b2 = _branch(api.conn, clinic, "Banjara Hills"), _branch(api.conn, clinic, "Kukatpally")
    U, uid = api.U, lambda: str(uuid.uuid4())
    return {"clinic": clinic, "b1": b1, "b2": b2,
            "admin": U("admin", "clinic_admin", clinic, uid()),
            "operator": U("op", "staff", clinic, uid(), [], staff_role="PHARMACY_OPERATOR"),
            "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK"),
            "pinned": U("mgr", "staff", clinic, uid(), [], staff_role="PHARMACY_MANAGER", branch_id=b2)}


def test_module_is_off_until_the_owner_enables_it(api, hosp):  # noqa: F811
    api.as_(hosp["admin"])
    r = api.call("GET", "/admin/rx/setup", hosp["clinic"])
    assert r.status_code == 403 and "not enabled" in r.text
    me = api.call("GET", "/admin/me", hosp["clinic"]).json()
    assert (me["rx_access"], me["rx_state"], me["rx_store_ids"], me["rx_perms"]) == (None, "OFF", None, [])


def test_owner_toggle_needs_opd(api):  # noqa: F811
    no_opd = _clinic(api.conn, opd=False)
    r = api.owner_toggle(no_opd, "pharmacy_enabled", True)
    assert r.status_code == 400 and "OPD" in r.text
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", no_opd) == "OFF"


def test_enable_provisions_one_store_and_opens_setup(api, hosp):  # noqa: F811
    r = api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    assert r.status_code == 200, r.text
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "ACTIVE"
    body = api.as_(hosp["admin"]).call("GET", "/admin/rx/setup", hosp["clinic"]).json()
    assert body["access"] == "ACTIVE" and body["settings"]["max_discount_bps"] == 1000
    (store,) = body["stores"]
    assert (store["code"], store["store_type"], store["can_sell"], store["can_receive_grn"], store["branch_id"]) == (
        "MAIN", "retail_counter", True, True, None)
    checklist = {c["key"]: c["done"] for c in body["checklist"]}
    assert checklist == {"stores": True, "items": False, "gstin": False, "settings": False}
    # re-enabling never duplicates or overwrites stores
    api.call("PATCH", f"/admin/rx/stores/{store['id']}", hosp["clinic"], {"name": "OPD Pharmacy"})
    assert api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True).status_code == 200
    assert _q(api.conn, "SELECT array_agg(name) FROM rx_stores WHERE clinic_id = %s", hosp["clinic"]) == ["OPD Pharmacy"]
    me = api.call("GET", "/admin/me", hosp["clinic"]).json()
    assert me["rx_access"] == "ACTIVE" and me["rx_store_ids"] is None and set(me["rx_perms"]) == set(RX_PERMS)


def test_every_route_checks_permission_first(api, hosp):  # noqa: F811
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    api.as_(hosp["desk"])
    for method, path, body in (("GET", "/admin/rx/setup", None), ("GET", "/admin/rx/settings", None),
                               ("GET", "/admin/rx/stores", None), ("PATCH", "/admin/rx/settings", {"version": 0}),
                               ("POST", "/admin/rx/stores", {"code": "X1", "name": "Tray", "store_type": "ot_tray"})):
        r = api.call(method, path, hosp["clinic"], body)
        assert r.status_code == 403 and "Missing permission" in r.text, (path, r.text)
    # a pharmacy operator saved before migration 134 reads setup but cannot change settings or stores
    api.as_(hosp["operator"])
    assert api.call("GET", "/admin/rx/setup", hosp["clinic"]).status_code == 200
    assert api.call("PATCH", "/admin/rx/settings", hosp["clinic"], {"version": 0}).status_code == 403
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"],
                    {"code": "X1", "name": "Tray", "store_type": "ot_tray"}).status_code == 403
    assert set(api.call("GET", "/admin/me", hosp["clinic"]).json()["rx_perms"]) == {"RX_READ", "RX_DISPENSE"}


def test_settings_cas(api, hosp, monkeypatch):  # noqa: F811
    from app.routers import rx as rx_router
    audits = []

    async def _spy(**kw):
        audits.append(kw["action"])
    monkeypatch.setattr(rx_router, "log_admin_action", _spy)
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    api.as_(hosp["admin"])
    r = api.call("PATCH", "/admin/rx/settings", hosp["clinic"], {"version": 0, "return_days": 3, "receipt_format": "a5"})
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert (s["return_days"], s["receipt_format"], s["version"]) == (3, "a5", 1)
    stale = api.call("PATCH", "/admin/rx/settings", hosp["clinic"], {"version": 0, "return_days": 5})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "rx_settings_conflict"
    assert api.call("PATCH", "/admin/rx/settings", hosp["clinic"], {"version": 1, "near_expiry_days": 1}).status_code == 422
    assert api.call("PATCH", "/admin/rx/settings", hosp["clinic"], {"version": 1, "nope": 1}).status_code == 422
    assert audits == ["rx_settings_update"]                     # refused writes are not audited as changes
    # a branch-pinned manager cannot change clinic-wide settings
    assert api.as_(hosp["pinned"]).call("PATCH", "/admin/rx/settings", hosp["clinic"],
                                        {"version": 1}).status_code == 403


def test_stores_are_branch_and_tenant_scoped(api, hosp):  # noqa: F811
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    other = _clinic(api.conn)
    their_branch = _branch(api.conn, other, "Theirs")
    api.as_(hosp["admin"])
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "LEAK", "name": "Leak", "store_type": "central",
                                                                 "branch_id": their_branch}).status_code == 404
    dup = api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "MAIN", "name": "Dup", "store_type": "ot_tray"})
    assert dup.status_code == 409 and "code" in dup.text
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "C1", "name": "Central", "store_type": "central",
                                                                 "can_sell": True}).status_code == 422
    central = api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "C1", "name": "Central",
                                                                    "store_type": "central", "branch_id": hosp["b1"]})
    assert central.status_code == 201 and central.json()["can_receive_grn"] and not central.json()["can_sell"]
    again = api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "C2", "name": "Central 2",
                                                                  "store_type": "central", "branch_id": hosp["b1"]})
    assert again.status_code == 409
    # ward sub-store: needs its ward, of this clinic and branch
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "W3", "name": "Ward 3",
                                                                 "store_type": "ward_substore"}).status_code == 422
    their_ward = _ward(api.conn, other)
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "W3", "name": "Ward 3", "store_type":
                                                                 "ward_substore", "ward_id": their_ward}).status_code == 404
    ward = _ward(api.conn, hosp["clinic"], hosp["b2"])
    ws = api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "W3", "name": "Ward 3", "store_type": "ward_substore",
                                                               "ward_id": ward, "branch_id": hosp["b2"],
                                                               "parent_store_id": central.json()["id"]})
    assert ws.status_code == 201, ws.text
    # identity never changes through the API (code / type are not patchable fields)
    assert api.call("PATCH", f"/admin/rx/stores/{ws.json()['id']}", hosp["clinic"], {"code": "W4"}).status_code == 422
    assert api.call("PATCH", f"/admin/rx/stores/{ws.json()['id']}", hosp["clinic"],
                    {"parent_store_id": ws.json()["id"]}).status_code == 422
    # a branch-pinned manager sees and edits only its branch's stores
    api.as_(hosp["pinned"])
    assert [s["code"] for s in api.call("GET", "/admin/rx/stores", hosp["clinic"]).json()["stores"]] == ["W3"]
    setup = api.call("GET", "/admin/rx/setup", hosp["clinic"]).json()
    assert [x["code"] for x in setup["stores"]] == ["W3"]
    # readiness stays clinic-wide: the clinic can sell even though this login sees only its ward store
    assert {c["key"]: c["done"] for c in setup["checklist"]}["stores"] is True
    c1 = central.json()["id"]
    assert api.call("PATCH", f"/admin/rx/stores/{c1}", hosp["clinic"], {"name": "Nope"}).status_code == 404
    assert api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "T1", "name": "Tray", "store_type": "ot_tray",
                                                                 "branch_id": hosp["b1"]}).status_code == 403
    assert api.call("GET", "/admin/me", hosp["clinic"]).json()["rx_store_ids"] == [ws.json()["id"]]
    # another tenant's admin cannot read this clinic's stores
    api.as_(api.U("admin2", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", "/admin/rx/stores", hosp["clinic"]).status_code in (403, 404)


def test_owner_disable_wind_down_and_reenable(api, hosp, monkeypatch):  # noqa: F811
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    # nothing open: straight to OFF, routes close, data stays
    assert api.owner_toggle(hosp["clinic"], "pharmacy_enabled", False).status_code == 200
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "OFF"
    assert api.as_(hosp["admin"]).call("GET", "/admin/rx/setup", hosp["clinic"]).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM rx_stores WHERE clinic_id = %s", hosp["clinic"]) == 1
    # clearing the override (enabled = None) is a disable too
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    assert api.owner_toggle(hosp["clinic"], "pharmacy_enabled", None).status_code == 200
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "OFF"
    # open work: 409 with the preview, then confirm -> WIND_DOWN
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    busy = {"open_sales": 2, "open_indents": 1, "transfers_in_transit": 0, "open_audits": 0, "blocking": True}

    async def _busy(clinic_id):
        return busy
    monkeypatch.setattr(rx_service, "deactivation_preview", _busy)
    r = api.owner_toggle(hosp["clinic"], "pharmacy_enabled", False)
    assert r.status_code == 409 and r.json()["detail"]["preview"]["open_sales"] == 2
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "ACTIVE"
    preview = api.client.get(f"/platform/clinics/{hosp['clinic']}/rx/deactivation-preview").json()
    assert preview["blocking"] and preview["state"] == "ACTIVE" and preview["eligible"]
    assert api.owner_toggle(hosp["clinic"], "pharmacy_enabled", False, confirm=True).status_code == 200
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "WIND_DOWN"
    # wind-down: reads work, nothing new starts
    api.as_(hosp["admin"])
    assert api.call("GET", "/admin/rx/setup", hosp["clinic"]).json()["access"] == "WIND_DOWN"
    r = api.call("POST", "/admin/rx/stores", hosp["clinic"], {"code": "NEW", "name": "New", "store_type": "ot_tray"})
    assert r.status_code == 409 and "switched off" in r.text
    store = _q(api.conn, "SELECT id::text FROM rx_stores WHERE clinic_id = %s", hosp["clinic"])
    assert api.call("PATCH", f"/admin/rx/stores/{store}", hosp["clinic"], {"is_active": False}).status_code == 200
    assert api.call("PATCH", f"/admin/rx/stores/{store}", hosp["clinic"], {"is_active": True}).status_code == 409
    # re-enable: ACTIVE with data intact
    assert api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True).status_code == 200
    assert _q(api.conn, "SELECT rx_state FROM clinics WHERE id = %s", hosp["clinic"]) == "ACTIVE"
    assert _q(api.conn, "SELECT count(*) FROM rx_stores WHERE clinic_id = %s", hosp["clinic"]) == 1


def test_opd_cannot_be_disabled_under_a_live_pharmacy(api, hosp):  # noqa: F811
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", True)
    for enabled in (False, None):
        r = api.owner_toggle(hosp["clinic"], "opd_enabled", enabled)
        assert r.status_code == 409 and "Pharmacy OS" in r.text, enabled
    assert _q(api.conn, "SELECT features ->> 'opd_enabled' FROM clinics WHERE id = %s", hosp["clinic"]) == "true"
    # once the pharmacy is off, OPD can go
    api.owner_toggle(hosp["clinic"], "pharmacy_enabled", False)
    assert api.owner_toggle(hosp["clinic"], "opd_enabled", False, confirm=True).status_code == 200


def test_clinic_deletion_purges_pharmacy_before_opd(api, monkeypatch):  # noqa: F811
    from app.database import supabase
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    _branch(api.conn, clinic, "Main")
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
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
        assert purges.index("rx_purge_clinic") < purges.index("opd_purge_clinic"), purges
        assert purges[0] == "dx_purge_clinic"            # the DX-first contract is kept
        assert _q(api.conn, "SELECT count(*) FROM rx_stores WHERE clinic_id = %s", clinic) == 0
    finally:   # an open walk-in would block 103_down in the shared test database
        _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def test_deletion_of_a_clinic_without_pharmacy_makes_no_rx_purge_call(api, monkeypatch):  # noqa: F811
    from app.database import supabase
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    admin = api.U("admin", "clinic_admin", clinic, str(uuid.uuid4()))
    walk = api.as_(admin).call("POST", "/admin/opd/walk-ins", clinic,
                               {"patient_id": _patient(api.conn, clinic), "doctor_id": doctor, "skip_vitals": True})
    dr = api.U("drmeena", "staff", clinic, str(uuid.uuid4()), ["OPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor)
    api.as_(dr).call("GET", f"/admin/opd/encounters/by-appointment/{walk.json()['appointment_id']}", clinic)
    calls, real_rpc = [], supabase.rpc
    monkeypatch.setattr(supabase, "rpc", lambda name, params=None, **kw: (calls.append(name),
                                                                           real_rpc(name, params, **kw))[1])
    try:
        assert api.client.delete(f"/platform/clinics/{clinic}", params={"confirm_opd_purge": True}).status_code == 200
        assert "rx_purge_clinic" not in calls
    finally:
        _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)
