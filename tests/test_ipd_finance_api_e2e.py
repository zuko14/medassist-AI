"""G5 IPD finance end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Deposit -> package -> discount approval -> final bill (deposits applied on issue) ->
settle -> discharge; leftover refund; void returns deposits; credit bill discharged
without a waiver and settled later by bank transfer; branch scope and discount limits.
"""

import uuid
from datetime import date

import pytest

from tests.test_ipd_api_e2e import _admit, _code, _patient, _q, _setup_hospital, _clinic, api  # noqa: F401  (fixture)


def _key():
    return uuid.uuid4().hex


@pytest.fixture
def hosp(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    return {"clinic": clinic, "doctor": doctor, "u": u, "beds": beds}


def _stay(api, h, bed="101", payer=None):  # noqa: F811
    r = _admit(api, h["clinic"], h["u"], h["doctor"], h["beds"][bed])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _charge(api, h, aid, category, description, price, qty=1):  # noqa: F811
    api.as_(h["u"]["billing"])
    r = api.call("POST", f"/admin/ipd/admissions/{aid}/charges", h["clinic"], {
        "category": category, "description": description, "service_date": date.today().isoformat(),
        "quantity": qty, "unit_price_paise": price, "source_ref": _key()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post_all(api, h, aid):  # noqa: F811
    api.as_(h["u"]["billing"])
    ids = [c["id"] for c in api.call("GET", f"/admin/ipd/admissions/{aid}", h["clinic"]).json()["charges"]
           if c["status"] == "proposed"]
    if ids:
        assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges/post", h["clinic"], {"charge_ids": ids}).status_code == 200


def _to_discharge(api, h, aid):  # noqa: F811
    api.as_(h["u"]["doctor"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", h["clinic"],
                    {"discharge_type": "regular"}).status_code == 200
    ds = api.call("PUT", f"/admin/ipd/admissions/{aid}/discharge-summary", h["clinic"],
                  {"final_diagnosis": "Acute appendicitis"}).json()
    assert api.call("POST", f"/admin/ipd/discharge-summaries/{ds['id']}/sign", h["clinic"]).status_code == 200


def _final_bill(api, h, aid):  # noqa: F811
    api.as_(h["u"]["billing"])
    bill = api.call("POST", f"/admin/ipd/admissions/{aid}/final-bill", h["clinic"])
    assert bill.status_code == 200, bill.text
    return bill.json()["invoice_id"]


def test_deposit_package_discount_bill_and_discharge(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid = _stay(api, h)

    # package master (admin) and its application to the stay (billing)
    api.as_(h["u"]["admin"])
    pkg = api.call("POST", "/admin/ipd/packages", c, {
        "code": "APPX", "name": "Appendectomy package", "price_paise": 2_000_000,
        "included_categories": ["bed", "nursing", "procedure"], "max_days": 3})
    assert pkg.status_code == 201, pkg.text
    api.as_(h["u"]["billing"])
    assert api.call("POST", "/admin/ipd/packages", c, {
        "code": "X2", "name": "Not allowed", "price_paise": 100, "included_categories": ["bed"]}).status_code == 403
    applied = api.call("POST", f"/admin/ipd/admissions/{aid}/package", c, {"package_id": pkg.json()["id"]})
    assert applied.status_code == 200, applied.text
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/package", c,
                    {"package_id": pkg.json()["id"]}).json().get("replayed") is True

    # deposit at admission: idempotent, numbered, in the cashier's shift
    body = {"mode": "cash", "amount_paise": 1_500_000, "idempotency_key": _key()}
    dep = api.call("POST", f"/admin/ipd/admissions/{aid}/deposits", c, body)
    assert dep.status_code == 201, dep.text
    assert dep.json()["doc_number"].startswith("DEP-") and dep.json()["shift_id"]
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/deposits", c, body).json()["id"] == dep.json()["id"]
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/deposits", c,
                    {"mode": "upi", "amount_paise": 100, "idempotency_key": _key()}).status_code == 422   # no UTR

    # charges: surgeon fee is covered by the package, pharmacy is not
    _charge(api, h, aid, "procedure", "Surgeon fee", 800_000)
    _charge(api, h, aid, "pharmacy", "IV antibiotics", 250_000)
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/charges/accrue-bed", c).json()["added"] >= 1
    _post_all(api, h, aid)
    run = api.call("GET", f"/admin/ipd/admissions/{aid}/running-bill", c).json()
    bed_days = _q(api.conn, "SELECT count(*) FROM ipd_charges WHERE admission_id = %s AND source = 'bed_day'", aid)
    assert run["charged_paise"] == 2_000_000 + 250_000                      # package + pharmacy
    assert run["covered_value_paise"] == 800_000 + 120_000 * bed_days       # surgeon fee + bed days
    cover = [ln for ln in run["lines"] if ln["covered"]]
    assert len(cover) == 1 and cover[0]["covered_count"] == 1 + bed_days and cover[0]["unit_price_paise"] == 0
    assert run["deposits"]["balance_paise"] == 1_500_000
    assert run["due_after_deposits_paise"] == 750_000

    # discount: billing requests (limit 0), cannot approve; an admin approves
    disc = api.call("POST", f"/admin/ipd/admissions/{aid}/discounts", c,
                    {"amount_paise": 50_000, "reason": "Staff family member", "idempotency_key": _key()})
    assert disc.status_code == 201 and disc.json()["status"] == "requested"
    assert api.call("POST", f"/admin/ipd/discounts/{disc.json()['id']}/decision", c,
                    {"decision": "approved"}).status_code == 403
    api.as_(h["u"]["admin"])
    assert api.call("GET", "/admin/ipd/discounts/pending", c).json()["discounts"][0]["id"] == disc.json()["id"]
    ok = api.call("POST", f"/admin/ipd/discounts/{disc.json()['id']}/decision", c, {"decision": "approved"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    # final bill: package line + pharmacy + one covered line; discount carried; deposits applied on issue
    _to_discharge(api, h, aid)
    inv = _final_bill(api, h, aid)
    bill = api.call("GET", f"/admin/opd/invoices/{inv}", c).json()
    assert bill["subtotal_paise"] == 2_250_000 and bill["discount_paise"] == 50_000
    assert any(it["unit_price_paise"] == 0 and "Covered by package" in it["description"] for it in bill["items"])
    issued = api.call("POST", f"/admin/opd/invoices/{inv}/issue", c)
    assert issued.status_code == 200, issued.text
    assert issued.json()["paid_paise"] == 1_500_000 and issued.json()["status"] == "partially_paid"
    assert [r["mode"] for r in issued.json()["receipts"]] == ["deposit"]

    # the rest at the counter; the shift's expected cash holds deposit + receipt
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", c,
                    {"mode": "cash", "amount_paise": 700_000, "idempotency_key": _key()}).status_code == 201
    shift = api.call("GET", "/admin/opd/shifts/current", c).json()
    assert shift["expected_cash_paise"] == 1_500_000 + 700_000

    chk = api.call("GET", f"/admin/ipd/admissions/{aid}/discharge/checklist", c).json()
    assert chk["ready"] and not chk["needs_waiver"], chk
    done = api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", c, {})
    assert done.status_code == 200 and done.json()["status"] == "discharged"
    closed = api.call("POST", f"/admin/opd/shifts/{shift['id']}/close", c, {"declared_cash_paise": 2_200_000})
    assert closed.status_code == 200 and closed.json()["variance_paise"] == 0


def test_leftover_deposit_must_be_refunded_and_void_returns_deposits(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid = _stay(api, h)
    api.as_(h["u"]["billing"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/deposits", c,
                    {"mode": "cash", "amount_paise": 500_000, "idempotency_key": _key()}).status_code == 201
    _charge(api, h, aid, "procedure", "Dressing", 100_000)
    _post_all(api, h, aid)
    _to_discharge(api, h, aid)
    inv = _final_bill(api, h, aid)
    assert api.call("POST", f"/admin/opd/invoices/{inv}/issue", c).json()["status"] == "paid"

    # void (admin): the deposit goes back to the stay; the bill can be raised again
    admin = h["u"]["admin"]
    admin.permissions = ["OPD_ADMIN"]
    api.as_(admin)
    voided = api.call("POST", f"/admin/opd/invoices/{inv}/void", c, {"reason": "Wrong package rate"})
    assert voided.status_code == 200, voided.text
    assert api.call("GET", f"/admin/ipd/admissions/{aid}/running-bill", c).json()["deposits"]["balance_paise"] == 500_000
    inv2 = _final_bill(api, h, aid)
    api.as_(h["u"]["billing"])
    assert api.call("POST", f"/admin/opd/invoices/{inv2}/issue", c).json()["paid_paise"] == 100_000

    # ₹4,000 left over blocks discharge until refunded (admin only, never beyond the balance)
    blocked = api.as_(admin).call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", c, {})
    assert blocked.status_code == 409
    refund = {"mode": "cash", "reason": "Unused advance", "idempotency_key": _key()}
    assert api.as_(h["u"]["billing"]).call("POST", f"/admin/ipd/admissions/{aid}/deposits/refund", c,
                                           {**refund, "amount_paise": 400_000}).status_code == 403
    api.as_(admin)
    over = api.call("POST", f"/admin/ipd/admissions/{aid}/deposits/refund", c, {**refund, "amount_paise": 400_001})
    assert over.status_code == 409 and _code(over) == "ipd_deposit_insufficient"
    ok = api.call("POST", f"/admin/ipd/admissions/{aid}/deposits/refund", c,
                  {**refund, "amount_paise": 400_000, "idempotency_key": _key()})
    assert ok.status_code == 201 and ok.json()["doc_number"].startswith("DRF-")
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", c, {}).status_code == 200


def test_credit_bill_discharges_without_waiver_and_is_settled_by_bank_transfer(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid = _stay(api, h)
    api.as_(h["u"]["billing"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/credit", c, {
        "payer_name": "Acme Corp", "reference": "PO-778", "amount_paise": 1_000_000}).status_code == 403
    api.as_(h["u"]["admin"])
    credit = api.call("POST", f"/admin/ipd/admissions/{aid}/credit", c, {
        "payer_name": "Acme Corp", "reference": "PO-778", "amount_paise": 1_000_000})
    assert credit.status_code == 201, credit.text
    _charge(api, h, aid, "procedure", "Minor procedure", 300_000)
    _post_all(api, h, aid)
    _to_discharge(api, h, aid)
    inv = _final_bill(api, h, aid)
    assert api.call("POST", f"/admin/opd/invoices/{inv}/issue", c).json()["status"] == "issued"
    chk = api.call("GET", f"/admin/ipd/admissions/{aid}/discharge/checklist", c).json()
    assert chk["ready"] and not chk["needs_waiver"]
    assert api.as_(h["u"]["billing"]).call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", c,
                                           {}).status_code == 200

    ar = api.call("GET", "/admin/ipd/receivables", c).json()
    row = next(b for b in ar["bills"] if b["id"] == inv)
    assert row["due_paise"] == 300_000 and row["credit"]["payer_name"] == "Acme Corp" and row["bucket"] == "0-30"
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", c, {
        "mode": "bank_transfer", "amount_paise": 300_000, "idempotency_key": _key()}).status_code == 422   # no UTR
    paid = api.call("POST", f"/admin/opd/invoices/{inv}/receipts", c, {
        "mode": "bank_transfer", "amount_paise": 300_000, "reference": "UTR998877", "idempotency_key": _key()})
    assert paid.status_code == 201, paid.text
    assert inv not in [b["id"] for b in api.call("GET", "/admin/ipd/receivables", c).json()["bills"]]


def test_discount_limits_and_four_eyes(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid = _stay(api, h)
    api.as_(h["u"]["admin"])
    assert api.call("PUT", "/admin/ipd/settings", c, {"discount_approver_limit_paise": 10_000}).status_code == 200
    ipd_admin = api.U("ipdadm", "staff", c, str(uuid.uuid4()), ["IPD_ADMIN", "IPD_BILLING"], staff_role="STAFF")
    other_admin = api.U("ipdadm2", "staff", c, str(uuid.uuid4()), ["IPD_ADMIN"], staff_role="STAFF")
    api.as_(ipd_admin)
    small = api.call("POST", f"/admin/ipd/admissions/{aid}/discounts", c,
                     {"amount_paise": 5_000, "reason": "Rounding off", "idempotency_key": _key()}).json()
    assert small["status"] == "approved" and small["auto_approved"] is True      # within own limit
    assert api.call("POST", f"/admin/ipd/discounts/{small['id']}/decision", c, {"decision": "cancelled"}).status_code == 200
    big = api.call("POST", f"/admin/ipd/admissions/{aid}/discounts", c,
                   {"amount_paise": 50_000, "reason": "Hardship case", "idempotency_key": _key()}).json()
    assert big["status"] == "requested"
    own = api.call("POST", f"/admin/ipd/discounts/{big['id']}/decision", c, {"decision": "approved"})
    assert own.status_code == 403                                          # above limit (and own request)
    assert api.as_(other_admin).call("POST", f"/admin/ipd/discounts/{big['id']}/decision", c,
                                     {"decision": "approved"}).status_code == 403   # above that admin's limit too
    assert api.as_(h["u"]["admin"]).call("POST", f"/admin/ipd/discounts/{big['id']}/decision", c,
                                         {"decision": "approved"}).json()["status"] == "approved"
    # the bill cannot be issued with a different discount, nor while one is pending
    _charge(api, h, aid, "procedure", "Procedure", 200_000)
    _post_all(api, h, aid)
    _to_discharge(api, h, aid)
    inv = _final_bill(api, h, aid)
    assert _q(api.conn, "SELECT discount_paise FROM opd_invoices WHERE id = %s", inv) == 50_000
    _q(api.conn, "UPDATE opd_invoices SET discount_paise = 1 WHERE id = %s", inv)
    bad = api.as_(h["u"]["billing"]).call("POST", f"/admin/opd/invoices/{inv}/issue", c)
    assert bad.status_code == 409 and _code(bad) == "ipd_discount_mismatch"


def test_branch_pinned_billing_cannot_touch_another_branchs_stay(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid = _stay(api, h)
    branch = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'East') RETURNING id", c))
    _q(api.conn, "UPDATE ipd_admissions SET branch_id = %s WHERE id = %s", branch, aid)
    other = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'West') RETURNING id", c))
    pinned = api.U("west_cash", "staff", c, str(uuid.uuid4()), ["IPD_BILLING", "OPD_BILLING"], staff_role="CASHIER",
                   branch_id=other)
    api.as_(pinned)
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/deposits", c,
                    {"mode": "cash", "amount_paise": 1000, "idempotency_key": _key()}).status_code == 403
    assert api.call("GET", f"/admin/ipd/admissions/{aid}/running-bill", c).status_code == 403
    assert _q(api.conn, "SELECT count(*) FROM ipd_deposits WHERE admission_id = %s", aid) == 0
