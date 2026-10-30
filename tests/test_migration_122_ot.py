"""Migration 122 (operation theatre, G8) against real PostgreSQL.

Booking needs an active admission; the database refuses a double booking of a
theatre or a patient, also under concurrency (READ COMMITTED and REPEATABLE
READ); OT numbers are gapless; the WHO checklist runs strictly in order; the
anaesthesia record locks on signing; completion is refused until the checklist
is complete and then posts exactly one charge; an identity merge moves the
surgery with its admission; the owner purge removes it; rollback refuses data,
then re-applies. HTTP behaviour: tests/test_ot_api_e2e.py.
"""

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import _q, _rows

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "122_operation_theatre.sql"
DOWN = MIG / "rollback" / "122_down.sql"
SIGNER = {"doctor_id": None, "name": "Dr. Anaes", "registration_number": "TSMC/2010/77"}
ALL = {
    "sign_in": ["patient_identity_confirmed", "site_marked", "anaesthesia_machine_checked",
                "pulse_oximeter_functioning", "allergy_checked", "airway_risk_assessed", "blood_loss_risk_assessed"],
    "time_out": ["team_members_introduced", "patient_name_procedure_confirmed", "anticipated_critical_events",
                 "antibiotic_prophylaxis_given", "essential_imaging_displayed"],
    "sign_out": ["procedure_recorded", "instrument_swab_needle_count_correct", "specimen_labelled",
                 "equipment_problems_addressed", "key_recovery_concerns_reviewed"],
}
BASE = datetime(2027, 3, 1, 3, 30, tzinfo=timezone.utc)   # 09:00 IST


def unwind_122(cur) -> None:
    """Inside an open transaction: clear OT data the only way the guards allow
    (purge flag) and run 122_down. Rollback tests of earlier migrations call
    this first (via unwind_121), since 122 references their tables."""
    from tests.test_migration_123_radiology import unwind_123
    unwind_123(cur)   # newest first: 123 references 115's orders
    cur.execute("SELECT to_regclass('public.ot_surgeries') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT DISTINCT clinic_id FROM ot_surgeries")
    for (clinic,) in cur.fetchall():
        cur.execute("SELECT set_config('kriya.ipd_purge_clinic', %s, true)", (str(clinic),))
        cur.execute("DELETE FROM ot_surgeries WHERE clinic_id = %s", (clinic,))
    cur.execute("SELECT set_config('kriya.ipd_purge_clinic', '', true)")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _patient(c, clinic, name="Sita Devi"):
    return str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, %s, %s) RETURNING id",
                  clinic, name, "+9187" + uuid.uuid4().hex[:8]))


def _admit(c, w, patient=None):
    patient = patient or _patient(c, w["clinic"])
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, %s, 'general') "
                    "RETURNING id", w["clinic"], w["ward"], "B" + uuid.uuid4().hex[:6]))
    adm = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", w["clinic"],
             json.dumps({"patient_id": patient, "admitting_doctor_id": w["doctor"], "reason": "Cholelithiasis"}), bed)
    return adm["id"]


@pytest.fixture
def ot(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, ipd_settings)
                          VALUES ('OT Hospital', %s, 'polyclinic', true, '{"bed_tariffs": {"general": 100000}}')
                          RETURNING id""", "+9186" + uuid.uuid4().hex[:8]))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active, registration_number)
                          VALUES (%s, 'Dr. Iyer', 'Surgery', 'Surgeon', true, 'KMC/1/1') RETURNING id""", clinic))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, 'W1', 'Ward') RETURNING id", clinic))
    theatre = str(_q(c, "INSERT INTO ot_theatres (clinic_id, name) VALUES (%s, 'OT 1') RETURNING id", clinic))
    theatre2 = str(_q(c, "INSERT INTO ot_theatres (clinic_id, name, theatre_type) VALUES (%s, 'OT 2', 'minor') "
                         "RETURNING id", clinic))
    service = str(_q(c, """INSERT INTO billable_services (clinic_id, service_code, name, category, standard_price_paise)
                           VALUES (%s, 'LAP_CHOLE', 'Laparoscopic cholecystectomy', 'procedure', 4500000)
                           RETURNING id""", clinic))
    w = {"c": c, "clinic": clinic, "doctor": doctor, "ward": ward, "theatre": theatre, "theatre2": theatre2,
         "service": service}
    w["adm"] = _admit(c, w)
    return w


def _book(w, start_h=0.0, hours=2.0, theatre=None, adm=None, key=None, conn=None, **extra):
    start = BASE + timedelta(hours=start_h)
    body = {"admission_id": adm or w["adm"], "theatre_id": theatre or w["theatre"],
            "procedure_name": "Laparoscopic cholecystectomy", "service_id": w["service"],
            "scheduled_start": start.isoformat(), "scheduled_end": (start + timedelta(hours=hours)).isoformat(),
            "primary_surgeon_id": w["doctor"], "pre_op_diagnosis": "Symptomatic gallstones",
            "idempotency_key": key or uuid.uuid4().hex, **extra}
    return _q(conn or w["c"], "SELECT ot_schedule_surgery(%s, %s::jsonb, NULL, 'desk')", w["clinic"], json.dumps(body))


def _check(w, sid, phase, items=None):
    items = items if items is not None else {k: True for k in ALL[phase]}
    return _q(w["c"], "SELECT ot_update_checklist(%s, %s, %s, %s::jsonb, NULL, 'nurse')",
              w["clinic"], sid, phase, json.dumps(items))


def _status(w, sid, action, reason=None):
    return _q(w["c"], "SELECT ot_set_status(%s, %s, %s, %s, NULL, 'nurse')", w["clinic"], sid, action, reason)


def _complete(w, sid, amount=None, note="Gall bladder removed, no spillage."):
    return _q(w["c"], "SELECT ot_complete_surgery(%s, %s, %s::jsonb, %s, NULL, 'driyer')", w["clinic"], sid,
              json.dumps({"procedure_notes": note, "post_op_diagnosis": "Cholelithiasis"}), amount)


def _err(fn) -> str:
    with pytest.raises(psycopg2.Error) as e:
        fn()
    return str(e.value)


def test_schema_rls_and_tenant_tables(real_pg_conn):
    c = real_pg_conn
    names = {r[0] for r in _rows(c, """SELECT conname FROM pg_constraint WHERE conrelid IN ('ot_theatres'::regclass,
             'ot_surgeries'::regclass, 'ot_who_checklists'::regclass, 'ot_anaesthesia_records'::regclass)""")}
    assert {"ot_theatres_name_key", "ot_surgeries_seq_key", "ot_surgeries_times", "ot_surgeries_admission_fk",
            "ot_surgeries_service_fk", "ot_who_checklists_surgery_key", "ot_who_checklists_sign_out",
            "ot_anaesthesia_records_signed"} <= names
    from app.tenancy import TENANT_OWNED_TABLES
    for t in ("ot_theatres", "ot_surgeries", "ot_who_checklists", "ot_anaesthesia_records"):
        assert _q(c, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True
        assert t in TENANT_OWNED_TABLES


def test_booking_numbers_checklist_replay_and_admission_rule(ot):
    s = _book(ot, key="book-key-0001")
    assert s["replayed"] is False and s["status"] == "scheduled" and s["patient_id"]
    assert s["surgery_number"].startswith("OT-") and len(s["surgery_number"]) == len("OT-2026-00001")
    assert _q(ot["c"], "SELECT count(*) FROM ot_who_checklists WHERE surgery_id = %s", s["id"]) == 1
    again = _book(ot, key="book-key-0001")
    assert again["replayed"] is True and again["id"] == s["id"]
    other = _admit(ot["c"], ot)
    assert "ot_key_reused" in _err(lambda: _book(ot, 5, adm=other, key="book-key-0001"))
    assert "ot_surgeries_times" in _err(lambda: _book(ot, 0, hours=0))                   # end > start
    assert "ot_surgeries_times" in _err(lambda: _book(ot, 30, hours=25))
    _q(ot["c"], "UPDATE ot_theatres SET is_active = false WHERE id = %s", ot["theatre2"])
    assert "ot_theatre_inactive" in _err(lambda: _book(ot, 10, theatre=ot["theatre2"]))
    # an admission that is not admitted (cancelled pending stay) cannot be booked
    pending = _q(ot["c"], "SELECT ipd_admit(%s, %s::jsonb, NULL, NULL, 'desk')", ot["clinic"], json.dumps(
        {"patient_id": _patient(ot["c"], ot["clinic"]), "admitting_doctor_id": ot["doctor"], "reason": "Waiting"}))
    assert "ot_admission_not_active" in _err(lambda: _book(ot, 40, adm=pending["id"]))


def test_no_double_booking_of_a_theatre_or_a_patient(ot):
    first = _book(ot, 0, 2)
    other = _admit(ot["c"], ot)
    assert "ot_theatre_busy" in _err(lambda: _book(ot, 1, 2, adm=other))                 # overlaps 10:00-12:00
    assert _book(ot, 2, 1, adm=other)["status"] == "scheduled"                           # back-to-back is fine
    assert "ot_patient_busy" in _err(lambda: _book(ot, 1, 1, theatre=ot["theatre2"]))     # same patient, other OT
    _status(ot, first["id"], "postpone", "Patient not fasting")                          # frees the slot
    assert _book(ot, 0.5, 1, theatre=ot["theatre"], adm=_admit(ot["c"], ot))["status"] == "scheduled"
    # direct writes cannot slip past the functions
    assert "ot_via_function_only" in _err(lambda: _q(ot["c"], "UPDATE ot_surgeries SET scheduled_start = "
                                                     "scheduled_start - interval '1 hour' WHERE id = %s", first["id"]))
    assert "ot_append_only" in _err(lambda: _q(ot["c"], "DELETE FROM ot_surgeries WHERE id = %s", first["id"]))
    assert "ot_via_function_only" in _err(lambda: _q(ot["c"], "INSERT INTO ot_who_checklists (clinic_id, surgery_id) "
                                                     "VALUES (%s, %s)", ot["clinic"], first["id"]))


def test_ten_concurrent_overlapping_bookings_admit_exactly_one(ot, real_postgres_uri):
    adms = [_admit(ot["c"], ot) for _ in range(10)]
    barrier, results = threading.Barrier(10), []

    def run(i):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            results.append(_book(ot, 100 + i * 0.1, 2, adm=adms[i], conn=conn)["surgery_seq"])
        except psycopg2.Error as e:
            results.append(str(e).splitlines()[0])
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(isinstance(r, int) for r in results) == 1, results
    assert sum("ot_theatre_busy" in str(r) for r in results) == 9, results


def test_ten_concurrent_bookings_take_consecutive_numbers(ot, real_postgres_uri):
    adms = [_admit(ot["c"], ot) for _ in range(10)]
    barrier, seqs = threading.Barrier(10), []

    def run(i):
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            seqs.append(_book(ot, 200 + i * 3, 2, adm=adms[i], conn=conn)["surgery_seq"])
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(seqs) == 10 and sorted(seqs) == list(range(min(seqs), min(seqs) + 10))   # gapless, no duplicates


def test_repeatable_read_race_fails_instead_of_double_booking(ot, real_postgres_uri):
    a2 = _admit(ot["c"], ot)
    c1, c2 = psycopg2.connect(real_postgres_uri), psycopg2.connect(real_postgres_uri)
    for conn in (c1, c2):
        conn.set_session(isolation_level="REPEATABLE READ")
    try:
        _q(c1, "SELECT count(*) FROM ot_surgeries")            # both snapshots taken before either booking
        _q(c2, "SELECT count(*) FROM ot_surgeries")
        _book(ot, 300, 2, conn=c1)
        out = []
        t = threading.Thread(target=lambda: out.append(_err(lambda: _book(ot, 301, 2, adm=a2, conn=c2))))
        t.start()
        c1.commit()
        t.join(30)
        assert out and ("could not serialize" in out[0] or "ot_theatre_busy" in out[0]), out
    finally:
        c2.rollback()
        c1.close()
        c2.close()
    assert _q(ot["c"], "SELECT count(*) FROM ot_surgeries WHERE clinic_id = %s AND scheduled_start >= %s",
              ot["clinic"], (BASE + timedelta(hours=299)).isoformat()) == 1


def test_who_checklist_runs_in_order_and_locks(ot):
    sid = _book(ot)["id"]
    assert "ot_checklist_order:sign_in" in _err(lambda: _check(ot, sid, "time_out"))
    part = _check(ot, sid, "sign_in", {"patient_identity_confirmed": True, "site_marked": True})
    assert part["sign_in_completed"] is False and part["site_marked"] is True
    assert "ot_checklist_bad_item" in _err(lambda: _check(ot, sid, "sign_in", {"specimen_labelled": True}))
    assert "ot_checklist_bad_item" in _err(lambda: _check(ot, sid, "sign_in", {"site_marked": "yes"}))
    assert "ot_checklist_bad_phase" in _err(lambda: _check(ot, sid, "pre_op", {}))
    done = _check(ot, sid, "sign_in")
    assert done["sign_in_completed"] is True and done["sign_in_at"] and done["sign_in_by_name"] == "nurse"
    assert _check(ot, sid, "sign_in", {"site_marked": False})["replayed"] is True       # a completed phase is final
    assert _q(ot["c"], "SELECT site_marked FROM ot_who_checklists WHERE surgery_id = %s", sid) is True
    assert "ot_checklist_order:time_out" in _err(lambda: _check(ot, sid, "sign_out"))
    _check(ot, sid, "time_out")
    assert _check(ot, sid, "sign_out")["sign_out_completed"] is True
    # the table's own constraint: no completed phase with an unticked item, whatever the path
    assert "ot_who_checklists_sign_in" in _err(lambda: _q(ot["c"], """
        SELECT set_config('kriya.ot_tx', %s, false);
        UPDATE ot_who_checklists SET site_marked = false WHERE surgery_id = %s""", ot["clinic"], sid))
    _q(ot["c"], "SELECT set_config('kriya.ot_tx', '', false)")


def test_anaesthesia_record_locks_when_signed(ot):
    sid = _book(ot)["id"]
    r = _q(ot["c"], "SELECT ot_record_anaesthesia(%s, %s, %s::jsonb, false, NULL, NULL, NULL)", ot["clinic"], sid,
           json.dumps({"mallampati": "II", "vital_signs_log": [{"time": "09:05", "pulse": 82}]}))
    assert r["mallampati"] == "II" and r["vital_signs_log"][0]["pulse"] == 82 and r["is_signed"] is False
    signer = {**SIGNER, "doctor_id": ot["doctor"]}
    sign = lambda payload: _q(ot["c"], "SELECT ot_record_anaesthesia(%s, %s, %s::jsonb, true, %s, %s::jsonb, NULL)",  # noqa: E731
                              ot["clinic"], sid, json.dumps(payload), ot["doctor"], json.dumps(signer))
    assert "ot_anaesthesia_incomplete" in _err(lambda: sign({}))
    assert "ot_doctor_required" in _err(lambda: _q(ot["c"], "SELECT ot_record_anaesthesia(%s, %s, '{}', true, "
                                                   "NULL, NULL, NULL)", ot["clinic"], sid))
    assert "ot_bad_field" in _err(lambda: sign({"is_signed": True}))
    out = sign({"asa_status": "IIE", "technique": "general", "blood_loss_ml": 150})
    assert out["is_signed"] and out["anaesthetist_id"] == ot["doctor"] and out["blood_loss_ml"] == 150
    assert "ot_anaesthesia_signed" in _err(lambda: sign({"blood_loss_ml": 10}))
    assert "ot_via_function_only" in _err(lambda: _q(ot["c"], "UPDATE ot_anaesthesia_records SET blood_loss_ml = 1 "
                                                     "WHERE surgery_id = %s", sid))


def test_status_transitions(ot):
    sid = _book(ot)["id"]
    assert "ot_bad_transition" in _err(lambda: _status(ot, sid, "recovery"))
    assert "ot_reason_required" in _err(lambda: _status(ot, sid, "cancel", " x "))
    started = _status(ot, sid, "start")
    assert started["status"] == "in_theatre" and started["actual_start"]
    assert _status(ot, sid, "start")["replayed"] is True
    assert "ot_bad_transition" in _err(lambda: _status(ot, sid, "postpone", "Late"))
    assert _status(ot, sid, "recovery")["status"] == "recovery"
    sid2 = _book(ot, 10)["id"]
    cancelled = _status(ot, sid2, "cancel", "Patient withdrew consent")
    assert cancelled["status"] == "cancelled" and cancelled["cancellation_reason"] == "Patient withdrew consent"
    assert "ot_surgery_closed" in _err(lambda: _check(ot, sid2, "sign_in"))


def test_completion_gate_and_exactly_one_charge(ot):
    sid = _book(ot)["id"]
    assert "ot_bad_transition" in _err(lambda: _complete(ot, sid))                     # not started
    _status(ot, sid, "start")
    assert "ot_checklist_incomplete" in _err(lambda: _complete(ot, sid))
    _check(ot, sid, "sign_in")
    _check(ot, sid, "time_out")
    assert "ot_checklist_incomplete" in _err(lambda: _complete(ot, sid))
    _check(ot, sid, "sign_out")
    assert "ot_note_required" in _err(lambda: _complete(ot, sid, note=" "))
    out = _complete(ot, sid)
    assert out["replayed"] is False and out["surgery"]["status"] == "completed" and out["surgery"]["actual_end"]
    ch = out["charge"]
    assert (ch["category"], ch["source"], ch["status"], ch["unit_price_paise"]) == ("procedure", "order", "posted", 4500000)
    assert ch["description"] == "Surgical Procedure: Laparoscopic cholecystectomy"
    assert ch["source_ref"] == f"ot_surgery:{sid}" and out["surgery"]["charge_id"] == ch["id"]
    again = _complete(ot, sid, amount=1)
    assert again["replayed"] is True and again["charge"]["id"] == ch["id"]
    assert _q(ot["c"], "SELECT count(*) FROM ipd_charges WHERE admission_id = %s AND source_ref = %s",
              ot["adm"], f"ot_surgery:{sid}") == 1
    assert "ot_surgery_closed" in _err(lambda: _q(ot["c"], """
        SELECT set_config('kriya.ot_tx', %s, false);
        UPDATE ot_surgeries SET procedure_notes = 'edited' WHERE id = %s""", ot["clinic"], sid))
    _q(ot["c"], "SELECT set_config('kriya.ot_tx', '', false)")


def _ready(w, adm=None, start_h=0.0, **extra):
    sid = _book(w, start_h, adm=adm or w["adm"], **extra)["id"]
    _status(w, sid, "start")
    for p in ("sign_in", "time_out", "sign_out"):
        _check(w, sid, p)
    return sid


def test_manual_amount_service_less_and_final_bill_rule(ot):
    sid = _ready(ot, service_id=None)
    assert "ot_charge_amount_required" in _err(lambda: _complete(ot, sid))
    assert _complete(ot, sid, amount=0)["charge"]["unit_price_paise"] == 0                # explicit zero is allowed
    # a stay whose final bill exists never gets a charge outside it. The discharge guard already
    # keeps an open surgery from reaching a final bill, so this checks the second line of defence
    # with that guard switched off, inside a transaction that is rolled back.
    adm = _admit(ot["c"], ot)
    sid2 = _ready(ot, adm=adm, start_h=20)
    c = ot["c"]
    _q(c, "BEGIN")
    try:
        _q(c, "ALTER TABLE ipd_admissions DISABLE TRIGGER trg_ipd_admissions_ot_open")
        _q(c, "SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", ot["clinic"], adm)
        _q(c, "SELECT ipd_accrue_bed_charges(%s, %s, NULL, 'desk')", ot["clinic"], adm)
        ids = [r[0] for r in _rows(c, "SELECT id FROM ipd_charges WHERE admission_id = %s AND status = 'proposed'", adm)]
        _q(c, "SELECT ipd_post_charges(%s, %s, %s::uuid[], NULL, 'desk')", ot["clinic"], adm, ids)
        _q(c, "SELECT ipd_generate_final_bill(%s, %s, NULL, 'desk')", ot["clinic"], adm)
        _q(c, "SAVEPOINT before_finish")
        assert "ot_final_bill_exists" in _err(lambda: _complete(ot, sid2, amount=1000))
        _q(c, "ROLLBACK TO SAVEPOINT before_finish")
        assert _q(c, "SELECT status FROM ot_surgeries WHERE id = %s", sid2) == "in_theatre"   # nothing changed
    finally:
        _q(c, "ROLLBACK")


def test_entered_amount_keeps_the_linked_services_gst(ot):
    """A surgeon's amount replaces the tariff price, never the service's GST rate / SAC."""
    c = ot["c"]
    _q(c, "UPDATE clinics SET opd_settings = jsonb_build_object('gstin', '36AABCU9603R1ZM') WHERE id = %s", ot["clinic"])
    _q(c, "UPDATE billable_services SET tax_rate_bps = 1800, hsn_sac = '999312' WHERE id = %s", ot["service"])
    ch = _complete(ot, _ready(ot), amount=5_000_000)["charge"]
    assert (ch["unit_price_paise"], ch["tax_rate_bps"], ch["hsn_sac"]) == (5_000_000, 1800, "999312")


@pytest.mark.parametrize("status", ["scheduled", "in_theatre", "recovery"])
def test_discharge_waits_for_an_open_surgery(ot, status):
    sid = _book(ot)["id"]
    if status != "scheduled":
        _status(ot, sid, "start")
    if status == "recovery":
        _status(ot, sid, "recovery")
    for kind in ("regular", "lama", "death"):
        assert "ipd_surgery_open" in _err(lambda: _q(ot["c"], "SELECT ipd_initiate_discharge(%s, %s, %s, NULL, 'dr')",
                                                     ot["clinic"], ot["adm"], kind))
    assert _q(ot["c"], "SELECT status FROM ipd_admissions WHERE id = %s", ot["adm"]) == "admitted"
    if status == "recovery":   # finishing it releases the stay
        for p in ("sign_in", "time_out", "sign_out"):
            _check(ot, sid, p)
        _complete(ot, sid)
    else:                      # so does cancelling it
        _status(ot, sid, "cancel", "Patient going home")
    out = _q(ot["c"], "SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", ot["clinic"], ot["adm"])
    assert out["status"] == "discharge_pending"


def test_postponed_surgery_does_not_hold_the_stay(ot):
    sid = _book(ot)["id"]
    _status(ot, sid, "postpone", "Fever")
    out = _q(ot["c"], "SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", ot["clinic"], ot["adm"])
    assert out["status"] == "discharge_pending"


def test_identity_merge_moves_the_surgery_with_its_admission(ot):
    from tests.test_migration_121_emergency import _register
    v = _register(ot["c"], ot["clinic"])
    temp = v["patient_id"]
    adm = _admit(ot["c"], ot, patient=temp)
    sid = _book(ot, 50, adm=adm)["id"]
    real = _patient(ot["c"], ot["clinic"], "Ramesh")
    _q(ot["c"], "SELECT emergency_merge_patient(%s, %s, %s, NULL, 'desk', 'Son identified him')",
       ot["clinic"], temp, real)
    assert _q(ot["c"], "SELECT patient_id::text FROM ot_surgeries WHERE id = %s", sid) == real
    # outside a merge the patient never changes
    assert "ot_via_function_only" in _err(lambda: _q(ot["c"], "UPDATE ot_surgeries SET patient_id = %s WHERE id = %s",
                                                     temp, sid))


def test_owner_purge_removes_surgeries(ot):
    sid = _ready(ot, start_h=60)
    _complete(ot, sid)
    _q(ot["c"], "SELECT ipd_purge_clinic(%s)", ot["clinic"])
    assert _q(ot["c"], "SELECT count(*) FROM ot_surgeries WHERE clinic_id = %s", ot["clinic"]) == 0
    assert _q(ot["c"], "SELECT count(*) FROM ot_who_checklists WHERE clinic_id = %s", ot["clinic"]) == 0


def test_rollback_refuses_surgeries_then_reapplies(ot):
    _book(ot, 70)
    cur = ot["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "122_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_122(cur)
        cur.execute("SELECT to_regclass('public.ot_surgeries'), to_regprocedure('ot_schedule_surgery(uuid,jsonb,uuid,text)'), "
                    "to_regclass('public.uq_ipd_admissions_clinic_id_patient')")
        assert cur.fetchone() == (None, None, None)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")
