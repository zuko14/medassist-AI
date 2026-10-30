"""G1 orders end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Consult order -> visit draft bill -> lab desk worklist -> sample collection,
with branch scoping, the paid-order cancellation block, supplementary bills
and IPD orders reaching the same worklist.
"""

import uuid

import pytest

from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, _setup_hospital, api  # noqa: F401  (fixture)


@pytest.fixture
def world(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    cbc = str(_q(api.conn, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                              VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id""", clinic))
    ecg = {"order_type": "procedure", "catalog_type": "custom", "item_name": "ECG", "unit_price_paise": 20000}
    U, uid = api.U, lambda: str(uuid.uuid4())
    users = {
        "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK", "OPD_BILLING"], staff_role="FRONT_DESK"),
        "doctor": U("drmeena", "staff", clinic, uid(), ["OPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor),
        "other_doctor": U("drx", "staff", clinic, uid(), ["OPD_CLINICAL"], staff_role="DOCTOR",
                          doctor_id=_doctor(api.conn, clinic)),
        "phleb": U("phleb", "staff", clinic, uid(), [], staff_role="PHLEBOTOMIST"),
        "admin": U("admin", "clinic_admin", clinic, uid()),
    }
    yield {"clinic": clinic, "doctor": doctor, "cbc": cbc, "ecg": ecg, "u": users}
    _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def _visit(api, w, branch=None):  # noqa: F811
    desk = w["u"]["desk"] if not branch else api.U("desk_" + uuid.uuid4().hex[:5], "staff", w["clinic"],
                                                    str(uuid.uuid4()), ["OPD_FRONT_DESK", "OPD_BILLING"],
                                                    staff_role="FRONT_DESK", branch_id=branch)
    api.as_(desk)
    walk = api.call("POST", "/admin/opd/walk-ins", w["clinic"],
                    {"patient_id": _patient(api.conn, w["clinic"]), "doctor_id": w["doctor"], "skip_vitals": True})
    assert walk.status_code == 201, walk.text
    appt = walk.json()["appointment_id"]
    enc = api.as_(w["u"]["doctor"]).call("GET", f"/admin/opd/encounters/by-appointment/{appt}", w["clinic"])
    assert enc.status_code == 200, enc.text
    return appt, enc.json()["id"]


def _order(api, w, enc, items, key=None):  # noqa: F811
    return api.as_(w["u"]["doctor"]).call("POST", f"/admin/opd/encounters/{enc}/orders", w["clinic"],
                                          {"orders": items, "idempotency_key": key or uuid.uuid4().hex})


def _cbc(w, **kw):
    return {"order_type": "investigation", "catalog_type": "lab_test", "catalog_reference_id": w["cbc"], **kw}


def test_consult_order_reaches_the_draft_bill_and_the_worklist(api, world):  # noqa: F811
    w = world
    appt, enc = _visit(api, w)
    inv = api.as_(w["u"]["desk"]).call("POST", f"/admin/opd/appointments/{appt}/invoice", w["clinic"]).json()

    key = uuid.uuid4().hex
    r = _order(api, w, enc, [_cbc(w, urgency="stat"), w["ecg"]], key=key)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["billing"] == {"invoice_id": inv["id"], "added": 2, "created": False, "pending": 0}
    cbc_id = body["orders"][0]["id"]
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", cbc_id) == "ordered"
    lines = api.as_(w["u"]["desk"]).call("GET", f"/admin/opd/invoices/{inv['id']}", w["clinic"]).json()["items"]
    assert [(ln["description"], ln["unit_price_paise"], ln["order_id"]) for ln in lines[1:]] == \
        [("CBC", 35000, cbc_id), ("ECG", 20000, body["orders"][1]["id"])]

    replay = _order(api, w, enc, [_cbc(w)], key=key)   # double click
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    assert _q(api.conn, "SELECT count(*) FROM opd_orders WHERE appointment_id = %s", appt) == 2

    # the cashier cannot re-price an ordered line
    api.as_(w["u"]["desk"])
    full = api.call("GET", f"/admin/opd/invoices/{inv['id']}", w["clinic"]).json()
    items = [{k: ln[k] for k in ("item_type", "description", "quantity", "unit_price_paise", "order_id", "doctor_id",
                                 "lab_test_id")} for ln in full["items"]]
    items[1]["unit_price_paise"] = 100
    saved = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", w["clinic"],
                     {"expected_updated_at": full["updated_at"], "items": items})
    assert saved.status_code == 200, saved.text
    assert [ln["unit_price_paise"] for ln in saved.json()["items"]][1] == 35000
    assert saved.json()["items"][1]["order_id"] == cbc_id   # the draft save keeps the link

    wl = api.as_(w["u"]["phleb"]).call("GET", "/admin/diagnostic/worklist", w["clinic"]).json()
    mine = [o for o in wl["orders"] if o["appointment_id"] == appt]
    assert mine[0]["item_name"] == "CBC" and mine[0]["urgency"] == "stat" and mine[0]["patient_name"]
    assert mine[0]["billing"]["invoice_status"] == "draft"


def test_only_the_treating_doctor_orders(api, world):  # noqa: F811
    w = world
    _, enc = _visit(api, w)
    r = api.as_(w["u"]["other_doctor"]).call("POST", f"/admin/opd/encounters/{enc}/orders", w["clinic"],
                                             {"orders": [_cbc(w)], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403
    r = api.as_(w["u"]["desk"]).call("POST", f"/admin/opd/encounters/{enc}/orders", w["clinic"],
                                     {"orders": [_cbc(w)], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 403


def test_sample_collection_is_compare_and_set(api, world):  # noqa: F811
    w = world
    _, enc = _visit(api, w)
    orders = _order(api, w, enc, [_cbc(w), w["ecg"]]).json()["orders"]
    cbc, ecg = orders[0]["id"], orders[1]["id"]
    api.as_(w["u"]["phleb"])
    ok = api.call("POST", f"/admin/diagnostic/orders/{cbc}/collect", w["clinic"], {"specimen_barcode": "S-000123"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "sample_collected" and ok.json()["sample_collected_by_name"] == "phleb"
    again = api.call("POST", f"/admin/diagnostic/orders/{cbc}/collect", w["clinic"], {"specimen_barcode": "S-000999"})
    assert again.status_code == 409
    assert _q(api.conn, "SELECT specimen_barcode FROM opd_orders WHERE id = %s", cbc) == "S-000123"
    assert api.call("POST", f"/admin/diagnostic/orders/{ecg}/collect", w["clinic"],
                    {"specimen_barcode": "S-000124"}).status_code == 409     # an ECG has no specimen
    assert api.call("POST", f"/admin/diagnostic/orders/{cbc}/collect", w["clinic"],
                    {"specimen_barcode": "S1"}).status_code == 422           # barcode too short
    # phlebotomists collect; they do not record results
    assert api.call("POST", f"/admin/diagnostic/orders/{cbc}/complete", w["clinic"],
                    {"result_summary": "Normal"}).status_code == 403
    api.as_(w["u"]["admin"])
    done = api.call("POST", f"/admin/diagnostic/orders/{ecg}/complete", w["clinic"], {"result_summary": "Sinus rhythm"})
    assert done.status_code == 200 and done.json()["status"] == "completed"


def test_branch_b_staff_cannot_see_or_collect_branch_a_orders(api, world):  # noqa: F811
    w = world
    a = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'A') RETURNING id", w["clinic"]))
    b = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'B') RETURNING id", w["clinic"]))
    _, enc = _visit(api, w, branch=a)
    order = _order(api, w, enc, [_cbc(w)]).json()["orders"][0]
    assert order["branch_id"] == a

    def phleb(branch):
        return api.U("ph_" + uuid.uuid4().hex[:5], "staff", w["clinic"], str(uuid.uuid4()), [],
                     staff_role="PHLEBOTOMIST", branch_id=branch)
    api.as_(phleb(b))
    ids = [o["id"] for o in api.call("GET", "/admin/diagnostic/worklist", w["clinic"]).json()["orders"]]
    assert order["id"] not in ids
    assert api.call("GET", "/admin/diagnostic/worklist", w["clinic"], branch_id=a).status_code == 403
    assert api.call("POST", f"/admin/diagnostic/orders/{order['id']}/collect", w["clinic"],
                    {"specimen_barcode": "B-0001"}).status_code == 403
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", order["id"]) == "ordered"
    api.as_(phleb(a))
    assert order["id"] in [o["id"] for o in api.call("GET", "/admin/diagnostic/worklist", w["clinic"]).json()["orders"]]


def test_cancel_removes_a_draft_line_and_a_paid_order_needs_a_refund_first(api, world):  # noqa: F811
    w = world
    appt, enc = _visit(api, w)
    inv = api.as_(w["u"]["desk"]).call("POST", f"/admin/opd/appointments/{appt}/invoice", w["clinic"]).json()
    base = inv["subtotal_paise"]
    cbc, ecg = [o["id"] for o in _order(api, w, enc, [_cbc(w), w["ecg"]]).json()["orders"]]

    api.as_(w["u"]["doctor"])
    gone = api.call("DELETE", f"/admin/opd/orders/{ecg}", w["clinic"], {"reason": "Not indicated"})
    assert gone.status_code == 200, gone.text
    assert gone.json()["removed_line"] is True and gone.json()["order"]["status"] == "cancelled"
    assert _q(api.conn, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", inv["id"]) == base + 35000

    api.as_(w["u"]["desk"])
    issued = api.call("POST", f"/admin/opd/invoices/{inv['id']}/issue", w["clinic"])
    assert issued.status_code == 200, issued.text
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", cbc) == "billed"
    assert api.call("POST", "/admin/opd/shifts/open", w["clinic"], {"opening_float_paise": 0}).status_code in (200, 201)
    paid = api.call("POST", f"/admin/opd/invoices/{inv['id']}/receipts", w["clinic"],
                    {"mode": "cash", "amount_paise": issued.json()["total_paise"], "idempotency_key": uuid.uuid4().hex})
    assert paid.status_code == 201, paid.text

    api.as_(w["u"]["doctor"])
    blocked = api.call("DELETE", f"/admin/opd/orders/{cbc}", w["clinic"], {"reason": "Patient declined"})
    assert blocked.status_code == 409
    assert "refund" in blocked.json()["detail"]["message"]
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", cbc) == "billed"


def test_order_after_the_consult_is_paid_goes_on_a_supplementary_bill(api, world):  # noqa: F811
    w = world
    appt, enc = _visit(api, w)
    api.as_(w["u"]["desk"])
    inv = api.call("POST", f"/admin/opd/appointments/{appt}/invoice", w["clinic"]).json()
    assert api.call("POST", f"/admin/opd/invoices/{inv['id']}/issue", w["clinic"]).status_code == 200

    placed = _order(api, w, enc, [_cbc(w)]).json()
    assert placed["billing"]["pending"] == 1 and placed["billing"]["invoice_id"] is None
    api.as_(w["u"]["desk"])
    pending = api.call("GET", f"/admin/opd/appointments/{appt}/orders", w["clinic"]).json()["orders"]
    assert pending[0]["billing"] is None
    billed = api.call("POST", f"/admin/opd/appointments/{appt}/orders/bill", w["clinic"])
    assert billed.status_code == 200, billed.text
    assert billed.json()["created"] is True and billed.json()["added"] == 1
    sup = billed.json()["invoice_id"]
    assert sup != inv["id"]
    assert _q(api.conn, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", sup) == 35000
    again = api.call("POST", f"/admin/opd/appointments/{appt}/orders/bill", w["clinic"]).json()
    assert again["added"] == 0     # nothing billed twice


def test_ipd_investigation_order_reaches_the_diagnostic_worklist(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    api.as_(u["desk"])
    adm = api.call("POST", "/admin/ipd/admissions", clinic, {
        "patient_id": _patient(api.conn, clinic), "admitting_doctor_id": doctor, "reason": "Fever",
        "bed_id": beds["101"], "consent_recorded": True, "idempotency_key": uuid.uuid4().hex}).json()
    cbc = str(_q(api.conn, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                              VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id""", clinic))
    api.as_(u["doctor"])
    order = api.call("POST", f"/admin/ipd/admissions/{adm['id']}/orders", clinic, {
        "order_type": "investigation", "description": "CBC", "lab_test_id": cbc, "priority": "urgent",
        "idempotency_key": uuid.uuid4().hex})
    assert order.status_code == 201, order.text
    wl = api.as_(u["nurse"]).call("GET", "/admin/diagnostic/worklist", clinic).json()["orders"]
    row = next(o for o in wl if o["admission_id"] == adm["id"])
    assert (row["source"], row["bed_code"], row["item_name"]) == ("ipd", "101", "CBC")
    assert row["admission_number"] == adm["admission_number"]
    assert api.call("POST", f"/admin/diagnostic/orders/{row['id']}/collect", clinic,
                    {"specimen_barcode": "IP-77001"}).status_code == 200
    # OPD cancellation is refused: IPD orders stop from the ward chart
    assert api.as_(u["admin"]).call("DELETE", f"/admin/opd/orders/{row['id']}", clinic,
                                    {"reason": "x" * 5}).status_code == 409
