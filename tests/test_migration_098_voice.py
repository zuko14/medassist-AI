"""Migration 098 against real PostgreSQL: tenant-consistent call events,
unique Exophone routing, duplicate-call and duplicate-outbound guards, RLS forced."""

import uuid

import psycopg2
import pytest


def _clinic(cur, name):
    cur.execute(
        "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, %s, true) RETURNING id",
        (name, "+9198" + uuid.uuid4().hex[:8], "polyclinic"),
    )
    return str(cur.fetchone()[0])


def _call(cur, clinic, sid=None, ref=None):
    cur.execute(
        "INSERT INTO voice_calls (clinic_id, call_ref, direction, caller_phone, provider_call_sid) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (clinic, ref or "CALL-" + uuid.uuid4().hex[:10], "inbound", "+919876543210", sid),
    )
    return str(cur.fetchone()[0])


@pytest.fixture
def world(real_pg_conn):
    cur = real_pg_conn.cursor()
    return cur, _clinic(cur, "Voice A " + uuid.uuid4().hex[:6]), _clinic(cur, "Voice B " + uuid.uuid4().hex[:6])


def test_event_cannot_point_at_another_clinics_call(world):
    cur, a, b = world
    call = _call(cur, a)
    cur.execute("INSERT INTO voice_call_events (clinic_id, call_id, kind, name) VALUES (%s, %s, %s, %s)",
                (a, call, "system", "CALL_STARTED"))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        cur.execute("INSERT INTO voice_call_events (clinic_id, call_id, kind, name) VALUES (%s, %s, %s, %s)",
                    (b, call, "system", "CALL_STARTED"))


def test_exophone_routes_to_one_tenant_only(world):
    cur, a, b = world
    num = "+9140" + str(uuid.uuid4().int)[:8]
    cur.execute("INSERT INTO voice_numbers (clinic_id, exophone) VALUES (%s, %s)", (a, num))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("INSERT INTO voice_numbers (clinic_id, exophone) VALUES (%s, %s)", (b, num))
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute("INSERT INTO voice_numbers (clinic_id, exophone) VALUES (%s, %s)", (a, "04012345678"))


def test_same_provider_call_sid_cannot_create_two_calls(world):
    cur, a, _ = world
    sid = "CA" + uuid.uuid4().hex
    _call(cur, a, sid=sid)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _call(cur, a, sid=sid)


def test_one_active_outbound_job_per_person(world):
    cur, a, _ = world
    q = ("INSERT INTO voice_outbound_jobs (clinic_id, patient_phone, purpose, status) "
         "VALUES (%s, %s, %s, %s) RETURNING id")
    cur.execute(q, (a, "+919876500000", "lead_followup", "queued"))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(q, (a, "+919876500000", "lead_followup", "queued"))
    cur.execute(q, (a, "+919876500000", "lead_followup", "completed"))  # history rows are fine


def test_lexicon_phrase_unique_case_insensitive(world):
    cur, a, b = world
    q = "INSERT INTO voice_lexicon_entries (clinic_id, kind, phrase, canonical) VALUES (%s, %s, %s, %s)"
    cur.execute(q, (a, "specialty_synonym", "Gunde OPD", "Cardiology"))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(q, (a, "specialty_synonym", " gunde opd ", "Cardiology"))
    cur.execute(q, (b, "specialty_synonym", "Gunde OPD", "Cardiology"))


def test_deleting_call_removes_its_events(world):
    cur, a, _ = world
    call = _call(cur, a)
    cur.execute("INSERT INTO voice_call_events (clinic_id, call_id, kind, name) VALUES (%s, %s, %s, %s)",
                (a, call, "turn_user", "UTTERANCE"))
    cur.execute("DELETE FROM voice_calls WHERE id = %s", (call,))
    cur.execute("SELECT count(*) FROM voice_call_events WHERE call_id = %s", (call,))
    assert cur.fetchone()[0] == 0


def test_rls_is_forced(world):
    cur = world[0]
    cur.execute("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname LIKE %s "
                "ORDER BY relname", ("voice_%",))
    rows = [r for r in cur.fetchall() if r[0] in ("voice_numbers", "voice_calls", "voice_call_events",
                                                   "voice_lexicon_entries", "voice_outbound_jobs")]
    assert len(rows) == 5 and all(r[1] and r[2] for r in rows)
