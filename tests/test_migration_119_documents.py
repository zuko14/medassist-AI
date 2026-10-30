"""Migration 119 (clinical documents) against real PostgreSQL.

Discharge-summary snapshots frozen at signing and immutable after, legacy
freeze-on-first-print, consent and certificate immutability, gapless
certificate numbers under concurrency, purge, and a guarded rollback +
idempotent re-apply. HTTP behaviour: tests/test_clinical_documents_e2e.py.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_121_emergency import unwind_121
from tests.test_migration_115_orders import _q, _rows, world as _orders_world  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "119_clinical_documents.sql"
DOWN = MIG / "rollback" / "119_down.sql"
SIGNER = {"name": "Dr. Rao", "registration_number": "TSMC/2001/9", "registration_council": "TSMC"}


@pytest.fixture
def stay(_orders_world):  # noqa: F811
    w = _orders_world
    c, clinic = w["c"], w["clinic"]
    _q(c, "UPDATE doctors SET registration_number = 'TSMC/2001/9', registration_council = 'TSMC' WHERE id = %s",
       w["doctor"])
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, %s, 'Ward') RETURNING id",
                  clinic, "W" + uuid.uuid4().hex[:5]))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B1', 'general') "
                    "RETURNING id", clinic, ward))
    admitted = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic,
                  json.dumps({"patient_id": w["patient"], "admitting_doctor_id": w["doctor"], "reason": "Fever"}), bed)
    w["adm"] = str(admitted["id"] if isinstance(admitted, dict) else admitted)
    return w


def _draft(w, version=1, supersedes=None, reason=None):
    return str(_q(w["c"], """INSERT INTO ipd_discharge_summaries (clinic_id, admission_id, version, supersedes_id,
                             final_diagnosis, course_in_hospital, amend_reason)
                             VALUES (%s, %s, %s, %s, 'Dengue fever', 'Recovered with fluids', %s) RETURNING id""",
                  w["clinic"], w["adm"], version, supersedes, reason))


def _sign(w, sid, lh=None, clinical=None):
    return _q(w["c"], "SELECT docs_sign_discharge_summary(%s, %s, %s, NULL, 'drrao', %s::jsonb, %s::jsonb)",
              w["clinic"], sid, w["doctor"], json.dumps(lh or {"clinic_name": "Orders Clinic"}),
              json.dumps(clinical or {"patient": {"name": "Ravi Kumar"}}))


def _cert(conn, w, key=None, **kw):
    row = {"patient_id": w["patient"], "doctor_id": w["doctor"], "certificate_type": "fitness",
           "diagnosis_text": "Fit for duty", "doctor_snapshot": SIGNER, **kw}
    return _q(conn, "SELECT docs_issue_certificate(%s, %s::jsonb, %s)", w["clinic"], json.dumps(row), key)


def test_schema_constraints_and_rls(real_pg_conn):
    names = {r[0] for r in _rows(real_pg_conn, """SELECT conname FROM pg_constraint WHERE conrelid IN
             ('clinical_consents'::regclass, 'clinical_certificates'::regclass)""")}
    assert {"clinical_consents_admission_fk", "clinical_consents_signed_consistent", "clinical_consents_proxy_witnessed",
            "clinical_certificates_seq_key", "clinical_certificates_signer", "clinical_certificates_leave_dates",
            "clinical_certificates_void_consistent"} <= names
    for t in ("clinical_consents", "clinical_certificates"):
        assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True


def test_signing_freezes_snapshots_and_the_signed_summary_is_immutable(stay):
    c, sid = stay["c"], _draft(stay)
    with pytest.raises(psycopg2.Error) as e:   # snapshots are written by the documents functions only
        _q(c, "UPDATE ipd_discharge_summaries SET letterhead_snapshot = '{}' WHERE id = %s", sid)
    assert "ipd_record_locked" in str(e.value)
    signed = _sign(stay, sid)
    assert signed["status"] == "signed" and signed["letterhead_snapshot"] == {"clinic_name": "Orders Clinic"}
    assert signed["doctor_snapshot"]["registration_number"] == "TSMC/2001/9"
    for sql in ("UPDATE ipd_discharge_summaries SET final_diagnosis = 'Typhoid' WHERE id = %s",
                "UPDATE ipd_discharge_summaries SET doctor_snapshot = '{}' WHERE id = %s",
                "UPDATE ipd_discharge_summaries SET letterhead_snapshot = '{\"clinic_name\": \"X\"}' WHERE id = %s",
                "DELETE FROM ipd_discharge_summaries WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, sid)
        assert "ipd_record_locked" in str(e.value)
    # delivery bookkeeping and the storage path (once) may still be written
    _q(c, "UPDATE ipd_discharge_summaries SET delivery_status = 'sent', send_count = 1 WHERE id = %s", sid)
    _q(c, "UPDATE ipd_discharge_summaries SET pdf_storage_path = 'a/b.pdf' WHERE id = %s", sid)
    with pytest.raises(psycopg2.Error):
        _q(c, "UPDATE ipd_discharge_summaries SET pdf_storage_path = 'c/d.pdf' WHERE id = %s", sid)


def test_an_amendment_supersedes_and_keeps_both_versions_printable(stay):
    v1 = _draft(stay)
    _sign(stay, v1)
    v2 = _draft(stay, version=2, supersedes=v1, reason="Corrected medications")
    _sign(stay, v2, clinical={"patient": {"name": "Ravi Kumar"}, "v": 2})
    assert _rows(stay["c"], "SELECT version, status, clinical_snapshot ? 'v' FROM ipd_discharge_summaries "
                            "WHERE admission_id = %s ORDER BY version", stay["adm"]) == \
        [(1, "superseded", False), (2, "signed", True)]


def test_a_summary_signed_before_119_is_frozen_on_first_print_only_once(stay):
    c, sid = stay["c"], _draft(stay)
    _q(c, "SELECT ipd_sign_discharge_summary(%s, %s, %s, NULL, 'drrao')", stay["clinic"], sid, stay["doctor"])
    assert _q(c, "SELECT letterhead_snapshot FROM ipd_discharge_summaries WHERE id = %s", sid) is None
    with pytest.raises(psycopg2.Error):   # outside the documents function a snapshot cannot be added
        _q(c, "UPDATE ipd_discharge_summaries SET clinical_snapshot = '{}' WHERE id = %s", sid)
    first = _q(c, "SELECT docs_freeze_discharge_summary(%s, %s, '{\"clinic_name\": \"A\"}', '{\"n\": 1}')",
               stay["clinic"], sid)
    again = _q(c, "SELECT docs_freeze_discharge_summary(%s, %s, '{\"clinic_name\": \"B\"}', '{\"n\": 2}')",
               stay["clinic"], sid)
    assert first["letterhead_snapshot"] == again["letterhead_snapshot"] == {"clinic_name": "A"}
    draft = _draft(stay, version=2, supersedes=sid, reason="Draft amendment")
    with pytest.raises(psycopg2.Error) as e:
        _q(c, "SELECT docs_freeze_discharge_summary(%s, %s, '{}', '{}')", stay["clinic"], draft)
    assert "docs_not_signed" in str(e.value)


def _consent(w, **kw):
    cols = {"clinic_id": w["clinic"], "admission_id": w["adm"], "patient_id": w["patient"],
            "consent_type": "surgical", "title": "Consent for surgery", "body_text": "I consent to the operation " * 2,
            "signer_name": "Ravi Kumar", "is_signed": True, "signed_by_name": "nurse", **kw}
    keys = list(cols)
    return str(_q(w["c"], f"INSERT INTO clinical_consents ({', '.join(keys)}, signed_at) "
                          f"VALUES ({', '.join(['%s'] * len(keys))}, now()) RETURNING id", *cols.values()))


def test_a_recorded_consent_never_changes(stay):
    c, cid = stay["c"], _consent(stay)
    for sql in ("UPDATE clinical_consents SET body_text = 'changed text that is long enough' WHERE id = %s",
                "UPDATE clinical_consents SET signer_name = 'Someone' WHERE id = %s",
                "DELETE FROM clinical_consents WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, cid)
        assert "docs_record_locked" in str(e.value)
    with pytest.raises(psycopg2.errors.CheckViolation):    # a representative signs only before a witness
        _consent(stay, signer_relationship="spouse", signer_name="Sita")
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('O', %s, 'polyclinic', true) "
                      "RETURNING id", "+9188" + uuid.uuid4().hex[:8]))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):   # another tenant's admission
        _consent(stay, clinic_id=other)


def test_certificate_numbers_are_gapless_and_issued_certificates_are_immutable(stay):
    c = stay["c"]
    a, b = _cert(c, stay), _cert(c, stay, key="cert-key-0001")
    assert b["certificate_seq"] == a["certificate_seq"] + 1
    assert b["certificate_number"] == f"CERT-{b['certificate_year']}-{b['certificate_seq']:05d}"
    replay = _cert(c, stay, key="cert-key-0001")
    assert replay["replayed"] is True and replay["id"] == b["id"]
    for sql in ("UPDATE clinical_certificates SET diagnosis_text = 'Unfit' WHERE id = %s",
                "UPDATE clinical_certificates SET doctor_snapshot = '{\"registration_number\": \"X\"}' WHERE id = %s",
                "DELETE FROM clinical_certificates WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(c, sql, a["id"])
        assert "docs_record_locked" in str(e.value)
    _q(c, "UPDATE clinical_certificates SET status = 'void', void_reason = 'Issued in error', voided_at = now() "
          "WHERE id = %s", a["id"])
    with pytest.raises(psycopg2.Error):   # void is final
        _q(c, "UPDATE clinical_certificates SET status = 'issued', void_reason = NULL, voided_at = NULL WHERE id = %s",
           a["id"])
    with pytest.raises(psycopg2.errors.CheckViolation):
        _cert(c, stay, certificate_type="medical_leave")                       # leave needs dates
    with pytest.raises(psycopg2.errors.CheckViolation):
        _cert(c, stay, doctor_snapshot={"name": "No Reg"})                      # unsigned


def test_ten_concurrent_certificates_take_ten_consecutive_numbers(stay, real_postgres_uri):
    out, errors, barrier = [], [], threading.Barrier(10)

    def worker():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            out.append(_cert(conn, stay)["certificate_seq"])
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


def test_owner_purge_removes_consents_and_unlinks_certificates(stay):
    _consent(stay)
    cert = _cert(stay["c"], stay, admission_id=stay["adm"])
    _q(stay["c"], "SELECT ipd_purge_clinic(%s)", stay["clinic"])
    assert _q(stay["c"], "SELECT count(*) FROM clinical_consents WHERE clinic_id = %s", stay["clinic"]) == 0
    assert _q(stay["c"], "SELECT admission_id FROM clinical_certificates WHERE id = %s", cert["id"]) is None


def test_rollback_refuses_documents_then_reapplies(stay):
    _cert(stay["c"], stay)
    cur = stay["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "119_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_121(cur)   # 121 references consents and certificates: newest rollback first
        # the shared test DB holds other suites' documents: only the owner purge may remove them
        for clinic in [r[0] for r in _rows(stay["c"], "SELECT DISTINCT clinic_id FROM clinical_certificates")] + \
                      [r[0] for r in _rows(stay["c"], "SELECT DISTINCT clinic_id FROM clinical_consents")]:
            cur.execute("SELECT set_config('kriya.opd_purge_clinic', %s, true)", (str(clinic),))
            cur.execute("DELETE FROM clinical_certificates WHERE clinic_id = %s", (clinic,))
            cur.execute("DELETE FROM clinical_consents WHERE clinic_id = %s", (clinic,))
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.clinical_consents'), to_regprocedure("
                    "'docs_issue_certificate(uuid,jsonb,text)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'ipd_discharge_summaries' "
                    "AND column_name = 'clinical_snapshot'")
        assert cur.fetchone()[0] == 0
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE proname LIKE 'docs\\_%' AND NOT EXISTS
                       (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
