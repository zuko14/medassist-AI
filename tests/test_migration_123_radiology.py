"""Migration 123 (radiology reports, PCPNDT Form F) against real PostgreSQL.

Schema shape and tenant-composite FKs, the PCPNDT text rules (and their parity
with the Python firewall), draft -> sign with the order completed atomically,
function-only writes and signed-report immutability, the obstetric rule (no
finalizing without a locked Form F, Form F never changes), versioned
amendments, gapless numbers under concurrency, an identity merge carrying
report and Form F along, and a guarded rollback + idempotent re-apply.
HTTP behaviour: tests/test_radiology_api_e2e.py.
"""

import json
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import pytest

from app.services.clinical_firewall import fetal_sex_disclosure
from app.services.radiology import is_obstetric_text
from tests.test_migration_115_orders import _place, _q, _rows, world as _orders_world  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "123_radiology_and_pcpndt.sql"
DOWN = MIG / "rollback" / "123_down.sql"
SIGNER = {"name": "Dr. Ray", "registration_number": "TSMC/2012/55", "registration_council": "TSMC"}
NORMAL = {"findings": "Lungs clear. Heart size normal.", "impression": "Normal chest radiograph."}


def unwind_123(cur) -> None:
    """Inside an open transaction: clear radiology data the only way the guards
    allow (purge flag) and run 123_down. Rollback tests of earlier migrations
    call this first (via unwind_122), since 123 references their tables."""
    from tests.test_migration_124_insurance import unwind_124
    unwind_124(cur)   # newest first: 124 sits on 111 / 116 / 117 tables
    cur.execute("SELECT to_regclass('public.radiology_reports') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT DISTINCT clinic_id FROM radiology_reports")
    for (clinic,) in cur.fetchall():
        cur.execute("SELECT set_config('kriya.opd_purge_clinic', %s, true)", (str(clinic),))
        cur.execute("DELETE FROM radiology_reports WHERE clinic_id = %s", (clinic,))
    cur.execute("SELECT set_config('kriya.opd_purge_clinic', '', true)")
    cur.execute(DOWN.read_text(encoding="utf-8"))


@pytest.fixture
def rad(_orders_world):  # noqa: F811
    w = _orders_world
    _q(w["c"], "UPDATE patients SET gender = 'female', age_years = 27, age_recorded_on = CURRENT_DATE WHERE id = %s", w["patient"])
    return w


def _order(w, name=None, code="XRAY_CHEST"):
    item = ({"order_type": "investigation", "catalog_type": "custom", "item_name": name} if name else
            {"order_type": "investigation", "catalog_type": "service_catalog", "catalog_code": code})
    return _place(w["c"], w, [item])["orders"][0]["id"]


def _save(conn, w, order, row=None, reason=None, expected=None, modality="x_ray"):
    body = {"modality": modality, **(NORMAL if row is None else row)}
    return _q(conn, "SELECT radiology_save_report(%s, %s, %s::jsonb, %s, %s, NULL, 'tech')",
              w["clinic"], order, json.dumps(body), reason, expected)


def _finalize(w, report, signer=SIGNER, actor=None):
    return _q(w["c"], "SELECT radiology_finalize_report(%s, %s, %s, %s, %s, %s::jsonb, '{}'::jsonb, '{}'::jsonb)",
              w["clinic"], report["id"], report["updated_at"], actor or str(uuid.uuid4()), w["doctor"],
              json.dumps(signer))


FORM = {"patient_age": 27, "relative_name": "Ramesh", "num_living_children": 1, "num_female_children": 1,
        "num_male_children": 0, "gestational_age_weeks": "20.0", "clinical_indication": "anomaly_scan_target",
        "pregnant_woman_declaration_signed": True, "doctor_declaration_signed": True}


def _form_f(w, report, **over):
    body = {**FORM, "last_menstrual_period": str(date.today() - timedelta(weeks=20)), **over}
    return _q(w["c"], "SELECT radiology_record_form_f(%s, %s, %s::jsonb, %s, %s::jsonb, NULL)",
              w["clinic"], report["id"], json.dumps(body), w["doctor"], json.dumps(SIGNER))


def _usg(w):
    order = _order(w, "USG Obstetric Level II (anomaly scan)")
    return order, _save(w["c"], w, order, {"findings": "Single live intrauterine gestation. Placenta posterior.",
                                           "impression": "Live pregnancy of 20 weeks."}, modality="ultrasound")


# ── schema ───────────────────────────────────────────────────────────────────

def test_schema_constraints_and_rls(real_pg_conn):
    names = {r[0] for r in _rows(real_pg_conn, """SELECT conname FROM pg_constraint WHERE conrelid IN
             ('radiology_reports'::regclass, 'pcpndt_form_f_records'::regclass)""")}
    assert {"radiology_reports_order_fk", "radiology_reports_patient_fk", "radiology_reports_doctor_fk",
            "radiology_reports_finalized_consistent", "radiology_reports_obstetric_shape",
            "radiology_reports_no_fetal_sex", "radiology_reports_seq_key", "pcpndt_form_f_records_report_fk",
            "pcpndt_form_f_records_order_key", "pcpndt_form_f_records_children", "pcpndt_form_f_records_signer",
            "pcpndt_form_f_records_no_fetal_sex"} <= names
    for t in ("radiology_reports", "pcpndt_form_f_records"):
        assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True
    for f in ("radiology_save_report(uuid,uuid,jsonb,text,timestamptz,uuid,text)",
              "radiology_finalize_report(uuid,uuid,timestamptz,uuid,uuid,jsonb,jsonb,jsonb)"):
        assert _q(real_pg_conn, "SELECT has_function_privilege('anon', %s, 'EXECUTE')", f) is False


# ── PCPNDT text rules: SQL and Python agree ──────────────────────────────────

FETAL_SEX_CORPUS = [
    ("Single live male fetus of 20 weeks.", False, True),
    ("Female foetus with normal biometry", False, True),
    ("Sex of the baby not commented upon", False, True),
    ("fetal gender could not be assessed", False, True),
    ("Sex determination was requested and refused", False, True),
    ("बच्चा लड़का है", False, True),
    ("అబ్బాయి", False, True),
    ("Normal male pelvis. Prostate 18 cc.", False, False),           # a man's own anatomy, not obstetric
    ("Both testes normal in size. Scrotal wall normal.", False, False),
    ("Female patient. Uterus anteverted.", False, False),
    ("External genitalia normal", True, True),                       # obstetric: zero tolerance
    ("Gender: not disclosed", True, True),
    ("It's a boy", True, True),
    ("Scrotum seen", True, True),
    ("Single live intrauterine gestation. Placenta posterior, upper segment.", True, False),
    ("Liquor adequate. AFI 12 cm. No anomaly detected.", True, False),
    ("Essex road", True, False),                                     # word boundaries hold
]


@pytest.mark.parametrize("text,obstetric,blocked", FETAL_SEX_CORPUS)
def test_fetal_sex_rule_matches_the_python_firewall(real_pg_conn, text, obstetric, blocked):
    assert _q(real_pg_conn, "SELECT radiology_fetal_sex_text(%s, %s)", text, obstetric) is blocked
    assert (fetal_sex_disclosure(text, obstetric) is not None) is blocked


@pytest.mark.parametrize("text,obstetric", [
    ("USG Obstetric (Level II)", True), ("NT scan", True), ("Antenatal growth scan", True),
    ("USG abdomen and pelvis", False), ("Scrotal doppler", False), ("X-ray chest PA", False),
    ("Early pregnancy viability scan", True), ("TIFFA", True)])
def test_obstetric_rule_matches_python(real_pg_conn, text, obstetric):
    assert _q(real_pg_conn, "SELECT radiology_is_obstetric_text(%s)", text) is obstetric
    assert is_obstetric_text(text) is obstetric


# ── draft -> sign ────────────────────────────────────────────────────────────

def test_finalize_completes_the_order_atomically_and_freezes_the_report(rad):
    order = _order(rad)
    r = _save(rad["c"], rad, order, {"findings": "", "impression": ""})
    assert (r["status"], r["version"], r["created"]) == ("draft", 1, True)
    assert r["report_number"].startswith("RAD-") and r["procedure_name"] == "X-ray chest PA"
    with pytest.raises(psycopg2.Error) as e:          # empty findings never get signed
        _finalize(rad, r)
    assert "radiology_report_incomplete" in str(e.value)
    r2 = _save(rad["c"], rad, order, expected=r["updated_at"])
    assert r2["id"] == r["id"] and r2["created"] is False
    with pytest.raises(psycopg2.Error) as e:          # stale editor
        _save(rad["c"], rad, order, expected=r["updated_at"])
    assert "radiology_report_changed" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _finalize(rad, r2, signer={"name": "Dr. NoReg", "registration_number": " "})
    assert "radiology_signer_registration_missing" in str(e.value)

    actor = str(uuid.uuid4())
    signed = _finalize(rad, r2, actor=actor)
    assert (signed["status"], signed["replayed"]) == ("finalized", False)
    assert signed["radiologist_snapshot"]["registration_number"] == "TSMC/2012/55"
    o = _rows(rad["c"], "SELECT status, result_summary FROM opd_orders WHERE id = %s", order)[0]
    assert o[0] == "completed" and o[1].startswith(signed["report_number"])
    assert _finalize(rad, r2, actor=actor)["replayed"] is True        # double click

    for sql in ("UPDATE radiology_reports SET findings = 'edited' WHERE id = %s",
                "UPDATE radiology_reports SET radiologist_snapshot = '{}'::jsonb WHERE id = %s",
                "DELETE FROM radiology_reports WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(rad["c"], sql, signed["id"])
        assert "radiology_report_locked" in str(e.value) or "radiology_append_only" in str(e.value)
    # the one later write: where the delivered PDF lives, once
    _q(rad["c"], "UPDATE radiology_reports SET pdf_storage_path = 'p/1.pdf' WHERE id = %s", signed["id"])
    with pytest.raises(psycopg2.Error):
        _q(rad["c"], "UPDATE radiology_reports SET pdf_storage_path = 'p/2.pdf' WHERE id = %s", signed["id"])


def test_writes_go_through_the_functions_only(rad):
    order = _order(rad)
    r = _save(rad["c"], rad, order)
    with pytest.raises(psycopg2.Error) as e:
        _q(rad["c"], "UPDATE radiology_reports SET findings = 'sneaky' WHERE id = %s", r["id"])
    assert "radiology_via_function_only" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _q(rad["c"], """INSERT INTO radiology_reports (clinic_id, order_id, patient_id, report_year, report_seq, modality,
                        procedure_name, drafted_by_name) VALUES (%s, %s, %s, 2026, 999, 'x_ray', 'X', 'x')""",
           rad["clinic"], order, rad["patient"])
    assert "radiology_via_function_only" in str(e.value)


def test_lab_cancelled_and_text_closed_orders_have_no_report(rad):
    lab = _place(rad["c"], rad, [{"order_type": "investigation", "catalog_type": "lab_test",
                                  "catalog_reference_id": rad["cbc"]}])["orders"][0]["id"]
    with pytest.raises(psycopg2.Error) as e:
        _save(rad["c"], rad, lab)
    assert "radiology_order_not_imaging" in str(e.value)
    closed = _order(rad)
    _q(rad["c"], """UPDATE opd_orders SET status = 'completed', resulted_at = now(), result_summary = 'NAD'
                    WHERE id = %s""", closed)
    with pytest.raises(psycopg2.Error) as e:
        _save(rad["c"], rad, closed)
    assert "radiology_order_closed" in str(e.value)


def test_pcpndt_text_is_refused_by_the_database(rad):
    order = _order(rad)
    with pytest.raises(psycopg2.Error) as e:
        _save(rad["c"], rad, order, {"findings": "Single live male fetus", "impression": "Normal"})
    assert "radiology_pcpndt_violation" in str(e.value)
    o, r = _usg(rad)
    with pytest.raises(psycopg2.Error) as e:   # obstetric draft: any genital word is refused
        _save(rad["c"], rad, o, {"findings": "External genitalia seen", "impression": "Normal"},
              expected=r["updated_at"], modality="ultrasound")
    assert "radiology_pcpndt_violation" in str(e.value)


# ── obstetric ultrasound and Form F ──────────────────────────────────────────

def test_obstetric_ultrasound_needs_a_locked_form_f(rad):
    order, r = _usg(rad)
    assert r["is_obstetric"] is True                       # forced by the procedure name
    with pytest.raises(psycopg2.Error) as e:
        _finalize(rad, r)
    assert "radiology_form_f_required" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _form_f(rad, r, last_menstrual_period=str(date.today() + timedelta(days=1)))
    assert "radiology_form_f_bad_lmp" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _form_f(rad, r, num_male_children=1)              # 1 + 1 children != 1 living
    assert "pcpndt_form_f_records_children" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:
        _form_f(rad, r, doctor_declaration_signed=False)
    assert "radiology_form_f_declarations" in str(e.value)

    f = _form_f(rad, r)
    assert f["form_f_number"].startswith("PNDT-") and f["is_locked"] is True and f["replayed"] is False
    assert _form_f(rad, r)["id"] == f["id"]                # a repeat returns the recorded form
    for sql in ("UPDATE pcpndt_form_f_records SET patient_age = 30 WHERE id = %s",
                "UPDATE pcpndt_form_f_records SET is_locked = false WHERE id = %s",
                "DELETE FROM pcpndt_form_f_records WHERE id = %s"):
        with pytest.raises(psycopg2.Error) as e:
            _q(rad["c"], sql, f["id"])
        assert "radiology_form_f_locked" in str(e.value) or "violates check" in str(e.value)
    # the study stays obstetric once Form F exists
    with pytest.raises(psycopg2.Error) as e:
        _q(rad["c"], "SELECT radiology_save_report(%s, %s, %s::jsonb, NULL, NULL, NULL, 'tech')", rad["clinic"],
           order, json.dumps({"modality": "ultrasound", "procedure_name": "USG pelvis", "findings": "x",
                              "impression": "y", "is_obstetric": False}))
    assert "radiology_form_f_fixes_obstetric" in str(e.value)

    signed = _finalize(rad, r)
    assert signed["status"] == "finalized"
    assert _q(rad["c"], "SELECT status FROM opd_orders WHERE id = %s", order) == "completed"


def test_form_f_only_for_an_obstetric_draft(rad):
    r = _save(rad["c"], rad, _order(rad))
    with pytest.raises(psycopg2.Error) as e:
        _form_f(rad, r)
    assert "radiology_form_f_not_obstetric" in str(e.value)
    with pytest.raises(psycopg2.Error) as e:      # obstetric is an ultrasound only
        _save(rad["c"], rad, _order(rad), {**NORMAL, "is_obstetric": True})
    assert "radiology_obstetric_needs_ultrasound" in str(e.value)


# ── amendments ───────────────────────────────────────────────────────────────

def test_amendment_is_a_new_version_with_the_same_number(rad):
    order = _order(rad)
    v1 = _finalize(rad, _save(rad["c"], rad, order))
    with pytest.raises(psycopg2.Error) as e:
        _save(rad["c"], rad, order)
    assert "radiology_amendment_reason_required" in str(e.value)
    v2 = _save(rad["c"], rad, order, {**NORMAL, "impression": "Mild cardiomegaly."}, reason="Missed cardiomegaly")
    assert (v2["version"], v2["supersedes_id"], v2["report_number"]) == (2, v1["id"], v1["report_number"])
    signed = _finalize(rad, v2)
    assert signed["status"] == "finalized"
    assert _q(rad["c"], "SELECT status FROM radiology_reports WHERE id = %s", v1["id"]) == "amended"
    # the order kept the first report's completion
    assert _q(rad["c"], "SELECT result_summary FROM opd_orders WHERE id = %s", order).endswith("Normal chest radiograph.")


# ── concurrency ──────────────────────────────────────────────────────────────

def test_concurrent_first_saves_get_gapless_numbers_and_one_draft_per_order(rad, real_postgres_uri):
    orders_ = [_order(rad) for _ in range(10)]
    same = _order(rad)
    barrier, results, errors = threading.Barrier(20), [], []

    def run(order):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            results.append(_save(conn, rad, order))
        except psycopg2.Error as e:
            errors.append(str(e))
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(o,)) for o in orders_ + [same] * 10]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    seqs = sorted({r["report_seq"] for r in results})
    assert len(seqs) == 11 and seqs == list(range(seqs[0], seqs[0] + 11))      # gapless, no duplicates
    assert _q(rad["c"], "SELECT count(*) FROM radiology_reports WHERE order_id = %s", same) == 1


# ── identity merge ───────────────────────────────────────────────────────────

def test_emergency_identity_merge_carries_report_and_form_f(rad):
    c, clinic = rad["c"], rad["clinic"]
    temp = str(_q(c, """INSERT INTO patients (clinic_id, name, phone, gender, is_temporary)
                        VALUES (%s, 'Unknown Female', %s, 'female', true) RETURNING id""",
                  clinic, "+9195" + uuid.uuid4().hex[:8]))
    appt = str(_q(c, """INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id,
                        doctor_name, department, appointment_date, appointment_time, status, is_walk_in, booking_type)
                        VALUES (%s, %s, '+919000000002', 'Unknown', %s, 'Dr. Rao', 'Medicine', '2026-11-03',
                                %s, 'completed', false, 'consultation') RETURNING id""",
                  clinic, temp, rad["doctor"], f"{9 + uuid.uuid4().int % 9:02d}:{uuid.uuid4().int % 60:02d}:00"))
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) "
                    "VALUES (%s, %s, %s, %s) RETURNING id", clinic, appt, temp, rad["doctor"]))
    order, r = _usg({**rad, "enc": enc})
    f = _form_f(rad, r)
    signed = _finalize(rad, r)
    _q(c, "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', 'identified')", clinic, temp, rad["patient"])
    assert _q(c, "SELECT patient_id::text FROM radiology_reports WHERE id = %s", signed["id"]) == rad["patient"]
    assert _q(c, "SELECT patient_id::text FROM pcpndt_form_f_records WHERE id = %s", f["id"]) == rad["patient"]
    # and nothing else moved: the signed text is untouched
    assert _q(c, "SELECT findings FROM radiology_reports WHERE id = %s", signed["id"]) == signed["findings"]


# ── purge + rollback ─────────────────────────────────────────────────────────

def test_owner_purge_removes_reports_and_forms(rad):
    order, r = _usg(rad)
    _form_f(rad, r)
    _q(rad["c"], "SELECT ipd_purge_clinic(%s)", rad["clinic"])
    assert _q(rad["c"], "SELECT count(*) FROM radiology_reports WHERE clinic_id = %s", rad["clinic"]) == 0
    assert _q(rad["c"], "SELECT count(*) FROM pcpndt_form_f_records WHERE clinic_id = %s", rad["clinic"]) == 0


def test_rollback_refuses_statutory_records_then_reapplies(rad):
    _save(rad["c"], rad, _order(rad))
    cur = rad["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "123_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_123(cur)
        cur.execute("SELECT to_regclass('public.radiology_reports'), to_regclass('public.pcpndt_form_f_records'), "
                    "to_regclass('public.uq_opd_orders_clinic_id_patient')")
        assert cur.fetchone() == (None, None, None)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")
