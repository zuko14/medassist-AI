"""Continuous Care migrations 145–149 (CC-M1…M5) against real PostgreSQL (docs/continuous-care/TEST-PLAN.md §2, CG6).

M1 145: clinics.cc_state default / CHECK; voice purpose and quota kind widened (old values still valid); the rollback
        refuses while a new value is used; signed-activation CHECK; one live consent.
M2 146: one open enrollment per programme; never active without a live consent; cross-tenant patient / doctor
        refused by the same-clinic guard; one active care plan.
M3 147: observations append-only (UPDATE / DELETE refused, purge allowed); unique source_message_id.
M4 148: one open case per (person, enrollment, reason); case events append-only; no drug / dose key on an action.
M5 149: cc_purge_patient removes the person's CC rows and withdraws consent; one verification per action.
Chain: 149 → 145 rollback, then re-apply twice, all in one transaction; no trigger on any pre-existing table.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_ipd_api_e2e import _clinic, _doctor, _patient

MIG = Path(__file__).resolve().parents[1] / "migrations"
NUMS = ("145_cc_foundation", "146_cc_enrollments_plans", "147_cc_measure", "148_cc_act", "149_cc_verify_outcomes")
UP = {n[:3]: MIG / f"{n}.sql" for n in NUMS}
DOWN = {n[:3]: MIG / "rollback" / f"{n[:3]}_rollback_{n[4:]}.sql" for n in NUMS}


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _err(c, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(c, sql, *args)
    return str(e.value)


def _activation(c, clinic, status="active"):
    snap = json.dumps({"registration_number": "TSMC/2012/777", "doctor_id": str(uuid.uuid4())})
    return str(_q(c, """INSERT INTO cc_protocol_activations (clinic_id, protocol_code, version, status, signed_at,
                        signed_by_admin_id, signer_snapshot) VALUES (%s, 't2dm', '1.0.0', %s, now(), %s, %s::jsonb)
                        RETURNING id""", clinic, status, str(uuid.uuid4()), snap))


def _consent(c, clinic, patient):
    return str(_q(c, """INSERT INTO cc_consents (clinic_id, patient_id, notice_version, notice_sha256, method, granted_at)
                        VALUES (%s, %s, 'cc-notice-1', %s, 'counter_signed', now()) RETURNING id""",
                  clinic, patient, "a" * 64))


def _world(c):
    clinic = _clinic(c)
    doctor, patient = _doctor(c, clinic), _patient(c, clinic)
    return clinic, doctor, patient, _activation(c, clinic)


def _enroll(c, clinic, doctor, patient, act, status="invited", consent=None):
    return str(_q(c, """INSERT INTO cc_enrollments (clinic_id, patient_id, activation_id, protocol_code, primary_doctor_id,
                        status, source, consent_id, enrolled_at, doctor_approved_at)
                        VALUES (%s, %s, %s, 't2dm', %s, %s, 'doctor_enrolled', %s,
                                CASE WHEN %s THEN now() END, now()) RETURNING id""",
                  clinic, patient, act, doctor, status, consent, consent is not None))


# ── M1 ────────────────────────────────────────────────────────────────────────

def test_m1_clinic_columns_and_widened_checks(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    assert _q(c, "SELECT cc_state || '|' || cc_settings::text FROM clinics WHERE id = %s", clinic) == "OFF|{}"
    assert "clinics_cc_state_check" in _err(c, "UPDATE clinics SET cc_state = 'ON' WHERE id = %s", clinic)
    assert "clinics_cc_settings_object" in _err(c, "UPDATE clinics SET cc_settings = '[]' WHERE id = %s", clinic)
    defs = _q(c, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'voice_outbound_jobs_purpose_check'")
    for v in ("lead_followup", "callback_request", "appointment_reminder", "care_checkin"):
        assert v in defs
    kinds = _q(c, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'clinic_message_quota_usage_kind_check'")
    for v in ("patient", "doctor", "review", "care"):
        assert v in kinds


def test_m1_signed_activation_and_one_live_consent(real_pg_conn):
    c = real_pg_conn
    clinic, patient = _clinic(c), None
    patient = _patient(c, clinic)
    assert "cc_protocol_activations_signed" in _err(
        c, "INSERT INTO cc_protocol_activations (clinic_id, protocol_code, version, status) VALUES (%s, 't2dm', '1.0.0', "
           "'active')", clinic)
    assert "cc_protocol_activations_signed" in _err(
        c, """INSERT INTO cc_protocol_activations (clinic_id, protocol_code, version, status, signed_at, signed_by_admin_id,
              signer_snapshot) VALUES (%s, 't2dm', '1.0.0', 'active', now(), %s, '{"registration_number": " "}')""",
        clinic, str(uuid.uuid4()))
    _activation(c, clinic)
    assert "uq_cc_activation_active" in _err(c, """INSERT INTO cc_protocol_activations (clinic_id, protocol_code, version,
        status, signed_at, signed_by_admin_id, signer_snapshot) VALUES (%s, 't2dm', '1.0.0', 'active', now(), %s,
        '{"registration_number": "ABC/1"}')""", clinic, str(uuid.uuid4()))
    first = _consent(c, clinic, patient)
    assert "uq_cc_consent_live" in _err(c, """INSERT INTO cc_consents (clinic_id, patient_id) VALUES (%s, %s)""",
                                        clinic, patient)
    assert "cc_consents_granted_shape" in _err(c, "UPDATE cc_consents SET notice_version = NULL WHERE id = %s", first)
    _q(c, "UPDATE cc_consents SET withdrawn_at = now(), withdrawn_via = 'stop_keyword' WHERE id = %s", first)
    assert _consent(c, clinic, patient)                                 # a withdrawn consent frees the slot


# ── M2 ────────────────────────────────────────────────────────────────────────

def test_m2_enrollment_rules_and_cross_tenant_guard(real_pg_conn):
    c = real_pg_conn
    clinic, doctor, patient, act = _world(c)
    e = _enroll(c, clinic, doctor, patient, act)
    assert "uq_cc_enrollment_open" in _err(c, """INSERT INTO cc_enrollments (clinic_id, patient_id, activation_id,
        protocol_code, primary_doctor_id, source) VALUES (%s, %s, %s, 't2dm', %s, 'import')""", clinic, patient, act, doctor)
    assert "cc_enrollments_active_consent" in _err(c, "UPDATE cc_enrollments SET status = 'active', enrolled_at = now() "
                                                      "WHERE id = %s", e)
    consent = _consent(c, clinic, patient)
    _q(c, "UPDATE cc_enrollments SET status = 'active', enrolled_at = now(), consent_id = %s WHERE id = %s", consent, e)
    other = _clinic(c)
    stranger, their_doctor = _patient(c, other), _doctor(c, other)
    assert "kriya_cross_tenant_ref" in _err(c, """INSERT INTO cc_enrollments (clinic_id, patient_id, activation_id,
        protocol_code, primary_doctor_id, source) VALUES (%s, %s, %s, 't2dm', %s, 'import')""", clinic, stranger, act, doctor)
    assert "kriya_cross_tenant_ref" in _err(c, """INSERT INTO cc_enrollments (clinic_id, patient_id, activation_id,
        protocol_code, primary_doctor_id, source) VALUES (%s, %s, %s, 't2dm', %s, 'import')""",
                                            clinic, _patient(c, clinic), act, their_doctor)
    assert "cc_enrollments_activation_fk" in _err(c, """INSERT INTO cc_enrollments (clinic_id, patient_id, activation_id,
        protocol_code, primary_doctor_id, source) VALUES (%s, %s, %s, 't2dm', %s, 'import')""",
                                                  other, stranger, act, their_doctor)
    _q(c, "INSERT INTO cc_care_plans (clinic_id, enrollment_id, version, valid_from, built_by) VALUES (%s, %s, 1, "
          "CURRENT_DATE, 'system')", clinic, e)
    assert "uq_cc_care_plan_active" in _err(c, "INSERT INTO cc_care_plans (clinic_id, enrollment_id, version, valid_from, "
                                               "built_by) VALUES (%s, %s, 2, CURRENT_DATE, 'system')", clinic, e)


def test_m2_install_plan_supersedes_atomically(real_pg_conn):
    c = real_pg_conn
    clinic, doctor, patient, act = _world(c)
    e = _enroll(c, clinic, doctor, patient, act, "active", _consent(c, clinic, patient))
    items = json.dumps([{"kind": "measure", "measure_code": "fbg", "prompt_mode": "reading", "display_text": "Fasting",
                         "schedule": {"every_days": 3}, "starts_on": "2026-11-25"}])
    for _ in range(2):
        _q(c, "SELECT cc_install_plan(%s, %s, NULL, %s::jsonb, 'system', CURRENT_DATE)", clinic, e, items)
    assert _q(c, "SELECT count(*) FROM cc_care_plans WHERE enrollment_id = %s AND status = 'active'", e) == 1
    assert _q(c, "SELECT count(*) FROM cc_care_plans WHERE enrollment_id = %s AND status = 'superseded'", e) == 1
    assert _q(c, "SELECT max(version) FROM cc_care_plans WHERE enrollment_id = %s", e) == 2


# ── M3 / M4 / M5 ──────────────────────────────────────────────────────────────

def _active(c):
    clinic, doctor, patient, act = _world(c)
    return clinic, patient, _enroll(c, clinic, doctor, patient, act, "active", _consent(c, clinic, patient))


def test_m3_observations_append_only_and_message_unique(real_pg_conn):
    c = real_pg_conn
    clinic, _, e = _active(c)
    ins = ("INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, observed_at, source, "
           "source_message_id) VALUES (%s, %s, 'fbg', 142, now(), 'whatsapp_typed', %s) RETURNING id")
    o = str(_q(c, ins, clinic, e, "wamid.A1"))
    assert "uq_cc_observations_message" in _err(c, ins, clinic, e, "wamid.A1")
    assert "cc_observation_append_only" in _err(c, "UPDATE cc_observations SET value_num = 140 WHERE id = %s", o)
    assert "cc_observation_append_only" in _err(c, "DELETE FROM cc_observations WHERE id = %s", o)
    assert "cc_observations_value" in _err(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, "
                                              "observed_at, source) VALUES (%s, %s, 'fbg', now(), 'whatsapp_typed')", clinic, e)
    assert "cc_observations_staff" in _err(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, "
                                              "value_num, observed_at, source) VALUES (%s, %s, 'fbg', 1, now(), "
                                              "'coordinator_entered')", clinic, e)


def test_m4_one_open_case_append_only_events_and_no_drug_keys(real_pg_conn):
    c = real_pg_conn
    clinic, patient, e = _active(c)
    ins = ("INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by) VALUES (%s, %s, %s, "
           "'t2dm:hypo', 'urgent', now()) RETURNING id")
    case = str(_q(c, ins, clinic, patient, e))
    assert "uq_cc_case_open" in _err(c, ins, clinic, patient, e)
    ev = str(_q(c, "INSERT INTO cc_case_events (clinic_id, case_id, kind) VALUES (%s, %s, 'opened') RETURNING id",
                clinic, case))
    assert "cc_case_event_append_only" in _err(c, "DELETE FROM cc_case_events WHERE id = %s", ev)
    assert "cc_case_event_append_only" in _err(c, "UPDATE cc_case_events SET body = 'x' WHERE id = %s", ev)
    for key in ("drug", "dose", "dosage", "medicine", "frequency", "strength", "prescription", "drug_name"):
        assert "cc_actions_no_drug" in _err(c, f"""INSERT INTO cc_actions (clinic_id, case_id, action_code, params,
            decided_by_name, decider_role) VALUES (%s, %s, 'continue_plan', '{{"{key}": "x"}}', 'Dr', 'doctor')""",
                                           clinic, case)
    assert "cc_actions_booked" in _err(c, """INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name,
        decider_role, status) VALUES (%s, %s, 'book_review_visit', 'Dr', 'doctor', 'booked')""", clinic, case)


def test_m5_purge_patient_and_one_verification_per_action(real_pg_conn):
    c = real_pg_conn
    clinic, patient, e = _active(c)
    _q(c, "INSERT INTO cc_observations (clinic_id, enrollment_id, measure_code, value_num, observed_at, source) "
          "VALUES (%s, %s, 'fbg', 142, now(), 'whatsapp_typed')", clinic, e)
    case = str(_q(c, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by) VALUES "
                     "(%s, %s, %s, 't2dm:hypo', 'urgent', now()) RETURNING id", clinic, patient, e))
    act = str(_q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role) VALUES "
                    "(%s, %s, 'order_lab', 'Dr', 'doctor') RETURNING id", clinic, case))
    ver = ("INSERT INTO cc_verifications (clinic_id, action_id, evidence_kind, evidence_ref) VALUES (%s, %s, "
           "'validated_result', '{\"table\": \"lab_results\", \"id\": \"x\"}')")
    _q(c, ver, clinic, act)
    assert "cc_verifications_action_key" in _err(c, ver, clinic, act)
    out = _q(c, "SELECT cc_purge_patient(%s, %s, NULL)", clinic, patient)
    assert out["enrollments"] == 1 and out["observations"] == 1 and out["cases"] == 1
    for t in ("cc_enrollments", "cc_observations", "cc_cases", "cc_actions", "cc_verifications"):
        assert _q(c, f"SELECT count(*) FROM {t} WHERE clinic_id = %s", clinic) == 0, t
    assert _q(c, "SELECT withdrawn_via FROM cc_consents WHERE clinic_id = %s", clinic) == "erasure"
    assert "cc_bad_args" in _err(c, "SELECT cc_purge_patient(NULL, %s, NULL)", patient)


# ── chain ─────────────────────────────────────────────────────────────────────

_NON_CC_TRIGGERS = """SELECT coalesce(string_agg(c.relname || '.' || t.tgname, ',' ORDER BY c.relname, t.tgname), '')
                      FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                      WHERE NOT t.tgisinternal AND c.relname NOT LIKE 'cc\\_%%'"""


def test_no_trigger_on_any_pre_existing_table(real_pg_conn):
    c = real_pg_conn
    cc_trig = _q(c, """SELECT coalesce(string_agg(c.relname, ','), '') FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
                       WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg\\_cc\\_%%' AND c.relname NOT LIKE 'cc\\_%%'""")
    assert cc_trig == ""
    for n in UP.values():
        sql = n.read_text(encoding="utf-8")
        import re
        targets = set(re.findall(r"CREATE TRIGGER\s+\w+\s+BEFORE\s+[A-Z ]+?\s+ON\s+([a-z_]+)", sql))
        assert targets <= {t for t in targets if t.startswith("cc_")}, (n.name, targets)


def test_rollback_chain_149_to_145_and_reapply(real_pg_conn):
    c = real_pg_conn
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(_NON_CC_TRIGGERS)
        before = cur.fetchone()[0]
        # clear CC data left by other tests (inside this transaction only), else the rollbacks refuse — by design
        cur.execute("SELECT id FROM clinics")
        for (cid,) in cur.fetchall():
            cur.execute("SELECT cc_purge_clinic(%s)", (cid,))
        cur.execute("DELETE FROM voice_outbound_jobs WHERE purpose = 'care_checkin'")
        cur.execute("DELETE FROM clinic_message_quota_usage WHERE kind = 'care'")
        # the 145 rollback refuses while a care_checkin job exists
        cur.execute("SAVEPOINT s")
        cur.execute("SELECT id FROM clinics LIMIT 1")
        cid = cur.fetchone()[0]
        for n in ("149", "148", "147", "146"):
            cur.execute(DOWN[n].read_text(encoding="utf-8"))
        cur.execute("INSERT INTO voice_outbound_jobs (clinic_id, patient_phone, purpose, status, next_attempt_at) "
                    "VALUES (%s, '+919999999999', 'care_checkin', 'queued', now())", (cid,))
        with pytest.raises(psycopg2.Error, match="145_down: care_checkin"):
            cur.execute(DOWN["145"].read_text(encoding="utf-8"))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        for n in ("149", "148", "147", "146", "145"):
            cur.execute(DOWN[n].read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'cc\\_%%' AND relkind = 'r'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'clinics' "
                    "AND column_name IN ('cc_state', 'cc_settings')")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'voice_outbound_jobs_purpose_check'")
        assert "care_checkin" not in cur.fetchone()[0]
        cur.execute("SELECT to_regprocedure('kriya_guard_refs()') IS NOT NULL")
        assert cur.fetchone()[0]                                          # 144's function survives 145's rollback
        for _ in range(2):                                                # re-apply, idempotent
            for n in ("145", "146", "147", "148", "149"):
                cur.execute(UP[n].read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM pg_class WHERE relname LIKE 'cc\\_%%' AND relkind = 'r'")
        assert cur.fetchone()[0] == 18
        cur.execute(_NON_CC_TRIGGERS)
        assert cur.fetchone()[0] == before                                # CC added no trigger to any other table
    finally:
        cur.execute("ROLLBACK")
