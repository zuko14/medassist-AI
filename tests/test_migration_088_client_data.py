"""Migration 088 against real PostgreSQL: constraints, undo-by-cascade, RLS backstop."""

import uuid

import psycopg2
import pytest


def _clinic(cur, name):
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'essential', true) RETURNING id",
        (name, "+9190" + uuid.uuid4().hex[:8]),
    )
    return str(cur.fetchone()[0])


def _batch(cur, clinic_id):
    cur.execute("INSERT INTO patient_import_batches (clinic_id) VALUES (%s) RETURNING id", (clinic_id,))
    return str(cur.fetchone()[0])


def _record(cur, clinic_id, batch_id, key, phone="+919876543210", name="Ravi"):
    cur.execute(
        "INSERT INTO patient_records (clinic_id, full_name, phone, dedupe_key, import_batch_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (clinic_id, name, phone, key, batch_id),
    )


def test_088_tables_constraints_and_cascade(real_pg_conn):
    cur = real_pg_conn.cursor()
    a = _clinic(cur, "Smile Dental 088")
    b = _clinic(cur, "Other Clinic 088")
    try:
        batch = _batch(cur, a)
        _record(cur, a, batch, "pn:+919876543210|ravi")
        _record(cur, b, _batch(cur, b), "pn:+919876543210|ravi")  # same key, other tenant: allowed

        # Same key in the same clinic is rejected: a re-import cannot double rows.
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _record(cur, a, batch, "pn:+919876543210|ravi")

        # Phone must be the normalised digits form (never free text).
        with pytest.raises(psycopg2.errors.CheckViolation):
            _record(cur, a, batch, "k2", phone="call me")

        # Undo an import = delete its batch row; records go with it, other tenant untouched.
        cur.execute("DELETE FROM patient_import_batches WHERE id = %s", (batch,))
        cur.execute("SELECT count(*) FROM patient_records WHERE clinic_id = %s", (a,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM patient_records WHERE clinic_id = %s", (b,))
        assert cur.fetchone()[0] == 1

        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO support_messages (clinic_id, category, subject, message, created_by_username, created_by_role) "
                "VALUES (%s, 'gossip', 's', 'm', 'u', 'clinic_admin')", (a,))

        # Invoice add-on defaults to 0 so every existing invoice row is unchanged.
        cur.execute("SELECT column_default, is_nullable FROM information_schema.columns "
                    "WHERE table_name='platform_invoices' AND column_name='storage_addon_paise'")
        default, nullable = cur.fetchone()
        assert default == "0" and nullable == "NO"
    finally:
        cur.execute("DELETE FROM clinics WHERE id IN (%s, %s)", (a, b))


def test_088_force_rls_isolates_tenants_for_app_role(real_pg_conn):
    cur = real_pg_conn.cursor()
    a = _clinic(cur, "RLS A 088")
    b = _clinic(cur, "RLS B 088")
    try:
        _record(cur, a, _batch(cur, a), "k-a", name="Alpha Secret")
        _record(cur, b, _batch(cur, b), "k-b", name="Beta Secret")
        for c in (a, b):
            cur.execute(
                "INSERT INTO support_messages (clinic_id, category, subject, message, created_by_username, created_by_role) "
                "VALUES (%s, 'concern', 's', 'm', 'u', 'clinic_admin')", (c,))

        cur.execute("SET ROLE kriya_app")
        try:
            cur.execute("SET app.clinic_id = %s", (a,))
            cur.execute("SELECT full_name FROM patient_records")
            assert cur.fetchall() == [("Alpha Secret",)]
            cur.execute("SELECT count(*) FROM support_messages")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM patient_import_batches")
            assert cur.fetchone()[0] == 1
            cur.execute("SET app.clinic_id = ''")
            cur.execute("SELECT count(*) FROM patient_records")
            assert cur.fetchone()[0] == 0
        finally:
            cur.execute("RESET ROLE")
    finally:
        cur.execute("DELETE FROM clinics WHERE id IN (%s, %s)", (a, b))
