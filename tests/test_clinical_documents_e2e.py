"""G9 clinical documents end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Inpatient stay with vitals, a procedure note and a signed LIS result ->
discharge summary signed (record frozen) -> official PDF -> WhatsApp send ->
amendment; recorded consents; gapless certificates; permissions and tenancy.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import clinical_documents as docs
from tests.test_ipd_api_e2e import _admit, _clinic, _doctor, _patient, _q, _setup_hospital, api  # noqa: F401


@pytest.fixture
def stay(api, monkeypatch):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, clinic)
    patient = _patient(api.conn, clinic, gender="male")
    _q(api.conn, "UPDATE patients SET opted_in = true WHERE id = %s", patient)
    adm = _admit(api, clinic, u, doctor, beds["101"], patient=patient)
    assert adm.status_code == 201, adm.text
    aid = adm.json()["id"]
    queued = []

    async def fake_quiet(clinic_id, summary_id):
        queued.append(summary_id)
    monkeypatch.setattr(docs, "_deliver_quietly", fake_quiet)

    t0 = datetime.now(timezone.utc) - timedelta(days=2)
    api.as_(u["nurse"])
    for when, pulse in ((t0, 112), (t0 + timedelta(days=2), 78)):
        assert api.call("POST", f"/admin/ipd/admissions/{aid}/observations", clinic,
                        {"kind": "vitals", "observed_at": when.isoformat(), "pulse_bpm": pulse,
                         "spo2_pct": 95}).status_code == 201
    api.as_(u["doctor"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/notes", clinic,
                    {"note_type": "procedure", "body": "Pleural tap, 300 ml straw fluid"}).status_code == 201

    # a signed LIS result on the stay's own investigation order
    cbc = str(_q(api.conn, "INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise) "
                           "VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id", clinic))
    hb = str(_q(api.conn, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, unit, normal_low, normal_high) "
                          "VALUES (%s, %s, 'Hemoglobin', 'g/dL', 13, 17) RETURNING id", clinic, cbc))
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/orders", clinic, {
        "order_type": "investigation", "description": "CBC", "lab_test_id": cbc,
        "idempotency_key": uuid.uuid4().hex}).status_code == 201
    order = str(_q(api.conn, "SELECT id FROM opd_orders WHERE admission_id = %s", aid))
    _q(api.conn, "UPDATE opd_orders SET status = 'sample_collected', specimen_barcode = 'IP-1', "
                 "sample_collected_at = now() WHERE id = %s", order)
    res = _q(api.conn, "SELECT lis_record_results(%s, %s, %s::jsonb, false, NULL, NULL, 'tech')", clinic, order,
             json.dumps([{"parameter_id": hb, "parameter_name": "Hemoglobin", "unit": "g/dL", "numeric_value": "11.2",
                          "flag": "low", "reference_range_display": "13 - 17 g/dL"}]))
    _q(api.conn, "SELECT lis_validate_result(%s, %s, %s, %s, %s::jsonb, '{}', '{}')", clinic, res["id"],
       res["updated_at"], str(uuid.uuid4()), json.dumps({"name": "Dr. Path", "registration_number": "TSMC/1/1"}))

    api.as_(u["doctor"])
    assert api.call("POST", f"/admin/ipd/admissions/{aid}/discharge/initiate", clinic,
                    {"discharge_type": "regular"}).status_code == 200
    yield {"clinic": clinic, "doctor": doctor, "u": u, "aid": aid, "patient": patient, "queued": queued}


def _summary(api, s, **fields):
    body = {"final_diagnosis": "Pneumonia with effusion", "course_in_hospital": "IV antibiotics, pleural tap",
            "discharge_medications": "Tab Amoxiclav 625 mg BD x 5 days", "follow_up": "OPD in 7 days", **fields}
    ds = api.as_(s["u"]["doctor"]).call("PUT", f"/admin/ipd/admissions/{s['aid']}/discharge-summary", s["clinic"], body)
    assert ds.status_code == 200, ds.text
    signed = api.call("POST", f"/admin/ipd/discharge-summaries/{ds.json()['id']}/sign", s["clinic"])
    assert signed.status_code == 200, signed.text
    return signed.json()


def test_signing_freezes_the_record_and_the_pdf_prints_from_it(api, stay):  # noqa: F811
    s, path = stay, f"/admin/ipd/admissions/{stay['aid']}/discharge-summary/pdf"
    assert api.as_(s["u"]["doctor"]).call("GET", path, s["clinic"]).status_code == 404     # nothing signed yet
    signed = _summary(api, s)
    snap = signed["clinical_snapshot"]
    assert snap["patient"]["gender"] == "male" and snap["admission"]["admission_number"].startswith("IPD-")
    assert snap["vitals"]["admission"]["pulse_bpm"] == 112 and snap["vitals"]["discharge"]["pulse_bpm"] == 78
    (lab,) = snap["lab_results"]
    assert lab["test_name"] == "CBC" and lab["values"][0]["flag"] == "low" and lab["values"][0]["value"] == "11.2"
    assert snap["procedure_notes"][0]["body"].startswith("Pleural tap")
    assert signed["letterhead_snapshot"]["clinic_name"] == "E2E Hospital"
    assert s["queued"] == [signed["id"]]                                        # WhatsApp send queued on signing

    # later chart activity does not change the signed document
    api.as_(s["u"]["nurse"]).call("POST", f"/admin/ipd/admissions/{s['aid']}/observations", s["clinic"], {
        "kind": "vitals", "observed_at": datetime.now(timezone.utc).isoformat(), "pulse_bpm": 140})
    pdf = api.call("GET", path, s["clinic"])                                    # a nurse may print it
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert _q(api.conn, "SELECT clinical_snapshot -> 'vitals' -> 'discharge' ->> 'pulse_bpm' "
                        "FROM ipd_discharge_summaries WHERE id = %s", signed["id"]) == "78"
    assert api.as_(s["u"]["billing"]).call("GET", path, s["clinic"]).status_code == 403


def test_an_amendment_is_version_two_and_version_one_still_prints(api, stay):  # noqa: F811
    s = stay
    v1 = _summary(api, s)
    v2 = _summary(api, s, discharge_medications="Tab Amoxiclav 625 mg TDS x 5 days", amend_reason="Dose corrected")
    assert (v2["version"], v2["supersedes_id"]) == (2, v1["id"])
    path = f"/admin/ipd/admissions/{s['aid']}/discharge-summary/pdf"
    assert "v2" in api.call("GET", path, s["clinic"]).headers["content-disposition"]
    old = api.call("GET", path, s["clinic"], version=1)
    assert old.status_code == 200 and "v1" in old.headers["content-disposition"]


def test_send_via_whatsapp_records_the_delivery(api, stay, monkeypatch):  # noqa: F811
    s = stay
    signed = _summary(api, s)
    from app.services.whatsapp import whatsapp_service
    sent = []

    async def upload_media(clinic, data, filename, content_type):
        return "media-1"

    async def can_freeform(clinic, phone):
        return True

    async def send_document(clinic, phone, **kw):
        sent.append((phone, kw["filename"]))
        kw["_capture"]["meta_message_id"] = "wamid.1"
        return True

    async def store(clinic_id, folder, row, pdf, filename):
        return f"{clinic_id}/documents/{folder}/{row['id']}/{filename}"
    monkeypatch.setattr(whatsapp_service, "upload_media", upload_media)
    monkeypatch.setattr(whatsapp_service, "_can_send_freeform", can_freeform)
    monkeypatch.setattr(whatsapp_service, "send_document", send_document)
    monkeypatch.setattr(docs, "_store", store)

    path = f"/admin/ipd/admissions/{s['aid']}/discharge-summary/pdf"
    assert api.as_(s["u"]["nurse"]).call("POST", path, s["clinic"]).status_code == 403   # nurses read, not send
    out = api.as_(s["u"]["doctor"]).call("POST", path, s["clinic"])
    assert out.status_code == 200 and out.json() == {"sent": True, "channel": "session", "reason": None}
    assert sent[0][1].startswith("Discharge_Summary_IPD-")
    row = _q(api.conn, "SELECT to_jsonb(d) FROM ipd_discharge_summaries d WHERE id = %s", signed["id"])
    assert (row["delivery_status"], row["send_count"], row["whatsapp_message_id"]) == ("sent", 1, "wamid.1")
    assert row["pdf_storage_path"].endswith(".pdf")

    _q(api.conn, "UPDATE patients SET opted_in = false WHERE id = %s", s["patient"])
    refused = api.call("POST", path, s["clinic"]).json()
    assert refused == {"sent": False, "channel": None, "reason": "no_whatsapp_consent"} and len(sent) == 1


def test_consents_are_recorded_once_and_never_change(api, stay):  # noqa: F811
    s = stay
    body = {"admission_id": s["aid"], "consent_type": "surgical", "language": "en", "title": "Consent for pleural tap",
            "procedure_name": "Thoracocentesis", "body_text": "I consent to the procedure explained to me in detail.",
            "risks_explained": "Bleeding, pneumothorax", "doctor_id": s["doctor"], "signer_relationship": "spouse",
            "signer_name": "Sita Devi", "idempotency_key": uuid.uuid4().hex}
    api.as_(s["u"]["doctor"])
    assert api.call("POST", "/admin/clinical/consents", s["clinic"], body).status_code == 422   # proxy needs a witness
    body.update(witness_name="Nurse Rani", witness_phone="+919812345678")
    first = api.call("POST", "/admin/clinical/consents", s["clinic"], body)
    assert first.status_code == 201, first.text
    c = first.json()
    assert c["patient_id"] == s["patient"] and c["is_signed"] and c["doctor_snapshot"]["registration_number"]
    assert c["patient_snapshot"]["admission_number"].startswith("IPD-")
    again = api.call("POST", "/admin/clinical/consents", s["clinic"], body).json()
    assert again["replayed"] is True and again["id"] == c["id"]

    listed = api.as_(s["u"]["nurse"]).call("GET", "/admin/clinical/consents", s["clinic"], admission_id=s["aid"])
    assert [x["id"] for x in listed.json()["consents"]] == [c["id"]]
    assert api.call("POST", "/admin/clinical/consents", s["clinic"], body).status_code == 403   # nurses don't record
    pdf = api.call("GET", f"/admin/clinical/consents/{c['id']}/pdf", s["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    with pytest.raises(Exception):
        _q(api.conn, "UPDATE clinical_consents SET signer_name = 'X' WHERE id = %s", c["id"])

    other = _clinic(api.conn)
    assert api.as_(s["u"]["admin"]).call("GET", f"/admin/clinical/consents/{c['id']}/pdf", other).status_code in (403, 404)


def test_certificates_are_numbered_without_gaps_signed_and_voidable(api, stay):  # noqa: F811
    s = stay
    start = date.today()
    body = {"patient_id": s["patient"], "admission_id": s["aid"], "certificate_type": "medical_leave",
            "diagnosis_text": "Pneumonia, recovering", "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=2)).isoformat()}
    api.as_(s["u"]["doctor"])
    a = api.call("POST", "/admin/clinical/certificates", s["clinic"], body)
    assert a.status_code == 201, a.text
    a = a.json()
    year = a["certificate_year"]
    assert a["certificate_number"] == f"CERT-{year}-00001" and a["rest_days_advised"] == 3
    assert a["doctor_snapshot"]["registration_number"] == "TSMC/2012/777"
    b = api.call("POST", "/admin/clinical/certificates", s["clinic"],
                 {"patient_id": s["patient"], "certificate_type": "fitness", "diagnosis_text": "Fit to resume work"}).json()
    assert b["certificate_number"] == f"CERT-{year}-00002"
    assert api.call("POST", "/admin/clinical/certificates", s["clinic"],
                    {**body, "end_date": (start - timedelta(days=1)).isoformat()}).status_code == 422
    pdf = api.call("GET", f"/admin/clinical/certificates/{a['id']}/pdf", s["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    listed = api.call("GET", "/admin/clinical/certificates", s["clinic"], patient_id=s["patient"]).json()
    assert [x["certificate_number"] for x in listed["certificates"]] == [b["certificate_number"], a["certificate_number"]]

    void = api.call("POST", f"/admin/clinical/certificates/{a['id']}/void", s["clinic"], {"reason": "Wrong dates"})
    assert void.status_code == 200 and void.json()["status"] == "void"
    assert api.call("POST", f"/admin/clinical/certificates/{a['id']}/void", s["clinic"],
                    {"reason": "Again please"}).status_code == 409
    assert api.call("POST", f"/admin/clinical/certificates/{a['id']}/send", s["clinic"]).status_code == 409
    assert api.call("GET", f"/admin/clinical/certificates/{a['id']}/pdf", s["clinic"]).status_code == 200   # VOID banner


def test_who_may_issue_a_certificate(api, stay):  # noqa: F811
    s = stay
    body = {"patient_id": s["patient"], "certificate_type": "fitness", "diagnosis_text": "Fit"}
    assert api.as_(s["u"]["admin"]).call("POST", "/admin/clinical/certificates", s["clinic"], body).status_code == 403
    assert api.as_(s["u"]["desk"]).call("POST", "/admin/clinical/certificates", s["clinic"], body).status_code == 403
    unreg = str(_q(api.conn, "INSERT INTO doctors (clinic_id, name, department, specialization, is_active) "
                             "VALUES (%s, 'Dr. NoReg', 'Medicine', 'GP', true) RETURNING id", s["clinic"]))
    noreg = api.U("drnoreg", "staff", s["clinic"], str(uuid.uuid4()), ["OPD_CLINICAL"], staff_role="DOCTOR",
                  doctor_id=unreg)
    assert api.as_(noreg).call("POST", "/admin/clinical/certificates", s["clinic"], body).status_code == 409
    other = _clinic(api.conn)
    foreign = _patient(api.conn, other)
    r = api.as_(s["u"]["doctor"]).call("POST", "/admin/clinical/certificates", s["clinic"],
                                       {**body, "patient_id": foreign})
    assert r.status_code == 404
    assert _q(api.conn, "SELECT count(*) FROM clinical_certificates WHERE clinic_id = %s", s["clinic"]) == 0
