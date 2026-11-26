"""Follow-up radar (W6, ADR-CC-13; TEST-PLAN P6) on real Postgres.

Each reason opens on its documented event and closes when the event that resolves it happens; an auto-completed
no-show (status 'completed', no check-in) is still a missed visit — appointments.status is never trusted (ADR-CC-08);
a not-enrolled patient with an ICD-10 E11 signed encounter is a t2dm candidate (proposed, never enrolled or cased);
a dismissed candidate is not raised again for 90 days; a dismissal needs a reason.
"""

import asyncio
import json
import uuid
from datetime import timedelta

from app.services import cc_radar
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from app.utils.helpers import today_ist
from tests.cc_world import activate_pack, care, patient, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _clinic(care):
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _encounter(care, pid, follow_up=None, diagnoses=()):
    c = care.conn
    appt = str(_q(c, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                     "department, appointment_date, appointment_time, status, is_walk_in, booking_type, checked_in_at) "
                     "VALUES (%s, %s, %s, 'P', %s, 'Dr. Meena', 'Medicine', CURRENT_DATE, %s, 'confirmed', false, "
                     "'consultation', now()) RETURNING id", care.clinic, pid, care.phone(pid), care.doctor,
                  f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}"))
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, follow_up_date, diagnoses) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb) RETURNING id", care.clinic, appt, pid, care.doctor, follow_up,
                 json.dumps([{"code": d, "label": d} for d in diagnoses])))
    admin = _q(c, "SELECT max(id::text) FROM clinic_admins WHERE clinic_id = %s AND doctor_id = %s", care.clinic, care.doctor) or \
        str(_q(c, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, is_active) "
                  "VALUES (%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id", care.clinic,
               "dr_" + uuid.uuid4().hex[:8], care.doctor))
    _q(c, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", care.clinic, enc, admin, care.doctor,
       json.dumps({"name": "Dr. Meena", "registration_number": "TSMC/2012/777"}))
    return enc


def _rows(care, pid, reason=None, open_only=True):
    return _q(care.conn, "SELECT coalesce(json_agg(json_build_object('reason', reason, 'close', close_reason, "
                         "'source', source_key) ORDER BY created_at), '[]') FROM cc_radar_rows WHERE clinic_id = %s "
                         "AND patient_id = %s" + (" AND closed_on IS NULL" if open_only else "")
                         + (" AND reason = %s" if reason else ""), *([care.clinic, pid] + ([reason] if reason else [])))


def test_followup_overdue_opens_and_closes_on_a_checked_in_visit(care):  # noqa: F811
    pid = patient(care.conn, care.clinic)
    later = today_ist() + timedelta(days=10)
    _encounter(care, pid, follow_up=(today_ist() + timedelta(days=2)).isoformat())
    asyncio.run(cc_radar.rebuild(_clinic(care), today_ist() + timedelta(days=4)))           # fu + 3 not passed yet
    assert _rows(care, pid, "followup_overdue") == []
    out = asyncio.run(cc_radar.rebuild(_clinic(care), later))
    assert out["opened"] >= 1 and len(_rows(care, pid, "followup_overdue")) == 1
    assert _q(care.conn, "SELECT count(*) FROM cc_cases WHERE patient_id = %s AND reason = 'followup_overdue' "
                         "AND radar_row_id IS NOT NULL", pid) == 1                              # above the threshold
    asyncio.run(cc_radar.rebuild(_clinic(care), later))                                       # idempotent
    assert len(_rows(care, pid, "followup_overdue")) == 1
    _q(care.conn, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                  "department, appointment_date, appointment_time, status, is_walk_in, booking_type, checked_in_at) VALUES "
                  "(%s, %s, %s, 'P', %s, 'Dr. Meena', 'Medicine', %s, '07:07', 'confirmed', false, 'consultation', "
                  "now() + interval '3 days')", care.clinic, pid, care.phone(pid), care.doctor,
       (today_ist() + timedelta(days=3)).isoformat())
    asyncio.run(cc_radar.rebuild(_clinic(care), later))
    assert _rows(care, pid, "followup_overdue") == []
    assert [r["close"] for r in _rows(care, pid, "followup_overdue", open_only=False)] == ["resolved"]


def test_auto_completed_no_show_is_still_a_missed_visit(care):  # noqa: F811
    pid = patient(care.conn, care.clinic)
    _q(care.conn, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                  "department, appointment_date, appointment_time, status, is_walk_in, booking_type) VALUES "
                  "(%s, %s, %s, 'P', %s, 'Dr. Meena', 'Medicine', %s, '09:00', 'completed', false, 'consultation')",
       care.clinic, pid, care.phone(pid), care.doctor, (today_ist() - timedelta(days=4)).isoformat())
    asyncio.run(cc_radar.rebuild(_clinic(care)))
    assert [r["reason"] for r in _rows(care, pid)] == ["visit_missed"]


def test_icd_e11_encounter_is_a_candidate_never_enrolled_or_cased(care):  # noqa: F811
    pid = patient(care.conn, care.clinic)
    _encounter(care, pid, diagnoses=("E11.9", "I10"))
    asyncio.run(cc_radar.rebuild(_clinic(care)))
    cands = _rows(care, pid, "candidate")
    assert [r["source"] for r in cands] == ["pack:t2dm"]                     # htn has no signed activation here
    assert _q(care.conn, "SELECT count(*) FROM cc_enrollments WHERE patient_id = %s", pid) == 0
    assert _q(care.conn, "SELECT count(*) FROM cc_cases WHERE patient_id = %s", pid) == 0
    care.api.as_(care.u.nurse)
    listed = care.api.call("GET", "/admin/cc/radar", care.clinic, reason="candidate").json()["rows"]
    assert any(r["patient_id"] == pid and r["label"] == "Candidate" for r in listed)
    row = next(r for r in listed if r["patient_id"] == pid)
    assert care.api.call("POST", f"/admin/cc/radar/{row['id']}/dismiss", care.clinic,
                         {"reason": "known"}).status_code == 403                  # nurse: view only
    care.api.as_(care.u.coord)
    assert care.api.call("POST", f"/admin/cc/radar/{row['id']}/dismiss", care.clinic, {"reason": "abc"}).status_code == 422
    assert care.api.call("POST", f"/admin/cc/radar/{row['id']}/dismiss", care.clinic,
                         {"reason": "Treated elsewhere"}).status_code == 200
    asyncio.run(cc_radar.rebuild(_clinic(care)))
    assert _rows(care, pid, "candidate") == []                                # not raised again within 90 days
    asyncio.run(cc_radar.rebuild(_clinic(care), today_ist() + timedelta(days=91)))
    assert len(_rows(care, pid, "candidate")) <= 1                            # after 90 days it may come back


def test_enrolled_patient_is_not_a_candidate_and_not_responding_shows(care):  # noqa: F811
    e, pid = care.enroll()
    _encounter(care, pid, diagnoses=("E11.9",))
    _q(care.conn, "UPDATE cc_enrollments SET unanswered_streak = 3 WHERE id = %s", e["id"])
    asyncio.run(cc_radar.rebuild(_clinic(care)))
    reasons = [r["reason"] for r in _rows(care, pid)]
    assert "candidate" not in reasons and "not_responding" in reasons


def test_score():
    assert cc_radar.score("visit_missed") == 45 and cc_radar.score("visit_missed", 10) == 50
    assert cc_radar.score("off_target", 0, "urgent") == 100 and cc_radar.score("followup_overdue", 500) == 70


def test_incremental_candidate_watermark_and_weekly_full_rebuild(care):  # noqa: F811
    """VERIFICATION D9: after a full pass, the candidate scan reads diagnoses only of encounters signed since the watermark
    and carries open candidates forward; the Sunday pass re-reads everything (and drops what no longer qualifies)."""
    base = today_ist()
    weekday = base + timedelta(days=(2 - base.weekday()) % 7 or 7)            # a Wednesday: incremental allowed
    sunday = weekday + timedelta(days=4)
    old = patient(care.conn, care.clinic)
    _encounter(care, old, diagnoses=("E11.9", "I10"))
    first = asyncio.run(cc_radar.rebuild(_clinic(care), weekday))
    wm = _q(care.conn, "SELECT cc_settings->>'radar_candidate_wm' FROM clinics WHERE id = %s", care.clinic)
    assert wm and len(_rows(care, old, "candidate")) == 1
    assert cc_radar.candidate_watermark(_clinic(care), weekday) == wm
    assert cc_radar.candidate_watermark(_clinic(care), sunday) is None                    # weekly full rebuild
    assert cc_radar.candidate_watermark(_clinic(care), weekday + timedelta(days=14)) is None   # stale → full
    assert "radar_candidate_wm" not in __import__("app.services.cc", fromlist=["x"]).get_settings(_clinic(care))
    # a second pack (htn) is signed after the full pass: the old I10 encounter is not re-read incrementally …
    activate_pack(care.api, care.clinic, care.u, code="htn", cards=("bp_very_high_symptoms", "low_bp_dizzy", "visit_due"))
    new = patient(care.conn, care.clinic)
    _encounter(care, new, diagnoses=("I10",))
    out = asyncio.run(cc_radar.rebuild(_clinic(care), weekday))
    assert out["full_scan"] is False
    assert [r["source"] for r in _rows(care, old, "candidate")] == ["pack:t2dm"]          # carried forward
    assert [r["source"] for r in _rows(care, new, "candidate")] == ["pack:htn"]          # signed since the watermark
    # … the weekly full pass re-reads every encounter in the 180-day window
    out = asyncio.run(cc_radar.rebuild(_clinic(care), sunday))
    assert out["full_scan"] is True
    assert sorted(r["source"] for r in _rows(care, old, "candidate")) == ["pack:htn", "pack:t2dm"]
