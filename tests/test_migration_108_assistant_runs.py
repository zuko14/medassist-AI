"""Migration 108 (Kriya Ketu, KK-P1-01) against a real PostgreSQL schema.

assistant_runs holds one row per staff instruction to the assistant. It is
tenant-owned and purged after 90 days. Runs on the embedded server the
real-Postgres fixture migrates to head.
"""

import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.conftest_db import get_default_clinic_id

ROLLBACK_SQL = Path(__file__).resolve().parent.parent / "migrations" / "rollback" / "108_down.sql"


def _run(cur, clinic_id, **over):
    row = {
        "clinic_id": clinic_id, "username": "frontdesk1", "role": "staff",
        "client_request_id": str(uuid.uuid4()), "input_mode": "text",
        "input_text": "show today's appointments", "state": "RECEIVED",
    }
    row.update(over)
    cols = ", ".join(row)
    cur.execute(
        f"INSERT INTO assistant_runs ({cols}) VALUES ({', '.join(['%s'] * len(row))}) RETURNING id;",
        tuple(row.values()),
    )
    return cur.fetchone()[0]


def test_a_minimal_run_gets_its_defaults(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    run_id = _run(cur, get_default_clinic_id(cur))
    cur.execute("SELECT params, steps, llm_tokens, llm_cost_paise, created_at FROM assistant_runs WHERE id = %s;",
                (run_id,))
    params, steps, tokens, paise, created = cur.fetchone()
    assert (params, steps, tokens, paise) == ({}, [], 0, 0)
    assert created is not None


def test_client_request_id_is_unique_per_clinic_and_user(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    clinic = get_default_clinic_id(cur)
    req = str(uuid.uuid4())
    _run(cur, clinic, client_request_id=req)
    _run(cur, clinic, client_request_id=req, username="frontdesk2")  # another user may reuse it
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _run(cur, clinic, client_request_id=req)


@pytest.mark.parametrize("over", [
    {"input_text": "x" * 1001},
    {"transcript_original": "x" * 1001, "input_mode": "voice"},
    {"input_mode": "email"},
    {"route_layer": "guess"},
])
def test_check_constraints_reject_bad_values(real_pg_conn, clean_db, over):
    cur = real_pg_conn.cursor()
    with pytest.raises(psycopg2.errors.CheckViolation):
        _run(cur, get_default_clinic_id(cur), **over)


def test_rows_go_with_their_clinic(real_pg_conn, clean_db):
    cur = real_pg_conn.cursor()
    cur.execute("INSERT INTO clinics (name, whatsapp_number, plan, is_active) "
                "VALUES ('Ketu Cascade Clinic', %s, 'essential', true) RETURNING id;",
                (f"+9190{uuid.uuid4().int % 10**8:08d}",))  # unique: the embedded DB outlives a failed run
    clinic = cur.fetchone()[0]
    _run(cur, clinic)
    cur.execute("DELETE FROM clinics WHERE id = %s;", (clinic,))
    cur.execute("SELECT count(*) FROM assistant_runs WHERE clinic_id = %s;", (clinic,))
    assert cur.fetchone()[0] == 0


def test_rls_is_forced_and_the_indexes_exist(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'assistant_runs';")
    assert cur.fetchone() == (True, True)
    cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'assistant_runs';")
    idx = dict(cur.fetchall())
    assert "(clinic_id, created_at DESC)" in idx["idx_assistant_runs_clinic_created"]
    assert "WHERE" in idx["idx_assistant_runs_active"]


def test_rollback_drops_only_assistant_runs(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN;")
    try:
        cur.execute(ROLLBACK_SQL.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('assistant_runs'), to_regclass('clinics');")
        assert cur.fetchone() == (None, "clinics")
    finally:
        cur.execute("ROLLBACK;")
    cur.execute("SELECT to_regclass('assistant_runs');")
    assert cur.fetchone()[0] == "assistant_runs"
