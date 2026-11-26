"""Shared Continuous Care world for the real-Postgres route tests (tests/test_cc_*.py).

A polyclinic with OPD on and Continuous Care enabled by the owner, a doctor with a registration number, the logins
each role uses, the t2dm card texts approved and a doctor-signed t2dm activation. `enroll()` takes a new patient
through the real routes: doctor enrolls (invited, WhatsApp opt-in ticked) → coordinator counter-signs consent →
active with a care plan. WhatsApp is replaced by a recorder (nothing leaves the test); the 24-hour window is
controlled per test through `wa.window`.
"""

import uuid
from types import SimpleNamespace

import pytest

from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)

T2DM_CARDS = ("high_sugar_symptoms", "lab_due", "eye_check_due")


class WA:
    """Records every WhatsApp call CC makes; `window` decides the 24-hour customer-service window."""

    def __init__(self):
        self.sent, self.window, self.fail = [], False, False

    def install(self, monkeypatch):
        from app.services.whatsapp import whatsapp_service as w

        async def _rec(kind, clinic, phone, *a, **k):
            self.sent.append(SimpleNamespace(kind=kind, phone=phone, args=a, kw=k, clinic_id=clinic.get("id")))
            return not self.fail

        async def text(clinic, phone, message, **k):
            return await _rec("text", clinic, phone, message, **k)

        async def template(clinic, phone, name, **k):
            ok = await _rec("template", clinic, phone, name, **k)
            if ok and isinstance(k.get("_capture"), dict):
                k["_capture"]["meta_message_id"] = "wamid." + uuid.uuid4().hex[:12]
            return ok

        async def buttons(clinic, phone, body, btns, **k):
            return await _rec("buttons", clinic, phone, body, btns, **k)

        async def lst(clinic, phone, body, button, sections, **k):
            return await _rec("list", clinic, phone, body, sections, **k)

        async def window(clinic, phone):
            return self.window

        monkeypatch.setattr(w, "send_text", text)
        monkeypatch.setattr(w, "send_template", template)
        monkeypatch.setattr(w, "send_interactive_buttons", buttons)
        monkeypatch.setattr(w, "send_interactive_list", lst)
        monkeypatch.setattr(w, "_can_send_freeform", window)
        return self

    def of(self, kind):
        return [s for s in self.sent if s.kind == kind]


@pytest.fixture
def wa(monkeypatch):
    return WA().install(monkeypatch)


def users(api, clinic, doctor):  # noqa: F811
    U, uid = api.U, lambda: str(uuid.uuid4())
    return SimpleNamespace(
        admin=U("admin", "clinic_admin", clinic, uid()),
        doctor=U("drmeena", "staff", clinic, uid(), [], staff_role="DOCTOR", doctor_id=doctor),
        coord=U("coord", "staff", clinic, uid(), [], staff_role="CARE_COORDINATOR"),
        nurse=U("nurse", "staff", clinic, uid(), [], staff_role="NURSE"),
        desk=U("desk", "staff", clinic, uid(), [], staff_role="FRONT_DESK"),
        cashier=U("cashier", "staff", clinic, uid(), [], staff_role="CASHIER"),
    )


def activate_pack(api, clinic, u, code="t2dm", params=None, cards=T2DM_CARDS):  # noqa: F811
    api.as_(u.admin)
    for key in cards:
        r = api.call("PUT", f"/admin/cc/cards/{key}/en", clinic, {"body": f"Care card text for {key}, approved."})
        assert r.status_code == 200, r.text
    r = api.call("POST", "/admin/cc/activations", clinic, {"protocol_code": code, "params": params or {}})
    assert r.status_code == 201, r.text
    api.as_(u.doctor)
    s = api.call("POST", f"/admin/cc/activations/{r.json()['id']}/sign", clinic)
    assert s.status_code == 200, s.text
    return s.json()


def patient(conn, clinic, name="Lakshmi Devi", language=None):
    """A patient with a real E.164 phone (digits only, as the registry stores them)."""
    return str(_q(conn, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on, language) "
                        "VALUES (%s, %s, %s, 'female', 58, CURRENT_DATE, %s) RETURNING id",
                  clinic, name, "+9194" + str(uuid.uuid4().int)[:8], language))


class World(SimpleNamespace):
    def enroll(self, protocol="t2dm", patient=None, channel="whatsapp", active=True, facts=None):
        api = self.api
        patient = patient or globals()["patient"](api.conn, self.clinic)
        api.as_(self.u.doctor)
        r = api.call("POST", "/admin/cc/enrollments", self.clinic, {
            "patient_id": patient, "protocol_code": protocol, "primary_doctor_id": self.doctor,
            "channel_pref": channel, "wa_optin_method": "in_person", "facts": facts or {}})
        assert r.status_code == 201 and r.json()["status"] == "invited", r.text
        e = r.json()
        if active:
            api.as_(self.u.coord)
            c = api.call("POST", f"/admin/cc/enrollments/{e['id']}/consent/counter", self.clinic,
                         {"notice_version": "cc-notice-1"})
            assert c.status_code == 200 and c.json()["status"] == "active", c.text
            e = c.json()
        return e, patient

    def phone(self, patient):
        return _q(self.api.conn, "SELECT phone FROM patients WHERE id = %s", patient)


@pytest.fixture
def care(api, wa):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "care_enabled", True).status_code == 200
    doctor = _doctor(api.conn, clinic)
    u = users(api, clinic, doctor)
    act = activate_pack(api, clinic, u)
    return World(api=api, clinic=clinic, doctor=doctor, u=u, activation=act, wa=wa, conn=api.conn)


def signed_rx(conn, clinic, patient, doctor, lines, follow_up=None, sign=True):
    """A signed OPD prescription for `patient` by `doctor` (the 103 functions, as the OPD routes leave it).
    lines: dicts with drug_name, formulation, dosage, frequency and optional route / timing / duration_days /
    instructions / strength."""
    import json
    phone = _q(conn, "SELECT phone FROM patients WHERE id = %s", patient)
    appt = str(_q(conn, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, "
                        "doctor_name, department, appointment_date, appointment_time, status, queue_status, is_walk_in, "
                        "booking_type, token_number) VALUES (%s, %s, %s, 'Lakshmi Devi', %s, 'Dr. Meena', 'Medicine', "
                        "CURRENT_DATE, %s, 'confirmed', 'waiting', false, 'consultation', %s) RETURNING id",
                  clinic, patient, phone, doctor, f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}",
                  100 + uuid.uuid4().int % 9000))
    admin = _q(conn, "SELECT id::text FROM clinic_admins WHERE clinic_id = %s AND doctor_id = %s", clinic, doctor) \
        if _q(conn, "SELECT count(*) FROM clinic_admins WHERE clinic_id = %s AND doctor_id = %s", clinic, doctor) else \
        str(_q(conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, "
                     "is_active) VALUES (%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id",
               clinic, "dr_" + uuid.uuid4().hex[:8], doctor))
    snap = json.dumps({"name": "Dr. Meena", "registration_number": "TSMC/2012/777", "registration_council": "TSMC"})
    enc = str(_q(conn, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id, follow_up_date) "
                       "VALUES (%s, %s, %s, %s, %s) RETURNING id", clinic, appt, patient, doctor, follow_up))
    _q(conn, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", clinic, enc, admin, doctor, snap)
    rx = str(_q(conn, "INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id) "
                      "VALUES (%s, %s, %s, %s, %s) RETURNING id", clinic, enc, appt, patient, doctor))
    for n, ln in enumerate(lines, start=1):
        ln = {"route": "oral", "timing": "any", "duration_days": None, "instructions": None, "strength": None, **ln}
        _q(conn, "INSERT INTO opd_prescription_items (clinic_id, prescription_id, line_no, drug_name, strength, "
                 "formulation, route, dosage, frequency, timing, duration_days, instructions) VALUES "
                 "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", clinic, rx, n, ln["drug_name"], ln["strength"],
           ln["formulation"], ln["route"], ln["dosage"], ln["frequency"], ln["timing"], ln["duration_days"],
           ln["instructions"])
    if sign:
        _q(conn, "SELECT id FROM opd_sign_prescription(%s, %s, %s, %s, %s::jsonb, '{\"clinic_name\": \"CC\"}'::jsonb, "
                 "%s::jsonb, '[]'::jsonb)", clinic, rx, admin, doctor, snap,
           json.dumps({"patient_name": "Lakshmi Devi", "mrn": "004211"}))
    return rx
