"""Real PostgreSQL checks for migration 104 (OPD online-payment exceptions)."""

import os

import psycopg2
import pytest

from tests.test_migration_103_opd import _create_clinic, _create_patient

_MIGRATION = os.path.join(os.path.dirname(__file__), "..", "migrations", "104_opd_payment_exceptions.sql")


def _issued_invoice(cur, clinic_id, patient_id, total=50000):
    cur.execute("INSERT INTO opd_invoices (clinic_id, patient_id, status) VALUES (%s, %s, 'draft') RETURNING id;",
                (clinic_id, patient_id))
    inv = str(cur.fetchone()[0])
    cur.execute("""INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
                   VALUES (%s, %s, 1, 'consultation', 'Consultation', 1, %s);""", (clinic_id, inv, total))
    cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s;", (inv,))
    return inv


def _exception(cur, clinic_id, inv, payment_id="pay_1", paid=60000, applied=50000, reason="overpaid"):
    cur.execute("""INSERT INTO opd_payment_exceptions (clinic_id, invoice_id, gateway, gateway_payment_id, reason, paid_paise, applied_paise)
                   VALUES (%s, %s, 'razorpay', %s, %s, %s, %s) RETURNING id, excess_paise, status;""",
                (clinic_id, inv, payment_id, reason, paid, applied))
    return cur.fetchone()


def test_104_constraints_guard_and_purge(real_pg_conn):
    cur = real_pg_conn.cursor()
    # Re-apply twice: idempotent, and restores the FK if 103_down ran earlier in the session.
    with open(_MIGRATION, encoding="utf-8") as f:
        sql = f.read()
    cur.execute(sql)
    cur.execute(sql)

    c = _create_clinic(cur, "Exceptions Clinic")
    try:
        pat, _ = _create_patient(cur, c)
        inv = _issued_invoice(cur, c, pat)

        exc_id, excess, status = _exception(cur, c, inv)
        assert (excess, status) == (10000, "open")

        # One row per gateway payment.
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _exception(cur, c, inv)
        # Nothing to park when everything was applied.
        with pytest.raises(psycopg2.errors.CheckViolation):
            _exception(cur, c, inv, payment_id="pay_2", paid=50000, applied=50000)
        # A refund must carry the gateway refund id.
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE opd_payment_exceptions SET status='refunded', resolved_at=now() WHERE id=%s;", (exc_id,))
        # Money facts are immutable while open.
        with pytest.raises(psycopg2.errors.RaiseException, match="opd_identity_immutable"):
            cur.execute("UPDATE opd_payment_exceptions SET paid_paise=70000 WHERE id=%s;", (exc_id,))

        cur.execute("""UPDATE opd_payment_exceptions SET status='refunded', resolution_reference='rfnd_1', resolved_at=now()
                       WHERE id=%s AND status='open' RETURNING id;""", (exc_id,))
        assert cur.fetchone()
        # Resolved rows are locked, and nothing is deletable outside a purge.
        with pytest.raises(psycopg2.errors.RaiseException, match="opd_record_locked"):
            cur.execute("UPDATE opd_payment_exceptions SET resolution_note='x' WHERE id=%s;", (exc_id,))
        with pytest.raises(psycopg2.errors.RaiseException, match="opd_append_only"):
            cur.execute("DELETE FROM opd_payment_exceptions WHERE id=%s;", (exc_id,))

        # Composite FK refuses another clinic's invoice.
        c2 = _create_clinic(cur, "Other Clinic")
        try:
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                _exception(cur, c2, inv, payment_id="pay_x")
        finally:
            cur.execute("DELETE FROM clinics WHERE id = %s;", (c2,))

        # Owner purge clears exceptions through the invoice cascade.
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("SELECT count(*) FROM opd_payment_exceptions WHERE clinic_id=%s;", (c,))
        assert cur.fetchone()[0] == 0
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))
    except Exception:
        cur.execute("SELECT opd_purge_clinic(%s);", (c,))
        cur.execute("DELETE FROM clinics WHERE id = %s;", (c,))
        raise
