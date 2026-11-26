"""A Specialty OS clinic on real Postgres for the SP suites (lane SP, migrations 151–155).

`sp_world(api, plan, packs)` creates an OPD clinic on `plan`, switches on Specialty OS and the given packs through the
owner toggles (so eligibility is exercised), adds a doctor (with a doctor login), a patient and role users. Encounters
are inserted as drafts and signed through opd_sign_encounter (the real signing function).
"""

import json
import uuid
from types import SimpleNamespace

from tests.test_ipd_api_e2e import _doctor, _patient, _q

PACK_FLAGS = {"eye": "optical_enabled", "dental": "dental_chart", "derma": "derma_os", "ivf": "ivf_lab_enabled",
              "anc": "obgyn_enabled", "imm": "paediatrics_enabled"}


def sp_world(api, plan="eye", packs=("eye",)):
    c = api.conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state)
                          VALUES ('Netra Eye Hospital', %s, %s, true, '{"opd_enabled": true}'::jsonb, 'READY') RETURNING id""",
                    "+9196" + str(uuid.uuid4().int)[:8], plan))
    assert api.owner_toggle(clinic, "specialty_os", True).status_code == 200
    for p in packs:
        r = api.owner_toggle(clinic, PACK_FLAGS[p], True)
        assert r.status_code == 200, r.text
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    doctor = _doctor(c, clinic)
    U, uid = api.U, lambda: str(uuid.uuid4())
    doc_login = str(_q(c, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, "
                          "is_active) VALUES (%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id", clinic,
                       "dr_" + uuid.uuid4().hex[:8], doctor))
    users = SimpleNamespace(
        admin=U("admin", "clinic_admin", clinic, uid()),
        doctor=U("drmeena", "staff", clinic, doc_login, [], staff_role="DOCTOR", doctor_id=doctor),
        nurse=U("nurse", "staff", clinic, uid(), [], staff_role="NURSE"),
        desk=U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK"),   # the role preset's grant
        pharma=U("pharma", "staff", clinic, uid(), [], staff_role="PHARMACIST"),
    )
    patient = _patient(c, clinic)
    _q(c, "UPDATE patients SET phone = %s WHERE id = %s", "+9198" + str(uuid.uuid4().int)[:8], patient)   # a real-looking number
    return SimpleNamespace(api=api, conn=c, clinic=clinic, doctor=doctor, doc_login=doc_login, u=users,
                           patient=patient, plan=plan)


def encounter(w, patient=None):
    c = w.conn
    pid = patient or w.patient
    appt = str(_q(c, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                     "department, appointment_date, appointment_time, status, is_walk_in, booking_type, checked_in_at) "
                     "VALUES (%s, %s, '+919800000001', 'P', %s, 'Dr. Meena', 'Medicine', CURRENT_DATE, %s, 'confirmed', "
                     "false, 'consultation', now()) RETURNING id", w.clinic, pid, w.doctor,
                  f"{uuid.uuid4().int % 24:02d}:{uuid.uuid4().int % 60:02d}"))
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s) "
                    "RETURNING id", w.clinic, appt, pid, w.doctor))
    return enc, appt


def sign(w, enc):
    _q(w.conn, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", w.clinic, enc, w.doc_login, w.doctor,
       json.dumps({"name": "Dr. Meena", "registration_number": "TSMC/2012/777"}))
