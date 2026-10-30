"""G13 radiology end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Consult order -> imaging worklist -> draft -> radiologist sign-off (order
completed, PDF, WhatsApp hand-off) -> versioned amendment; the obstetric path
(Form F mandatory, locked, its own PDF); the PCPNDT firewall at the API; and
permission, doctor-link and tenant boundaries. Plus the pure modality rules.
"""

import asyncio
import uuid
from datetime import date, timedelta

import pytest

from app.services import radiology
from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)


# ── pure rules: no database ──────────────────────────────────────────────────

@pytest.mark.parametrize("name,want", [
    ("X-ray chest PA", "x_ray"), ("XRAY KUB", "x_ray"), ("Digital radiograph left knee", "x_ray"),
    ("USG abdomen & pelvis", "ultrasound"), ("Obstetric Doppler", "ultrasound"), ("NT scan", "ultrasound"),
    ("CT brain plain", "ct"), ("HRCT thorax", "ct"), ("MRI LS spine", "mri"), ("MRCP", "mri"),
    ("Mammography bilateral", "mammography"), ("DEXA scan", "dexa"), ("Barium swallow", "fluoroscopy"),
    ("CBC", None), ("ECG", None), ("Consultation", None)])
def test_modality_detection(name, want):
    assert radiology.detect_modality(name) == want


def test_classification_uses_the_tariff_master_category():
    cat = {"RAD1": {"name": "Head scan", "category": "radiology"}, "PHY": {"name": "Physio", "category": "physiotherapy"}}
    assert radiology.classify_order({"catalog_type": "service_catalog", "catalog_code": "RAD1", "item_name": "Head scan"},
                                    cat) == "other"
    assert radiology.classify_order({"catalog_type": "service_catalog", "catalog_code": "PHY", "item_name": "Physio"},
                                    cat) is None
    assert radiology.classify_order({"catalog_type": "lab_test", "item_name": "X-ray"}, cat) is None


# ── HTTP + Postgres ──────────────────────────────────────────────────────────

@pytest.fixture
def world(api, monkeypatch):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    rad_doc = _doctor(api.conn, clinic)
    unreg_doc = str(_q(api.conn, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                                    VALUES (%s, 'Dr. NoReg', 'Radiology', 'Radiologist', true) RETURNING id""", clinic))
    _q(api.conn, """INSERT INTO billable_services (clinic_id, service_code, name, category, standard_price_paise)
                    VALUES (%s, 'RAD_HEAD', 'Head scan plain', 'radiology', 250000) RETURNING id""", clinic)
    other = _clinic(api.conn)
    U, uid = api.U, lambda: str(uuid.uuid4())
    users = {
        "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK", "OPD_BILLING"], staff_role="FRONT_DESK"),
        "doctor": U("drmeena", "staff", clinic, uid(), ["OPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor),
        # saved before migration 123: the role alone implies RADIOLOGY_READ / RADIOLOGY_WRITE
        "rad": U("drray", "staff", clinic, uid(), [], staff_role="RADIOLOGIST", doctor_id=rad_doc),
        "unreg": U("drnoreg", "staff", clinic, uid(), [], staff_role="RADIOLOGIST", doctor_id=unreg_doc),
        "nurse": U("nurse", "staff", clinic, uid(), ["IPD_NURSING"], staff_role="NURSE"),
        "cashier": U("cash", "staff", clinic, uid(), ["OPD_BILLING"], staff_role="CASHIER"),
        "admin": U("admin", "clinic_admin", clinic, uid()),
        "other_admin": U("oadmin", "clinic_admin", other, uid()),
    }
    sent = []

    async def fake_deliver(clinic_id, report_id):
        sent.append(report_id)
    w = {"clinic": clinic, "other": other, "doctor": doctor, "u": users, "sent": sent,
         "deliver": radiology.deliver_report}
    monkeypatch.setattr(radiology, "deliver_report", fake_deliver)
    yield w
    _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def _place(api, w, items, gender="female"):  # noqa: F811
    patient = _patient(api.conn, w["clinic"], gender=gender)
    api.as_(w["u"]["desk"])
    walk = api.call("POST", "/admin/opd/walk-ins", w["clinic"],
                    {"patient_id": patient, "doctor_id": w["doctor"], "skip_vitals": True})
    assert walk.status_code == 201, walk.text
    enc = api.as_(w["u"]["doctor"]).call("GET", f"/admin/opd/encounters/by-appointment/{walk.json()['appointment_id']}",
                                         w["clinic"]).json()["id"]
    placed = api.call("POST", f"/admin/opd/encounters/{enc}/orders", w["clinic"],
                      {"orders": items, "idempotency_key": uuid.uuid4().hex})
    assert placed.status_code == 201, placed.text
    return [o["id"] for o in placed.json()["orders"]]


def _custom(name, **kw):
    return {"order_type": "investigation", "catalog_type": "custom", "item_name": name, **kw}


def _save(api, w, order, user="rad", **body):
    payload = {"order_id": order, "findings": "Lungs clear. Cardiac silhouette normal.",
               "impression": "Normal chest radiograph.", **body}
    return api.as_(w["u"][user]).call("POST", "/admin/radiology/reports", w["clinic"], payload)


FORM_F = {"patient_age": 27, "relative_name": "Ramesh Kumar", "referred_by": "Dr. Meena", "num_living_children": 1,
          "num_female_children": 1, "num_male_children": 0, "gestational_age_weeks": 20.0,
          "clinical_indication": "anomaly_scan_target", "invasive_procedure_done": False,
          "pregnant_woman_declaration_signed": True, "doctor_declaration_signed": True}


def _form(**over):
    return {**FORM_F, "last_menstrual_period": str(date.today() - timedelta(weeks=20)), **over}


def test_xray_workflow_worklist_draft_sign_pdf_and_amendment(api, world):  # noqa: F811
    w = world
    xray, cbc_free = _place(api, w, [_custom("X-ray chest PA", urgency="stat"), _custom("Consultation note")])

    api.as_(w["u"]["rad"])
    wl = api.call("GET", "/admin/radiology/worklist", w["clinic"])
    assert wl.status_code == 200, wl.text
    item = next(i for i in wl.json()["items"] if i["order_id"] == xray)
    assert (item["modality"], item["urgency"], item["report"]) == ("x_ray", "stat", None)
    assert cbc_free not in {i["order_id"] for i in wl.json()["items"]}        # not imaging
    assert xray not in {i["order_id"] for i in api.call("GET", "/admin/radiology/worklist", w["clinic"],
                                                         modality="ultrasound").json()["items"]}
    console = api.call("GET", f"/admin/radiology/orders/{xray}", w["clinic"]).json()
    assert console["report"] is None and console["suggested"]["modality"] == "x_ray"

    # read-only roles see, cannot write; billing sees nothing
    assert api.as_(w["u"]["nurse"]).call("GET", "/admin/radiology/worklist", w["clinic"]).status_code == 200
    assert _save(api, w, xray, user="nurse").status_code == 403
    assert api.as_(w["u"]["cashier"]).call("GET", "/admin/radiology/worklist", w["clinic"]).status_code == 403

    draft = _save(api, w, xray, pacs_viewer_url="https://pacs.local/viewer?study=1.2.840.1",
                  pacs_study_uid="1.2.840.1")
    assert draft.status_code == 200, draft.text
    d = draft.json()
    assert d["status"] == "draft" and d["report_number"].startswith("RAD-") and d["is_obstetric"] is False
    # a text note cannot close a study whose report is in progress
    assert api.as_(w["u"]["admin"]).call("POST", f"/admin/diagnostic/orders/{xray}/complete", w["clinic"],
                                         {"result_summary": "Normal"}).status_code == 409

    assert api.as_(w["u"]["unreg"]).call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                                         {"expected_updated_at": d["updated_at"]}).status_code == 409
    assert api.as_(w["u"]["admin"]).call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                                         {"expected_updated_at": d["updated_at"]}).status_code == 403  # no doctor link
    signed = api.as_(w["u"]["rad"]).call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                                         {"expected_updated_at": d["updated_at"]})
    assert signed.status_code == 200, signed.text
    s = signed.json()
    assert s["status"] == "finalized" and s["radiologist_snapshot"]["registration_number"] == "TSMC/2012/777"
    assert s["letterhead_snapshot"]["clinic_name"] == "E2E Hospital"
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", xray) == "completed"
    assert w["sent"] == [d["id"]]
    again = api.call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                     {"expected_updated_at": d["updated_at"]})
    assert again.status_code == 200 and again.json()["replayed"] is True and w["sent"] == [d["id"]]

    pdf = api.as_(w["u"]["nurse"]).call("GET", f"/admin/radiology/reports/{d['id']}/pdf", w["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    done = api.as_(w["u"]["rad"]).call("GET", "/admin/radiology/worklist", w["clinic"], status="completed").json()
    assert d["id"] in {i["report"]["id"] for i in done["items"]}

    # a correction is a new version under the same number
    assert _save(api, w, xray).status_code == 422
    v2 = _save(api, w, xray, impression="Mild cardiomegaly.", amendment_reason="Cardiomegaly missed on first read")
    assert v2.status_code == 200, v2.text
    assert (v2.json()["version"], v2.json()["report_number"]) == (2, d["report_number"])
    s2 = api.call("POST", f"/admin/radiology/reports/{v2.json()['id']}/finalize", w["clinic"],
                  {"expected_updated_at": v2.json()["updated_at"]}).json()
    detail = api.call("GET", f"/admin/radiology/reports/{d['id']}", w["clinic"]).json()
    assert detail["status"] == "amended" and [v["status"] for v in detail["versions"]] == ["amended", "finalized"]
    assert s2["supersedes_id"] == d["id"]


def test_obstetric_ultrasound_requires_and_locks_form_f(api, world):  # noqa: F811
    w = world
    (usg,) = _place(api, w, [_custom("USG Obstetric Level II")])
    console = api.as_(w["u"]["rad"]).call("GET", f"/admin/radiology/orders/{usg}", w["clinic"]).json()
    assert console["suggested"] == {"modality": "ultrasound", "procedure_name": "USG Obstetric Level II",
                                    "is_obstetric": True, "clinical_history": None}
    d = _save(api, w, usg, modality="ultrasound", findings="Single live intrauterine gestation. Placenta posterior.",
              impression="Live intrauterine pregnancy of 20 weeks.").json()
    assert d["is_obstetric"] is True

    refused = api.call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                       {"expected_updated_at": d["updated_at"]})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "radiology_form_f_required"
    assert api.call("GET", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f/pdf", w["clinic"]).status_code == 404

    # Form F: shape checks, the PCPNDT guard, then lock
    assert api.call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"],
                    _form(num_male_children=1)).status_code == 422
    assert api.call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"],
                    _form(doctor_declaration_signed=False)).status_code == 422
    bad = api.call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"],
                   _form(clinical_indication="other_medical_indication", indication_notes="Parents want gender test"))
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "radiology_pcpndt_violation"
    assert api.as_(w["u"]["admin"]).call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"],
                                         _form()).status_code == 403     # a doctor signs Form F
    f = api.as_(w["u"]["rad"]).call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"], _form())
    assert f.status_code == 200, f.text
    form = f.json()
    assert form["form_f_number"].startswith("PNDT-") and form["is_locked"] and form["replayed"] is False
    assert form["letterhead_snapshot"]["clinic_name"] == "E2E Hospital"
    assert api.call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"],
                    _form(patient_age=40)).json()["id"] == form["id"]       # locked: the recorded form comes back
    fpdf = api.call("GET", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f/pdf", w["clinic"])
    assert fpdf.status_code == 200 and fpdf.content[:4] == b"%PDF"
    # Form F is the writers' register, not every report reader's
    assert api.as_(w["u"]["nurse"]).call("GET", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f/pdf",
                                         w["clinic"]).status_code == 403

    wl = api.as_(w["u"]["rad"]).call("GET", "/admin/radiology/worklist", w["clinic"], modality="ultrasound").json()
    row = next(i for i in wl["items"] if i["order_id"] == usg)
    assert row["needs_form_f"] is False and row["form_f_number"] == form["form_f_number"]

    signed = api.call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"],
                      {"expected_updated_at": d["updated_at"]})
    assert signed.status_code == 200, signed.text
    assert signed.json()["patient_snapshot"]["form_f_number"] == form["form_f_number"]
    assert api.call("GET", f"/admin/radiology/reports/{d['id']}/pdf", w["clinic"]).content[:4] == b"%PDF"


def test_pcpndt_firewall_refuses_fetal_sex_text_before_saving(api, world):  # noqa: F811
    w = world
    usg, xray = _place(api, w, [_custom("USG Obstetric"), _custom("X-ray chest PA")])
    r = _save(api, w, usg, modality="ultrasound", findings="Single live female fetus.", impression="Normal.")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "radiology_pcpndt_violation"
    r = _save(api, w, usg, modality="ultrasound", findings="Genitalia not commented.", impression="Normal.")
    assert r.status_code == 400                                 # obstetric: zero tolerance on genital words
    assert _q(api.conn, "SELECT count(*) FROM radiology_reports WHERE order_id = %s", usg) == 0
    # a man's own anatomy on a non-obstetric study is fine; the explicit phrase never is
    assert _save(api, w, xray, findings="Normal male chest.", impression="Normal.").status_code == 200
    assert _save(api, w, xray, findings="Sex of the baby", impression="x").status_code == 400


def test_boundaries_lab_orders_bad_links_men_and_other_tenants(api, world):  # noqa: F811
    w = world
    cbc = str(_q(api.conn, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                              VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id""", w["clinic"]))
    lab, head = _place(api, w, [{"order_type": "investigation", "catalog_type": "lab_test", "catalog_reference_id": cbc},
                                {"order_type": "investigation", "catalog_type": "service_catalog",
                                 "catalog_code": "RAD_HEAD"}])
    assert _save(api, w, lab).status_code == 409
    assert _save(api, w, head, pacs_viewer_url="javascript:alert(1)").status_code == 422
    d = _save(api, w, head)
    assert d.status_code == 200 and d.json()["modality"] == "other"           # the master's 'radiology' category

    (male_usg,) = _place(api, w, [_custom("USG Obstetric")], gender="male")
    assert _save(api, w, male_usg, modality="ultrasound").status_code == 422

    api.as_(w["u"]["other_admin"])
    assert api.call("GET", f"/admin/radiology/reports/{d.json()['id']}", w["other"]).status_code == 404
    assert api.call("GET", f"/admin/radiology/orders/{head}", w["other"]).status_code == 404
    assert api.call("GET", "/admin/radiology/worklist", w["other"]).json()["items"] == []


def test_delivery_skips_the_ai_summary_for_obstetric_reports(api, world, monkeypatch):  # noqa: F811
    w = world
    (usg,) = _place(api, w, [_custom("USG Obstetric")])
    d = _save(api, w, usg, modality="ultrasound", findings="Single live intrauterine gestation.",
              impression="Live pregnancy.").json()
    api.call("POST", f"/admin/radiology/reports/{d['id']}/pcpndt-form-f", w["clinic"], _form())
    api.call("POST", f"/admin/radiology/reports/{d['id']}/finalize", w["clinic"], {"expected_updated_at": d["updated_at"]})
    calls = []

    async def fake_send(self, **kw):
        calls.append(kw)
        return {"status": "sent", "file_path": "reports/x.pdf"}
    from app.services.lab_reports import LabReportService
    monkeypatch.setattr(LabReportService, "upload_and_send", fake_send)
    asyncio.run(w["deliver"](w["clinic"], d["id"]))
    assert calls and calls[0]["ai_summary"] is False and calls[0]["external_report_id"] == f"rad-{d['id']}"
    assert _q(api.conn, "SELECT pdf_storage_path FROM radiology_reports WHERE id = %s", d["id"]) == "reports/x.pdf"
