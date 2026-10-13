"""Migration 107 against a real PostgreSQL schema (Kriya Ketu bug register D-20, D-11).

Migration 001 created doctor_leaves with UNIQUE(doctor_name, leave_date) before the
table had a clinic_id, so two clinics with a same-named doctor could not both
record leave for one date. 107 replaces it with UNIQUE(clinic_id, doctor_name,
leave_date). Runs on the embedded server the real-Postgres fixture migrates to head.
"""

import psycopg2
import pytest

from tests.conftest_db import get_default_clinic_id


def _second_clinic(cur) -> str:
    cur.execute("SELECT id FROM clinics WHERE whatsapp_number = '+919999999998';")
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute("""
    INSERT INTO clinics (name, whatsapp_number, plan, is_active)
    VALUES ('Second Test Clinic', '+919999999998', 'essential', true)
    RETURNING id;
    """)
    return str(cur.fetchone()[0])


def _leave(cur, clinic_id, leave_type="full"):
    cur.execute(
        "INSERT INTO doctor_leaves (clinic_id, doctor_name, leave_date, leave_type) "
        "VALUES (%s, 'Dr. Rao', '2035-06-11', %s);",
        (clinic_id, leave_type),
    )


def test_two_clinics_can_both_record_leave_for_a_same_named_doctor(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    _leave(cur, get_default_clinic_id(cur))
    _leave(cur, _second_clinic(cur))  # failed with a unique violation before 107
    cur.execute("SELECT count(*) FROM doctor_leaves WHERE doctor_name = 'Dr. Rao';")
    assert cur.fetchone()[0] == 2


def test_one_clinic_still_cannot_store_the_same_doctor_and_date_twice(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic = get_default_clinic_id(cur)
    _leave(cur, clinic, "half_morning")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _leave(cur, clinic, "half_evening")


def test_the_cross_tenant_constraint_is_gone_and_the_per_clinic_index_exists(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'doctor_leaves_doctor_name_leave_date_key';")
    assert cur.fetchone() is None
    cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_doctor_leaves_clinic_doctor_date';")
    row = cur.fetchone()
    assert row and "UNIQUE" in row[0] and "clinic_id, doctor_name, leave_date" in row[0]
