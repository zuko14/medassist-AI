"""One-tap doctor actions (W8, ADR-CC-04, VERIFICATION D5 / D16; TEST-PLAN P7).

No action accepts a drug / dose key (extra="forbid" + the DB CHECK); a coordinator cannot take a clinical action;
visits are booked only on a slot get_available_slots offers (never on a doctor's leave day or a hospital holiday),
'confirmed' pay-at-counter (survives expire_stale_bookings), and CC sends cc_visit_booked_v1 itself exactly once;
an idempotency key makes a double tap one action and one appointment; continue_plan closes the case.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest

from app.services import cc_cases
from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from app.utils.helpers import today_ist
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


def _weekday(after=2):
    d = today_ist() + timedelta(days=after)
    while d.weekday() > 4:                                   # the test doctor works Mon–Fri
        d += timedelta(days=1)
    return d.isoformat()


def _case(care, e, pid):
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(care.clinic))
    return asyncio.run(cc_cases.open_case(clinic, patient_id=pid, enrollment_id=e["id"], reason="t2dm:a1c_above_target",
                                          tier="watch"))


def _act(care, case, body, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return care.api.client.post(f"/admin/cc/cases/{case}/actions", params={"clinic_id": care.clinic}, json=body,
                                headers=headers)


@pytest.mark.parametrize("params", [{"drug": "Metformin"}, {"dose": "500 mg"}, {"medicine": "x"}, {"frequency": "BD"},
                                    {"dosage": "1"}, {"strength": "1 g"}, {"prescription": "x"}])
def test_no_action_accepts_a_drug_or_dose(care, params):  # noqa: F811
    e, pid = care.enroll()
    c = _case(care, e, pid)
    care.api.as_(care.u.doctor)
    assert _act(care, c, {"action_code": "continue_plan", "params": params}).status_code == 422
    assert _act(care, c, {"action_code": "continue_plan", "drug": "x"}).status_code == 422
    assert _q(care.conn, "SELECT count(*) FROM cc_actions WHERE case_id = %s", c) == 0


def test_coordinator_cannot_take_clinical_actions(care):  # noqa: F811
    e, pid = care.enroll()
    c = _case(care, e, pid)
    care.api.as_(care.u.coord)
    for code in ("book_review_visit", "order_lab", "refer", "continue_plan", "recheck_in_days", "end"):
        assert _act(care, c, {"action_code": code}).status_code == 403, code
    r = _act(care, c, {"action_code": "call_patient_now"})
    assert r.status_code == 201 and r.json()["decider_role"] == "coordinator"
    care.api.as_(care.u.nurse)
    assert _act(care, c, {"action_code": "call_patient_now"}).status_code == 403


def test_book_review_visit_uses_available_slots_and_confirms_once(care):  # noqa: F811
    from app.services.whatsapp import whatsapp_service  # noqa: F401  (recorded by the wa fixture)
    e, pid = care.enroll()
    c = _case(care, e, pid)
    day = _weekday()
    care.api.as_(care.u.doctor)
    care.wa.window = True
    key = "tap-" + uuid.uuid4().hex
    r = _act(care, c, {"action_code": "book_review_visit", "params": {"date": day, "time": "10:30"}}, key)
    assert r.status_code == 201, r.text
    act = r.json()
    assert act["status"] == "booked" and act["due_on"] == day and act["decider_role"] == "doctor"
    appt = _q(care.conn, "SELECT row_to_json(a) FROM appointments a WHERE id = %s", act["appointment_id"])
    assert (appt["status"], str(appt["appointment_time"])[:5], appt["patient_id"]) == ("confirmed", "10:30", pid)
    texts = [s.args[0] for s in care.wa.of("text") if s.kw.get("_source") == "cc_visit"]
    assert len(texts) == 1 and "booked" in texts[0] and appt["booking_ref"] in texts[0]
    again = _act(care, c, {"action_code": "book_review_visit", "params": {"date": day, "time": "10:30"}}, key)
    assert again.status_code == 201 and again.json()["id"] == act["id"]                     # double tap: one action
    assert _q(care.conn, "SELECT count(*) FROM appointments WHERE patient_id = %s AND appointment_date = %s",
              pid, day) == 1
    assert len([s for s in care.wa.of("text") if s.kw.get("_source") == "cc_visit"]) == 1      # confirmation once
    assert _q(care.conn, "SELECT status FROM cc_cases WHERE id = %s", c) == "actioned"
    from app.services.scheduler import SchedulerService
    exp = getattr(SchedulerService, "expire_stale_bookings", None)
    if exp:
        try:
            asyncio.run(exp(SchedulerService.__new__(SchedulerService)))
        except Exception:                                    # the job's other queries are out of scope here
            pass
    assert _q(care.conn, "SELECT status FROM appointments WHERE id = %s", act["appointment_id"]) == "confirmed"


@pytest.mark.parametrize("block", ["leave", "holiday", "taken", "not_a_slot"])
def test_never_books_a_blocked_slot(care, block):  # noqa: F811
    e, pid = care.enroll()
    c = _case(care, e, pid)
    day = _weekday(3)
    if block == "leave":
        _q(care.conn, "INSERT INTO doctor_leaves (clinic_id, doctor_name, leave_date, leave_type) VALUES "
                      "(%s, 'Dr. Meena', %s, 'full')", care.clinic, day)
    elif block == "holiday":
        _q(care.conn, "INSERT INTO hospital_holidays (clinic_id, holiday_date, name) VALUES (%s, %s, 'Diwali')",
           care.clinic, day)
    elif block == "taken":
        _q(care.conn, "INSERT INTO appointments (clinic_id, patient_phone, patient_name, doctor_id, doctor_name, department, "
                      "appointment_date, appointment_time, status, is_walk_in, booking_type) VALUES (%s, '+919811111111', "
                      "'X', %s, 'Dr. Meena', 'Medicine', %s, '10:30', 'confirmed', false, 'consultation')",
           care.clinic, care.doctor, day)
    from app import database
    database._holiday_cache.clear() if hasattr(database, "_holiday_cache") else None
    care.api.as_(care.u.doctor)
    r = _act(care, c, {"action_code": "book_review_visit",
                       "params": {"date": day, "time": "10:45" if block == "not_a_slot" else "10:30"}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "slot_unavailable", r.text
    assert _q(care.conn, "SELECT count(*) FROM appointments WHERE patient_id = %s", pid) == 0
    assert _q(care.conn, "SELECT count(*) FROM cc_actions WHERE case_id = %s", c) == 0


def test_first_free_slot_skips_leave_days(care):  # noqa: F811
    e, pid = care.enroll()
    c = _case(care, e, pid)
    first = _weekday(1)
    _q(care.conn, "INSERT INTO doctor_leaves (clinic_id, doctor_name, leave_date, leave_type) VALUES "
                  "(%s, 'Dr. Meena', %s, 'full')", care.clinic, first)
    care.api.as_(care.u.doctor)
    r = _act(care, c, {"action_code": "book_review_visit"})
    assert r.status_code == 201 and r.json()["due_on"] != first


def test_order_lab_recheck_and_continue_plan(care):  # noqa: F811
    e, pid = care.enroll()
    care.api.as_(care.u.doctor)
    c1 = _case(care, e, pid)
    r = _act(care, c1, {"action_code": "order_lab", "params": {"within_days": 5}})
    assert r.status_code == 201 and r.json()["due_on"] == (today_ist() + timedelta(days=5)).isoformat()
    assert _q(care.conn, "SELECT status FROM cc_cases WHERE id = %s", c1) == "actioned"
    _q(care.conn, "UPDATE cc_cases SET status = 'closed', closed_reason = 'clinical_decision', closed_at = now() "
                  "WHERE id = %s", c1)
    c2 = _case(care, e, pid)
    assert _act(care, c2, {"action_code": "recheck_in_days", "params": {"days": 91}}).status_code == 422
    r = _act(care, c2, {"action_code": "continue_plan"})
    assert r.status_code == 201
    assert _q(care.conn, "SELECT status || ':' || closed_reason FROM cc_cases WHERE id = %s", c2) == "closed:clinical_decision"
    assert _act(care, c2, {"action_code": "continue_plan"}).status_code == 409             # closed
    care.api.as_(care.u.nurse)
    assert care.api.call("GET", "/admin/cc/actions", care.clinic).status_code == 200
