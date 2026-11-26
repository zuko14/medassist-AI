"""Verification — "counts only when recorded" (ADR-CC-08, X2 / X15; TEST-PLAN P8).

A booked visit is verified only by a check-in or a signed encounter (the stronger evidence wins), never by
appointments.status='completed' (the 00:30 job marks no-shows completed too). Past its due date an unverified
action becomes not_verified and the case re-opens. Revenue comes from paid invoice lines only. A parallel run never
writes two verifications.
"""

import asyncio
import json
import uuid
from datetime import timedelta

from app.services import cc_verify
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from app.utils.helpers import today_ist
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _clinic(care):
    invalidate_tenant_cache()
    return asyncio.run(get_clinic_by_id(care.clinic))


def _booked(care, status="confirmed", due_days=0):
    e, pid = care.enroll()
    c = care.conn
    appt = str(_q(c, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                     "department, appointment_date, appointment_time, status, is_walk_in, booking_type) VALUES "
                     "(%s, %s, %s, 'P', %s, 'Dr. Meena', 'Medicine', %s, %s, %s, false, 'consultation') RETURNING id",
                  care.clinic, pid, care.phone(pid), care.doctor, (today_ist() + timedelta(days=due_days)).isoformat(),
                  f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}", status))
    case = str(_q(c, "INSERT INTO cc_cases (clinic_id, patient_id, enrollment_id, reason, tier, due_by, status) VALUES "
                     "(%s, %s, %s, 't2dm:a1c_above_target', 'watch', now(), 'actioned') RETURNING id",
                  care.clinic, pid, e["id"]))
    act = str(_q(c, "INSERT INTO cc_actions (clinic_id, case_id, action_code, decided_by_name, decider_role, appointment_id, "
                    "status, due_on) VALUES (%s, %s, 'book_review_visit', 'Dr', 'doctor', %s, 'booked', %s) RETURNING id",
                 care.clinic, case, appt, (today_ist() + timedelta(days=due_days)).isoformat()))
    return SimpleNS(e=e, pid=pid, appt=appt, case=case, act=act)


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_completed_status_alone_is_never_verified_and_reopens_after_due(care):  # noqa: F811
    b = _booked(care, status="completed", due_days=-2)
    out = asyncio.run(cc_verify.run(_clinic(care)))
    assert out == {"verified": 0, "not_verified": 1}
    assert _q(care.conn, "SELECT status FROM cc_actions WHERE id = %s", b.act) == "not_verified"
    assert _q(care.conn, "SELECT status FROM cc_cases WHERE id = %s", b.case) == "open"
    assert _q(care.conn, "SELECT count(*) FROM cc_verifications WHERE action_id = %s", b.act) == 0


def test_not_yet_due_stays_booked(care):  # noqa: F811
    b = _booked(care, due_days=3)
    assert asyncio.run(cc_verify.run(_clinic(care))) == {"verified": 0, "not_verified": 0}
    assert _q(care.conn, "SELECT status FROM cc_actions WHERE id = %s", b.act) == "booked"


def test_check_in_verifies_with_paid_revenue_only(care):  # noqa: F811
    b = _booked(care)
    _q(care.conn, "UPDATE appointments SET checked_in_at = now() WHERE id = %s", b.appt)
    asyncio.run(cc_verify.run(_clinic(care)))
    v = _q(care.conn, "SELECT row_to_json(v) FROM cc_verifications v WHERE action_id = %s", b.act)
    assert v["evidence_kind"] == "check_in" and v["evidence_ref"] == {"table": "appointments", "id": b.appt}
    assert v["revenue_paise"] == 0                                     # nothing paid yet
    assert _q(care.conn, "SELECT status || ':' || closed_reason FROM cc_cases WHERE id = %s", b.case) == "closed:verified"
    assert _q(care.conn, "SELECT status FROM cc_actions WHERE id = %s", b.act) == "verified"
    assert asyncio.run(cc_verify.run(_clinic(care))) == {"verified": 0, "not_verified": 0}   # once


def test_signed_encounter_is_the_stronger_evidence(care):  # noqa: F811
    b = _booked(care)
    _q(care.conn, "UPDATE appointments SET checked_in_at = now() WHERE id = %s", b.appt)
    enc = str(_q(care.conn, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES "
                            "(%s, %s, %s, %s) RETURNING id", care.clinic, b.appt, b.pid, care.doctor))
    admin = _q(care.conn, "SELECT max(id::text) FROM clinic_admins WHERE clinic_id = %s AND doctor_id = %s",
               care.clinic, care.doctor) or str(_q(care.conn, "INSERT INTO clinic_admins (clinic_id, username, "
                                                              "password_hash, role, staff_role, doctor_id, is_active) VALUES "
                                                              "(%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id",
                                                   care.clinic, "dr_" + uuid.uuid4().hex[:8], care.doctor))
    _q(care.conn, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", care.clinic, enc, admin, care.doctor,
       json.dumps({"name": "Dr. Meena", "registration_number": "TSMC/2012/777"}))
    asyncio.run(cc_verify.run(_clinic(care)))
    assert _q(care.conn, "SELECT evidence_kind FROM cc_verifications WHERE action_id = %s", b.act) == "signed_encounter"


def test_revenue_counts_paid_invoices_only(care):  # noqa: F811
    b = _booked(care)
    for status, paid in (("paid", 50000), ("issued", 0), ("partially_paid", 20000)):
        try:
            _q(care.conn, "INSERT INTO opd_invoices (clinic_id, patient_id, appointment_id, status, paid_paise) VALUES "
                          "(%s, %s, %s, %s, %s)", care.clinic, b.pid, b.appt, status, paid)
        except Exception:                                    # an invoice shape the schema refuses: drop that row
            care.conn.rollback() if not care.conn.autocommit else None
    total = _q(care.conn, "SELECT coalesce(sum(paid_paise), 0) FROM opd_invoices WHERE appointment_id = %s "
                          "AND status IN ('paid', 'partially_paid')", b.appt)
    assert asyncio.run(cc_verify.visit_revenue(care.clinic, b.appt)) == total


def test_parallel_runs_write_one_verification(care):  # noqa: F811
    b = _booked(care)
    _q(care.conn, "UPDATE appointments SET checked_in_at = now() WHERE id = %s", b.appt)
    clinic = _clinic(care)

    async def both():
        return await asyncio.gather(cc_verify.run(clinic), cc_verify.run(clinic), return_exceptions=True)
    res = asyncio.run(both())
    assert not [r for r in res if isinstance(r, Exception)], res
    assert _q(care.conn, "SELECT count(*) FROM cc_verifications WHERE action_id = %s", b.act) == 1
