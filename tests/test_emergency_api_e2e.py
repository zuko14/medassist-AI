"""G7 emergency end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Unknown-patient registration in one call, casualty board, re-triage, MLC record /
lock / police intimation PDF, disposition straight into an IPD bed, LAMA behind
the G9 refusal consent, death by a doctor only, identify & merge.
"""

import uuid

import pytest

from tests.test_ipd_api_e2e import _clinic, _patient, _q, _setup_hospital, api  # noqa: F401  (fixture)


@pytest.fixture
def er(api):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    return {"clinic": clinic, "doctor": doctor, "u": u, "beds": beds}


def _register(api, e, user="nurse", **body):  # noqa: F811
    payload = {"is_unknown": True, "estimated_gender": "male", "estimated_age": 40, "triage_category": "red",
               "arrival_mode": "ambulance", "chief_complaints": "RTA, head injury, GCS 9",
               "initial_vitals": {"bp_sys": 90, "bp_dia": 60, "pulse": 128, "spo2": 91, "gcs": 9},
               "idempotency_key": uuid.uuid4().hex, **body}
    return api.as_(e["u"][user]).call("POST", "/admin/emergency/register", e["clinic"], payload)


MLC = {"police_station_name": "Banjara Hills PS", "injury_type": "rto", "officer_name": "HC Ramesh",
       "officer_badge": "1123", "incident_place": "Road No 12", "injury_description": "CLW scalp 3 cm, abrasions L forearm"}


def test_rapid_unknown_registration_board_and_triage(api, er):  # noqa: F811
    e = er
    key = uuid.uuid4().hex
    r = _register(api, e, idempotency_key=key)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["patient_name"] == "Unknown Male ~40y" and v["visit_number"].startswith("EMG-")
    assert v["initial_vitals"]["pulse"] == 128 and v["initial_vitals"]["recorded_at"]
    assert _register(api, e, idempotency_key=key).json()["replayed"] is True
    green = _register(api, e, is_unknown=False, name="Asha", phone="+91" + str(9000000000 + uuid.uuid4().int % 10**9),
                      triage_category="green", chief_complaints="Sprained ankle").json()
    assert green["patient_name"] == "Asha" and green["is_unknown_patient"] is False

    board = api.as_(e["u"]["billing"]).call("GET", "/admin/emergency/visits", e["clinic"]).json()   # billing reads
    assert [x["id"] for x in board["visits"]][:2] == [v["id"], green["id"]]                         # RED first
    assert board["counts"]["red"] == 1 and board["counts"]["green"] == 1
    assert board["visits"][0]["is_temporary"] is True
    assert _register(api, e, user="billing").status_code == 403
    assert api.call("GET", "/admin/emergency/visits", e["clinic"], triage="green").json()["visits"][0]["id"] == green["id"]

    api.as_(e["u"]["doctor"])
    upd = api.call("PATCH", f"/admin/emergency/visits/{v['id']}", e["clinic"],
                   {"triage_category": "yellow", "attending_doctor_id": e["doctor"], "clinical_notes": "CT head: no bleed",
                    "vitals": {"pulse": 102, "spo2": 97}})
    assert upd.status_code == 200, upd.text
    assert (upd.json()["status"], upd.json()["triage_category"], upd.json()["latest_vitals"]["pulse"]) == \
        ("under_treatment", "yellow", 102)
    assert api.call("PATCH", f"/admin/emergency/visits/{v['id']}", e["clinic"],
                    {"vitals": {"pulse": 900}}).status_code == 422


def test_mlc_record_correct_lock_and_police_intimation(api, er):  # noqa: F811
    e = er
    v = _register(api, e).json()
    assert api.as_(e["u"]["nurse"]).call("POST", f"/admin/emergency/visits/{v['id']}/mlc", e["clinic"],
                                         MLC).status_code == 403                                  # doctors only
    api.as_(e["u"]["doctor"])
    first = api.call("POST", f"/admin/emergency/visits/{v['id']}/mlc", e["clinic"], MLC)
    assert first.status_code == 200, first.text
    m = first.json()
    assert m["mlc_number"].startswith("MLC-") and m["is_locked"] is False
    assert m["doctor_snapshot"]["registration_number"] == "TSMC/2012/777"
    locked = api.call("POST", f"/admin/emergency/visits/{v['id']}/mlc", e["clinic"],
                      {**MLC, "is_dying_declaration": True, "lock": True}).json()
    assert locked["is_locked"] and locked["mlc_number"] == m["mlc_number"] and locked["is_dying_declaration"]
    again = api.call("POST", f"/admin/emergency/visits/{v['id']}/mlc", e["clinic"], MLC)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "emergency_mlc_locked"
    pdf = api.as_(e["u"]["nurse"]).call("GET", f"/admin/emergency/visits/{v['id']}/mlc/pdf", e["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    board = api.call("GET", "/admin/emergency/visits", e["clinic"]).json()["visits"]
    assert next(x for x in board if x["id"] == v["id"])["mlc"]["is_locked"] is True


def test_disposition_admits_to_an_ipd_bed_in_one_step(api, er):  # noqa: F811
    e = er
    v = _register(api, e).json()
    api.as_(e["u"]["doctor"])
    body = {"action": "admitted_ipd", "notes": "For neuro observation",
            "ipd": {"admitting_doctor_id": e["doctor"], "bed_id": e["beds"]["ICU1"], "provisional_diagnosis": "Head injury"}}
    r = api.call("POST", f"/admin/emergency/visits/{v['id']}/disposition", e["clinic"], body)
    assert r.status_code == 200, r.text
    out = r.json()
    adm = out["admission"]
    assert out["status"] == "admitted_ipd" and out["ipd_admission_id"] == adm["id"]
    assert (adm["source"], adm["priority"], adm["status"]) == ("emergency", "emergency", "admitted")
    assert adm["reason"].startswith(f"Emergency {v['visit_number']}:")
    assert _q(api.conn, "SELECT status FROM ipd_beds WHERE id = %s", e["beds"]["ICU1"]) == "occupied"
    assert api.call("POST", f"/admin/emergency/visits/{v['id']}/disposition", e["clinic"], body).json()["replayed"] is True
    assert api.call("POST", f"/admin/emergency/visits/{v['id']}/disposition", e["clinic"],
                    {"action": "discharged"}).status_code == 409
    assert api.call("PATCH", f"/admin/emergency/visits/{v['id']}", e["clinic"],
                    {"clinical_notes": "late"}).status_code == 409


def test_lama_needs_the_refusal_consent_and_death_needs_a_doctor(api, er):  # noqa: F811
    e = er
    v = _register(api, e, triage_category="yellow").json()
    api.as_(e["u"]["nurse"])
    blocked = api.call("POST", f"/admin/emergency/visits/{v['id']}/disposition", e["clinic"], {"action": "lama"})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "emergency_lama_consent_missing"
    consent = api.as_(e["u"]["doctor"]).call("POST", "/admin/clinical/consents", e["clinic"], {
        "patient_id": v["patient_id"], "consent_type": "refusal_lama", "title": "Leaving against medical advice",
        "body_text": "We take the patient against medical advice; risks explained.", "signer_relationship": "relative",
        "signer_name": "Brother", "witness_name": "Nurse Rani", "idempotency_key": uuid.uuid4().hex})
    assert consent.status_code == 201, consent.text
    done = api.as_(e["u"]["nurse"]).call("POST", f"/admin/emergency/visits/{v['id']}/disposition", e["clinic"],
                                         {"action": "lama", "notes": "Family insisted"}).json()
    assert done["status"] == "lama" and done["lama_consent_id"] == consent.json()["id"]

    dead = _register(api, e, triage_category="black", chief_complaints="Brought dead").json()
    no = api.call("POST", f"/admin/emergency/visits/{dead['id']}/disposition", e["clinic"], {"action": "deceased"})
    assert no.status_code == 403 and no.json()["detail"]["code"] == "emergency_doctor_required"
    yes = api.as_(e["u"]["doctor"]).call("POST", f"/admin/emergency/visits/{dead['id']}/disposition", e["clinic"],
                                         {"action": "deceased", "notes": "Brought dead"})
    assert yes.status_code == 200 and yes.json()["time_of_death"]


def test_identify_and_merge(api, er):  # noqa: F811
    e = er
    v = _register(api, e).json()
    api.as_(e["u"]["doctor"]).call("POST", f"/admin/emergency/visits/{v['id']}/mlc", e["clinic"], {**MLC, "lock": True})
    real = _patient(api.conn, e["clinic"], gender="male")
    _q(api.conn, "UPDATE patients SET name = 'Suresh Reddy' WHERE id = %s", real)
    api.as_(e["u"]["desk"])
    found = api.call("GET", "/admin/emergency/patients/search", e["clinic"], q="suresh").json()["patients"]
    assert [p["id"] for p in found] == [real]                                  # temporary records never offered
    assert api.call("GET", "/admin/emergency/patients/search", e["clinic"], q="Unknown").json()["patients"] == []
    r = api.call("POST", "/admin/emergency/patients/merge", e["clinic"],
                 {"temp_patient_id": v["patient_id"], "target_patient_id": real, "reason": "Wife brought Aadhaar"})
    assert r.status_code == 200, r.text
    assert r.json()["merged_target_name"] == "Suresh Reddy" and r.json()["moved"]["emergency_mlc_records"] == 1
    row = next(x for x in api.call("GET", "/admin/emergency/visits", e["clinic"]).json()["visits"] if x["id"] == v["id"])
    assert (row["patient_name"], row["is_temporary"], row["original_patient_id"]) == ("Suresh Reddy", False, v["patient_id"])
    bad = api.call("POST", "/admin/emergency/patients/merge", e["clinic"],
                   {"temp_patient_id": real, "target_patient_id": v["patient_id"]})
    assert bad.status_code == 409
    assert api.as_(e["u"]["billing"]).call("POST", "/admin/emergency/patients/merge", e["clinic"],
                                           {"temp_patient_id": v["patient_id"], "target_patient_id": real}).status_code == 403
