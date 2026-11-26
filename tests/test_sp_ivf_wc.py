"""IVF (SP-IVF-01…07) and Women & Child (SP-WC-01, SP-WC-03) on real Postgres.

IVF: couple rules (partner XOR donor; single woman uses a donor; ID type + last 4 only); cycle chart with drugs copied
from a SIGNED prescription only; the trigger time is set by a doctor login, sent with an "I have noted it" button and
acknowledged by the female partner's own number (or recorded by staff); embryo grades are validated; witnessing needs a
second, different, re-authenticated login of the same clinic; freeze / discard need a consent and a free straw; the
witness chain is append-only and verifies; the outcome is held until released; the ART register CSV has no identity
beyond ID type + last 4. Women & Child: EDD-based gestational age, ANC markers, immunisation due / given / overdue.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg2
import pytest

from tests.sp_world import encounter, sign, sp_world
from tests.test_ipd_api_e2e import _patient, _q, api  # noqa: F401  (fixture)


def _witness_login(w, password="Witness#2026"):
    from app.routers.admin import hash_password
    name = "emb_" + uuid.uuid4().hex[:8]
    _q(w.conn, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, is_active) "
               "VALUES (%s, %s, %s, 'staff', 'NURSE', true)", w.clinic, name, hash_password(password))
    return name, password


def _couple(api, w):  # noqa: F811
    male = _patient(w.conn, w.clinic, gender="male")
    api.as_(w.u.doctor)
    bad = api.call("POST", "/admin/sp/ivf/couples", w.clinic, {
        "female_patient_id": w.patient, "male_patient_id": male, "donor_code": "D-1", "female_id_type": "aadhaar",
        "female_id_last4": "1234", "male_id_type": "pan", "male_id_last4": "9Z9Z", "marital_status_declared": "married"})
    assert bad.status_code == 422
    single = api.call("POST", "/admin/sp/ivf/couples", w.clinic, {
        "female_patient_id": w.patient, "male_patient_id": male, "female_id_type": "aadhaar", "female_id_last4": "1234",
        "male_id_type": "pan", "male_id_last4": "9Z9Z", "marital_status_declared": "single_woman"})
    assert single.status_code == 422
    r = api.call("POST", "/admin/sp/ivf/couples", w.clinic, {
        "female_patient_id": w.patient, "male_patient_id": male, "female_id_type": "aadhaar", "female_id_last4": "1234",
        "male_id_type": "pan", "male_id_last4": "9Z9Z", "marital_status_declared": "married"})
    assert r.status_code == 201, r.text
    return r.json()


def test_ivf_cycle_trigger_embryology_witnessing_and_release(api, monkeypatch):  # noqa: F811
    from app.services import sp_hooks
    from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
    from app.services.whatsapp import whatsapp_service
    w = sp_world(api, plan="ivf", packs=("ivf",))
    couple = _couple(api, w)
    cyc = api.call("POST", "/admin/sp/ivf/cycles", w.clinic, {"couple_id": couple["id"], "cycle_type": "icsi",
                                                              "protocol_text": "Antagonist", "start_date": str(date.today())})
    assert cyc.status_code == 201, cyc.text
    cid = cyc.json()["id"]
    enc, appt = encounter(w)
    rx = str(_q(w.conn, "INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id, "
                        "status) VALUES (%s, %s, %s, %s, %s, 'draft') RETURNING id", w.clinic, enc, appt, w.patient, w.doctor))
    day = {"day_no": 6, "on_date": str(date.today()), "prescription_id": rx, "follicles_left": [14, 18, 19],
           "follicles_right": [17, 12], "endometrium_mm": 9.5, "e2_pg_ml": 1450}
    assert api.call("PUT", f"/admin/sp/ivf/cycles/{cid}/days", w.clinic, day).status_code == 409   # draft Rx
    _q(w.conn, "INSERT INTO opd_prescription_items (clinic_id, prescription_id, line_no, drug_name, strength, formulation, "
               "dosage, route, frequency, timing) VALUES (%s, %s, 1, 'Follitropin', '225 IU', 'injection', '225 IU', 'sc', "
               "'OD', 'any')", w.clinic, rx)
    sign(w, enc)
    _q(w.conn, "SELECT id FROM opd_sign_prescription(%s, %s, %s, %s, %s::jsonb, '{\"clinic_name\": \"IVF\"}'::jsonb, "
               "'{\"name\": \"P\"}'::jsonb, '[]'::jsonb)", w.clinic, rx, w.doc_login, w.doctor,
       '{"name": "Dr. Meena", "registration_number": "TSMC/2012/777"}')
    d = api.call("PUT", f"/admin/sp/ivf/cycles/{cid}/days", w.clinic, day)
    assert d.status_code == 200, d.text
    assert d.json()["drugs"][0]["name"].startswith("Follitropin")
    assert d.json()["follicles_left"] == [19, 18, 14]
    # trigger: a doctor login only, exactly as entered; acknowledgement by the female partner's own number
    api.as_(w.u.nurse)
    t_at = datetime(2026, 12, 1, 21, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/trigger", w.clinic, {"trigger_at": t_at.isoformat()}).status_code == 403
    api.as_(w.u.doctor)
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/trigger", w.clinic, {"trigger_at": t_at.isoformat()}).status_code == 200
    _q(w.conn, "UPDATE patients SET opted_in = true WHERE id = %s", w.patient)
    buttons = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp_service, "send_interactive_buttons", buttons)
    sent = api.call("POST", f"/admin/sp/ivf/cycles/{cid}/trigger/send", w.clinic).json()
    assert sent["time_ist"] == "01 Dec 2026, 09:30 PM" and "09:30 PM" in buttons.await_args.args[2]
    chart = api.call("GET", f"/admin/sp/ivf/cycles/{cid}", w.clinic).json()
    assert chart["trigger"]["acknowledged"] is False and chart["lead_follicles"]["6"] == 3
    invalidate_tenant_cache()
    clinic = asyncio.run(get_clinic_by_id(w.clinic))
    m = SimpleNamespace(whatsapp=SimpleNamespace(send_text=AsyncMock(return_value=True)))
    tap = buttons.await_args.args[3][0]["id"]
    asyncio.run(sp_hooks.handle_tap(m, clinic, "+918888888888", tap, "w1"))                     # someone else
    assert api.call("GET", f"/admin/sp/ivf/cycles/{cid}", w.clinic).json()["trigger"]["acknowledged"] is False
    phone = _q(w.conn, "SELECT phone FROM patients WHERE id = %s", w.patient)
    asyncio.run(sp_hooks.handle_tap(m, clinic, phone, tap, "w2"))
    assert api.call("GET", f"/admin/sp/ivf/cycles/{cid}", w.clinic).json()["trigger"]["ack_via"] == "whatsapp_button"
    # embryology + double witnessing
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/embryos", w.clinic, {"label": "E1", "day": 5,
                                                                              "grade": "great"}).status_code == 422
    e1 = api.call("POST", f"/admin/sp/ivf/cycles/{cid}/embryos", w.clinic, {"label": "E1", "day": 5, "grade": "4AA"}).json()
    e2 = api.call("POST", f"/admin/sp/ivf/cycles/{cid}/embryos", w.clinic, {"label": "E2", "day": 5, "grade": "3BB"}).json()
    wname, wpass = _witness_login(w)
    wit = lambda body: api.call("POST", f"/admin/sp/ivf/cycles/{cid}/witness", w.clinic,  # noqa: E731
                                {"witness_username": wname, "witness_password": wpass, **body})
    assert wit({"step": "freeze", "embryo_id": e1["id"], "cryo_location": "T1-C2-K3-S4"}).status_code == 422  # no consent
    consent = api.call("POST", "/admin/sp/consents", w.clinic, {"template": "ivf_embryo_freeze", "patient_id": w.patient,
                                                                "signer_name": "Lakshmi Devi"})
    assert consent.status_code == 201, consent.text
    ok = wit({"step": "freeze", "embryo_id": e1["id"], "cryo_location": "T1-C2-K3-S4", "consent_id": consent.json()["id"]})
    assert ok.status_code == 201, ok.text
    assert "row_hash" not in ok.json()
    assert wit({"step": "freeze", "embryo_id": e2["id"], "cryo_location": "T1-C2-K3-S4",
                "consent_id": consent.json()["id"]}).status_code == 409                       # straw already used
    assert wit({"step": "insemination"}).status_code == 201
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/witness", w.clinic, {
        "step": "insemination", "witness_username": wname, "witness_password": "wrong"}).status_code == 401
    selfname = _q(w.conn, "SELECT username FROM clinic_admins WHERE id = %s", w.doc_login)
    from app.routers.admin import hash_password
    _q(w.conn, "UPDATE clinic_admins SET password_hash = %s WHERE id = %s", hash_password("Self#2026"), w.doc_login)
    r = api.call("POST", f"/admin/sp/ivf/cycles/{cid}/witness", w.clinic, {"step": "insemination",
                                                                         "witness_username": selfname,
                                                                         "witness_password": "Self#2026"})
    assert r.status_code == 422 and "different person" in r.text                          # the performer cannot witness
    api.as_(w.u.admin)
    assert api.call("GET", "/admin/sp/ivf/witness/verify", w.clinic).json() == {"intact": True, "first_broken_seq": None}
    with pytest.raises(psycopg2.Error, match="ivf_witness_append_only"):
        _q(w.conn, "UPDATE ivf_witness_events SET note = 'edited' WHERE cycle_id = %s", cid)
    assert _q(w.conn, "SELECT ivf_witness_verify(%s)", w.clinic) is None
    # result hold → release; ART register
    api.as_(w.u.doctor)
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/release", w.clinic, {"outcome": "clinical"}).status_code == 200
    assert api.call("POST", f"/admin/sp/ivf/cycles/{cid}/release", w.clinic, {"outcome": "negative"}).status_code == 409
    api.as_(w.u.admin)
    csv_text = api.call("GET", "/admin/sp/ivf/art-register.csv", w.clinic).text
    assert "aadhaar …1234" in csv_text and "clinical" in csv_text and "Lakshmi" not in csv_text
    assert "sex" not in csv_text.lower()


def test_anc_timeline_and_immunisation(api):  # noqa: F811
    w = sp_world(api, plan="womenchild", packs=("anc", "imm"))
    api.as_(w.u.doctor)
    edd = date.today() + timedelta(days=280 - 7 * 24)                                     # 24 + 0 weeks today
    bad = api.call("POST", "/admin/sp/anc/pregnancies", w.clinic, {"patient_id": w.patient, "edd": str(edd),
                                                                   "edd_source": "lmp", "gravida": 2})
    assert bad.status_code == 422                                                         # LMP-based EDD needs the LMP
    p = api.call("POST", "/admin/sp/anc/pregnancies", w.clinic, {"patient_id": w.patient, "edd": str(edd),
                                                                 "edd_source": "dating_scan", "gravida": 2, "para": 1,
                                                                 "living": 1, "blood_group": "O-"})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    assert api.call("POST", "/admin/sp/anc/pregnancies", w.clinic, {"patient_id": w.patient, "edd": str(edd),
                                                                    "edd_source": "dating_scan", "gravida": 2}).status_code == 409
    v = api.call("POST", f"/admin/sp/anc/pregnancies/{pid}/visits", w.clinic, {
        "visit_date": str(date.today() - timedelta(days=14 * 7)), "bp_sys": 110, "bp_dia": 70, "weight_kg": 58.5})
    assert v.status_code == 201, v.text
    assert v.json()["ga_weeks"] == 10
    assert api.call("POST", f"/admin/sp/anc/pregnancies/{pid}/visits", w.clinic, {
        "visit_date": str(date.today()), "bp_sys": 110}).status_code == 422
    tl = api.call("GET", f"/admin/sp/anc/pregnancies/{pid}/timeline", w.clinic).json()
    assert tl["ga_today"] == {"weeks": 24, "days": 0} and tl["weeks"][0] == 0 and tl["weeks"][-1] == 40
    first = next(m for m in tl["markers"] if m["label"] == "First contact")
    assert first["state"] == "done"                                                       # the 10-week visit
    assert next(m for m in tl["markers"] if m["label"] == "Contact 4")["state"] == "upcoming"
    # immunisation for a child (the patient row is the child here)
    child = _patient(w.conn, w.clinic)
    dob = date.today() - timedelta(days=120)
    _q(w.conn, "UPDATE patients SET date_of_birth = %s WHERE id = %s", dob, child)
    s = api.call("GET", "/admin/sp/immunisation", w.clinic, patient_id=child, schedule="iap_2023").json()
    birth = [r for r in s["rows"] if r["age_label"] == "Birth"]
    assert {r["state"] for r in birth} == {"overdue"} and s["counts"]["given"] == 0
    dose = {"patient_id": child, "schedule_code": "iap_2023", "vaccine_code": "bcg", "dose_no": 1, "given_on": str(dob),
            "batch_no": "bcg-0425", "expiry": str(dob + timedelta(days=300)), "site": "left_arm", "brand": "BCG-SII"}
    assert api.call("POST", "/admin/sp/immunisation/doses", w.clinic, {**dose, "vaccine_code": "mmr", "dose_no": 9}).status_code == 422
    assert api.call("POST", "/admin/sp/immunisation/doses", w.clinic, {**dose, "expiry": str(dob - timedelta(days=1))}).status_code == 422
    assert api.call("POST", "/admin/sp/immunisation/doses", w.clinic, dose).status_code == 201
    assert api.call("POST", "/admin/sp/immunisation/doses", w.clinic, dose).status_code == 409
    s = api.call("GET", "/admin/sp/immunisation", w.clinic, patient_id=child, schedule="iap_2023").json()
    bcg = next(r for r in s["rows"] if r["vaccine_code"] == "bcg")
    assert bcg["state"] == "given" and bcg["given"]["batch_no"] == "BCG-0425"
    assert next(r for r in s["rows"] if r["label"] == "MMR 1")["state"] == "upcoming"
    no_dob = api.call("GET", "/admin/sp/immunisation", w.clinic, patient_id=w.patient).json()
    assert no_dob["rows"] == [] and no_dob["child"]["dob"] is None and "date of birth" in no_dob["note"]
    other = sp_world(api, plan="eye", packs=("eye",))
    api.as_(other.u.admin)
    assert api.call("GET", "/admin/sp/immunisation", other.clinic, patient_id=other.patient).status_code == 403
