"""Migration 089 against real PostgreSQL: sitting uniqueness, formats, atomic
monthly quota under concurrency, RLS backstop."""

import threading
import uuid

import psycopg2
import pytest


def _clinic(cur, name):
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'dental', true) RETURNING id",
        (name, "+9198" + uuid.uuid4().hex[:8]),
    )
    return str(cur.fetchone()[0])


def _plan(cur, clinic_id, phone="+919876543210"):
    cur.execute(
        "INSERT INTO dental_treatment_plans (clinic_id, patient_phone, patient_name, treatment_name, planned_sittings) "
        "VALUES (%s, %s, 'Ravi', 'Root Canal', 3) RETURNING id", (clinic_id, phone))
    return str(cur.fetchone()[0])


def _sitting(cur, clinic_id, plan_id, number, time_, status="confirmed"):
    cur.execute(
        "INSERT INTO appointments (clinic_id, patient_phone, patient_name, department, doctor_name, "
        "appointment_date, appointment_time, status, treatment_plan_id, sitting_number) "
        "VALUES (%s, '+919876543210', 'Ravi', 'Dental', 'Dr. Priya', '2026-10-06', %s, %s, %s, %s) RETURNING id",
        (clinic_id, time_, status, plan_id, number))
    return str(cur.fetchone()[0])


def test_089_one_live_booking_per_sitting_number(real_pg_conn):
    cur = real_pg_conn.cursor()
    c = _clinic(cur, "Sitting Uniq 089")
    try:
        p = _plan(cur, c)
        first = _sitting(cur, c, p, 1, "10:00:00")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _sitting(cur, c, p, 1, "11:00:00")
        # Rescheduling = cancel + rebook the same number: allowed.
        cur.execute("UPDATE appointments SET status = 'cancelled' WHERE id = %s", (first,))
        _sitting(cur, c, p, 1, "11:00:00")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _sitting(cur, c, p, 31, "12:00:00")
        # Deleting a plan keeps its appointments (history), just unlinks them.
        cur.execute("DELETE FROM dental_treatment_plans WHERE id = %s", (p,))
        cur.execute("SELECT count(*) FROM appointments WHERE clinic_id = %s AND treatment_plan_id IS NULL", (c,))
        assert cur.fetchone()[0] == 2
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (c,))


def test_089_formats_are_enforced(real_pg_conn):
    cur = real_pg_conn.cursor()
    c = _clinic(cur, "Formats 089")
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            _plan(cur, c, phone="call me")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO doctors (clinic_id, name, specialization, department, whatsapp_phone) "
                        "VALUES (%s, 'Dr X', 'Dentist', 'Dental', 'abc')", (c,))
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO clinic_message_quota_usage (clinic_id, period_month, kind) "
                        "VALUES (%s, '2026-10', 'marketing')", (c,))
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (c,))


def test_089_quota_cannot_be_overspent_concurrently(real_postgres_uri, real_pg_conn):
    cur = real_pg_conn.cursor()
    c = _clinic(cur, "Quota Race 089")
    results = []

    def worker():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            k = conn.cursor()
            for _ in range(10):
                k.execute("SELECT reserve_message_quota(%s, '2026-10', 'doctor', 7)", (c,))
                results.append(k.fetchone()[0])
        finally:
            conn.close()

    try:
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        granted = [r for r in results if r > 0]
        assert len(granted) == 7 and sorted(granted) == list(range(1, 8))
        assert results.count(-1) == 33
        cur.execute("SELECT used FROM clinic_message_quota_usage WHERE clinic_id = %s AND kind = 'doctor'", (c,))
        assert cur.fetchone()[0] == 7
    finally:
        cur.execute("DELETE FROM clinics WHERE id = %s", (c,))


def test_089_force_rls_isolates_plans(real_pg_conn):
    cur = real_pg_conn.cursor()
    a = _clinic(cur, "RLS A 089")
    b = _clinic(cur, "RLS B 089")
    try:
        _plan(cur, a)
        _plan(cur, b)
        cur.execute("SET ROLE kriya_app")
        try:
            cur.execute("SET app.clinic_id = %s", (a,))
            cur.execute("SELECT count(*) FROM dental_treatment_plans")
            assert cur.fetchone()[0] == 1
            cur.execute("SET app.clinic_id = ''")
            cur.execute("SELECT count(*) FROM dental_treatment_plans")
            assert cur.fetchone()[0] == 0
        finally:
            cur.execute("RESET ROLE")
    finally:
        cur.execute("DELETE FROM clinics WHERE id IN (%s, %s)", (a, b))
