"""IPD OS end-to-end through the real HTTP routes and a REAL Postgres.

tests/ipd_pg_bridge.py executes the routes' own postgrest queries against the
migrated test database, so the SQL functions, triggers, constraints and the
OPD billing integration all run for real. Only authentication is overridden
(one AdminUser per role), as in every other route test in this suite.
"""

import json
import uuid
from datetime import date, datetime, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient

from tests.ipd_pg_bridge import PgBridge


# ── world ────────────────────────────────────────────────────────────────────

def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


def _clinic(conn, opd=True):
    feats = {"opd_enabled": True} if opd else {}
    return str(_q(conn, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state)
                           VALUES ('E2E Hospital', %s, 'polyclinic', true, %s::jsonb, 'READY') RETURNING id""",
                  "+9195" + uuid.uuid4().hex[:8], json.dumps(feats)))


def _doctor(conn, clinic):
    return str(_q(conn, """INSERT INTO doctors (clinic_id, name, department, specialization, registration_number,
                           registration_council, is_active) VALUES (%s, 'Dr. Meena', 'Medicine', 'Physician',
                           'TSMC/2012/777', 'TSMC', true) RETURNING id""", clinic))


def _patient(conn, clinic, gender="female"):
    return str(_q(conn, """INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on)
                           VALUES (%s, 'Lakshmi Devi', %s, %s, 58, CURRENT_DATE) RETURNING id""",
                  clinic, "+9194" + uuid.uuid4().hex[:8], gender))


class Who:
    """The authenticated user for the next request (swapped per step)."""
    user = None


@pytest.fixture
def api(real_postgres_uri, monkeypatch):
    from app.main import app
    from app.routers import admin, platform
    from app.routers.admin import AdminUser

    bridge = PgBridge(real_postgres_uri).install(monkeypatch)
    owner = AdminUser(username="owner", role="super_admin", clinic_id=None, user_id="platform_owner_env")
    app.dependency_overrides[admin.verify_credentials] = lambda: Who.user
    app.dependency_overrides[platform.verify_owner_credentials] = lambda: owner
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    try:
        yield SimpleApi(TestClient(app), conn, AdminUser)
    finally:
        app.dependency_overrides.pop(admin.verify_credentials, None)
        app.dependency_overrides.pop(platform.verify_owner_credentials, None)
        conn.close()
        bridge.close()


class SimpleApi:
    def __init__(self, client, conn, user_cls):
        self.client, self.conn, self.U = client, conn, user_cls

    def as_(self, user):
        Who.user = user
        return self

    def call(self, method, path, clinic, body=None, **params):
        params["clinic_id"] = clinic
        return self.client.request(method, path, params=params, json=body)

    def owner_toggle(self, clinic, feature, enabled, confirm=False):
        return self.client.patch(f"/platform/clinics/{clinic}/features",
                                 json={"feature": feature, "enabled": enabled, "confirm_disable": confirm})

    def users(self, clinic, doctor):
        U, uid = self.U, lambda: str(uuid.uuid4())
        return {
            "admin": U("admin", "clinic_admin", clinic, uid()),
            "desk": U("desk", "staff", clinic, uid(), ["IPD_ADMISSIONS"], staff_role="FRONT_DESK"),
            "nurse": U("nurse", "staff", clinic, uid(), ["IPD_NURSING"], staff_role="NURSE"),
            "doctor": U("drmeena", "staff", clinic, uid(), ["IPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor),
            "billing": U("cashier", "staff", clinic, uid(), ["IPD_BILLING", "OPD_BILLING"], staff_role="CASHIER"),
        }


def _code(res):
    d = res.json().get("detail")
    return d.get("code") if isinstance(d, dict) else d


def _setup_hospital(api, clinic):
    """Owner enables IPD; admin configures tariffs, one ward with 3 beds."""
    assert api.owner_toggle(clinic, "ipd_enabled", True).status_code == 200
    doctor = _doctor(api.conn, clinic)
    u = api.users(clinic, doctor)
    api.as_(u["admin"])
    r = api.call("PUT", "/admin/ipd/settings", clinic, {"bed_tariffs": {"general": 120000, "icu": 500000}})
    assert r.status_code == 200, r.text
    ward = api.call("POST", "/admin/ipd/wards", clinic, {"code": "W1", "name": "Ward 1"}).json()
    beds = api.call("POST", f"/admin/ipd/wards/{ward['id']}/beds", clinic, {"beds": [
        {"code": "101", "bed_category": "general"}, {"code": "102", "bed_category": "general"},
        {"code": "ICU1", "bed_category": "icu"}]}).json()["beds"]
    return doctor, u, {b["code"]: b["id"] for b in beds}


def _admit(api, clinic, u, doctor, bed, patient=None, key=None):
    api.as_(u["desk"])
    return api.call("POST", "/admin/ipd/admissions", clinic, {
        "patient_id": patient or _patient(api.conn, clinic), "admitting_doctor_id": doctor,
        "reason": "Community acquired pneumonia", "provisional_diagnosis": "CAP", "bed_id": bed,
        "consent_recorded": True, "idempotency_key": key or uuid.uuid4().hex})


# ── tests ────────────────────────────────────────────────────────────────────

def test_module_is_off_until_the_owner_enables_it(api):
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    api.as_(api.users(clinic, doctor)["admin"])
    assert api.call("GET", "/admin/ipd/board", clinic).status_code == 403
    assert api.call("POST", "/admin/ipd/wards", clinic, {"code": "W1", "name": "x"}).status_code == 403
    cur = api.conn.cursor()
    cur.execute("SELECT ipd_state, features ? 'ipd_enabled' FROM clinics WHERE id = %s", (clinic,))
    assert cur.fetchone() == ("OFF", False)


def test_ipd_requires_opd(api):
    clinic = _clinic(api.conn, opd=False)
    r = api.owner_toggle(clinic, "ipd_enabled", True)
    assert r.status_code == 400 and "OPD" in r.json()["detail"]


def test_full_inpatient_journey(api):
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)

    # ── admission: idempotent, bed allocated atomically ──
    key = uuid.uuid4().hex
    patient = _patient(api.conn, clinic)
    r = _admit(api, clinic, u, doctor, beds["101"], patient=patient, key=key)
    assert r.status_code == 201, r.text
    adm = r.json()
    assert adm["status"] == "admitted" and adm["admission_number"].startswith("IPD-")
    replay = _admit(api, clinic, u, doctor, beds["101"], patient=patient, key=key)
    assert replay.status_code == 201 and replay.json()["id"] == adm["id"]
    clash = _admit(api, clinic, u, doctor, beds["101"])
    assert clash.status_code == 409 and _code(clash) == "ipd_bed_unavailable"
    aid = adm["id"]

    # ── bed board: occupancy; diagnosis only for clinical roles ──
    board = api.as_(u["desk"]).call("GET", "/admin/ipd/board", clinic).json()
    assert board["summary"]["occupied"] == 1 and board["summary"]["available"] == 2
    occ = next(b for w in board["wards"] for b in w["beds"] if b["code"] == "101")["occupant"]
    assert occ["patient_name"] == "Lakshmi Devi" and "provisional_diagnosis" not in occ
    occ = next(b for w in api.as_(u["nurse"]).call("GET", "/admin/ipd/board", clinic).json()["wards"]
               for b in w["beds"] if b["code"] == "101")["occupant"]
    assert occ["provisional_diagnosis"] == "CAP"

    # ── nursing station ──
    now = datetime.now(timezone.utc).isoformat()
    api.as_(u["nurse"])
    obs = api.call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic,
                   {"kind": "vitals", "observed_at": now, "pulse_bpm": 104, "spo2_pct": 91, "temperature_c": 38.6})
    assert obs.status_code == 201, obs.text
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic,
                    {"kind": "intake", "observed_at": now, "io_route": "iv", "volume_ml": 500}).status_code == 201
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic,
                    {"kind": "vitals", "observed_at": now, "pulse_bpm": 900}).status_code == 422
    fix = api.call("POST", f"/admin/ipd/observations/{obs.json()['id']}/correct", clinic,
                   {"reason": "Entered on the wrong chart", "replacement": {"pulse_bpm": 96, "spo2_pct": 94}})
    assert fix.status_code == 200 and fix.json()["replacement"]["corrects_id"] == obs.json()["id"]
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/notes", clinic,
                    {"note_type": "progress", "body": "x"}).status_code == 403      # doctors only
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/notes", clinic,
                    {"note_type": "nursing", "body": "Febrile, on O2 2L"}).status_code == 201

    # ── doctor round: note, order; nurse administers ──
    api.as_(u["doctor"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/notes", clinic,
                    {"note_type": "progress", "body": "CAP, start IV antibiotics"}).status_code == 201
    okey = uuid.uuid4().hex
    order_body = {"order_type": "medication", "description": "Inj Ceftriaxone 1 g IV BD", "drug_name": "Ceftriaxone",
                  "dose": "1 g", "route": "iv", "frequency": "BD", "idempotency_key": okey}
    order = api.call("POST", f"/admin/ipd/admissions/{aid}/orders", clinic, order_body).json()
    again = api.call("POST", f"/admin/ipd/admissions/{aid}/orders", clinic, order_body).json()
    assert again["id"] == order["id"] and again.get("replayed") is True
    api.as_(u["nurse"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/orders", clinic, {**order_body, "idempotency_key": uuid.uuid4().hex}).status_code == 403
    mar = {"order_id": order["id"], "administered_at": now, "idempotency_key": uuid.uuid4().hex}
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/administrations", clinic,
                    {**mar, "outcome": "given", "dose_given": "1 g"}).status_code == 201
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/administrations", clinic,
                    {**mar, "outcome": "refused", "idempotency_key": uuid.uuid4().hex}).status_code == 422
    # stopping a medication is the doctor's decision, even as "completed"
    assert api.call("POST", f"/admin/ipd/orders/{order['id']}/end", clinic, {"status": "completed"}).status_code == 403

    # ── transfer to ICU; stale screen is refused; old bed goes to cleaning ──
    t = api.call("POST", f"/admin/ipd/admissions/{aid}/transfer", clinic,
                 {"to_bed_id": beds["ICU1"], "expected_bed_id": beds["101"], "reason": "Desaturation, needs ICU"})
    assert t.status_code == 200 and t.json()["current_bed_id"] == beds["ICU1"]
    stale = api.call("POST", f"/admin/ipd/admissions/{aid}/transfer", clinic,
                     {"to_bed_id": beds["102"], "expected_bed_id": beds["101"], "reason": "retry"})
    assert stale.status_code == 409 and _code(stale) == "ipd_stale_bed"
    assert api.call("POST", f"/admin/ipd/beds/{beds['101']}/status", clinic, {"status": "available"}).status_code == 200
    assert api.call("POST", f"/admin/ipd/beds/{beds['102']}/status", clinic,
                    {"status": "blocked", "reason": "Leak"}).status_code == 403          # admin only

    # ── discharge decision: doctor only for a regular discharge ──
    assert api.as_(u["desk"]).call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", clinic,
                                   {"discharge_type": "regular"}).status_code == 403
    api.as_(u["doctor"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", clinic,
                    {"discharge_type": "regular"}).json()["status"] == "discharge_pending"
    assert api.call("POST", f"/admin/ipd/orders/{order['id']}/end", clinic,
                    {"status": "discontinued", "reason": "Switching to oral"}).status_code == 200
    ds = api.call("PUT", f"/admin/ipd/admissions/{aid}/discharge-summary", clinic,
                  {"final_diagnosis": "Community acquired pneumonia", "course_in_hospital": "IV antibiotics, ICU 1 day",
                   "advice": "Complete oral course"}).json()
    signed = api.call("POST", f"/admin/ipd/discharge-summaries/{ds['id']}/sign", clinic)
    assert signed.status_code == 200 and signed.json()["status"] == "signed"

    # ── billing: proposed -> posted -> final bill (an OPD invoice) ──
    api.as_(u["billing"])
    ref = uuid.uuid4().hex
    charge = {"category": "procedure", "description": "Nebulisation", "service_date": date.today().isoformat(),
              "quantity": 2, "unit_price_paise": 30000, "source_ref": ref}
    c1 = api.call("POST", f"/admin/ipd/admissions/{aid}/charges", clinic, charge).json()
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges", clinic, charge).json()["id"] == c1["id"]
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges/accrue-bed", clinic).json()["added"] >= 1
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/final-bill", clinic).status_code == 409   # proposed left
    detail = api.call("GET", f"/admin/ipd/admissions/{aid}", clinic).json()
    assert "observations" not in detail                                # billing sees no clinical data
    ids = [c["id"] for c in detail["charges"] if c["status"] == "proposed"]
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges/post", clinic, {"charge_ids": ids}).json()["posted"] == len(ids)
    bill = api.call("POST", f"/admin/ipd/admissions/{aid}/final-bill", clinic).json()
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/final-bill", clinic).json()["invoice_id"] == bill["invoice_id"]
    issued = api.call("POST", f"/admin/opd/invoices/{bill['invoice_id']}/issue", clinic)
    assert issued.status_code == 200, issued.text
    posted_total = sum(c["quantity"] * c["unit_price_paise"] for c in detail["charges"])
    assert issued.json()["total_paise"] == posted_total               # bill reconciles to the posted charges

    # ── completion: unpaid needs an admin waiver ──
    blocked = api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", clinic, {})
    assert blocked.status_code == 409 and _code(blocked) == "ipd_settlement_waiver_required"
    assert blocked.json()["detail"]["checklist"]["needs_waiver"] is True
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", clinic,
                    {"waiver_reason": "Cashless insurance claim"}).status_code == 403
    done = api.as_(u["admin"]).call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", clinic,
                                    {"waiver_reason": "Cashless insurance claim"})
    assert done.status_code == 200 and done.json()["status"] == "discharged"

    # ── final record and bed state are consistent ──
    detail = api.call("GET", f"/admin/ipd/admissions/{aid}", clinic).json()
    assert [h["bed_code"] for h in detail["bed_history"]] == ["101", "ICU1"]
    assert all(h["ended_at"] for h in detail["bed_history"])
    events = [e["event_type"] for e in detail["timeline"]]
    for e in ("admission_created", "bed_assigned", "transferred", "discharge_initiated", "summary_signed",
              "final_bill_created", "discharged"):
        assert e in events
    board = api.call("GET", "/admin/ipd/board", clinic).json()
    assert {b["code"]: b["status"] for w in board["wards"] for b in w["beds"]}["ICU1"] == "cleaning"
    census = api.call("GET", "/admin/ipd/census", clinic).json()
    assert census["inpatients"] == 0 and census["discharged_today"] == 1


def test_cross_tenant_access_is_impossible(api):
    a = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, a)
    aid = _admit(api, a, u, doctor, beds["101"]).json()["id"]
    b = _clinic(api.conn)
    db_ = _doctor(api.conn, b)
    ub = api.users(b, db_)
    assert api.owner_toggle(b, "ipd_enabled", True).status_code == 200
    api.as_(ub["admin"])
    assert api.call("GET", f"/admin/ipd/admissions/{aid}", b).status_code == 404       # other tenant's id
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/observations", b,
                    {"kind": "vitals", "observed_at": datetime.now(timezone.utc).isoformat(),
                     "pulse_bpm": 80}).status_code == 404
    assert api.call("POST", f"/admin/ipd/beds/{beds['102']}/status", b, {"status": "cleaning"}).status_code == 404
    leak = api.call("POST", "/admin/ipd/admissions", b, {
        "patient_id": _patient(api.conn, b), "admitting_doctor_id": db_, "reason": "test", "bed_id": beds["102"],
        "idempotency_key": uuid.uuid4().hex})
    assert leak.status_code == 404                                         # A's bed is not B's
    assert api.call("GET", "/admin/ipd/board", b).json()["summary"]["total"] == 0
    # a clinic A user naming clinic B in the query string
    assert api.as_(u["admin"]).call("GET", "/admin/ipd/board", b).status_code == 403


def test_owner_disable_with_patients_winds_down_and_reenable_loses_nothing(api):
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    aid = _admit(api, clinic, u, doctor, beds["101"]).json()["id"]
    r = api.owner_toggle(clinic, "ipd_enabled", False)
    assert r.status_code == 409 and r.json()["detail"]["preview"]["in_bed"] == 1
    assert api.owner_toggle(clinic, "opd_enabled", False).status_code == 409     # OPD stays while IPD runs
    assert api.owner_toggle(clinic, "ipd_enabled", False, confirm=True).status_code == 200
    cur = api.conn.cursor()
    cur.execute("SELECT ipd_state FROM clinics WHERE id = %s", (clinic,))
    assert cur.fetchone()[0] == "WIND_DOWN"
    # no new work ...
    blocked = _admit(api, clinic, u, doctor, beds["102"])
    assert blocked.status_code == 409 and "switched off" in blocked.json()["detail"]
    assert api.as_(u["admin"]).call("POST", "/admin/ipd/wards", clinic, {"code": "W2", "name": "x"}).status_code == 409
    # ... but the admitted patient is still cared for
    assert api.as_(u["nurse"]).call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic, {
        "kind": "vitals", "observed_at": datetime.now(timezone.utc).isoformat(), "pulse_bpm": 82}).status_code == 201
    cur.execute("SELECT status FROM ipd_beds WHERE id = %s", (beds["101"],))
    assert cur.fetchone()[0] == "occupied"                                # disable never frees a bed
    assert api.owner_toggle(clinic, "ipd_enabled", True).status_code == 200
    cur.execute("SELECT ipd_state FROM clinics WHERE id = %s", (clinic,))
    assert cur.fetchone()[0] == "ACTIVE"
    detail = api.as_(u["nurse"]).call("GET", f"/admin/ipd/admissions/{aid}", clinic).json()
    assert detail["admission"]["status"] == "admitted" and len(detail["observations"]) == 1


def test_disable_without_patients_turns_off_and_blocks_routes(api):
    clinic = _clinic(api.conn)
    _, u, _ = _setup_hospital(api, clinic)
    assert api.owner_toggle(clinic, "ipd_enabled", False).status_code == 200
    cur = api.conn.cursor()
    cur.execute("SELECT ipd_state, (SELECT count(*) FROM ipd_beds WHERE clinic_id = %s) FROM clinics WHERE id = %s",
                (clinic, clinic))
    assert cur.fetchone() == ("OFF", 3)                                    # configuration retained
    assert api.as_(u["admin"]).call("GET", "/admin/ipd/board", clinic).status_code == 403


def test_concurrent_api_admissions_to_one_bed(api):
    """Two desks pressing Admit on the same bed at once: one wins, one gets a clear 409."""
    import concurrent.futures
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    patients = [_patient(api.conn, clinic) for _ in range(6)]
    Who.user = u["desk"]

    def go(i):
        return api.call("POST", "/admin/ipd/admissions", clinic, {
            "patient_id": patients[i], "admitting_doctor_id": doctor, "reason": "Fever", "bed_id": beds["102"],
            "idempotency_key": uuid.uuid4().hex}).status_code
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, range(6)))
    assert sorted(codes) == [201] + [409] * 5


def test_lama_can_be_recorded_by_the_desk(api):
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    aid = _admit(api, clinic, u, doctor, beds["101"]).json()["id"]
    r = api.as_(u["desk"]).call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", clinic,
                                {"discharge_type": "lama"})
    assert r.status_code == 200 and r.json()["discharge_type"] == "lama"


def test_doctor_without_link_cannot_sign_or_order(api):
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    aid = _admit(api, clinic, u, doctor, beds["101"]).json()["id"]
    unlinked = api.U("resident", "staff", clinic, str(uuid.uuid4()), ["IPD_CLINICAL"], staff_role="STAFF")
    api.as_(unlinked)
    r = api.call("POST", f"/admin/ipd/admissions/{aid}/orders", clinic, {
        "order_type": "diet", "description": "NBM", "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403 and "linked to a doctor" in r.json()["detail"]
    # the admin (no doctor link) cannot make a clinical discharge decision either
    assert api.as_(u["admin"]).call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", clinic,
                                    {"discharge_type": "regular"}).status_code == 403


def test_branch_pinned_staff_cannot_touch_another_branchs_beds(api):
    clinic = _clinic(api.conn)
    _, u, _ = _setup_hospital(api, clinic)
    a = _q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", clinic)
    b = _q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", clinic)
    api.as_(u["admin"])
    ward_b = api.call("POST", "/admin/ipd/wards", clinic, {"code": "SB", "name": "South ward", "branch_id": str(b)}).json()
    bed_b = api.call("POST", f"/admin/ipd/wards/{ward_b['id']}/beds", clinic,
                     {"beds": [{"code": "S1", "bed_category": "general"}]}).json()["beds"][0]["id"]
    assert api.call("POST", f"/admin/ipd/beds/{bed_b}/status", clinic, {"status": "cleaning"}).status_code == 200
    north_nurse = api.U("north_nurse", "staff", clinic, str(uuid.uuid4()), ["IPD_NURSING"], branch_id=str(a),
                        staff_role="NURSE")
    north_admin = api.U("north_admin", "staff", clinic, str(uuid.uuid4()), ["IPD_ADMIN"], branch_id=str(a))
    api.as_(north_nurse)
    assert api.call("POST", f"/admin/ipd/beds/{bed_b}/status", clinic, {"status": "available"}).status_code == 403
    assert all(x["id"] != bed_b for x in api.call("GET", "/admin/ipd/worklist", clinic).json()["beds_to_clean"])
    api.as_(north_admin)
    assert api.call("PATCH", f"/admin/ipd/beds/{bed_b}", clinic, {"room_label": "x"}).status_code == 403
    assert api.call("PATCH", f"/admin/ipd/wards/{ward_b['id']}", clinic, {"name": "x"}).status_code == 403
    assert api.call("POST", f"/admin/ipd/wards/{ward_b['id']}/beds", clinic,
                    {"beds": [{"code": "S2", "bed_category": "general"}]}).status_code == 403


def test_observation_correction_with_invalid_replacement_is_422_and_changes_nothing(api):
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    aid = _admit(api, clinic, u, doctor, beds["101"]).json()["id"]
    api.as_(u["nurse"])
    obs = api.call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic, {
        "kind": "vitals", "observed_at": datetime.now(timezone.utc).isoformat(), "pulse_bpm": 104}).json()
    for bad in ({"pulse_bpm": 900}, {"kind": "intake"}, {"bogus": 1}):
        r = api.call("POST", f"/admin/ipd/observations/{obs['id']}/correct", clinic,
                     {"reason": "Wrong value", "replacement": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert _q(api.conn, "SELECT status FROM ipd_observations WHERE id = %s", obs["id"]) == "recorded"
