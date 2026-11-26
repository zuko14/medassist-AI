"""Future appointments (MI-06): each row goes through get_available_slots first — holidays, doctor leave, off days,
sessions, already-booked — and then book_appointment's conflict guard (VERIFICATION D16). Refusals are reported per
row, never forced; no WhatsApp message is sent; undo cancels only what is still untouched."""

import uuid
from datetime import timedelta

from app.utils.helpers import today_ist
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_mi_api_helpers import mi_clinic, run


def _next(weekday: int):
    d = today_ist() + timedelta(days=2)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def test_slots_validated_refusals_reported_no_message(api, monkeypatch):  # noqa: F811
    import app.services.whatsapp as wa
    sent = []
    monkeypatch.setattr(wa.whatsapp_service, "send_text", lambda *a, **k: sent.append(a))
    clinic = mi_clinic(api)
    _q(api.conn, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active, available_days,
                    morning_slots, evening_slots) VALUES (%s, 'Dr. Rao', 'Medicine', 'GP', true, 'Mon,Tue,Wed,Thu,Fri',
                    '["10:00","10:30"]', '[]')""", clinic)
    mon, tue, wed, sat = _next(0), _next(1), _next(2), _next(5)
    _q(api.conn, "INSERT INTO hospital_holidays (clinic_id, holiday_date, name) VALUES (%s, %s, 'Festival')", clinic, tue)
    _q(api.conn, "INSERT INTO doctor_leaves (clinic_id, doctor_name, leave_date, leave_type) VALUES (%s, 'Dr. Rao', %s, 'full')",
       clinic, wed)
    fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    csv = ("Patient Name,Phone,Doctor,Date,Time\n"
           f"Asha,9000000001,Dr. Rao,{fmt(mon)},10:00\n"           # booked
           f"Ravi,9000000002,Dr. Rao,{fmt(mon)},10:00\n"           # the same slot: taken
           f"Sita,9000000003,Dr. Rao,{fmt(tue)},10:00\n"           # holiday
           f"Gita,9000000004,Dr. Rao,{fmt(wed)},10:30\n"           # doctor on leave
           f"Hari,9000000005,Dr. Rao,{fmt(sat)},10:00\n"           # off day
           f"Usha,9000000006,Dr. Rao,{fmt(mon)},11:15\n"           # not one of the doctor's slots
           f"Ravi,9000000002,Dr. Nobody,{fmt(mon)},10:30\n"        # unknown doctor
           "Old,9000000007,Dr. Rao,01/01/2020,10:00\n")             # in the past
    dry, a = run(api, clinic, "appointments_future", csv)
    assert dry["rows_ok"] == 8
    out = a.json()
    assert out["written"] == 1 and out["refused"] == 7 and out["confirmations_sent"] == 0
    problems = [r["problem"] for r in out["refusals"]]
    for want in ("already booked", "hospital is closed", "on leave", "does not sit", "not one of the doctor",
                 "doctor not found", "in the past"):
        assert any(want in p for p in problems), (want, problems)
    appt = _q(api.conn, "SELECT row_to_json(a) FROM appointments a WHERE clinic_id = %s", clinic)
    assert (appt["patient_name"], appt["status"], appt["booking_channel"], str(appt["appointment_time"])[:5]) == (
        "Asha", "confirmed", "front_desk", "10:00")
    assert sent == []
    errs = api.call("GET", f"/admin/mi/batches/{out['batch_id']}", clinic).json()["errors"]
    assert len(errs) == 7
    # undo cancels the imported booking (nothing uses it yet)
    assert api.call("DELETE", f"/admin/mi/batches/{out['batch_id']}", clinic).status_code == 200
    assert _q(api.conn, "SELECT count(*) FROM appointments WHERE clinic_id = %s", clinic) == 0


def test_undo_refused_after_the_visit_happened(api):  # noqa: F811
    clinic = mi_clinic(api)
    doc = _q(api.conn, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active, available_days,
                          morning_slots) VALUES (%s, 'Dr. Rao', 'Medicine', 'GP', true, 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
                          '["10:00"]') RETURNING id""", clinic)
    d = (today_ist() + timedelta(days=3)).strftime("%d/%m/%Y")
    _, a = run(api, clinic, "appointments_future", f"Patient Name,Phone,Doctor,Date,Time\nAsha,9000000001,Dr. Rao,{d},10:00\n")
    appt = _q(api.conn, "SELECT id FROM appointments WHERE clinic_id = %s", clinic)
    pat = _q(api.conn, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Asha', '+919000000001') RETURNING id", clinic)
    _q(api.conn, "UPDATE appointments SET patient_id = %s WHERE id = %s", pat, appt)
    _q(api.conn, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s)",
       clinic, appt, pat, doc)
    assert api.call("DELETE", f"/admin/mi/batches/{a.json()['batch_id']}", clinic).status_code == 409
    assert _q(api.conn, "SELECT count(*) FROM appointments WHERE clinic_id = %s", clinic) == 1
