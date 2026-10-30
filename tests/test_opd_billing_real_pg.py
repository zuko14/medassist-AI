"""OPD money paths through the real HTTP routes and a REAL Postgres (tests/ipd_pg_bridge.py):
receipt idempotency must hold across worker processes, and receipts / refunds / voids are
branch-scoped and audited like the rest of OPD billing."""

import uuid

import pytest

from app.services import opd_billing
from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)


def _issued_invoice(api, clinic, branch=None, total=50000):
    patient = _patient(api.conn, clinic)
    inv = str(_q(api.conn, """INSERT INTO opd_invoices (clinic_id, branch_id, patient_id, status, subtotal_paise,
                              patient_snapshot) VALUES (%s, %s, %s, 'draft', 0, '{}'::jsonb) RETURNING id""",
                 clinic, branch, patient))
    _q(api.conn, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description,
                    quantity, unit_price_paise) VALUES (%s, %s, 1, 'consultation', 'Consultation', 1, %s)""",
       clinic, inv, total)
    _q(api.conn, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    return inv


def _cashier(api, clinic, branch=None, perms=("OPD_BILLING",)):
    return api.U("cash_" + uuid.uuid4().hex[:6], "staff", clinic, str(uuid.uuid4()), list(perms),
                 staff_role="CASHIER", branch_id=branch)


@pytest.fixture
def opd_clinic(api):  # noqa: F811
    clinic = _clinic(api.conn)
    yield clinic
    # close this clinic's OPD day: the shared DB's 103 rollback test refuses live walk-ins
    _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def test_same_receipt_key_on_another_worker_records_the_money_once(api, opd_clinic, monkeypatch):  # noqa: F811
    inv = _issued_invoice(api, opd_clinic)
    api.as_(_cashier(api, opd_clinic))
    body = {"mode": "cash", "amount_paise": 20000, "idempotency_key": "dbl-click-" + uuid.uuid4().hex[:8]}
    first = api.call("POST", f"/admin/opd/invoices/{inv}/receipts", opd_clinic, body)
    assert first.status_code == 201, first.text
    monkeypatch.setattr(opd_billing, "_IDEMPOTENCY_CACHE", {}, raising=False)   # the retry hits another process
    second = api.call("POST", f"/admin/opd/invoices/{inv}/receipts", opd_clinic, body)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]
    assert _q(api.conn, "SELECT count(*) FROM opd_receipts WHERE invoice_id = %s", inv) == 1
    assert _q(api.conn, "SELECT paid_paise FROM opd_invoices WHERE id = %s", inv) == 20000


def test_receipt_refund_and_void_are_branch_scoped_and_audited(api, opd_clinic, monkeypatch):  # noqa: F811
    audited = []

    async def capture(**kw):
        audited.append((kw["action"], kw["resource_id"]))
    monkeypatch.setattr("app.routers.opd.log_admin_action", capture)
    north = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", opd_clinic))
    south = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", opd_clinic))
    inv = _issued_invoice(api, opd_clinic, branch=south)
    api.as_(_cashier(api, opd_clinic, branch=north, perms=("OPD_BILLING", "OPD_ADMIN")))
    key = {"idempotency_key": uuid.uuid4().hex}
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", opd_clinic,
                    {"mode": "cash", "amount_paise": 1000, **key}).status_code == 403
    assert api.call("POST", f"/admin/opd/invoices/{inv}/refunds", opd_clinic,
                    {"mode": "cash", "amount_paise": 1000, "reason": "Wrong patient billed"}).status_code == 403
    assert api.call("POST", f"/admin/opd/invoices/{inv}/void", opd_clinic,
                    {"reason": "Wrong patient billed"}).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM opd_receipts WHERE invoice_id = %s", inv) == 0

    api.as_(_cashier(api, opd_clinic, branch=south, perms=("OPD_BILLING", "OPD_ADMIN")))
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", opd_clinic,
                    {"mode": "cash", "amount_paise": 1000, **key}).status_code == 201
    assert api.call("POST", f"/admin/opd/invoices/{inv}/refunds", opd_clinic,
                    {"mode": "cash", "amount_paise": 1000, "reason": "Patient overpaid"}).status_code == 200
    assert api.call("POST", f"/admin/opd/invoices/{inv}/void", opd_clinic,
                    {"reason": "Raised in error"}).status_code == 200
    assert audited == [("OPD_RECEIPT_CREATE", inv), ("OPD_REFUND_CREATE", inv), ("OPD_INVOICE_VOID", inv)]


def test_a_retried_refund_pays_out_once(api, opd_clinic):  # noqa: F811
    inv = _issued_invoice(api, opd_clinic)
    api.as_(_cashier(api, opd_clinic, perms=("OPD_BILLING", "OPD_ADMIN")))
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", opd_clinic,
                    {"mode": "cash", "amount_paise": 30000, "idempotency_key": uuid.uuid4().hex}).status_code == 201
    body = {"mode": "cash", "amount_paise": 10000, "reason": "Test not done", "idempotency_key": uuid.uuid4().hex}
    first = api.call("POST", f"/admin/opd/invoices/{inv}/refunds", opd_clinic, body)
    second = api.call("POST", f"/admin/opd/invoices/{inv}/refunds", opd_clinic, body)
    assert first.status_code == second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert _q(api.conn, "SELECT count(*) FROM opd_receipts WHERE invoice_id = %s AND kind = 'refund'", inv) == 1
    assert _q(api.conn, "SELECT paid_paise FROM opd_invoices WHERE id = %s", inv) == 20000
    # the payment's key cannot be replayed as a refund
    pay_key = _q(api.conn, "SELECT idempotency_key FROM opd_receipts WHERE invoice_id = %s AND kind = 'payment'", inv)
    assert api.call("POST", f"/admin/opd/invoices/{inv}/refunds", opd_clinic,
                    {**body, "idempotency_key": pay_key}).status_code == 409



def test_desk_moves_only_its_own_branchs_queue(api, opd_clinic):  # noqa: F811
    north = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'North') RETURNING id", opd_clinic))
    south = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'South') RETURNING id", opd_clinic))
    doctor, patient = _doctor(api.conn, opd_clinic), _patient(api.conn, opd_clinic)

    def desk(branch):
        return api.U("desk_" + uuid.uuid4().hex[:6], "staff", opd_clinic, str(uuid.uuid4()), ["OPD_FRONT_DESK"],
                     staff_role="FRONT_DESK", branch_id=branch)
    api.as_(desk(south))
    walk = api.call("POST", "/admin/opd/walk-ins", opd_clinic, {"patient_id": patient, "doctor_id": doctor,
                                                                 "skip_vitals": True})
    assert walk.status_code == 201, walk.text
    appt = walk.json()["appointment_id"]
    assert walk.json()["branch_id"] == south
    stage = walk.json()["stage"]
    api.as_(desk(north))
    r = api.call("POST", f"/admin/opd/queue/{appt}/stage", opd_clinic,
                 {"to_stage": "cancelled", "expected_from": stage, "reason": "test"})
    assert r.status_code == 403, r.text
    assert _q(api.conn, "SELECT queue_status FROM appointments WHERE id = %s", appt) == stage


def test_prepaid_more_than_the_bill_applies_what_fits_and_parks_the_rest(api, opd_clinic, monkeypatch):  # noqa: F811
    """A WhatsApp-prepaid ₹500 on a ₹400 bill: ₹400 settles the bill, ₹100 is parked for refund.
    It used to crash after issuing, and a retry skipped the prepayment, so the desk collected again."""
    async def no_alert(*a, **k):
        return None
    monkeypatch.setattr("connectors.runner.send_admin_alert", no_alert, raising=False)
    doctor, patient = _doctor(api.conn, opd_clinic), _patient(api.conn, opd_clinic)
    _q(api.conn, "UPDATE doctors SET consultation_fee = 400 WHERE id = %s", doctor)
    api.as_(api.U("desk", "staff", opd_clinic, str(uuid.uuid4()), ["OPD_FRONT_DESK", "OPD_BILLING"],
                  staff_role="FRONT_DESK"))
    appt = api.call("POST", "/admin/opd/walk-ins", opd_clinic, {"patient_id": patient, "doctor_id": doctor,
                                                                 "skip_vitals": True}).json()["appointment_id"]
    _q(api.conn, """UPDATE appointments SET payment_id = 'pay_PREPAID1', amount_paise = 50000,
                    payment_gateway = 'razorpay' WHERE id = %s""", appt)
    inv = api.call("POST", f"/admin/opd/appointments/{appt}/invoice", opd_clinic).json()
    assert inv["total_paise"] == 40000
    issued = api.call("POST", f"/admin/opd/invoices/{inv['id']}/issue", opd_clinic)
    assert issued.status_code == 200, issued.text
    assert issued.json()["status"] == "paid" and issued.json()["paid_paise"] == 40000
    again = api.call("POST", f"/admin/opd/invoices/{inv['id']}/issue", opd_clinic)     # retry: no double apply
    assert again.status_code == 200 and again.json()["paid_paise"] == 40000
    assert _q(api.conn, "SELECT count(*) FROM opd_receipts WHERE invoice_id = %s", inv["id"]) == 1
    assert _q(api.conn, """SELECT paid_paise - applied_paise FROM opd_payment_exceptions
                           WHERE invoice_id = %s AND gateway_payment_id = 'pay_PREPAID1'""", inv["id"]) == 10000


def test_call_next_is_one_at_a_time_per_doctor(api, opd_clinic):  # noqa: F811
    """A double-clicked "Call next": the second click used to finish the patient the first had
    just called (straight to billing, no consultation) and call a third one."""
    doctor = _doctor(api.conn, opd_clinic)
    api.as_(api.U("desk", "staff", opd_clinic, str(uuid.uuid4()), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK"))
    for _ in range(2):
        r = api.call("POST", "/admin/opd/walk-ins", opd_clinic, {"patient_id": _patient(api.conn, opd_clinic),
                                                                  "doctor_id": doctor, "skip_vitals": True})
        assert r.status_code == 201 and r.json()["stage"] == "waiting", r.text
    lock = f"opd_call_next:{opd_clinic}:{doctor}"
    _q(api.conn, """INSERT INTO scheduler_locks (job_name, locked_by, locked_at, expires_at)
                    VALUES (%s, 'other-click', now(), now() + interval '30 seconds')""", lock)
    busy = api.call("POST", "/admin/opd/queue/call-next", opd_clinic, {"doctor_id": doctor})
    assert busy.status_code == 409, busy.text
    assert _q(api.conn, """SELECT count(*) FROM appointments WHERE doctor_id = %s
                           AND queue_status = 'in_consultation'""", doctor) == 0
    _q(api.conn, "DELETE FROM scheduler_locks WHERE job_name = %s", lock)
    ok = api.call("POST", "/admin/opd/queue/call-next", opd_clinic, {"doctor_id": doctor})
    assert ok.status_code == 200 and ok.json()["called"]["token_number"] == 1, ok.text
    assert _q(api.conn, "SELECT count(*) FROM scheduler_locks WHERE job_name = %s", lock) == 0   # released


def _draft_for_new_visit(api, clinic):
    doctor, patient = _doctor(api.conn, clinic), _patient(api.conn, clinic)
    _q(api.conn, "UPDATE doctors SET consultation_fee = 500 WHERE id = %s", doctor)
    api.as_(api.U("desk", "staff", clinic, str(uuid.uuid4()), ["OPD_FRONT_DESK", "OPD_BILLING"], staff_role="CASHIER"))
    appt = api.call("POST", "/admin/opd/walk-ins", clinic, {"patient_id": patient, "doctor_id": doctor,
                                                             "skip_vitals": True}).json()["appointment_id"]
    inv = api.call("POST", f"/admin/opd/appointments/{appt}/invoice", clinic).json()
    return doctor, inv


def _lines(doctor, *extra):
    return [{"item_type": "consultation", "doctor_id": doctor, "description": "Consultation", "quantity": 1,
             "unit_price_paise": 50000}, *extra]


def test_a_stale_draft_save_is_refused_and_keeps_the_other_cashiers_lines(api, opd_clinic):  # noqa: F811
    """Two cashiers open one draft; the second save used to overwrite the first (0.5 s tolerance,
    check-then-write)."""
    doctor, inv = _draft_for_new_visit(api, opd_clinic)
    seen_by_a = inv["updated_at"]
    dressing = {"item_type": "other", "description": "Dressing", "quantity": 1, "unit_price_paise": 20000}
    b = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic,
                 {"expected_updated_at": inv["updated_at"], "items": _lines(doctor, dressing)})
    assert b.status_code == 200, b.text
    a = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic,
                 {"expected_updated_at": seen_by_a, "items": _lines(doctor)})
    assert a.status_code == 409, a.text
    assert _q(api.conn, "SELECT count(*) FROM opd_invoice_items WHERE invoice_id = %s", inv["id"]) == 2
    assert _q(api.conn, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", inv["id"]) == 70000
    again = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic,      # with the fresh version: fine
                     {"expected_updated_at": b.json()["updated_at"], "items": _lines(doctor)})
    assert again.status_code == 200 and again.json()["subtotal_paise"] == 50000, again.text


def test_a_failed_draft_save_changes_nothing(api, opd_clinic):  # noqa: F811
    """The old save deleted every line, then inserted one by one: a failing line left the
    draft with lines missing."""
    doctor, inv = _draft_for_new_visit(api, opd_clinic)
    huge = {"item_type": "other", "description": "Package", "quantity": 999, "unit_price_paise": 100_000_000}
    r = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic,
                 {"expected_updated_at": inv["updated_at"], "items": _lines(doctor, huge)})
    assert r.status_code == 422, r.text
    assert _q(api.conn, "SELECT count(*) FROM opd_invoice_items WHERE invoice_id = %s", inv["id"]) == 1
    assert _q(api.conn, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", inv["id"]) == 50000


def test_a_discounted_draft_can_be_saved_again(api, opd_clinic):  # noqa: F811
    doctor, inv = _draft_for_new_visit(api, opd_clinic)
    first = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic, {
        "expected_updated_at": inv["updated_at"], "items": _lines(doctor),
        "discount_paise": 10000, "discount_reason": "Senior citizen"})
    assert first.status_code == 200, first.text
    second = api.call("PUT", f"/admin/opd/invoices/{inv['id']}", opd_clinic, {
        "expected_updated_at": first.json()["updated_at"], "items": _lines(doctor),
        "discount_paise": 5000, "discount_reason": "Senior citizen", "notes": "revised"})
    assert second.status_code == 200, second.text
    assert (second.json()["discount_paise"], second.json()["total_paise"]) == (5000, 45000)


def test_invoice_list_accepts_several_statuses(api, opd_clinic):  # noqa: F811
    """Kriya Ketu's "pending payments" reads issued + partially paid bills in one call; one status still works."""
    unpaid = _issued_invoice(api, opd_clinic)
    part = _issued_invoice(api, opd_clinic)
    paid = _issued_invoice(api, opd_clinic, total=1000)
    api.as_(_cashier(api, opd_clinic))
    assert api.call("POST", f"/admin/opd/invoices/{part}/receipts", opd_clinic,
                    {"mode": "cash", "amount_paise": 100, "idempotency_key": uuid.uuid4().hex}).status_code == 201
    assert api.call("POST", f"/admin/opd/invoices/{paid}/receipts", opd_clinic,
                    {"mode": "cash", "amount_paise": 1000, "idempotency_key": uuid.uuid4().hex}).status_code == 201
    ids = lambda status: {r["id"] for r in api.call("GET", "/admin/opd/invoices", opd_clinic, status=status).json()}  # noqa: E731
    assert ids("issued,partially_paid") == {unpaid, part}
    assert ids("paid") == {paid} and ids("issued") == {unpaid}
