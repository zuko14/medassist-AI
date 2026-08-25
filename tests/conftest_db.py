"""Pytest fixtures for testing against a real PostgreSQL instance."""

import os
import psycopg2
import pytest
from scripts.migrate import apply_migrations, bootstrap_database

_PG_SERVER = None
_PG_URI = None


@pytest.fixture(scope="session")
def real_postgres_uri():
    """Session-scoped real PostgreSQL server and migration fixture."""
    global _PG_SERVER, _PG_URI

    # Check if external PostgreSQL is provided via environment
    env_uri = os.getenv("TEST_DATABASE_URL")
    if env_uri:
        _PG_URI = env_uri
    else:
        import pgserver
        _PG_SERVER = pgserver.get_server("tmp_pg_pytest_data")
        _PG_URI = _PG_SERVER.get_uri()

    # Apply all migrations to head
    conn = psycopg2.connect(_PG_URI)
    conn.autocommit = False
    try:
        bootstrap_database(conn)
        applied, skipped, failed = apply_migrations(conn)
        if failed:
            raise RuntimeError(f"Failed applying migrations for test DB: {failed}")
    finally:
        conn.close()

    yield _PG_URI

    if _PG_SERVER is not None:
        try:
            _PG_SERVER.cleanup()
        except Exception:
            pass


@pytest.fixture
def real_pg_conn(real_postgres_uri):
    """Function-scoped connection to real PostgreSQL."""
    conn = psycopg2.connect(real_postgres_uri)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def clean_db(real_pg_conn):
    """Clean operational tables before/after test while preserving core clinics."""
    cur = real_pg_conn.cursor()
    try:
        cur.execute("""
        TRUNCATE TABLE
            appointments,
            payment_events,
            conversations,
            patients,
            family_members,
            lab_reports,
            prescriptions,
            doctor_leaves,
            hospital_holidays,
            processed_messages,
            failed_messages,
            inbound_messages,
            scheduler_locks,
            admin_audit_logs,
            rate_limits
        CASCADE;
        """)
    finally:
        cur.close()
    yield


def get_default_clinic_id(cur) -> str:
    """Fetch seeded clinic ID or create one for test."""
    cur.execute("SELECT id FROM clinics LIMIT 1;")
    row = cur.fetchone()
    if row:
        return str(row[0])
    cur.execute("""
    INSERT INTO clinics (name, whatsapp_number, plan, is_active)
    VALUES ('Invariant Test Clinic', '+919999999999', 'essential', true)
    RETURNING id;
    """)
    return str(cur.fetchone()[0])

