"""Migration 113 against real PostgreSQL: one receipt per idempotency key per clinic, so a
payment retried on another worker can never be recorded twice. The fixture connection is
autocommit: a refused insert does not affect the next one."""

import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"


def _q(cur, sql, *args):
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _issued_invoice(cur):
    clinic = _q(cur, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state)
                        VALUES ('Rcpt', %s, 'polyclinic', true, '{"opd_enabled": true}'::jsonb, 'READY')
                        RETURNING id""", "+9193" + uuid.uuid4().hex[:8])
    patient = _q(cur, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'P', %s) RETURNING id",
                 clinic, "+9192" + uuid.uuid4().hex[:8])
    inv = _q(cur, """INSERT INTO opd_invoices (clinic_id, patient_id, status, subtotal_paise, patient_snapshot)
                     VALUES (%s, %s, 'draft', 0, '{}'::jsonb) RETURNING id""", clinic, patient)
    _q(cur, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity,
               unit_price_paise) VALUES (%s, %s, 1, 'consultation', 'Consultation', 1, 50000)""", clinic, inv)
    _q(cur, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    return clinic, inv


def _online(cur, clinic, inv, key, amount=10000):
    _q(cur, """INSERT INTO opd_receipts (clinic_id, invoice_id, mode, amount_paise, gateway, gateway_payment_id,
               idempotency_key) VALUES (%s, %s, 'payment_link', %s, 'razorpay', %s, %s)""",
       clinic, inv, amount, "pay_" + uuid.uuid4().hex[:10], key)


def test_a_key_records_money_once_per_clinic(real_pg_conn):
    cur = real_pg_conn.cursor()
    clinic, inv = _issued_invoice(cur)
    _online(cur, clinic, inv, "same-key-123")
    with pytest.raises(psycopg2.errors.UniqueViolation) as e:
        _online(cur, clinic, inv, "same-key-123")
    assert "uq_opd_receipts_idempotency" in str(e.value)
    assert _q(cur, "SELECT paid_paise FROM opd_invoices WHERE id = %s", inv) == 10000
    other_clinic, other_inv = _issued_invoice(cur)
    _online(cur, other_clinic, other_inv, "same-key-123")         # another clinic: independent
    _online(cur, clinic, inv, None)                                # old rows / callers without a key still work
    _online(cur, clinic, inv, None)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _online(cur, clinic, inv, "short")


def test_rollback_then_reapply(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN")   # everything below is rolled back
    try:
        cur.execute((MIG / "rollback" / "113_down.sql").read_text(encoding="utf-8"))
        assert _q(cur, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'opd_receipts' "
                       "AND column_name = 'idempotency_key'") == 0
        cur.execute((MIG / "113_opd_receipt_idempotency.sql").read_text(encoding="utf-8"))
        cur.execute((MIG / "113_opd_receipt_idempotency.sql").read_text(encoding="utf-8"))   # idempotent
    finally:
        cur.execute("ROLLBACK")
