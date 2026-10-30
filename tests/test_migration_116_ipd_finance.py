"""Migration 116 (IPD finance) against real PostgreSQL: the ledger and bill invariants
the HTTP tests cannot reach directly. Journeys: tests/test_ipd_finance_api_e2e.py."""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "116_ipd_finance.sql", MIG / "rollback" / "116_down.sql"


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
                          VALUES ('Finance Hospital', %s, 'polyclinic', true, '{"bed_tariffs": {"general": 100000}}')
                          RETURNING id""", "+9189" + uuid.uuid4().hex[:8]))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                          VALUES (%s, 'Dr. Iyer', 'Surgery', 'Surgeon', true) RETURNING id""", clinic))
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Anil', %s) RETURNING id",
                     clinic, "+9188" + uuid.uuid4().hex[:8]))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'W1', 'Ward') RETURNING id", clinic))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B1', 'general') "
                    "RETURNING id", clinic, ward))
    adm = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic,
             json.dumps({"patient_id": patient, "admitting_doctor_id": doctor, "reason": "Hernia repair"}), bed)
    cashier = str(uuid.uuid4())
    shift = str(_q(c, """INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, opening_float_paise)
                         VALUES (%s, %s, 'cash1', 10000) RETURNING id""", clinic, cashier))
    return {"c": c, "clinic": clinic, "doctor": doctor, "patient": patient, "adm": adm["id"], "cashier": cashier,
            "shift": shift, "admitted_on": adm["admitted_at"][:10]}


def _deposit(conn, s, amount, kind="deposit", **extra):
    cols = {"clinic_id": s["clinic"], "admission_id": s["adm"], "kind": kind, "mode": "cash", "amount_paise": amount,
            "shift_id": s["shift"], "received_by_admin_id": s["cashier"], "received_by_name": "cash1", **extra}
    if kind == "refund":
        cols.setdefault("reason", "Unused advance")
    keys = ", ".join(cols)
    return _q(conn, f"INSERT INTO ipd_deposits ({keys}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING doc_number",
              *cols.values())


def test_tables_are_tenant_tables_with_forced_rls(real_pg_conn):
    for t in ("ipd_deposits", "ipd_packages", "ipd_admission_packages", "ipd_discounts", "ipd_credit_authorizations"):
        assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True
    from app.tenancy import TENANT_OWNED_TABLES
    assert {"ipd_deposits", "ipd_packages", "ipd_admission_packages", "ipd_discounts",
            "ipd_credit_authorizations"} <= TENANT_OWNED_TABLES


def test_deposit_ledger_numbers_balances_and_is_append_only(stay):
    c = stay["c"]
    assert _deposit(c, stay, 50_000).startswith("DEP-")
    assert "ipd_deposit_insufficient" in _err(c, "INSERT INTO ipd_deposits (clinic_id, admission_id, kind, mode, "
                                                 "amount_paise, shift_id, received_by_admin_id, received_by_name, reason) "
                                                 "VALUES (%s, %s, 'refund', 'cash', 50001, %s, %s, 'x', 'Too much')",
                                              stay["clinic"], stay["adm"], stay["shift"], stay["cashier"])
    assert _deposit(c, stay, 20_000, kind="refund").startswith("DRF-")
    assert _q(c, "SELECT ipd_deposit_balance(%s, %s)", stay["clinic"], stay["adm"]) == 30_000
    assert "ipd_append_only" in _err(c, "UPDATE ipd_deposits SET amount_paise = 1 WHERE admission_id = %s", stay["adm"])
    assert "ipd_append_only" in _err(c, "DELETE FROM ipd_deposits WHERE admission_id = %s", stay["adm"])
    # another cashier's shift, and the deposit-only paths, are refused
    assert "ipd_shift_not_yours" in _err(c, "INSERT INTO ipd_deposits (clinic_id, admission_id, kind, mode, amount_paise, "
                                            "shift_id, received_by_admin_id, received_by_name) VALUES (%s, %s, 'deposit', "
                                            "'cash', 100, %s, %s, 'x')", stay["clinic"], stay["adm"], stay["shift"],
                                         str(uuid.uuid4()))
    inv = str(_q(c, "INSERT INTO opd_invoices (clinic_id, patient_id, status, subtotal_paise) VALUES (%s, %s, 'draft', 0) "
                    "RETURNING id", stay["clinic"], stay["patient"]))
    assert "ipd_deposit_via_function_only" in _err(
        c, "INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, received_by_name) "
           "VALUES (%s, %s, 'payment', 'deposit', 1, 'x')", stay["clinic"], inv)


def test_two_concurrent_refunds_of_the_whole_balance_pay_out_once(stay, real_postgres_uri):
    _deposit(stay["c"], stay, 40_000)
    results, barrier = [], threading.Barrier(2)

    def refund():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            results.append(_deposit(conn, stay, 40_000, kind="refund"))
        except psycopg2.Error as e:
            results.append(str(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=refund) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(1 for r in results if str(r).startswith("DRF-")) == 1
    assert sum(1 for r in results if "ipd_deposit_insufficient" in str(r)) == 1
    assert _q(stay["c"], "SELECT ipd_deposit_balance(%s, %s)", stay["clinic"], stay["adm"]) == 0


def test_shift_close_counts_deposit_cash(stay):
    _deposit(stay["c"], stay, 70_000)
    _deposit(stay["c"], stay, 20_000, kind="refund")
    closed = _q(stay["c"], "SELECT to_jsonb(s) FROM opd_close_shift(%s, %s, %s, 60000, NULL) s",
                stay["clinic"], stay["shift"], stay["cashier"])
    assert closed["expected_cash_paise"] == 10_000 + 70_000 - 20_000 and closed["variance_paise"] == 0


def test_package_covers_included_categories_within_its_days_only(stay):
    c, clinic, adm = stay["c"], stay["clinic"], stay["adm"]
    pkg = str(_q(c, """INSERT INTO ipd_packages (clinic_id, code, name, price_paise, included_categories, max_days)
                       VALUES (%s, 'HERNIA', 'Hernia package', 3000000, ARRAY['bed', 'procedure'], 2) RETURNING id""", clinic))
    _q(c, "SELECT ipd_apply_package(%s, %s, %s, NULL, 'billing')", clinic, adm, pkg)
    for day, cat, price in ((0, "bed", 100000), (1, "bed", 100000), (2, "bed", 100000),
                            (0, "procedure", 900000), (0, "pharmacy", 50000)):
        _q(c, """INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity,
                 unit_price_paise, status, posted_at) VALUES (%s, %s, %s, %s, %s::date + %s, 1, %s, 'posted', now())""",
           clinic, adm, cat, cat + " charge", stay["admitted_on"], day, price)
    lines = _q(c, "SELECT jsonb_agg(to_jsonb(l)) FROM ipd_bill_lines(%s, %s) l", clinic, adm)
    covered = [ln for ln in lines if ln["covered"]]
    billed = {(ln["category"], ln["unit_price_paise"]): ln["qty"] for ln in lines if not ln["covered"]}
    assert covered[0]["covered_count"] == 3 and covered[0]["covered_value_paise"] == 100000 * 2 + 900000
    assert billed == {("package", 3000000): 1, ("bed", 100000): 1, ("pharmacy", 50000): 1}   # day 3 bed is overrun
    # a package is applied or removed only through its function, and only once per stay
    assert "ipd_finance_via_function_only" in _err(c, "UPDATE ipd_admission_packages SET status = 'removed' "
                                                      "WHERE admission_id = %s", adm)
    assert "ipd_package_exists" in _err(c, "SELECT ipd_apply_package(%s, %s, %s, NULL, 'x')", clinic, adm,
                                        str(_q(c, """INSERT INTO ipd_packages (clinic_id, code, name, price_paise,
                                                     included_categories) VALUES (%s, 'OTHER', 'Other', 1, ARRAY['bed'])
                                                     RETURNING id""", clinic)))


def test_discount_four_eyes_and_single_live_request(stay):
    c, clinic, adm = stay["c"], stay["clinic"], stay["adm"]
    me = str(uuid.uuid4())
    d = _q(c, "SELECT ipd_request_discount(%s, %s, 5000, 'Senior citizen', false, %s, %s, 'billing')",
           clinic, adm, uuid.uuid4().hex, me)
    assert d["status"] == "requested"
    assert "ipd_discount_exists" in _err(c, "SELECT ipd_request_discount(%s, %s, 100, 'Another one', false, %s, %s, 'b')",
                                         clinic, adm, uuid.uuid4().hex, me)
    assert "ipd_discount_own_request" in _err(c, "SELECT ipd_decide_discount(%s, %s, 'approved', NULL, %s, 'billing')",
                                              clinic, d["id"], me)
    assert "ipd_finance_via_function_only" in _err(c, "UPDATE ipd_discounts SET status = 'approved' WHERE id = %s", d["id"])
    ok = _q(c, "SELECT ipd_decide_discount(%s, %s, 'approved', 'ok', %s, 'admin')", clinic, d["id"], str(uuid.uuid4()))
    assert ok["status"] == "approved"


def test_rollback_refuses_finance_rows_then_reapplies(stay):
    _deposit(stay["c"], stay, 1_000)
    cur = stay["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "116_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        from tests.test_migration_124_insurance import unwind_124
        unwind_124(cur)   # 124 (insurance) sits on 116's bill functions and receipt modes
        # newest first: 117 (GST) sits on 116's bill functions
        cur.execute("""SELECT ipd_purge_clinic(x.clinic_id) FROM (
                         SELECT clinic_id FROM opd_credit_notes UNION SELECT clinic_id FROM opd_invoices
                         WHERE credited_paise <> 0 UNION SELECT clinic_id FROM opd_invoice_items WHERE tax_rate_bps <> 0
                         UNION SELECT clinic_id FROM ipd_charges WHERE tax_rate_bps <> 0) x""")
        cur.execute((MIG / "rollback" / "117_down.sql").read_text(encoding="utf-8"))
        # the shared test DB holds other suites' rows: the owner purge is the only path that removes them
        cur.execute("""SELECT ipd_purge_clinic(x.clinic_id) FROM (
                         SELECT clinic_id FROM ipd_deposits UNION SELECT clinic_id FROM ipd_discounts
                         UNION SELECT clinic_id FROM ipd_credit_authorizations UNION SELECT clinic_id FROM ipd_admission_packages
                         UNION SELECT clinic_id FROM ipd_charges WHERE source = 'package'
                         UNION SELECT clinic_id FROM opd_receipts WHERE mode IN ('deposit', 'bank_transfer')) x""")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.ipd_deposits'), to_regprocedure('ipd_running_bill(uuid,uuid)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT pg_get_functiondef('ipd_generate_final_bill(uuid,uuid,uuid,text)'::regprocedure)")
        assert "ipd_bill_lines" not in cur.fetchone()[0]                 # the 111 definition is back
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE (proname LIKE 'ipd\\_%%' OR proname = 'opd_close_shift')
                       AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
