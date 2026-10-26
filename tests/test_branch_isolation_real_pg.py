"""Multi-branch isolation through the real HTTP routes and a REAL Postgres (tests/ipd_pg_bridge.py).

One hospital, two branches (North, South). Staff pinned to North must not read or change South's
OPD visits, clinical records, bills, payment exceptions, drawers or IPD beds. Clinic admins and
unpinned staff keep clinic-wide access, and a doctor login keeps its own patients at any branch.
Each test also asserts that a refused request changed nothing.
"""

import uuid

import pytest

from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)


class H:
    """The hospital: ids plus logins."""


@pytest.fixture
def hosp(api):  # noqa: F811
    h = H()
    h.api, h.clinic = api, _clinic(api.conn)
    h.north = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", h.clinic))
    h.south = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", h.clinic))
    h.doctor = _doctor(api.conn, h.clinic)
    _q(api.conn, "UPDATE doctors SET consultation_fee = 500 WHERE id = %s", h.doctor)

    def user(name, perms, branch=None, **kw):
        return api.U(f"{name}_{uuid.uuid4().hex[:5]}", "staff", h.clinic, str(uuid.uuid4()), list(perms),
                     branch_id=branch, **kw)
    h.user = user
    h.north_desk = user("ndesk", ["OPD_FRONT_DESK"], h.north, staff_role="FRONT_DESK")
    h.south_desk = user("sdesk", ["OPD_FRONT_DESK"], h.south, staff_role="FRONT_DESK")
    h.north_cash = user("ncash", ["OPD_BILLING", "OPD_ADMIN"], h.north, staff_role="CASHIER")
    h.admin = api.U("admin", "clinic_admin", h.clinic, str(uuid.uuid4()))
    yield h
    _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", h.clinic)


def _call(h, user, method, path, body=None, **params):
    return h.api.as_(user).call(method, path, h.clinic, body, **params)


def _walk_in(h, desk, skip_vitals=True):
    r = _call(h, desk, "POST", "/admin/opd/walk-ins",
              {"patient_id": _patient(h.api.conn, h.clinic), "doctor_id": h.doctor, "skip_vitals": skip_vitals})
    assert r.status_code == 201, r.text
    return r.json()


def _encounter_and_rx(h, appt_id):
    a = h.api.conn
    pid = _q(a, "SELECT patient_id FROM appointments WHERE id = %s", appt_id)
    branch = _q(a, "SELECT branch_id FROM appointments WHERE id = %s", appt_id)
    enc = str(_q(a, """INSERT INTO opd_encounters (clinic_id, branch_id, appointment_id, patient_id, doctor_id)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""", h.clinic, branch, appt_id, pid, h.doctor))
    rx = str(_q(a, """INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id)
                      VALUES (%s, %s, %s, %s, %s) RETURNING id""", h.clinic, enc, appt_id, pid, h.doctor))
    return enc, rx


# ── OPD clinical ─────────────────────────────────────────────────────────────

def test_desk_cannot_record_or_read_another_branchs_vitals(hosp):
    visit = _walk_in(hosp, hosp.south_desk, skip_vitals=False)
    appt = visit["appointment_id"]
    vitals = {"bp_systolic": 120, "bp_diastolic": 80, "pulse_bpm": 72}
    assert _call(hosp, hosp.north_desk, "PUT", f"/admin/opd/encounters/by-appointment/{appt}/vitals",
                 vitals).status_code == 403
    assert _call(hosp, hosp.north_desk, "GET", f"/admin/opd/encounters/by-appointment/{appt}").status_code == 403
    assert _q(hosp.api.conn, "SELECT count(*) FROM opd_encounters WHERE appointment_id = %s", appt) == 0
    assert _q(hosp.api.conn, "SELECT queue_status FROM appointments WHERE id = %s", appt) == visit["stage"]
    # South's own desk still can
    assert _call(hosp, hosp.south_desk, "PUT", f"/admin/opd/encounters/by-appointment/{appt}/vitals",
                 vitals).status_code == 200


def test_desk_cannot_open_or_send_another_branchs_prescription(hosp):
    appt = _walk_in(hosp, hosp.south_desk)["appointment_id"]
    enc, rx = _encounter_and_rx(hosp, appt)
    for method, path in (("GET", f"/admin/opd/encounters/{enc}/prescription"),
                         ("GET", f"/admin/opd/prescriptions/{rx}/pdf"),
                         ("POST", f"/admin/opd/prescriptions/{rx}/send-whatsapp")):
        r = _call(hosp, hosp.north_desk, method, path)
        assert r.status_code == 403, (path, r.status_code, r.text)
    assert _q(hosp.api.conn, "SELECT send_count FROM opd_prescriptions WHERE id = %s", rx) == 0


# ── OPD queue ────────────────────────────────────────────────────────────────

def test_call_next_acts_only_on_the_desks_own_branch(hosp):
    """A visiting doctor at both branches: North's Call next must not pull South's patient."""
    south = _walk_in(hosp, hosp.south_desk)          # token 1, South
    north = _walk_in(hosp, hosp.north_desk)          # token 2, North
    r = _call(hosp, hosp.north_desk, "POST", "/admin/opd/queue/call-next", {"doctor_id": hosp.doctor})
    assert r.status_code == 200, r.text
    assert r.json()["called"]["appointment_id"] == north["appointment_id"]
    assert _q(hosp.api.conn, "SELECT queue_status FROM appointments WHERE id = %s",
              south["appointment_id"]) == "waiting"
    # the next North call must not finish a South consultation either
    _call(hosp, hosp.south_desk, "POST", "/admin/opd/queue/call-next", {"doctor_id": hosp.doctor})
    _call(hosp, hosp.north_desk, "POST", "/admin/opd/queue/call-next", {"doctor_id": hosp.doctor})
    assert _q(hosp.api.conn, "SELECT queue_status FROM appointments WHERE id = %s",
              south["appointment_id"]) == "in_consultation"


# ── OPD billing ──────────────────────────────────────────────────────────────

def _south_invoice(h, issue=False):
    appt = _walk_in(h, h.south_desk)["appointment_id"]
    south_cash = h.user("scash", ["OPD_BILLING", "OPD_FRONT_DESK"], h.south, staff_role="CASHIER")
    inv = _call(h, south_cash, "POST", f"/admin/opd/appointments/{appt}/invoice")
    assert inv.status_code == 200, inv.text
    if issue:
        assert _call(h, south_cash, "POST", f"/admin/opd/invoices/{inv.json()['id']}/issue").status_code == 200
    return appt, inv.json()


def test_cashier_cannot_edit_another_branchs_draft_bill(hosp):
    _, inv = _south_invoice(hosp)
    r = _call(hosp, hosp.north_cash, "PUT", f"/admin/opd/invoices/{inv['id']}", {
        "expected_updated_at": inv["updated_at"], "discount_paise": 0,
        "items": [{"item_type": "other", "description": "Tampered", "quantity": 1, "unit_price_paise": 1}]})
    assert r.status_code == 403, r.text
    assert _q(hosp.api.conn, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", inv["id"]) == 50000


def test_ad_hoc_bill_cannot_target_another_branchs_visit(hosp):
    appt = _walk_in(hosp, hosp.south_desk)["appointment_id"]
    pid = str(_q(hosp.api.conn, "SELECT patient_id FROM appointments WHERE id = %s", appt))
    r = _call(hosp, hosp.north_cash, "POST", "/admin/opd/invoices", {
        "patient_id": pid, "appointment_id": appt,
        "items": [{"item_type": "consultation", "doctor_id": hosp.doctor, "description": "Consultation",
                   "quantity": 1, "unit_price_paise": 50000}]})
    assert r.status_code == 403, r.text
    assert _q(hosp.api.conn, "SELECT count(*) FROM opd_invoices WHERE appointment_id = %s", appt) == 0


def test_payment_exceptions_are_branch_scoped(hosp):
    _, inv = _south_invoice(hosp, issue=True)
    exc = str(_q(hosp.api.conn, """INSERT INTO opd_payment_exceptions (clinic_id, invoice_id, gateway, gateway_payment_id,
                                   reason, paid_paise, applied_paise) VALUES (%s, %s, 'razorpay', %s, 'overpaid', 60000, 50000)
                                   RETURNING id""", hosp.clinic, inv["id"], "pay_" + uuid.uuid4().hex[:8]))
    listed = _call(hosp, hosp.north_cash, "GET", "/admin/opd/payment-exceptions", status="all").json()["items"]
    assert exc not in {x["id"] for x in listed}
    r = _call(hosp, hosp.north_cash, "POST", f"/admin/opd/payment-exceptions/{exc}/resolve",
              {"action": "settled_offline", "note": "Paid back in cash"})
    assert r.status_code == 403, r.text
    assert _q(hosp.api.conn, "SELECT status FROM opd_payment_exceptions WHERE id = %s", exc) == "open"
    # the clinic admin still sees it
    listed = _call(hosp, hosp.admin, "GET", "/admin/opd/payment-exceptions", status="all").json()["items"]
    assert exc in {x["id"] for x in listed}


def test_drawer_list_is_branch_scoped(hosp):
    south_shift = str(_q(hosp.api.conn, """INSERT INTO opd_cashier_shifts (clinic_id, branch_id, cashier_admin_id,
                        cashier_name) VALUES (%s, %s, %s, 'south cashier') RETURNING id""",
                         hosp.clinic, hosp.south, str(uuid.uuid4())))
    shifts = _call(hosp, hosp.north_cash, "GET", "/admin/opd/shifts").json()
    assert south_shift not in {s["id"] for s in shifts}
    assert south_shift in {s["id"] for s in _call(hosp, hosp.admin, "GET", "/admin/opd/shifts").json()}


# ── IPD ──────────────────────────────────────────────────────────────────────

def test_ipd_staff_cannot_place_a_patient_in_another_branchs_bed(hosp):
    api = hosp.api
    assert api.owner_toggle(hosp.clinic, "ipd_enabled", True).status_code == 200
    ward = _call(hosp, hosp.admin, "POST", "/admin/ipd/wards", {"code": "SW", "name": "South ward",
                                                                 "branch_id": hosp.south}).json()
    beds = _call(hosp, hosp.admin, "POST", f"/admin/ipd/wards/{ward['id']}/beds",
                 {"beds": [{"code": "S1", "bed_category": "general"}, {"code": "S2", "bed_category": "general"}]}
                 ).json()["beds"]
    # admitted by the clinic admin without a branch (allowed), waiting for a bed
    adm = _call(hosp, hosp.admin, "POST", "/admin/ipd/admissions", {
        "patient_id": _patient(api.conn, hosp.clinic), "admitting_doctor_id": hosp.doctor,
        "reason": "Observation", "consent_recorded": True, "idempotency_key": uuid.uuid4().hex}).json()
    north_ipd = hosp.user("nipd", ["IPD_ADMISSIONS", "IPD_NURSING"], hosp.north, staff_role="FRONT_DESK")
    for path, body in ((f"/admin/ipd/admissions/{adm['id']}/assign-bed", {"bed_id": beds[0]["id"]}),
                       (f"/admin/ipd/admissions/{adm['id']}/reserve", {"bed_id": beds[0]["id"]})):
        r = _call(hosp, north_ipd, "POST", path, body)
        assert r.status_code == 403, (path, r.text)
    assert _q(api.conn, "SELECT status FROM ipd_beds WHERE id = %s", beds[0]["id"]) == "available"
    # placed by the admin, a North user cannot move them within South either
    assert _call(hosp, hosp.admin, "POST", f"/admin/ipd/admissions/{adm['id']}/assign-bed",
                 {"bed_id": beds[0]["id"]}).status_code == 200
    r = _call(hosp, north_ipd, "POST", f"/admin/ipd/admissions/{adm['id']}/transfer",
              {"to_bed_id": beds[1]["id"], "expected_bed_id": beds[0]["id"], "reason": "Closer to station"})
    assert r.status_code == 403, r.text
    assert _q(api.conn, "SELECT current_bed_id FROM ipd_admissions WHERE id = %s", adm["id"]) == beds[0]["id"]


def test_pinned_ipd_staff_see_their_branch_and_shared_wards_not_others(hosp):
    """Wards made from the panel had no branch, and pinned staff saw an empty bed board."""
    api = hosp.api
    assert api.owner_toggle(hosp.clinic, "ipd_enabled", True).status_code == 200
    shared = _call(hosp, hosp.admin, "POST", "/admin/ipd/wards", {"code": "GEN", "name": "Shared ward"}).json()
    south = _call(hosp, hosp.admin, "POST", "/admin/ipd/wards", {"code": "SW", "name": "South",
                                                                   "branch_id": hosp.south}).json()
    for w in (shared, south):
        _call(hosp, hosp.admin, "POST", f"/admin/ipd/wards/{w['id']}/beds", {"beds": [{"code": "1", "bed_category": "general"}]})
    north_admin = hosp.user("nadm", ["IPD_ADMIN"], hosp.north)
    own = _call(hosp, north_admin, "POST", "/admin/ipd/wards", {"code": "NW", "name": "North ward"}).json()
    assert own["branch_id"] == hosp.north                  # a pinned admin's ward is their branch's
    north_nurse = hosp.user("nnur", ["IPD_NURSING"], hosp.north, staff_role="NURSE")
    seen = {w["code"] for w in _call(hosp, north_nurse, "GET", "/admin/ipd/board").json()["wards"]}
    assert seen == {"GEN", "NW"}
    census = _call(hosp, north_nurse, "GET", "/admin/ipd/census").json()
    assert census["beds"]["total"] == 1 and {w["code"] for w in census["wards"]} == {"GEN", "NW"}
    assert {w["code"] for w in _call(hosp, hosp.admin, "GET", "/admin/ipd/board").json()["wards"]} == {"GEN", "SW", "NW"}


def test_a_wards_branch_can_be_changed_safely(hosp):
    api = hosp.api
    assert api.owner_toggle(hosp.clinic, "ipd_enabled", True).status_code == 200
    ward = _call(hosp, hosp.admin, "POST", "/admin/ipd/wards", {"code": "GEN", "name": "General"}).json()
    beds = _call(hosp, hosp.admin, "POST", f"/admin/ipd/wards/{ward['id']}/beds",
                 {"beds": [{"code": "1", "bed_category": "general"}]}).json()["beds"]
    north_nurse = hosp.user("nnur", ["IPD_NURSING"], hosp.north, staff_role="NURSE")
    north_admin = hosp.user("nadm", ["IPD_ADMIN"], hosp.north)
    move = lambda who, to: _call(hosp, who, "PATCH", f"/admin/ipd/wards/{ward['id']}", {"branch_id": to})  # noqa: E731

    # shared -> South by the clinic admin; North stops seeing it
    r = move(hosp.admin, hosp.south)
    assert r.status_code == 200 and r.json()["branch_id"] == hosp.south, r.text
    assert "GEN" not in {w["code"] for w in _call(hosp, north_nurse, "GET", "/admin/ipd/board").json()["wards"]}
    # a North-pinned admin can neither take South's ward nor give one away / make it shared
    assert move(north_admin, hosp.north).status_code == 403
    own = _call(hosp, north_admin, "POST", "/admin/ipd/wards", {"code": "NW", "name": "North"}).json()
    for to in (hosp.south, None):
        r = _call(hosp, north_admin, "PATCH", f"/admin/ipd/wards/{own['id']}", {"branch_id": to})
        assert r.status_code == 403, (to, r.text)
    assert _q(api.conn, "SELECT branch_id FROM ipd_wards WHERE id = %s", own["id"]) == hosp.north
    # another clinic's branch id is "not found"
    other = _clinic(api.conn)
    foreign = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'X') RETURNING id", other))
    assert move(hosp.admin, foreign).status_code == 404

    # with a patient in a bed the branch is frozen (their admission belongs to the old branch)
    adm = _call(hosp, hosp.admin, "POST", "/admin/ipd/admissions", {
        "patient_id": _patient(api.conn, hosp.clinic), "admitting_doctor_id": hosp.doctor, "reason": "Observation",
        "consent_recorded": True, "idempotency_key": uuid.uuid4().hex, "bed_id": beds[0]["id"]})
    assert adm.status_code == 201, adm.text
    r = move(hosp.admin, None)
    assert r.status_code == 409 and (r.json()["detail"] or {}).get("code") == "ipd_ward_in_use", r.text
    assert _q(api.conn, "SELECT branch_id FROM ipd_wards WHERE id = %s", ward["id"]) == hosp.south
    # back to shared once nobody is in it (renaming alone never needs that)
    assert _call(hosp, hosp.admin, "PATCH", f"/admin/ipd/wards/{ward['id']}", {"name": "General A"}).status_code == 200


def test_the_database_refuses_moving_an_occupied_ward_even_outside_the_api(hosp):
    api = hosp.api
    assert api.owner_toggle(hosp.clinic, "ipd_enabled", True).status_code == 200
    ward = _call(hosp, hosp.admin, "POST", "/admin/ipd/wards", {"code": "W", "name": "W"}).json()
    bed = _call(hosp, hosp.admin, "POST", f"/admin/ipd/wards/{ward['id']}/beds",
                {"beds": [{"code": "1", "bed_category": "general"}]}).json()["beds"][0]
    _call(hosp, hosp.admin, "POST", "/admin/ipd/admissions", {
        "patient_id": _patient(api.conn, hosp.clinic), "admitting_doctor_id": hosp.doctor, "reason": "Observation",
        "consent_recorded": True, "idempotency_key": uuid.uuid4().hex, "bed_id": bed["id"]})
    import psycopg2
    with pytest.raises(psycopg2.errors.RaiseException, match="ipd_ward_in_use"):
        _q(api.conn, "UPDATE ipd_wards SET branch_id = %s WHERE id = %s", hosp.north, ward["id"])


def test_a_branchs_collection_counts_only_its_own_money(hosp):
    """The collection summary filtered bills by branch but not receipts: North's totals and cashier list
    included South's cash (seen on the Billing page, and by Kriya Ketu's "today's collection")."""
    _, south_inv = _south_invoice(hosp, issue=True)
    south_cash = hosp.user("scash2", ["OPD_BILLING"], hosp.south, staff_role="CASHIER")
    r = _call(hosp, south_cash, "POST", f"/admin/opd/invoices/{south_inv['id']}/receipts",
              {"mode": "cash", "amount_paise": 20000, "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    north = _call(hosp, hosp.north_cash, "GET", "/admin/opd/collections/summary").json()
    assert north["gross_paise"] == 0 and north["modes"]["cash"] == 0 and not north["cashiers"], north
    south = _call(hosp, south_cash, "GET", "/admin/opd/collections/summary").json()
    assert south["gross_paise"] == 20000 and south["modes"]["cash"] == 20000
    whole = _call(hosp, hosp.admin, "GET", "/admin/opd/collections/summary").json()
    assert whole["gross_paise"] >= 20000                      # the clinic admin sees every branch
