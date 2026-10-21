"""Migration 112 against real PostgreSQL: the reminder purpose is accepted, a reminder
key can be queued only once per clinic, and the old purposes and guards are unchanged.
The fixture connection is autocommit: a refused insert does not affect the next one."""

import json
import uuid

import psycopg2
import pytest

from tests.test_migration_098_voice import _clinic


def _job(cur, clinic, purpose, context=None, phone="+919876543210", status="queued"):
    cur.execute("INSERT INTO voice_outbound_jobs (clinic_id, patient_phone, purpose, context, status) "
                "VALUES (%s, %s, %s, %s, %s)", (clinic, phone, purpose, json.dumps(context or {}), status))


@pytest.fixture
def world(real_pg_conn):
    cur = real_pg_conn.cursor()
    return cur, _clinic(cur, "Rem A " + uuid.uuid4().hex[:6]), _clinic(cur, "Rem B " + uuid.uuid4().hex[:6])


def test_reminder_key_is_queued_once_per_clinic_whatever_its_status(world):
    cur, a, b = world
    key = {"appointment_id": "a1", "reminder_key": "a1|2026-10-06T17:30|60"}
    _job(cur, a, "appointment_reminder", key, status="completed")
    _job(cur, b, "appointment_reminder", key)                          # another clinic: independent
    with pytest.raises(psycopg2.errors.UniqueViolation) as e:
        _job(cur, a, "appointment_reminder", key, phone="+919000000009")
    assert "uq_voice_reminder_once" in str(e.value)
    # The same visit moved to 18:00 is a new reminder.
    _job(cur, a, "appointment_reminder", {**key, "reminder_key": "a1|2026-10-06T18:00|60"}, status="skipped")


def test_old_purposes_still_work_and_unknown_ones_are_refused(world):
    cur, a, _ = world
    _job(cur, a, "lead_followup")
    _job(cur, a, "callback_request")
    with pytest.raises(psycopg2.errors.UniqueViolation):                # one live call per person and purpose
        _job(cur, a, "lead_followup")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _job(cur, a, "marketing_blast", phone="+919000000008")
