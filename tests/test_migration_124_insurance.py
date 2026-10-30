"""Migration 124 (insurance / TPA, cashless claims) against real PostgreSQL.

Seeded payers, the pre-authorisation state machine and its cap, the dual-debtor
split on the issued bill (deposits never pay the payer's share; discharge needs
only the patient's share), settlement with NEFT / TDS / reason-coded
disallowances (append-only, resolved once: credit note or recovery from the
patient), concurrent settlements never over-collect, a live claim pins its bill,
an identity merge moves the pre-authorisation, purge, and a guarded rollback +
idempotent re-apply. HTTP behaviour: tests/test_insurance_api_e2e.py.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "124_insurance_tpa_and_cashless.sql", MIG / "rollback" / "124_down.sql"
LAKH = 10_000_000          # ₹1,00,000 in paise


def unwind_124(cur) -> None:
    """Inside an open transaction: clear insurance data the only way the guards
    allow (owner purge) and run 124_down. Rollback tests of earlier migrations
    call this first (via unwind_123, or directly), since 124 sits on their tables."""
    cur.execute("SELECT to_regclass('public.ipd_insurance_claims') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("""SELECT ipd_purge_clinic(x.clinic_id) FROM (
                     SELECT clinic_id FROM ipd_pre_authorizations UNION SELECT clinic_id FROM ipd_insurance_claims
                     UNION SELECT clinic_id FROM opd_receipts WHERE mode IN ('insurance', 'tds')
                     UNION SELECT clinic_id FROM opd_credit_notes WHERE reason = 'insurance_disallowance') x""")
    cur.execute("DELETE FROM opd_credit_notes WHERE reason = 'insurance_disallowance'")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


def _err(conn, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(conn, sql, *args)
    return str(e.value)


@pytest.fixture
def stay(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, ipd_settings)
                          VALUES ('Cashless Hospital', %s, 'polyclinic', true, '{"bed_tariffs": {"general": 100000}}')
                          RETURNING id""", "+9184" + uuid.uuid4().hex[:8]))
    _q(c, "SELECT insurance_seed_payers(%s)", clinic)
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                          VALUES (%s, 'Dr. Iyer', 'Surgery', 'Surgeon', true) RETURNING id""", clinic))
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Anil', %s) RETURNING id",
                     clinic, "+9183" + uuid.uuid4().hex[:8]))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'W1', 'Ward') RETURNING id", clinic))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B1', 'general') "
                    "RETURNING id", clinic, ward))
    adm = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic,
             json.dumps({"patient_id": patient, "admitting_doctor_id": doctor, "reason": "Lap cholecystectomy"}), bed)
    payer = str(_q(c, "SELECT id FROM insurance_payers WHERE clinic_id = %s AND name = 'Medi Assist TPA'", clinic))
    policy = str(_q(c, """INSERT INTO patient_insurance_policies (clinic_id, patient_id, payer_id, policy_number, member_id,
                          valid_from, valid_to) VALUES (%s, %s, %s, 'POL-1', 'MA-778', CURRENT_DATE - 100, CURRENT_DATE + 200)
                          RETURNING id""", clinic, patient, payer))
    cashier = str(uuid.uuid4())
    shift = str(_q(c, """INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, opening_float_paise)
                         VALUES (%s, %s, 'cash1', 0) RETURNING id""", clinic, cashier))
    return {"c": c, "clinic": clinic, "doctor": doctor, "patient": patient, "adm": adm["id"], "payer": payer,
            "policy": policy, "admitted_on": adm["admitted_at"][:10], "cashier": cashier, "shift": shift}


def _pre_auth(s, requested=LAKH, conn=None, **row):
    body = {"policy_id": s["policy"], "requested_paise": requested, "provisional_diagnosis": "Cholelithiasis", **row}
    return _q(conn or s["c"], "SELECT ins_create_pre_auth(%s, %s, %s::jsonb, NULL, 'tpa')",
              s["clinic"], s["adm"], json.dumps(body))


def _step(s, pa, action, **row):
    return _q(s["c"], "SELECT ins_pre_auth_transition(%s, %s, %s, %s::jsonb, NULL, 'tpa')",
              s["clinic"], pa["id"], action, json.dumps(row))


def _issued_bill(s, charges=((10_000_000, "bed"), (1_500_000, "pharmacy"))):
    """₹1,15,000 of posted charges -> discharge initiated -> final bill issued."""
    c = s["c"]
    for price, cat in charges:
        _q(c, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity,
                 unit_price_paise, status, posted_at) VALUES (%s, %s, %s, %s, %s, 1, %s, 'posted', now())""",
           s["clinic"], s["adm"], cat, cat + " charge", s["admitted_on"], price)
    _q(c, "SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", s["clinic"], s["adm"])
    inv = _q(c, "SELECT ipd_generate_final_bill(%s, %s, NULL, 'billing')", s["clinic"], s["adm"])["invoice_id"]
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    return inv


def _approved_claim(s, approved=9_000_000, np=0, cp=0):
    pa = _pre_auth(s, requested=max(LAKH, approved))
    _step(s, pa, "approve", amount_paise=approved, approval_ref="MA/APP/1")
    inv = _issued_bill(s)
    claim = _q(s["c"], "SELECT ins_file_claim(%s, %s, %s, %s, NULL, 'tpa')", s["clinic"], s["adm"], np, cp)
    return pa, inv, claim


def _settle(s, claim, conn=None, key=None, **row):
    return _q(conn or s["c"], "SELECT ins_record_settlement(%s, %s, %s::jsonb, %s, NULL, 'tpa')",
              s["clinic"], claim["id"], json.dumps(row), key)


def _inv(s, inv):
    return _q(s["c"], "SELECT to_jsonb(i) FROM opd_invoices i WHERE id = %s", inv)


# ── schema & seed ────────────────────────────────────────────────────────────

def test_schema_rls_and_seeded_payers(stay):
    c = stay["c"]
    for t in ("insurance_payers", "patient_insurance_policies", "ipd_pre_authorizations", "ipd_insurance_claims",
              "ipd_claim_transactions"):
        assert _q(c, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True
    from app.tenancy import TENANT_OWNED_TABLES
    assert {"insurance_payers", "patient_insurance_policies", "ipd_pre_authorizations", "ipd_insurance_claims",
            "ipd_claim_transactions"} <= TENANT_OWNED_TABLES
    names = {r for (r,) in _rows(c, "SELECT name FROM insurance_payers WHERE clinic_id = %s", stay["clinic"])}
    assert len(names) == 12 and {"Medi Assist TPA", "Ayushman Bharat (PM-JAY)", "CGHS", "Niva Bupa"} <= names
    assert _q(c, "SELECT insurance_seed_payers(%s)", stay["clinic"]) == 0            # idempotent
    assert _q(c, "SELECT has_function_privilege('anon', 'ins_file_claim(uuid,uuid,integer,integer,uuid,text)', "
                 "'EXECUTE')") is False


def _rows(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchall()
    cur.close()
    return out


# ── pre-authorisation ────────────────────────────────────────────────────────

def test_pre_auth_state_machine_cap_and_one_live_per_stay(stay):
    c, s = stay["c"], stay
    pa = _pre_auth(s, requested=LAKH)
    assert pa["status"] == "submitted" and pa["pre_auth_number"].startswith("PRE-")
    assert "ins_pre_auth_exists" in _err(c, "SELECT ins_create_pre_auth(%s, %s, %s::jsonb, NULL, 'x')", s["clinic"],
                                         s["adm"], json.dumps({"policy_id": s["policy"], "requested_paise": 1,
                                                               "provisional_diagnosis": "Again"}))
    assert "ins_bad_transition" in _err(c, "SELECT ins_pre_auth_transition(%s, %s, 'request_enhancement', "
                                           "'{\"amount_paise\": 5}'::jsonb, NULL, 'x')", s["clinic"], pa["id"])
    assert _step(s, pa, "query", note="Send USG report")["status"] == "query_raised"
    assert _step(s, pa, "submit", note="USG attached")["status"] == "submitted"
    assert "ins_approval_exceeds_request" in _err(
        c, "SELECT ins_pre_auth_transition(%s, %s, 'approve', %s::jsonb, NULL, 'x')", s["clinic"], pa["id"],
        json.dumps({"amount_paise": LAKH + 1, "approval_ref": "R1"}))
    ok = _step(s, pa, "approve", amount_paise=8_000_000, approval_ref="MA/APP/1")
    assert (ok["status"], ok["total_approved_paise"]) == ("approved", 8_000_000)
    _step(s, pa, "request_enhancement", amount_paise=3_000_000, note="Stay extended")
    assert "ins_enhancement_below_approved" in _err(
        c, "SELECT ins_pre_auth_transition(%s, %s, 'approve', %s::jsonb, NULL, 'x')", s["clinic"], pa["id"],
        json.dumps({"amount_paise": 7_000_000, "approval_ref": "R2"}))
    enh = _step(s, pa, "approve", amount_paise=11_000_000, approval_ref="MA/ENH/2")
    assert (enh["total_approved_paise"], enh["initial_approved_paise"], enh["enhanced_approved_paise"]) == \
           (11_000_000, 8_000_000, 3_000_000)
    # function-only writes; the history is in ipd_events
    assert "ins_via_function_only" in _err(c, "UPDATE ipd_pre_authorizations SET initial_approved_paise = 1 "
                                              "WHERE id = %s", pa["id"])
    assert "ins_append_only" in _err(c, "DELETE FROM ipd_pre_authorizations WHERE id = %s", pa["id"])
    steps = [r for (r,) in _rows(c, "SELECT event_type FROM ipd_events WHERE admission_id = %s AND event_type LIKE "
                                    "'preauth\\_%%' ORDER BY occurred_at, id", s["adm"])]
    assert steps == ["preauth_submitted", "preauth_query", "preauth_submit", "preauth_approve",
                     "preauth_request_enhancement", "preauth_approve"]


def test_policy_must_be_the_patients_and_valid(stay):
    c, s = stay["c"], stay
    other = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Other', %s) RETURNING id",
                   s["clinic"], "+9182" + uuid.uuid4().hex[:8]))
    foreign = str(_q(c, """INSERT INTO patient_insurance_policies (clinic_id, patient_id, payer_id, policy_number, member_id)
                           VALUES (%s, %s, %s, 'X', 'Y') RETURNING id""", s["clinic"], other, s["payer"]))
    assert "ins_policy_other_patient" in _err(c, "SELECT ins_create_pre_auth(%s, %s, %s::jsonb, NULL, 'x')", s["clinic"],
                                              s["adm"], json.dumps({"policy_id": foreign, "requested_paise": 100,
                                                                    "provisional_diagnosis": "Abc"}))
    _q(c, "UPDATE patient_insurance_policies SET valid_to = CURRENT_DATE - 200, valid_from = CURRENT_DATE - 400 "
          "WHERE id = %s", s["policy"])
    assert "ins_policy_not_valid" in _err(c, "SELECT ins_create_pre_auth(%s, %s, %s::jsonb, NULL, 'x')", s["clinic"],
                                          s["adm"], json.dumps({"policy_id": s["policy"], "requested_paise": 100,
                                                                "provisional_diagnosis": "Abc"}))


# ── the dual split ───────────────────────────────────────────────────────────

def test_claim_splits_the_issued_bill_between_payer_and_patient(stay):
    s = stay
    pa = _pre_auth(s)
    assert "ins_pre_auth_not_approved" in _err(s["c"], "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'x')", s["clinic"], s["adm"])
    _step(s, pa, "approve", amount_paise=9_000_000, approval_ref="MA/APP/1")
    assert "ins_bill_not_issued" in _err(s["c"], "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'x')", s["clinic"], s["adm"])
    inv = _issued_bill(s)
    claim = _q(s["c"], "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'tpa')", s["clinic"], s["adm"])
    # ₹1,15,000 bill, ₹90,000 approved: payer ₹90,000, patient ₹25,000
    assert (claim["total_bill_paise"], claim["claimed_amount_paise"], claim["patient_payable_paise"]) == \
           (11_500_000, 9_000_000, 2_500_000)
    assert claim["claim_number"].startswith("CLM-") and claim["outstanding_paise"] == 9_000_000
    assert _q(s["c"], "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'x')", s["clinic"], s["adm"])["replayed"] is True
    # the pre-auth is frozen once claimed; the bill cannot be voided under a live claim
    assert "ins_pre_auth_claimed" in _err(s["c"], "SELECT ins_pre_auth_transition(%s, %s, 'cancel', "
                                                  "'{\"note\": \"no\"}'::jsonb, NULL, 'x')", s["clinic"], pa["id"])
    assert "ins_claim_open_on_bill" in _err(s["c"], "UPDATE opd_invoices SET status = 'void', void_reason = 'redo', voided_at = now() "
                                                    "WHERE id = %s", inv)
    # the split columns never change
    assert "ins_via_function_only" in _err(s["c"], "UPDATE ipd_insurance_claims SET claimed_amount_paise = 1 "
                                                   "WHERE id = %s", claim["id"])


def test_non_payable_and_copay_go_to_the_patient_and_approval_above_bill_is_capped(stay):
    s = stay
    _, _, claim = _approved_claim(s, approved=20_000_000, np=500_000, cp=1_000_000)
    assert (claim["claimed_amount_paise"], claim["patient_payable_paise"]) == (10_000_000, 1_500_000)


def test_deposits_pay_only_the_patients_share_and_discharge_needs_only_that(stay):
    s, c = stay, stay["c"]
    _q(c, """INSERT INTO ipd_deposits (clinic_id, admission_id, kind, mode, amount_paise, shift_id, received_by_admin_id,
             received_by_name) VALUES (%s, %s, 'deposit', 'cash', 5000000, %s, %s, 'cash1')""",
       s["clinic"], s["adm"], s["shift"], s["cashier"])
    pa = _pre_auth(s)
    _step(s, pa, "approve", amount_paise=9_000_000, approval_ref="MA/APP/1")
    inv = _issued_bill(s)
    # issuing the bill applies deposits (G5): before any claim, the approval already holds the insurer's share
    early = _q(c, "SELECT ipd_apply_deposits(%s, %s, NULL, 'cash1')", s["clinic"], s["adm"])
    assert early["applied_paise"] == 2_500_000
    _q(c, "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'tpa')", s["clinic"], s["adm"])    # not "patient overpaid"
    run = _q(c, "SELECT ipd_running_bill(%s, %s)", s["clinic"], s["adm"])
    assert run["payable_paise"] == 0 and run["insurance"]["payer_hold_paise"] == 9_000_000
    assert run["refundable_paise"] == 2_500_000                                 # the unused advance goes back
    again = _q(c, "SELECT ipd_apply_deposits(%s, %s, NULL, 'cash1')", s["clinic"], s["adm"])
    assert again["applied_paise"] == 0                                          # never the insurer's ₹90,000
    # nor the counter: the patient's share is paid, so any further patient payment is refused
    assert "ins_receipt_exceeds_patient_share" in _err(
        c, "INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id, "
           "received_by_name) VALUES (%s, %s, %s, 'payment', 'cash', 100, %s, 'cash1')",
        s["clinic"], inv, s["shift"], s["cashier"])
    item = {i["key"]: i for i in _q(c, "SELECT ipd_discharge_checklist(%s, %s)", s["clinic"], s["adm"])["items"]}
    assert item["bill_settled"]["done"] is True and item["bill_settled"]["insurance_hold_paise"] == 9_000_000
    assert _inv(s, inv)["status"] == "partially_paid"


# ── settlement ───────────────────────────────────────────────────────────────

def test_settlement_neft_tds_disallowance_and_resolution(stay):
    s, c = stay, stay["c"]
    _, inv, claim = _approved_claim(s)
    assert "ins_receipt_via_function_only" in _err(
        c, "INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, reference, received_by_name) "
           "VALUES (%s, %s, 'payment', 'insurance', 1, 'UTR X', 'x')", s["clinic"], inv)
    assert "ins_exceeds_outstanding" in _err(c, "SELECT ins_record_settlement(%s, %s, %s::jsonb, NULL, NULL, 'x')",
                                             s["clinic"], claim["id"],
                                             json.dumps({"amount_received_paise": 9_000_001, "utr_number": "UTR1"}))
    assert "ins_disallowance_reason_required" in _err(
        c, "SELECT ins_record_settlement(%s, %s, %s::jsonb, NULL, NULL, 'x')", s["clinic"], claim["id"],
        json.dumps({"disallowances": [{"amount_paise": 5, "reason_code": ""}]}))
    key = uuid.uuid4().hex
    out = _settle(s, claim, key=key, amount_received_paise=8_000_000, utr_number="SBIN0001234", tds_paise=200_000,
                  disallowances=[{"amount_paise": 500_000, "reason_code": "room_rent_proportionate"},
                                 {"amount_paise": 300_000, "reason_code": "non_payable_item", "note": "Gloves"}])
    # payer_payable = received + TDS + disallowed (nothing outstanding) -> partially settled
    assert (out["status"], out["outstanding_paise"], out["settled_amount_paise"], out["tds_deducted_paise"],
            out["disallowed_amount_paise"]) == ("partially_settled", 0, 8_000_000, 200_000, 800_000)
    assert _settle(s, claim, key=key, amount_received_paise=1, utr_number="SBIN0001234")["replayed"] is True
    modes = sorted(_rows(c, "SELECT mode, amount_paise FROM opd_receipts WHERE invoice_id = %s", inv))
    assert modes == [("insurance", 8_000_000), ("tds", 200_000)]
    assert _inv(s, inv)["paid_paise"] == 8_200_000
    # the ledger is append-only; a disallowance is resolved once
    txns = {r[1]: r[0] for r in _rows(c, "SELECT id::text, reason_code FROM ipd_claim_transactions WHERE claim_id = %s "
                                         "AND kind = 'disallowance'", claim["id"])}
    assert "ins_append_only" in _err(c, "DELETE FROM ipd_claim_transactions WHERE claim_id = %s", claim["id"])
    assert "ins_via_function_only" in _err(c, "UPDATE ipd_claim_transactions SET amount_paise = 1 WHERE claim_id = %s",
                                           claim["id"])
    hold_before = _q(c, "SELECT ins_payer_hold(%s, %s)", s["clinic"], s["adm"])
    assert hold_before == 800_000                                       # unresolved disallowances stay off the patient
    wo = _q(c, "SELECT ins_resolve_disallowance(%s, %s, 'written_off', 'Agreed with TPA', NULL, 'admin')",
            s["clinic"], txns["room_rent_proportionate"])
    assert wo["resolution"] == "written_off" and wo["credit_note"]["credit_note_number"].startswith("CRN-")
    assert _inv(s, inv)["credited_paise"] == 500_000
    rec = _q(c, "SELECT ins_resolve_disallowance(%s, %s, 'recovered_from_patient', 'Patient informed', NULL, 'tpa')",
             s["clinic"], txns["non_payable_item"])
    assert rec["resolution"] == "recovered_from_patient"
    assert "ins_record_locked" in _err(c, "SELECT ins_resolve_disallowance(%s, %s, 'written_off', 'again', NULL, 'x')",
                                       s["clinic"], txns["non_payable_item"])
    assert _q(c, "SELECT ins_payer_hold(%s, %s)", s["clinic"], s["adm"]) == 0
    # the patient now owes their ₹25,000 share + the ₹3,000 recovered; the bill agrees
    i = _inv(s, inv)
    assert i["total_paise"] - i["credited_paise"] - i["paid_paise"] == 2_500_000 + 300_000


def test_repudiation_and_withdrawal(stay):
    s, c = stay, stay["c"]
    _, inv, claim = _approved_claim(s)
    assert "ins_note_required" in _err(c, "SELECT ins_claim_transition(%s, %s, 'withdraw', NULL, NULL, 'x')",
                                       s["clinic"], claim["id"])
    w = _q(c, "SELECT ins_claim_transition(%s, %s, 'withdraw', 'Filed on wrong policy', NULL, 'x')", s["clinic"], claim["id"])
    assert w["status"] == "withdrawn"
    _q(c, "UPDATE opd_invoices SET status = 'void', void_reason = 'Re-bill', voided_at = now() WHERE id = %s", inv)   # free again
    s2 = {**s}
    claim2 = None
    # a fresh bill can be claimed again on the same approval
    inv2 = _q(c, "SELECT ipd_generate_final_bill(%s, %s, NULL, 'billing')", s["clinic"], s["adm"])["invoice_id"]
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv2)
    claim2 = _q(c, "SELECT ins_file_claim(%s, %s, 0, 0, NULL, 'tpa')", s2["clinic"], s2["adm"])
    rep = _settle(s, claim2, disallowances=[{"amount_paise": 9_000_000, "reason_code": "repudiated",
                                             "note": "Pre-existing disease"}])
    assert rep["status"] == "repudiated"
    assert "ins_claim_closed" in _err(c, "SELECT ins_claim_transition(%s, %s, 'withdraw', 'late', NULL, 'x')",
                                      s["clinic"], claim2["id"])


def test_concurrent_settlements_never_overcollect(stay, real_postgres_uri):
    s = stay
    _, inv, claim = _approved_claim(s)
    barrier, ok, errs = threading.Barrier(10), [], []

    def run(i):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            ok.append(_settle(s, claim, conn=conn, amount_received_paise=1_000_000, utr_number=f"UTR{i:04d}"))
        except psycopg2.Error as e:
            errs.append(str(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # the tenth finds the claim fully paid: refused, nothing over-collected
    assert len(ok) == 9 and len(errs) == 1 and ("ins_claim_closed" in errs[0] or "ins_exceeds_outstanding" in errs[0])
    final = _q(s["c"], "SELECT to_jsonb(c) FROM ipd_insurance_claims c WHERE id = %s", claim["id"])
    assert (final["settled_amount_paise"], final["outstanding_paise"], final["status"]) == (9_000_000, 0, "settled")


# ── merge, purge, rollback ───────────────────────────────────────────────────

def test_identity_merge_moves_the_pre_auth_with_its_stay(stay):
    s, c = stay, stay["c"]
    temp = str(_q(c, """INSERT INTO patients (clinic_id, name, phone, is_temporary) VALUES (%s, 'Unknown', %s, true)
                        RETURNING id""", s["clinic"], "+9181" + uuid.uuid4().hex[:8]))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'W2', 'Ward 2') RETURNING id", s["clinic"]))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B9', 'general') "
                    "RETURNING id", s["clinic"], ward))
    real = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Real', %s) RETURNING id",
                  s["clinic"], "+9180" + uuid.uuid4().hex[:8]))
    adm = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", s["clinic"],
             json.dumps({"patient_id": temp, "admitting_doctor_id": s["doctor"], "reason": "RTA polytrauma"}), bed)
    pol = str(_q(c, """INSERT INTO patient_insurance_policies (clinic_id, patient_id, payer_id, policy_number, member_id)
                       VALUES (%s, %s, %s, 'P-T', 'M-T') RETURNING id""", s["clinic"], temp, s["payer"]))
    pa = _pre_auth({**s, "adm": adm["id"], "policy": pol})
    _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', 'identified')", s["clinic"], temp, real)
    assert _q(c, "SELECT patient_id::text FROM ipd_pre_authorizations WHERE id = %s", pa["id"]) == real


def test_owner_purge_removes_claims_and_ledger(stay):
    s, c = stay, stay["c"]
    _, _, claim = _approved_claim(s)
    _settle(s, claim, amount_received_paise=1_000_000, utr_number="UTRPURGE")
    _q(c, "SELECT ipd_purge_clinic(%s)", s["clinic"])
    for t in ("ipd_pre_authorizations", "ipd_insurance_claims", "ipd_claim_transactions"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", s["clinic"]) == 0


def test_rollback_refuses_claims_then_reapplies(stay):
    _pre_auth(stay)
    cur = stay["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "124_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_124(cur)
        cur.execute("SELECT to_regclass('public.ipd_insurance_claims'), to_regclass('public.insurance_payers')")
        assert cur.fetchone() == (None, None)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                         AND (proname LIKE 'ins\\_%%' OR proname = 'insurance_seed_payers')
                         AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
