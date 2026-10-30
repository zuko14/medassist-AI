"""Migration 117 (GST billing, credit notes, room-rent GST) against real PostgreSQL:
the money invariants the HTTP tests cannot reach directly. Journeys: tests/test_gst_billing_e2e.py."""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

from app.services.gst_billing import calculate_line_gst, gst_amount, valid_gstin

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "117_gst_billing_and_credit_notes.sql", MIG / "rollback" / "117_down.sql"
GSTIN = "27AAPFU0939F1ZV"
CATALOG = [{"code": "HAIR", "name": "Hair transplant", "item_type": "procedure", "price_paise": 100001,
            "hsn_sac": "999722", "tax_rate_bps": 1800, "active": True},
           {"code": "DRESS", "name": "Dressing", "item_type": "nursing", "price_paise": 20000, "active": True}]


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


def _clinic(conn, gstin=GSTIN):
    settings = {"service_catalog": CATALOG, **({"gstin": gstin} if gstin else {})}
    return str(_q(conn, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, opd_settings)
                           VALUES ('GST Hospital', %s, 'polyclinic', true, %s::jsonb) RETURNING id""",
                  "+9187" + uuid.uuid4().hex[:8], json.dumps(settings)))


def _invoice(conn, clinic, lines, discount=0):
    patient = str(_q(conn, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Ravi', %s) RETURNING id",
                     clinic, "+9186" + uuid.uuid4().hex[:8]))
    inv = str(_q(conn, "INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id",
                 clinic, patient))
    for n, (code, price, qty) in enumerate(lines, start=1):
        _q(conn, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, description,
                    quantity, unit_price_paise) VALUES (%s, %s, %s, 'procedure', %s, 'Line', %s, %s)""",
           clinic, inv, n, code, qty, price)
    if discount:
        _q(conn, "UPDATE opd_invoices SET discount_paise = %s, discount_reason = 'Staff' WHERE id = %s", discount, inv)
    _q(conn, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    return inv


def _inv(conn, inv):
    return _q(conn, "SELECT to_jsonb(i) FROM opd_invoices i WHERE id = %s", inv)


def _shift(conn, clinic):
    cashier = str(uuid.uuid4())
    shift = str(_q(conn, """INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, opening_float_paise)
                            VALUES (%s, %s, 'cash1', 100000) RETURNING id""", clinic, cashier))
    return cashier, shift


def _pay(conn, clinic, inv, shift, cashier, amount):
    _q(conn, """INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, received_by_admin_id,
                received_by_name) VALUES (%s, %s, %s, 'payment', 'cash', %s, %s, 'cash1')""",
       clinic, inv, shift, amount, cashier)


def _cn(conn, clinic, inv, amount, key=None, refund_mode=None, shift=None, actor=None, ref=None):
    return _q(conn, "SELECT opd_issue_credit_note(%s, %s, %s, 'billing_error', NULL, %s, %s, %s, %s, %s, 'cash1')",
              clinic, inv, amount, key, refund_mode, ref, shift, actor)


def test_python_and_sql_round_the_same_way(real_pg_conn):
    for value, rate in ((100001, 1800), (1, 500), (11, 500), (99999999, 4000), (0, 1800), (5555, 1200)):
        assert _q(real_pg_conn, "SELECT opd_gst_amount(%s, %s)", value, rate) == gst_amount(value, rate)
    assert calculate_line_gst(100001, 1800) == {"cgst_paise": 9000, "sgst_paise": 9000, "igst_paise": 0,
                                                "total_tax_paise": 18000}
    assert calculate_line_gst(1100, 500)["cgst_paise"] + calculate_line_gst(1100, 500)["sgst_paise"] == 55
    assert calculate_line_gst(1000, 1800, is_interstate=True)["igst_paise"] == 180
    assert valid_gstin(GSTIN) and not valid_gstin("27AAPFU0939F1ZW") and not valid_gstin("27aapfu0939f1zv")


def test_credit_notes_table_is_a_tenant_table_with_forced_rls(real_pg_conn):
    assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'opd_credit_notes'") is True
    from app.tenancy import TENANT_OWNED_TABLES
    assert "opd_credit_notes" in TENANT_OWNED_TABLES


def test_clinic_without_gstin_never_charges_tax(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c, gstin=None)
    inv = _invoice(c, clinic, [("HAIR", 100001, 1), ("DRESS", 20000, 1)])
    row = _inv(c, inv)
    assert row["subtotal_paise"] == 120001 and row["total_tax_paise"] == 0
    assert row["is_tax_invoice"] is False and row["clinic_gstin"] is None
    assert _q(c, "SELECT sum(total_tax_paise + tax_rate_bps) FROM opd_invoice_items WHERE invoice_id = %s", inv) == 0


def test_taxed_line_split_subtotal_and_issue_stamp(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    inv = _invoice(c, clinic, [("HAIR", 100001, 1), ("DRESS", 20000, 1)])
    hair = _q(c, "SELECT to_jsonb(i) FROM opd_invoice_items i WHERE invoice_id = %s AND line_no = 1", inv)
    assert (hair["tax_rate_bps"], hair["hsn_sac"]) == (1800, "999722")
    assert (hair["cgst_paise"], hair["sgst_paise"], hair["total_tax_paise"]) == (9000, 9000, 18000)
    row = _inv(c, inv)
    assert row["subtotal_paise"] == 100001 + 18000 + 20000 and row["total_paise"] == 138001
    assert row["total_tax_paise"] == 18000 and row["is_tax_invoice"] is True and row["clinic_gstin"] == GSTIN
    # issued: tax and GSTIN are frozen like every other header field
    assert "opd_record_locked" in _err(c, "UPDATE opd_invoices SET total_tax_paise = 0 WHERE id = %s", inv)
    assert "opd_record_locked" in _err(c, "UPDATE opd_invoice_items SET tax_rate_bps = 0 WHERE invoice_id = %s", inv)


def test_header_discount_lowers_tax_pro_rata(real_pg_conn):
    c = real_pg_conn
    inv = _invoice(c, _clinic(c), [("HAIR", 100000, 1), ("DRESS", 20000, 1)], discount=13800)   # 10% of 138000
    row = _inv(c, inv)
    assert row["subtotal_paise"] == 138000 and row["total_paise"] == 124200
    assert row["total_tax_paise"] == 18000 - 1800


def test_credit_note_on_unpaid_bill_lowers_due_and_is_gapless(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    inv = _invoice(c, clinic, [("HAIR", 100000, 1)])                       # 118000, tax 18000
    first = _cn(c, clinic, inv, 59000, key=uuid.uuid4().hex)
    assert first["credit_note_number"].endswith("-00001") and first["credit_note_number"].startswith("CRN-")
    assert (first["amount_paise"], first["tax_paise"], first["refund_paise"]) == (50000, 9000, 0)
    row = _inv(c, inv)
    assert row["credited_paise"] == 59000 and row["status"] == "issued"
    assert "opd_credit_note_exceeds_invoice" in _err(
        c, "SELECT opd_issue_credit_note(%s, %s, 59001, 'other', 'x', NULL, NULL, NULL, NULL, NULL, 'x')", clinic, inv)
    last = _cn(c, clinic, inv, 59000)
    assert last["credit_note_number"].endswith("-00002") and last["tax_paise"] == 9000
    row = _inv(c, inv)
    assert row["credited_paise"] == 118000 and row["status"] == "paid" and row["paid_paise"] == 0   # nothing left due
    # the invoice keeps its printed total; credits only through the function; notes are append-only
    assert row["total_paise"] == 118000
    assert "opd_credited_is_derived" in _err(c, "UPDATE opd_invoices SET credited_paise = 0 WHERE id = %s", inv)
    assert "opd_credit_note_via_function_only" in _err(
        c, """INSERT INTO opd_credit_notes (clinic_id, invoice_id, credit_note_year, credit_note_seq, amount_paise,
              reason, issued_by_name) VALUES (%s, %s, 2026, 99, 1, 'other', 'x')""", clinic, inv)
    assert "opd_append_only" in _err(c, "DELETE FROM opd_credit_notes WHERE invoice_id = %s", inv)
    assert "opd_append_only" in _err(c, "UPDATE opd_credit_notes SET amount_paise = 1 WHERE invoice_id = %s", inv)


def test_tax_reversed_never_exceeds_tax_charged(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    inv = _invoice(c, clinic, [("HAIR", 1001, 1)])          # 1001 + tax 180 = 1181
    taxes = [_cn(c, clinic, inv, 393)["tax_paise"] for _ in range(3)]
    taxes.append(_cn(c, clinic, inv, 2)["tax_paise"])         # the last note takes the exact remainder
    assert sum(taxes) == 180


def test_credit_on_paid_bill_refunds_the_excess_into_the_shift(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c, gstin=None)
    inv = _invoice(c, clinic, [("DRESS", 20000, 1)])
    cashier, shift = _shift(c, clinic)
    _pay(c, clinic, inv, shift, cashier, 20000)
    assert "opd_credit_note_refund_required" in _err(
        c, "SELECT opd_issue_credit_note(%s, %s, 5000, 'other', 'x', NULL, NULL, NULL, NULL, NULL, 'x')", clinic, inv)
    assert "opd_shift_not_yours" in _err(
        c, "SELECT opd_issue_credit_note(%s, %s, 5000, 'other', 'x', NULL, 'cash', NULL, %s, %s, 'x')",
        clinic, inv, shift, str(uuid.uuid4()))
    cn = _cn(c, clinic, inv, 5000, refund_mode="cash", shift=shift, actor=cashier)
    assert cn["refund_paise"] == 5000 and cn["refund_receipt_id"]
    row = _inv(c, inv)
    assert (row["status"], row["paid_paise"], row["credited_paise"]) == ("paid", 15000, 5000)
    rct = _q(c, "SELECT to_jsonb(r) FROM opd_receipts r WHERE id = %s", cn["refund_receipt_id"])
    assert rct["kind"] == "refund" and cn["credit_note_number"] in rct["reason"] and rct["shift_id"] == shift
    # a part-paid bill refunds only what was paid beyond the new amount due
    inv2 = _invoice(c, clinic, [("DRESS", 20000, 1)])
    _pay(c, clinic, inv2, shift, cashier, 12000)
    cn2 = _cn(c, clinic, inv2, 10000, refund_mode="cash", shift=shift, actor=cashier)
    assert cn2["refund_paise"] == 2000
    assert (_inv(c, inv2)["status"], _inv(c, inv2)["paid_paise"]) == ("paid", 10000)
    # a bill with a credit note cannot be voided any more
    inv3 = _invoice(c, clinic, [("DRESS", 20000, 1)])
    _cn(c, clinic, inv3, 100)
    assert "opd_void_has_credit_notes" in _err(
        c, "UPDATE opd_invoices SET status = 'void', voided_at = now(), void_reason = 'Wrong patient' WHERE id = %s", inv3)
    closed = _q(c, "SELECT to_jsonb(s) FROM opd_close_shift(%s, %s, %s, 100000 + 20000 + 12000 - 5000 - 2000, NULL) s",
                clinic, shift, cashier)
    assert closed["variance_paise"] == 0


def test_credit_note_key_replays_and_is_bound_to_one_invoice(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    inv = _invoice(c, clinic, [("DRESS", 20000, 1)])
    key = uuid.uuid4().hex
    first = _cn(c, clinic, inv, 1000, key=key)
    again = _cn(c, clinic, inv, 1000, key=key)
    assert again["replayed"] is True and again["id"] == first["id"]
    assert _inv(c, inv)["credited_paise"] == 1000
    other = _invoice(c, clinic, [("DRESS", 20000, 1)])
    assert "opd_credit_note_key_reused" in _err(
        c, "SELECT opd_issue_credit_note(%s, %s, 1000, 'other', 'x', %s, NULL, NULL, NULL, NULL, 'x')", clinic, other, key)


def test_two_concurrent_credit_notes_for_the_whole_bill_credit_it_once(real_pg_conn, real_postgres_uri):
    clinic = _clinic(real_pg_conn)
    inv = _invoice(real_pg_conn, clinic, [("HAIR", 100000, 1)])
    results, barrier = [], threading.Barrier(2)

    def credit():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            results.append(_cn(conn, clinic, inv, 118000))
        except psycopg2.Error as e:
            results.append(str(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=credit) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if isinstance(r, dict)) == 1
    assert sum(1 for r in results if "opd_credit_note_exceeds_invoice" in str(r)) == 1
    assert _inv(real_pg_conn, inv)["credited_paise"] == 118000


@pytest.fixture
def stay(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                          VALUES (%s, 'Dr. Rao', 'Surgery', 'Surgeon', true) RETURNING id""", clinic))
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Sita', %s) RETURNING id",
                     clinic, "+9185" + uuid.uuid4().hex[:8]))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'PVT', 'Private') RETURNING id", clinic))
    icu = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name, ward_type) VALUES (%s, 'ICU', 'ICU', 'icu') "
                    "RETURNING id", clinic))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'P1', 'deluxe') "
                    "RETURNING id", clinic, ward))
    icu_bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'I1', 'critical') "
                        "RETURNING id", clinic, icu))

    def admit(b):
        p = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Pt', %s) RETURNING id",
                   clinic, "+9184" + uuid.uuid4().hex[:8]))
        a = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic,
               json.dumps({"patient_id": p, "admitting_doctor_id": doctor, "reason": "Observation"}), b)
        return a["id"], a["admitted_at"][:10]

    return {"c": c, "clinic": clinic, "patient": patient, "admit": admit, "bed": bed, "icu_bed": icu_bed}


def _bed_charge(c, clinic, adm, day, price):
    return _q(c, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity,
                    unit_price_paise, status, posted_at) VALUES (%s, %s, 'bed', 'Room rent', %s, 1, %s, 'posted', now())
                    RETURNING to_jsonb(ipd_charges.*)""", clinic, adm, day, price)


def test_room_rent_above_5000_outside_icu_attracts_5_percent(stay):
    c, clinic = stay["c"], stay["clinic"]
    adm, day = stay["admit"](stay["bed"])
    dear = _bed_charge(c, clinic, adm, day, 600000)
    assert (dear["tax_rate_bps"], dear["hsn_sac"], dear["tax_paise"]) == (500, "999311", 30000)
    assert _bed_charge(c, clinic, adm, day, 500000)["tax_rate_bps"] == 0          # exactly Rs 5,000: exempt
    other = _q(c, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date,
                     unit_price_paise) VALUES (%s, %s, 'procedure', 'Surgery', %s, 900000) RETURNING tax_rate_bps""",
               clinic, adm, day)
    assert other == 0                                                              # treatment stays exempt
    icu_adm, icu_day = stay["admit"](stay["icu_bed"])
    assert _bed_charge(c, clinic, icu_adm, icu_day, 900000)["tax_rate_bps"] == 0   # ICU exempt at any rent
    _q(c, "UPDATE clinics SET opd_settings = opd_settings - 'gstin' WHERE id = %s", clinic)
    assert _bed_charge(c, clinic, adm, day, 600000)["tax_rate_bps"] == 0          # unregistered: never taxed


def test_final_bill_carries_room_gst_and_running_bill_estimates_it(stay):
    c, clinic = stay["c"], stay["clinic"]
    adm, day = stay["admit"](stay["bed"])
    _bed_charge(c, clinic, adm, day, 600000)
    _q(c, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, unit_price_paise,
             status, posted_at) VALUES (%s, %s, 'pharmacy', 'Drugs', %s, 100000, 'posted', now())""", clinic, adm, day)
    run = _q(c, "SELECT ipd_running_bill(%s, %s)", clinic, adm)
    assert run["charged_paise"] == 700000 and run["tax_paise"] == 30000 and run["payable_paise"] == 730000
    _q(c, "SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", clinic, adm)
    bill = _q(c, "SELECT ipd_generate_final_bill(%s, %s, NULL, 'billing')", clinic, adm)
    inv = bill["invoice_id"]
    room = _q(c, "SELECT to_jsonb(i) FROM opd_invoice_items i WHERE invoice_id = %s AND tax_rate_bps = 500", inv)
    assert room["hsn_sac"] == "999311" and room["total_tax_paise"] == 30000
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    row = _inv(c, inv)
    assert row["total_paise"] == 730000 and row["total_tax_paise"] == 30000 and row["is_tax_invoice"] is True


def test_rollback_refuses_credit_notes_then_reapplies(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    _cn(c, clinic, _invoice(c, clinic, [("DRESS", 20000, 1)]), 100)
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "117_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        from tests.test_migration_124_insurance import unwind_124
        unwind_124(cur)   # 124 (insurance) uses 117's credit notes and bill functions
        # the shared test DB holds other suites' rows: the owner purge is the only path that removes them
        cur.execute("""SELECT ipd_purge_clinic(x.clinic_id) FROM (
                         SELECT clinic_id FROM opd_credit_notes UNION SELECT clinic_id FROM opd_invoices
                         WHERE credited_paise <> 0 UNION SELECT clinic_id FROM opd_invoice_items WHERE tax_rate_bps <> 0
                         UNION SELECT clinic_id FROM ipd_charges WHERE tax_rate_bps <> 0) x""")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.opd_credit_notes'), to_regprocedure('opd_gst_amount(bigint,integer)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT pg_get_functiondef('opd_guard_invoice()'::regprocedure)")
        assert "credited_paise" not in cur.fetchone()[0]                    # the 103 definition is back
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname IN (
                         'opd_gst_amount', 'opd_clinic_gstin', 'opd_invoice_item_tax', 'opd_recalc_invoice_subtotal',
                         'opd_guard_invoice', 'opd_receipt_after_insert', 'opd_guard_credit_note_insert',
                         'opd_issue_credit_note', 'ipd_apply_room_rent_gst', 'ipd_bill_lines',
                         'ipd_generate_final_bill', 'ipd_running_bill', 'ipd_apply_deposits', 'ipd_discharge_checklist')
                       AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
