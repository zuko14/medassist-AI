"""G6 insurance / TPA end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Payer master (seeded on first read) -> policy on the patient -> pre-auth with a
query, approval and enhancement -> deposit -> cashless bill (issued + claim filed:
₹1,15,000 = insurer ₹90,000 + patient ₹25,000) -> counter takes only the patient's
share -> discharge -> NEFT + TDS + disallowances -> write-off (billing approval) and
recovery -> ageing. Plus permission, entitlement and tenant boundaries.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import insurance
from tests.test_ipd_api_e2e import _admit, _clinic, _code, _patient, _q, _setup_hospital, api  # noqa: F401  (fixture)
from tests.test_ipd_finance_api_e2e import _charge, _post_all, _to_discharge


def _key():
    return uuid.uuid4().hex


@pytest.fixture
def hosp(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    uid = lambda: str(uuid.uuid4())  # noqa: E731
    u["tpa"] = api.U("tpadesk", "staff", clinic, uid(), [], staff_role="TPA_DESK")   # role alone implies the grants
    other = _clinic(api.conn)
    u["other_admin"] = api.U("oadmin", "clinic_admin", other, uid())
    return {"clinic": clinic, "other": other, "doctor": doctor, "u": u, "beds": beds}


def _insured_stay(api, h, bed="101"):  # noqa: F811
    patient = _patient(api.conn, h["clinic"])
    r = _admit(api, h["clinic"], h["u"], h["doctor"], h["beds"][bed], patient=patient)
    assert r.status_code == 201, r.text
    api.as_(h["u"]["tpa"])
    payers = api.call("GET", "/admin/insurance/payers", h["clinic"]).json()["payers"]
    medi = next(p for p in payers if p["name"] == "Medi Assist TPA")
    pol = api.call("POST", "/admin/insurance/policies", h["clinic"], {
        "patient_id": patient, "payer_id": medi["id"], "policy_number": "SH-2026-778", "member_id": "MA1234567",
        "sum_insured_paise": 50_000_000, "valid_from": str(date.today() - timedelta(days=30)),
        "valid_to": str(date.today() + timedelta(days=335))})
    assert pol.status_code == 201, pol.text
    return r.json()["id"], patient, pol.json()["id"], medi


def _approved(api, h, aid, policy, approved=9_000_000):  # noqa: F811
    api.as_(h["u"]["tpa"])
    pa = api.call("POST", f"/admin/ipd/admissions/{aid}/pre-auth", h["clinic"], {
        "policy_id": policy, "requested_paise": 10_000_000, "provisional_diagnosis": "Cholelithiasis",
        "treatment_plan": "Lap cholecystectomy", "expected_stay_days": 3})
    assert pa.status_code == 201, pa.text
    ok = api.call("PATCH", f"/admin/ipd/pre-auth/{pa.json()['id']}/decision", h["clinic"], {
        "decision": "approved", "approved_amount_paise": approved, "approval_ref": "MA/APP/2026/1"})
    assert ok.status_code == 200, ok.text
    return pa.json()


def _billable(api, h, aid):  # noqa: F811
    _charge(api, h, aid, "bed", "General ward", 10_000_000)
    _charge(api, h, aid, "pharmacy", "Medicines", 1_500_000)
    _post_all(api, h, aid)
    _to_discharge(api, h, aid)


def test_seeded_payers_and_master_validation(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    api.as_(h["u"]["tpa"])
    payers = api.call("GET", "/admin/insurance/payers", c).json()["payers"]
    assert len(payers) == 12 and {p["payer_type"] for p in payers} == {"tpa", "private_insurance", "government_scheme"}
    assert len(api.call("GET", "/admin/insurance/payers", c).json()["payers"]) == 12      # seeded once
    made = api.call("POST", "/admin/insurance/payers", c, {"name": "Raksha TPA", "payer_type": "tpa",
                                                         "portal_url": "https://raksha.example"})
    assert made.status_code == 201, made.text
    assert api.call("POST", "/admin/insurance/payers", c, {"name": "Raksha TPA", "payer_type": "tpa"}).status_code == 409
    assert api.call("POST", "/admin/insurance/payers", c, {"name": "Evil", "payer_type": "tpa",
                                                         "portal_url": "javascript:alert(1)"}).status_code == 422
    off = api.call("PUT", f"/admin/insurance/payers/{made.json()['id']}", c,
                   {"name": "Raksha TPA", "payer_type": "tpa", "is_active": False})
    assert off.status_code == 200 and off.json()["is_active"] is False
    # cashier reads, cannot manage; nurse sees nothing; another clinic sees its own list only
    api.as_(h["u"]["billing"])
    assert api.call("GET", "/admin/insurance/payers", c).status_code == 200
    assert api.call("POST", "/admin/insurance/payers", c, {"name": "X", "payer_type": "tpa"}).status_code == 403
    assert api.as_(h["u"]["nurse"]).call("GET", "/admin/insurance/payers", c).status_code == 403
    assert api.as_(h["u"]["other_admin"]).call("GET", "/admin/insurance/payers", c).status_code == 403


def test_cashless_journey_split_counter_discharge_settlement_and_ageing(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid, patient, policy, medi = _insured_stay(api, h)

    # pre-auth: query -> resubmit -> approve ₹80,000 -> enhancement -> ₹90,000
    pa = api.call("POST", f"/admin/ipd/admissions/{aid}/pre-auth", c, {
        "policy_id": policy, "requested_paise": 8_000_000, "provisional_diagnosis": "Cholelithiasis"}).json()
    assert pa["status"] == "submitted" and pa["pre_auth_number"].startswith("PRE-")
    q = api.call("PATCH", f"/admin/ipd/pre-auth/{pa['id']}/decision", c, {"decision": "query_raised",
                                                                          "note": "Share USG abdomen"})
    assert q.json()["status"] == "query_raised"
    assert api.call("POST", f"/admin/ipd/pre-auth/{pa['id']}/action", c,
                    {"action": "submit", "note": "USG uploaded"}).json()["status"] == "submitted"
    assert api.call("PATCH", f"/admin/ipd/pre-auth/{pa['id']}/decision", c,
                    {"decision": "approved", "approved_amount_paise": 8_000_001,
                     "approval_ref": "R"}).status_code == 422           # above what was requested
    api.call("PATCH", f"/admin/ipd/pre-auth/{pa['id']}/decision", c,
             {"decision": "approved", "approved_amount_paise": 8_000_000, "approval_ref": "MA/APP/1"})
    api.call("POST", f"/admin/ipd/pre-auth/{pa['id']}/action", c,
             {"action": "request_enhancement", "amount_paise": 2_000_000, "note": "Stay extended by a day"})
    _billable(api, h, aid)
    api.as_(h["u"]["tpa"])
    blocked = api.call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c, {})
    assert blocked.status_code == 409 and _code(blocked) == "ins_enhancement_pending"
    enh = api.call("PATCH", f"/admin/ipd/pre-auth/{pa['id']}/decision", c,
                   {"decision": "approved", "approved_amount_paise": 9_000_000, "approval_ref": "MA/ENH/2"})
    assert enh.json()["total_approved_paise"] == 9_000_000

    # chart card: the nurse cannot see it; billing can
    assert api.as_(h["u"]["nurse"]).call("GET", f"/admin/ipd/admissions/{aid}/insurance", c).status_code == 403
    card = api.as_(h["u"]["billing"]).call("GET", f"/admin/ipd/admissions/{aid}/insurance", c).json()
    assert card["pre_auth"]["status"] == "approved" and card["pre_auth"]["payer"]["name"] == "Medi Assist TPA"
    assert [e["event_type"] for e in card["history"]][:2] == ["preauth_submitted", "preauth_query"]

    # the cashless bill: issued GST invoice + claim with the dual split
    api.as_(h["u"]["tpa"])
    cl = api.call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c, {})
    assert cl.status_code == 200, cl.text
    claim = cl.json()
    assert claim["split"]["total_bill_paise"] == 11_500_000
    assert (claim["split"]["payer_payable_paise"], claim["split"]["patient_payable_paise"]) == (9_000_000, 2_500_000)
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c, {}).json()["id"] == claim["id"]
    inv = claim["invoice_id"]
    assert _q(api.conn, "SELECT status FROM opd_invoices WHERE id = %s", inv) == "issued"

    # the counter takes the patient's ₹25,000 and not a rupee of the insurer's share
    api.as_(h["u"]["billing"])
    over = api.call("POST", f"/admin/opd/invoices/{inv}/receipts", c,
                    {"mode": "cash", "amount_paise": 2_500_001, "idempotency_key": _key()})
    assert over.status_code == 409 and "insurer" in over.text
    assert api.call("POST", f"/admin/opd/invoices/{inv}/receipts", c,
                    {"mode": "cash", "amount_paise": 2_500_000, "idempotency_key": _key()}).status_code == 201
    chk = api.call("GET", f"/admin/ipd/admissions/{aid}/discharge/checklist", c).json()
    assert chk["ready"] and not chk["needs_waiver"], chk
    done = api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/complete", c, {})
    assert done.status_code == 200 and done.json()["status"] == "discharged"

    # pipeline and claims lists
    api.as_(h["u"]["tpa"])
    assert any(i["id"] == claim["id"] for i in api.call("GET", "/admin/insurance/claims", c).json()["items"])
    assert api.call("GET", "/admin/insurance/pre-auths", c).json()["counts"].get("approved") == 1

    # ageing before settlement: ₹90,000 in the <30 bucket under Medi Assist
    age = api.call("GET", "/admin/insurance/ageing", c).json()
    row = next(p for p in age["payers"] if p["payer_id"] == medi["id"])
    assert (row["lt_30"], row["total_paise"], row["claims"]) == (9_000_000, 9_000_000, 1)

    # settlement: NEFT ₹80,000 + TDS ₹2,000 + two disallowances ₹8,000
    key = _key()
    body = {"amount_received_paise": 8_000_000, "utr_number": "SBIN026001234", "tds_paise": 200_000,
            "disallowances": [{"amount_paise": 500_000, "reason_code": "room_rent_proportionate"},
                              {"amount_paise": 300_000, "reason_code": "non_payable_item", "note": "Gloves, masks"}],
            "idempotency_key": key}
    assert api.call("POST", f"/admin/insurance/claims/{claim['id']}/settlement", c,
                    {**body, "utr_number": None}).status_code == 422
    st = api.call("POST", f"/admin/insurance/claims/{claim['id']}/settlement", c, body)
    assert st.status_code == 200, st.text
    assert st.json()["status"] == "partially_settled" and st.json()["split"]["payer_outstanding_paise"] == 0
    assert api.call("POST", f"/admin/insurance/claims/{claim['id']}/settlement", c, body).json()["replayed"] is True
    detail = api.call("GET", f"/admin/insurance/claims/{claim['id']}", c).json()
    dis = {t["reason_code"]: t for t in detail["transactions"] if t["kind"] == "disallowance"}
    assert detail["invoice"]["paid_paise"] == 2_500_000 + 8_000_000 + 200_000

    # write-off needs billing approval (a GST credit note); recovery goes to the patient
    wo = api.call("POST", f"/admin/insurance/disallowances/{dis['room_rent_proportionate']['id']}/resolve", c,
                  {"resolution": "written_off", "note": "Agreed with TPA"})
    assert wo.status_code == 403
    wo = api.as_(h["u"]["admin"]).call("POST", f"/admin/insurance/disallowances/{dis['room_rent_proportionate']['id']}/resolve",
                                       c, {"resolution": "written_off", "note": "Agreed with TPA"})
    assert wo.status_code == 200, wo.text
    assert wo.json()["credit_note"]["credit_note_number"].startswith("CRN-")
    rec = api.as_(h["u"]["tpa"]).call("POST", f"/admin/insurance/disallowances/{dis['non_payable_item']['id']}/resolve",
                                      c, {"resolution": "recovered_from_patient", "note": "Explained to family"})
    assert rec.status_code == 200
    final = api.call("GET", f"/admin/insurance/claims/{claim['id']}", c).json()
    assert final["split"]["patient_payable_paise"] == 2_800_000 and final["split"]["payer_hold_paise"] == 0
    inv_row = final["invoice"]
    assert inv_row["total_paise"] - inv_row["credited_paise"] - inv_row["paid_paise"] == 300_000   # the recovery
    # nothing outstanding from the payer any more
    assert all(p["payer_id"] != medi["id"] for p in api.call("GET", "/admin/insurance/ageing", c).json()["payers"])
    # and the counter may now collect the recovered ₹3,000
    assert api.as_(h["u"]["billing"]).call("POST", f"/admin/opd/invoices/{inv}/receipts", c, {
        "mode": "upi", "amount_paise": 300_000, "reference": "UPI778899", "idempotency_key": _key()}).status_code == 201


def test_ageing_buckets_by_payer_and_age(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid, _, policy, medi = _insured_stay(api, h)
    _approved(api, h, aid, policy)
    _billable(api, h, aid)
    claim = api.as_(h["u"]["tpa"]).call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c,
                                        {"non_payable_paise": 200_000}).json()
    assert claim["split"]["payer_payable_paise"] == 9_000_000                  # approval below bill - non-payable
    filed = datetime.fromisoformat(claim["filed_at"].replace("Z", "+00:00"))
    for days, bucket in ((10, "lt_30"), (45, "d30_60"), (75, "d60_90"), (120, "gt_90")):
        rep = asyncio.run(insurance.get_tpa_ageing_report(c, now=filed + timedelta(days=days)))
        row = next(p for p in rep["payers"] if p["payer_id"] == medi["id"])
        assert row[bucket] == 9_000_000 and row["total_paise"] == 9_000_000, (days, row)
    assert insurance.ageing_bucket(29) == "lt_30" and insurance.ageing_bucket(30) == "d30_60"
    assert insurance.ageing_bucket(90) == "gt_90"


def test_boundaries_withdraw_void_tenant_and_entitlement(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    aid, patient, policy, _ = _insured_stay(api, h)
    # no approval yet: nothing to file
    api.as_(h["u"]["tpa"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c, {}).status_code == 409
    pa = _approved(api, h, aid, policy)
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/pre-auth", c, {
        "policy_id": policy, "requested_paise": 1, "provisional_diagnosis": "Again"}).status_code == 409
    _billable(api, h, aid)
    claim = api.as_(h["u"]["tpa"]).call("POST", f"/admin/ipd/admissions/{aid}/cashless-bill", c, {}).json()

    # a live claim pins its bill; withdrawing frees it
    api.as_(h["u"]["admin"])
    voided = api.call("POST", f"/admin/opd/invoices/{claim['invoice_id']}/void", c, {"reason": "Re-bill"})
    assert voided.status_code in (409, 422) and "claim" in voided.text.lower()
    api.as_(h["u"]["tpa"])
    assert api.call("POST", f"/admin/insurance/claims/{claim['id']}/action", c, {"action": "withdraw"}).status_code == 422
    w = api.call("POST", f"/admin/insurance/claims/{claim['id']}/action", c,
                 {"action": "withdraw", "note": "Wrong policy used"})
    assert w.status_code == 200 and w.json()["status"] == "withdrawn"

    # tenant: another clinic's admin gets nothing of this one
    api.as_(h["u"]["other_admin"])
    other = h["other"]
    assert api.call("GET", f"/admin/insurance/claims/{claim['id']}", other).status_code in (403, 404)
    assert api.call("GET", f"/admin/insurance/policies/{patient}", other).status_code in (200, 403)
    if api.call("GET", f"/admin/insurance/policies/{patient}", other).status_code == 200:
        assert api.call("GET", f"/admin/insurance/policies/{patient}", other).json()["policies"] == []
    assert api.call("PATCH", f"/admin/ipd/pre-auth/{pa['id']}/decision", other,
                    {"decision": "rejected", "note": "nope"}).status_code in (403, 404)
