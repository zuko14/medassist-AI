"""Migration 144 (Data import, MI-01) against real PostgreSQL: ten tables with RLS forced, registered as tenant
tables; one applied batch per (kind, file); state shapes; no trigger and no column on an existing table; mi_undo is
atomic; rollback refuses while applied batches exist, then drops everything and re-applies twice."""

import re
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "144_data_import.sql", MIG / "rollback" / "144_rollback_data_import.sql"
TABLES = ("mi_batches", "mi_row_errors", "mi_batch_items", "mi_patient_links", "mi_match_queue", "mi_legacy_encounters",
          "mi_legacy_lab_values", "mi_opening_balances", "mi_diagnosis_map", "mi_chronic_candidates")


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _err(c, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(c, sql, *args)
    return str(e.value)


def _batch(c, clinic, status="dry_run", sha=None):
    return str(_q(c, "INSERT INTO mi_batches (clinic_id, kind, sha256, status, applied_at) VALUES (%s, 'payers', %s, %s, "
                     "CASE WHEN %s <> 'dry_run' THEN now() END) RETURNING id", clinic, sha or uuid.uuid4().hex * 2,
                  status, status))


def test_tables_rls_registration_and_no_existing_object_touched(real_pg_conn):
    from app.tenancy import TENANT_OWNED_TABLES
    c = real_pg_conn
    for t in TABLES:
        assert t in TENANT_OWNED_TABLES, t
        assert _q(c, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", t), t
    sql = UP.read_text(encoding="utf-8")
    assert "ALTER TABLE clinics" not in sql
    # every trigger 144 creates sits on an mi_* table (none on a pre-existing table); references use the guard
    assert set(re.findall(r"CREATE TRIGGER \w+ BEFORE [A-Z ]+ ON (\w+)", sql)) == {
        "mi_opening_balances", "mi_patient_links", "mi_match_queue"}
    assert _q(c, "SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text LIKE 'mi_%%' "
                 "AND confrelid::regclass::text NOT LIKE 'mi_%%' AND confrelid::regclass::text NOT IN "
                 "('clinics', 'patients', 'patient_records')") == 0
    assert not [t for t in re.findall(r"ALTER TABLE\s+(\w+)", sql) if not t.startswith("mi_") and t != "I"]


def test_state_shapes_and_one_applied_batch_per_file(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('MI', %s, 'polyclinic', true) "
                       "RETURNING id", "+9171" + uuid.uuid4().hex[:8]))
    sha = "ab" * 32
    _batch(c, clinic, "applied", sha)
    assert "uq_mi_batches_applied" in _err(c, "INSERT INTO mi_batches (clinic_id, kind, sha256, status, applied_at) "
                                              "VALUES (%s, 'payers', %s, 'applied', now())", clinic, sha)
    b = _batch(c, clinic)
    assert "mi_batches_state" in _err(c, "UPDATE mi_batches SET status = 'applied' WHERE id = %s", b)
    assert "sha256" in _err(c, "INSERT INTO mi_batches (clinic_id, kind, sha256) VALUES (%s, 'payers', 'nothex')", clinic)
    assert "kind" in _err(c, "INSERT INTO mi_batches (clinic_id, kind, sha256) VALUES (%s, 'made_up', %s)", clinic, "cd" * 32)
    assert "mi_batch_items_shape" in _err(c, "INSERT INTO mi_batch_items (clinic_id, batch_id, target_table, target_id, "
                                             "action) VALUES (%s, %s, 'doctors', %s, 'updated')", clinic, b, str(uuid.uuid4()))
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('MI2', %s, 'polyclinic', true) "
                      "RETURNING id", "+9172" + uuid.uuid4().hex[:8]))
    assert "mi_row_errors_batch_fk" in _err(c, "INSERT INTO mi_row_errors (clinic_id, batch_id, row_no, column_name, problem) "
                                               "VALUES (%s, %s, 2, 'x', 'y')", other, b)     # another clinic's batch
    assert "mi_not_applied" in _err(c, "SELECT mi_undo(%s, %s)", clinic, b)


def test_rollback_refused_then_drops_and_reapplies(real_pg_conn):
    c = real_pg_conn
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT count(*) FROM mi_batches WHERE status = 'applied'")
        if cur.fetchone()[0]:
            with pytest.raises(psycopg2.Error) as e:
                cur.execute(DOWN.read_text(encoding="utf-8"))
            assert "144_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        cur.execute("UPDATE mi_batches SET status = 'undone', undone_at = now() WHERE status = 'applied'")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT " + ", ".join(f"to_regclass('public.{t}')" for t in TABLES))
        assert set(cur.fetchone()) == {None}
        cur.execute("SELECT to_regclass('public.patient_records') IS NOT NULL")
        assert cur.fetchone()[0]                                         # 088's list is untouched
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")
