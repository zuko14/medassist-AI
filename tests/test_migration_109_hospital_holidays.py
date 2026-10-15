"""Migration 109 against a real PostgreSQL schema (Kriya Ketu bug register D-27).

Migration 001 created hospital_holidays with holiday_date as its only PRIMARY KEY; 003 added
clinic_id but never moved the key. So once one clinic had a holiday on a date, every other
clinic's POST /admin/holidays for that date failed with a PK violation (500). 109 makes the key
(clinic_id, holiday_date). Runs on the embedded server the real-Postgres fixture migrates to head.
"""

from pathlib import Path

import psycopg2
import pytest

from tests.conftest_db import get_default_clinic_id
from tests.test_migration_107_doctor_leaves import _second_clinic

_MIG = Path(__file__).resolve().parent.parent / "migrations"
UP, DOWN = _MIG / "109_hospital_holidays_key_per_clinic.sql", _MIG / "rollback" / "109_down.sql"


def _holiday(cur, clinic_id, day="2035-01-26", name="Republic Day"):
    cur.execute("INSERT INTO hospital_holidays (clinic_id, holiday_date, name) VALUES (%s, %s, %s);",
                (clinic_id, day, name))


def test_two_clinics_can_close_on_the_same_date(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    _holiday(cur, get_default_clinic_id(cur))
    _holiday(cur, _second_clinic(cur))  # PK violation before 109
    cur.execute("SELECT count(*) FROM hospital_holidays WHERE holiday_date = '2035-01-26';")
    assert cur.fetchone()[0] == 2


def test_one_clinic_still_cannot_store_a_date_twice(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic = get_default_clinic_id(cur)
    _holiday(cur, clinic)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _holiday(cur, clinic, name="Again")


def _pk(cur) -> list:
    cur.execute("""
        SELECT array_agg(a.attname::text ORDER BY k.ord)
        FROM pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.conrelid = 'hospital_holidays'::regclass AND c.contype = 'p';
    """)
    return cur.fetchone()[0]


def test_the_primary_key_is_clinic_and_date(real_pg_conn):
    assert _pk(real_pg_conn.cursor()) == ["clinic_id", "holiday_date"]


def test_109_is_idempotent_and_its_rollback_is_safe(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN;")
    try:
        cur.execute(UP.read_text(encoding="utf-8"))            # second run: no-op
        assert _pk(cur) == ["clinic_id", "holiday_date"]
        cur.execute(DOWN.read_text(encoding="utf-8"))          # no shared dates: old key back
        assert _pk(cur) == ["holiday_date"]
        cur.execute(UP.read_text(encoding="utf-8"))
        _holiday(cur, get_default_clinic_id(cur))
        _holiday(cur, _second_clinic(cur))
        cur.execute(DOWN.read_text(encoding="utf-8"))          # shared date: keeps the per-clinic key
        assert _pk(cur) == ["clinic_id", "holiday_date"]
    finally:
        cur.execute("ROLLBACK;")
