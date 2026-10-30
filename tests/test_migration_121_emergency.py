"""Migration 121 (emergency / casualty, MLC, identity merge) against real PostgreSQL.

Unknown-patient registration, gapless EMG numbers under concurrency, visit and
MLC guards (a locked MLC never changes), dispositions, and the atomic merge that
moves locked / signed / issued records to the real patient while every guard
still refuses anything but that one change. Rollback refuses data, then
re-applies. HTTP behaviour: tests/test_emergency_api_e2e.py.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import _q, _rows, world as _orders_world  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "121_emergency_and_casualty.sql"
DOWN = MIG / "rollback" / "121_down.sql"
SIGNER = {"name": "Dr. Rao", "registration_number": "TSMC/2001/9", "registration_council": "TSMC"}
GUARDS = ("opd_guard_signed_record", "opd_guard_invoice", "ipd_guard_admission", "opd_guard_order", "lis_guard_result",
          "docs_guard_consent", "docs_guard_certificate")


def unwind_121(cur) -> None:
    """Inside an open transaction: clear ER data the only way the guards allow
    (purge flag) and run 121_down. Rollback tests of earlier migrations call this
    first, since 121 references their tables."""
    from tests.test_migration_122_ot import unwind_122
    unwind_122(cur)   # newest first: 122 references 103 / 111 / 120 tables
    cur.execute("SELECT to_regclass('public.emergency_visits') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT DISTINCT clinic_id FROM emergency_visits UNION SELECT clinic_id FROM patient_merge_log")
    for (clinic,) in cur.fetchall():
        cur.execute("SELECT set_config('kriya.opd_purge_clinic', %s, true)", (str(clinic),))
        cur.execute("DELETE FROM patient_merge_log WHERE clinic_id = %s", (clinic,))
        cur.execute("DELETE FROM emergency_visits WHERE clinic_id = %s", (clinic,))
    cur.execute("SELECT set_config('kriya.opd_purge_clinic', '', true)")
    cur.execute("UPDATE patients SET merged_into_patient_id = NULL, merged_at = NULL, is_temporary = false WHERE is_temporary")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _register(conn, clinic, key=None, **payload):
    body = {"is_unknown": True, "estimated_gender": "male", "estimated_age": 40, "triage_category": "red",
            "chief_complaints": "RTA, unconscious", **payload}
    if key:
        body["idempotency_key"] = key
    return _q(conn, "SELECT emergency_register(%s, %s::jsonb, NULL, 'nurse')", clinic, json.dumps(body))


def _mlc(w, visit, lock=False, **payload):
    body = {"police_station_name": "Banjara Hills PS", "injury_type": "rto",
            "injury_description": "Lacerated wound scalp 3 cm", **payload}
    return _q(w["c"], "SELECT emergency_record_mlc(%s, %s, %s::jsonb, %s, %s::jsonb, '{}'::jsonb, %s, NULL)",
              w["clinic"], visit, json.dumps(body), w["doctor"], json.dumps(SIGNER), lock)


def _dispose(w, visit, action, doctor=None, **payload):
    return _q(w["c"], "SELECT emergency_disposition(%s, %s, %s, %s::jsonb, %s, NULL, 'staff')",
              w["clinic"], visit, action, json.dumps(payload), doctor)


def test_schema_guards_and_rls(real_pg_conn):
    c = real_pg_conn
    names = {r[0] for r in _rows(c, """SELECT conname FROM pg_constraint WHERE conrelid IN
             ('emergency_visits'::regclass, 'emergency_mlc_records'::regclass, 'patient_merge_log'::regclass)""")}
    assert {"emergency_visits_seq_key", "emergency_visits_disposed", "emergency_mlc_records_visit_key",
            "emergency_mlc_records_signer", "emergency_mlc_records_locked", "patient_merge_log_target_fk"} <= names
    for t in ("emergency_visits", "emergency_mlc_records", "patient_merge_log"):
        assert _q(c, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True
    for g in GUARDS:   # every patched guard carries the exemption and keeps a fixed search_path
        assert _rows(c, """SELECT prosrc LIKE '%%patient_merge_allows%%',
                           EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')
                           FROM pg_proc WHERE proname = %s""", g) == [(True, True)]


def test_unknown_registration_creates_a_temporary_identity(_orders_world):  # noqa: F811
    w, c = _orders_world, _orders_world["c"]
    key = "emg-" + uuid.uuid4().hex[:12]
    v = _register(c, w["clinic"], key)
    assert (v["status"], v["triage_category"], v["patient_name"]) == ("triage", "red", "Unknown Male ~40y")
    assert v["visit_number"] == f"EMG-{v['visit_year']}-{v['visit_seq']:05d}" and v["mrn"].startswith("MRN-")
    p = _rows(c, "SELECT is_temporary, phone, gender, age_years FROM patients WHERE id = %s", v["patient_id"])[0]
    assert p == (True, v["visit_number"], "male", 40)              # placeholder phone: never a real number
    assert _register(c, w["clinic"], key)["replayed"] is True       # double tap
    with pytest.raises(psycopg2.Error) as e:                         # one open ER visit per patient
        _q(c, "SELECT emergency_register(%s, %s::jsonb, NULL, 'x')", w["clinic"],
           json.dumps({"patient_id": v["patient_id"], "triage_category": "green", "chief_complaints": "again"}))
    assert "emergency_visit_open" in str(e.value)


def test_ten_concurrent_registrations_take_consecutive_numbers(_orders_world, real_postgres_uri):  # noqa: F811
    w, out, errors, barrier = _orders_world, [], [], threading.Barrier(10)

    def worker():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            out.append(_register(conn, w["clinic"])["visit_seq"])
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(out) == list(range(min(out), min(out) + 10))


def test_visit_and_mlc_guards(_orders_world):  # noqa: F811
    w, c = _orders_world, _orders_world["c"]
    v = _register(c, w["clinic"])
    _q(c, "UPDATE emergency_visits SET triage_category = 'yellow', clinical_notes = 'GCS 9' WHERE id = %s", v["id"])
    for sql, code in (("UPDATE emergency_visits SET status = 'discharged' WHERE id = %s", "emergency_via_function_only"),
                      ("UPDATE emergency_visits SET chief_complaints = 'changed' WHERE id = %s", "emergency_via_function_only"),
                      ("DELETE FROM emergency_visits WHERE id = %s", "emergency_append_only")):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, v["id"])
        assert code in str(e.value)

    m = _mlc(w, v["id"])
    assert m["mlc_number"] == f"MLC-{m['mlc_year']}-{m['mlc_seq']:05d}" and m["is_locked"] is False
    assert m["patient_snapshot"]["name"] == "Unknown Male ~40y" and m["patient_snapshot"]["is_unknown"] is True
    with pytest.raises(psycopg2.Error) as e:                                   # only through the function
        _q(c, "UPDATE emergency_mlc_records SET officer_name = 'X' WHERE id = %s", m["id"])
    assert "emergency_via_function_only" in str(e.value)
    corrected = _mlc(w, v["id"], officer_name="HC Ramesh", officer_badge="1123")
    assert corrected["id"] == m["id"] and corrected["mlc_seq"] == m["mlc_seq"]   # same number after a correction
    locked = _mlc(w, v["id"], lock=True, officer_name="HC Ramesh", officer_badge="1123")
    assert locked["is_locked"] and locked["locked_at"]
    with pytest.raises(psycopg2.Error) as e:
        _mlc(w, v["id"], officer_name="Someone else")
    assert "emergency_mlc_locked" in str(e.value)
    for sql in ("UPDATE emergency_mlc_records SET injury_description = 'edited' WHERE id = %s",
                "DELETE FROM emergency_mlc_records WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, m["id"])
        assert "emergency_mlc_locked" in str(e.value)
    with pytest.raises(psycopg2.errors.CheckViolation):                         # an MLC is signed by a registered doctor
        _q(c, "SELECT emergency_record_mlc(%s, %s, %s::jsonb, %s, '{\"name\": \"x\"}'::jsonb, NULL, false, NULL)",
           w["clinic"], _register(c, w["clinic"])["id"],
           json.dumps({"police_station_name": "PS", "injury_type": "burns", "injury_description": "Burns 20 percent"}),
           w["doctor"])


def test_dispositions(_orders_world):  # noqa: F811
    w, c = _orders_world, _orders_world["c"]
    v = _register(c, w["clinic"])
    with pytest.raises(psycopg2.Error) as e:
        _dispose(w, v["id"], "lama")
    assert "emergency_lama_consent_missing" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _dispose(w, v["id"], "deceased")                                          # no doctor
    assert "emergency_doctor_required" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _dispose(w, v["id"], "transferred")
    assert "emergency_transfer_to_required" in str(e.value)
    _q(c, """INSERT INTO clinical_consents (clinic_id, patient_id, consent_type, title, body_text, signer_name,
             signer_relationship, witness_name, is_signed, signed_at, signed_by_name)
             VALUES (%s, %s, 'refusal_lama', 'Refusal', 'I refuse the advised treatment and leave.', 'Brother',
                     'relative', 'Nurse', true, now(), 'nurse')""", w["clinic"], v["patient_id"])
    done = _dispose(w, v["id"], "lama", notes="Family took the patient")
    assert done["status"] == "lama" and done["lama_consent_id"] and done["disposition_at"]
    assert _dispose(w, v["id"], "lama")["replayed"] is True
    with pytest.raises(psycopg2.Error) as e:
        _dispose(w, v["id"], "discharged")
    assert "emergency_visit_closed" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:                                     # a closed visit is frozen
        _q(c, "UPDATE emergency_visits SET clinical_notes = 'late note' WHERE id = %s", v["id"])
    assert "emergency_visit_closed" in str(e.value)

    dead = _register(c, w["clinic"], triage_category="black")
    out = _dispose(w, dead["id"], "deceased", doctor=w["doctor"])
    assert out["status"] == "deceased" and out["time_of_death"] and out["attending_doctor_id"] == w["doctor"]


def test_merge_moves_locked_and_signed_records_and_only_patient_id(_orders_world):  # noqa: F811
    w, c, clinic = _orders_world, _orders_world["c"], _orders_world["clinic"]
    v = _register(c, clinic)
    temp = v["patient_id"]
    _mlc(w, v["id"], lock=True)
    cert = _q(c, "SELECT docs_issue_certificate(%s, %s::jsonb, NULL)", clinic, json.dumps({
        "patient_id": temp, "doctor_id": w["doctor"], "certificate_type": "fitness", "diagnosis_text": "Fit",
        "doctor_snapshot": SIGNER, "patient_snapshot": {"name": "Unknown Male ~40y"}}))
    with pytest.raises(psycopg2.Error):                    # outside a merge an issued certificate never moves
        _q(c, "UPDATE clinical_certificates SET patient_id = %s WHERE id = %s", w["patient"], cert["id"])
    # a forged merge flag still cannot change anything else
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT set_config('kriya.patient_merge', %s, true)", (f"{clinic}:{temp}:{w['patient']}",))
        with pytest.raises(psycopg2.Error):
            cur.execute("UPDATE clinical_certificates SET patient_id = %s, diagnosis_text = 'Unfit' WHERE id = %s",
                        (w["patient"], cert["id"]))
    finally:
        cur.execute("ROLLBACK")

    with pytest.raises(psycopg2.Error) as e:               # a registered patient is never merged away
        _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', NULL)", clinic, w["patient"], temp)
    assert "emergency_merge_not_temporary" in str(e.value)
    out = _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', 'Wife identified him')", clinic, temp, w["patient"])
    assert out["moved"]["emergency_visits"] == 1 and out["moved"]["emergency_mlc_records"] == 1
    assert out["moved"]["clinical_certificates"] == 1 and out["merged_target_name"] == "Ravi Kumar"
    row = _rows(c, "SELECT patient_id::text, original_patient_id::text, is_unknown_patient FROM emergency_visits WHERE id = %s",
                v["id"])[0]
    assert row == (w["patient"], temp, False)
    mlc = _rows(c, "SELECT patient_id::text, is_locked, patient_snapshot ->> 'name' FROM emergency_mlc_records "
                   "WHERE emergency_visit_id = %s", v["id"])[0]
    assert mlc == (w["patient"], True, "Unknown Male ~40y")        # still locked, signed identity kept as recorded
    assert _q(c, "SELECT patient_id::text FROM clinical_certificates WHERE id = %s", cert["id"]) == w["patient"]
    assert _rows(c, "SELECT merged_into_patient_id::text, merged_at IS NOT NULL FROM patients WHERE id = %s", temp) \
        == [(w["patient"], True)]
    log = _rows(c, "SELECT source_snapshot ->> 'name', reason, moved ->> 'emergency_visits' FROM patient_merge_log "
                   "WHERE source_patient_id = %s", temp)
    assert log == [("Unknown Male ~40y", "Wife identified him", "1")]
    again = _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', NULL)", clinic, temp, w["patient"])
    assert again["replayed"] is True
    with pytest.raises(psycopg2.Error):                    # the log is append-only
        _q(c, "DELETE FROM patient_merge_log WHERE source_patient_id = %s", temp)


def test_merge_refuses_a_temporary_target(_orders_world):  # noqa: F811
    w, c = _orders_world, _orders_world["c"]
    a, b = _register(c, w["clinic"]), _register(c, w["clinic"], estimated_gender="female", estimated_age=25)
    with pytest.raises(psycopg2.Error) as e:
        _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', NULL)", w["clinic"], a["patient_id"], b["patient_id"])
    assert "emergency_merge_bad_target" in str(e.value)
    assert b["patient_name"] == "Unknown Female ~25y"


def test_rollback_refuses_data_then_reapplies(_orders_world):  # noqa: F811
    _register(_orders_world["c"], _orders_world["clinic"])
    cur = _orders_world["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "121_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_121(cur)   # only a purge-flagged delete removes ER records
        cur.execute("SELECT to_regclass('public.emergency_visits'), to_regprocedure('patient_merge_allows(oid,jsonb,jsonb)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname = ANY(%s) AND prosrc LIKE '%%patient_merge_allows%%'",
                    (list(GUARDS),))
        assert cur.fetchone()[0] == 0                       # originals restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")
