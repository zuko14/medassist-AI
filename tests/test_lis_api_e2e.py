"""G2 LIS end to end: real HTTP routes, real Postgres (tests/ipd_pg_bridge.py).

Parameter set-up -> consult order -> sample collection -> worksheet with this
patient's ranges -> technician entry (server-side flags) -> pathologist queue
-> sign-off (order completed, PDF, WhatsApp hand-off) -> versioned amendment.
Plus the pure range / flag rules, which carry the clinical logic.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest

from app.services import lis
from tests.test_ipd_api_e2e import _clinic, _doctor, _patient, _q, api  # noqa: F401  (fixture)
from tests.test_orders_api_e2e import _visit


# ── pure rules: no database ──────────────────────────────────────────────────

def _band(**kw):
    base = {"id": str(uuid.uuid4()), "name": "Hb", "unit": "g/dL", "data_type": "numeric", "gender": "all",
            "min_age_days": 0, "max_age_days": 43800, "normal_low": None, "normal_high": None,
            "critical_low": None, "critical_high": None, "normal_text_value": None}
    return {**base, **kw}


def test_flags_check_critical_limits_before_normal_limits():
    p = _band(normal_low=13, normal_high=17, critical_low=7, critical_high=20)
    cases = {"6.9": "critical_low", "7": "low", "12.99": "low", "13": "normal", "17": "normal", "17.1": "high",
             "20": "high", "20.01": "critical_high"}
    for raw, want in cases.items():
        assert lis.flag_value(p, Decimal(raw), None) == want, raw
    assert lis.flag_value(None, Decimal("1"), None) == "not_evaluated"          # no band covers the patient
    assert lis.flag_value(_band(), Decimal("1"), None) == "not_evaluated"       # band without limits
    assert lis.flag_value(_band(normal_high=200), Decimal("250"), None) == "high"
    txt = _band(data_type="options", normal_text_value="Negative")
    assert lis.flag_value(txt, None, " negative ") == "normal"
    assert lis.flag_value(txt, None, "Positive") == "abnormal"


def test_range_selection_prefers_sex_specific_then_narrowest_band():
    allb = _band(normal_low=12, normal_high=16)
    male = _band(gender="male", normal_low=13, normal_high=17)
    child = _band(min_age_days=0, max_age_days=4380, normal_low=11, normal_high=14)
    rows = [allb, male, child]
    assert lis.select_range(rows, 40 * 365, "male") is male
    assert lis.select_range(rows, 40 * 365, "female") is allb
    assert lis.select_range(rows, 5 * 365, "female") is child
    assert lis.select_range(rows, None, None) is allb          # unknown age/sex: only the all-ages 'all' band
    assert lis.select_range([male], 40 * 365, None) is None    # unknown sex never borrows a male range
    assert lis.range_display(male) == "13 - 17 g/dL"
    assert lis.range_display(_band(normal_high=200, unit="mg/dL")) == "< 200 mg/dL"


def test_age_in_days_counts_from_when_the_age_was_recorded():
    from datetime import date
    assert lis.age_in_days(None) is None
    assert lis.age_in_days(1, "2026-01-01", today=date(2026, 1, 31)) == 365 + 30


# ── HTTP + Postgres ──────────────────────────────────────────────────────────

@pytest.fixture
def world(api, monkeypatch):  # noqa: F811
    clinic = _clinic(api.conn)
    doctor = _doctor(api.conn, clinic)
    path_doc = _doctor(api.conn, clinic)
    unreg_doc = str(_q(api.conn, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                                    VALUES (%s, 'Dr. NoReg', 'Lab', 'Pathology', true) RETURNING id""", clinic))
    cbc = str(_q(api.conn, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                              VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id""", clinic))
    U, uid = api.U, lambda: str(uuid.uuid4())
    users = {
        "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK", "OPD_BILLING"], staff_role="FRONT_DESK"),
        "doctor": U("drmeena", "staff", clinic, uid(), ["OPD_CLINICAL"], staff_role="DOCTOR", doctor_id=doctor),
        # saved before migration 118: the role implies LAB_ENTRY / LAB_VALIDATE
        "tech": U("tech", "staff", clinic, uid(), ["ORDERS_READ", "ORDERS_COLLECT", "ORDERS_WRITE"],
                  staff_role="LAB_OPERATOR"),
        "path": U("drpath", "staff", clinic, uid(), [], staff_role="PATHOLOGIST", doctor_id=path_doc),
        "unreg": U("drnoreg", "staff", clinic, uid(), [], staff_role="PATHOLOGIST", doctor_id=unreg_doc),
        "phleb": U("phleb", "staff", clinic, uid(), [], staff_role="PHLEBOTOMIST"),
        "admin": U("admin", "clinic_admin", clinic, uid()),
    }
    sent = []

    async def fake_deliver(clinic_id, result_id):
        sent.append(result_id)
    w = {"clinic": clinic, "doctor": doctor, "cbc": cbc, "u": users, "sent": sent, "deliver": lis.deliver_report}
    monkeypatch.setattr(lis, "deliver_report", fake_deliver)

    api.as_(users["admin"])
    for body in ({"name": "Hemoglobin", "unit": "g/dL", "gender": "male", "normal_low": 13, "normal_high": 17,
                  "critical_low": 7, "critical_high": 20, "sort_order": 1},
                 {"name": "Hemoglobin", "unit": "g/dL", "gender": "female", "normal_low": 12, "normal_high": 15,
                  "critical_low": 7, "critical_high": 20, "sort_order": 1},
                 {"name": "WBC", "unit": "/cumm", "normal_low": 4000, "normal_high": 11000, "sort_order": 2},
                 {"name": "Malaria antigen", "data_type": "options", "options": ["Negative", "Positive"],
                  "normal_text_value": "Negative", "sort_order": 3}):
        r = api.call("POST", f"/admin/lis/tests/{cbc}/parameters", clinic, body)
        assert r.status_code == 201, r.text
    yield w
    _q(api.conn, "UPDATE appointments SET status = 'completed' WHERE clinic_id = %s AND is_walk_in", clinic)


def _collected_order(api, w, gender="male", branch=None):  # noqa: F811
    patient = _patient(api.conn, w["clinic"], gender=gender)
    desk = w["u"]["desk"] if not branch else api.U("d_" + uuid.uuid4().hex[:5], "staff", w["clinic"],
                                                    str(uuid.uuid4()), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK",
                                                    branch_id=branch)
    api.as_(desk)
    walk = api.call("POST", "/admin/opd/walk-ins", w["clinic"],
                    {"patient_id": patient, "doctor_id": w["doctor"], "skip_vitals": True})
    assert walk.status_code == 201, walk.text
    enc = api.as_(w["u"]["doctor"]).call("GET", f"/admin/opd/encounters/by-appointment/{walk.json()['appointment_id']}",
                                         w["clinic"]).json()["id"]
    placed = api.call("POST", f"/admin/opd/encounters/{enc}/orders", w["clinic"], {
        "orders": [{"order_type": "investigation", "catalog_type": "lab_test", "catalog_reference_id": w["cbc"]}],
        "idempotency_key": uuid.uuid4().hex})
    assert placed.status_code == 201, placed.text
    order = placed.json()["orders"][0]["id"]
    assert api.as_(w["u"]["phleb"]).call("POST", f"/admin/diagnostic/orders/{order}/collect", w["clinic"],
                                         {"specimen_barcode": "S-" + uuid.uuid4().hex[:8]}).status_code == 200
    return order


def _enter(api, w, order, hb="6.5", wbc="12000", malaria="Negative", reason=None):
    sheet = api.as_(w["u"]["tech"]).call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).json()
    ids = {p["name"]: p["parameter_id"] for p in sheet["parameters"]}
    body = {"values": [{"parameter_id": ids["Hemoglobin"], "value": hb},
                       {"parameter_id": ids["WBC"], "value": wbc},
                       {"parameter_id": ids["Malaria antigen"], "value": malaria}]}
    if reason:
        body["amendment_reason"] = reason
    return api.call("POST", f"/admin/lis/orders/{order}/results", w["clinic"], body)


def test_full_workflow_entry_flags_signoff_and_pdf(api, world):  # noqa: F811
    w = world
    order = _collected_order(api, w, gender="male")
    sheet = api.as_(w["u"]["tech"]).call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"])
    assert sheet.status_code == 200, sheet.text
    lines = {p["name"]: p for p in sheet.json()["parameters"]}
    assert list(lines) == ["Hemoglobin", "WBC", "Malaria antigen"]
    assert lines["Hemoglobin"]["reference_range_display"] == "13 - 17 g/dL"   # the male band for a male patient

    saved = _enter(api, w, order)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    flags = {v["parameter_name"]: v["flag"] for v in body["values"]}
    assert flags == {"Hemoglobin": "critical_low", "WBC": "high", "Malaria antigen": "normal"}
    assert body["has_critical_value"] is True and body["status"] == "entered"
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", order) == "in_progress"
    # a text note cannot close an order whose values await the pathologist
    assert api.as_(w["u"]["admin"]).call("POST", f"/admin/diagnostic/orders/{order}/complete", w["clinic"],
                                         {"result_summary": "Normal"}).status_code == 409
    api.as_(w["u"]["tech"])

    # the technician cannot sign; the pathologist's queue shows it, critical first
    assert api.call("POST", f"/admin/lis/results/{body['id']}/validate", w["clinic"],
                    {"expected_updated_at": body["updated_at"]}).status_code == 403
    api.as_(w["u"]["path"])
    queue = api.call("GET", "/admin/lis/results", w["clinic"]).json()["results"]
    row = next(r for r in queue if r["id"] == body["id"])
    assert row["has_critical_value"] and row["patient_name"] and row["test_name"] == "CBC"
    assert queue[0]["has_critical_value"] is True

    signed = api.call("POST", f"/admin/lis/results/{body['id']}/validate", w["clinic"],
                      {"expected_updated_at": body["updated_at"]})
    assert signed.status_code == 200, signed.text
    s = signed.json()
    assert s["status"] == "validated" and s["pathologist_snapshot"]["registration_number"] == "TSMC/2012/777"
    assert s["patient_snapshot"]["gender"] == "male" and s["letterhead_snapshot"]["clinic_name"] == "E2E Hospital"
    assert _q(api.conn, "SELECT status FROM opd_orders WHERE id = %s", order) == "completed"
    assert w["sent"] == [body["id"]]                                           # WhatsApp hand-off queued once
    again = api.call("POST", f"/admin/lis/results/{body['id']}/validate", w["clinic"],
                     {"expected_updated_at": body["updated_at"]})
    assert again.status_code == 200 and again.json()["replayed"] is True and w["sent"] == [body["id"]]

    pdf = api.as_(w["u"]["doctor"]).call("GET", f"/admin/lis/results/{body['id']}/pdf", w["clinic"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert pdf.headers["content-type"] == "application/pdf"


def test_female_patient_gets_the_female_band(api, world):  # noqa: F811
    w = world
    order = _collected_order(api, w, gender="female")
    sheet = api.as_(w["u"]["tech"]).call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).json()
    assert {p["name"]: p["reference_range_display"] for p in sheet["parameters"]}["Hemoglobin"] == "12 - 15 g/dL"
    vals = {v["parameter_name"]: v["flag"] for v in _enter(api, w, order, hb="16").json()["values"]}
    assert vals["Hemoglobin"] == "high"           # high for a woman; would be normal for a man


def test_stale_review_and_unregistered_signer_are_refused(api, world):  # noqa: F811
    w = world
    order = _collected_order(api, w)
    first = _enter(api, w, order).json()
    _enter(api, w, order, hb="14")                 # the technician corrects after the pathologist opened it
    api.as_(w["u"]["path"])
    stale = api.call("POST", f"/admin/lis/results/{first['id']}/validate", w["clinic"],
                     {"expected_updated_at": first["updated_at"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "lis_result_changed"
    current = api.call("GET", f"/admin/lis/results/{first['id']}", w["clinic"]).json()
    unreg = api.as_(w["u"]["unreg"]).call("POST", f"/admin/lis/results/{first['id']}/validate", w["clinic"],
                                          {"expected_updated_at": current["updated_at"]})
    assert unreg.status_code == 409 and unreg.json()["detail"]["code"] == "lis_signer_registration_missing"
    assert _q(api.conn, "SELECT status FROM lab_results WHERE id = %s", first["id"]) == "entered"


def test_correcting_a_signed_report_is_a_versioned_amendment(api, world):  # noqa: F811
    w = world
    order = _collected_order(api, w)
    v1 = _enter(api, w, order).json()
    api.as_(w["u"]["path"]).call("POST", f"/admin/lis/results/{v1['id']}/validate", w["clinic"],
                                 {"expected_updated_at": v1["updated_at"]})
    no_reason = _enter(api, w, order, hb="14")
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "lis_amendment_reason_required"
    v2 = _enter(api, w, order, hb="14", reason="Sample re-run after clot").json()
    assert (v2["version"], v2["supersedes_id"], v2["has_critical_value"]) == (2, v1["id"], False)
    sheet = api.call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).json()
    assert [v["version"] for v in sheet["versions"]] == [1, 2]
    signed = api.as_(w["u"]["path"]).call("POST", f"/admin/lis/results/{v2['id']}/validate", w["clinic"],
                                          {"expected_updated_at": v2["updated_at"]})
    assert signed.status_code == 200, signed.text
    assert _q(api.conn, "SELECT status FROM lab_results WHERE id = %s", v1["id"]) == "amended"
    old_pdf = api.call("GET", f"/admin/lis/results/{v1['id']}/pdf", w["clinic"])   # the superseded copy still prints
    assert old_pdf.status_code == 200 and old_pdf.content[:4] == b"%PDF"


def test_bad_input_wrong_state_and_branch_scope(api, world):  # noqa: F811
    w = world
    a = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'A') RETURNING id", w["clinic"]))
    b = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'B') RETURNING id", w["clinic"]))
    order = _collected_order(api, w, branch=a)
    api.as_(w["u"]["tech"])
    sheet = api.call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).json()
    hb = next(p["parameter_id"] for p in sheet["parameters"] if p["name"] == "Hemoglobin")
    for values, code in (([{"parameter_id": str(uuid.uuid4()), "value": "1"}], 422),     # not on the worksheet
                         ([{"parameter_id": hb, "value": "abc"}], 422),                  # not a number
                         ([{"parameter_id": hb, "value": ""}], 422)):                    # nothing entered
        assert api.call("POST", f"/admin/lis/orders/{order}/results", w["clinic"], {"values": values}).status_code == code
    malaria = next(p["parameter_id"] for p in sheet["parameters"] if p["name"] == "Malaria antigen")
    assert api.call("POST", f"/admin/lis/orders/{order}/results", w["clinic"],
                    {"values": [{"parameter_id": malaria, "value": "Maybe"}]}).status_code == 422
    other_branch = api.U("tb", "staff", w["clinic"], str(uuid.uuid4()), [], staff_role="LAB_OPERATOR", branch_id=b)
    assert api.as_(other_branch).call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).status_code == 403
    # phlebotomists and front desk never touch results
    assert api.as_(w["u"]["phleb"]).call("GET", f"/admin/lis/orders/{order}/worksheet", w["clinic"]).status_code == 403
    assert api.as_(w["u"]["desk"]).call("GET", "/admin/lis/results", w["clinic"]).status_code == 403

    # an order whose sample is not collected has no results yet
    walkin = _visit(api, {**w, "u": {**w["u"]}})[1]
    placed = api.as_(w["u"]["doctor"]).call("POST", f"/admin/opd/encounters/{walkin}/orders", w["clinic"], {
        "orders": [{"order_type": "investigation", "catalog_type": "lab_test", "catalog_reference_id": w["cbc"]}],
        "idempotency_key": uuid.uuid4().hex}).json()["orders"][0]["id"]
    api.as_(w["u"]["tech"])
    own = api.call("GET", f"/admin/lis/orders/{placed}/worksheet", w["clinic"]).json()["parameters"][0]
    early = api.call("POST", f"/admin/lis/orders/{placed}/results", w["clinic"],
                     {"values": [{"parameter_id": own["parameter_id"], "value": "14"}]})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "lis_order_not_ready"


def test_parameter_management_needs_lab_tests_manage_and_stays_in_tenant(api, world):  # noqa: F811
    w = world
    body = {"name": "Platelets", "unit": "/cumm", "normal_low": 150000, "normal_high": 450000}
    assert api.as_(w["u"]["tech"]).call("POST", f"/admin/lis/tests/{w['cbc']}/parameters", w["clinic"],
                                        body).status_code == 403
    api.as_(w["u"]["admin"])
    bad = dict(body, normal_low=500000)
    assert api.call("POST", f"/admin/lis/tests/{w['cbc']}/parameters", w["clinic"], bad).status_code == 422
    created = api.call("POST", f"/admin/lis/tests/{w['cbc']}/parameters", w["clinic"], body).json()
    off = api.call("PUT", f"/admin/lis/tests/{w['cbc']}/parameters/{created['id']}", w["clinic"],
                   dict(body, is_active=False))
    assert off.status_code == 200 and off.json()["is_active"] is False
    other = _clinic(api.conn)
    foreign = str(_q(api.conn, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                                  VALUES (%s, 'LFT', 'Serum', 50000) RETURNING id""", other))
    assert api.call("GET", f"/admin/lis/tests/{foreign}/parameters", w["clinic"]).status_code == 404
    assert api.call("POST", f"/admin/lis/tests/{foreign}/parameters", w["clinic"], body).status_code == 404


def test_delivery_hands_the_signed_pdf_to_the_lab_report_pipeline(api, world, monkeypatch):  # noqa: F811
    w = world
    order = _collected_order(api, w)
    r = _enter(api, w, order).json()
    api.as_(w["u"]["path"]).call("POST", f"/admin/lis/results/{r['id']}/validate", w["clinic"],
                                 {"expected_updated_at": r["updated_at"]})
    calls = []

    async def fake_upload_and_send(self, **kw):
        calls.append(kw)
        return {"status": "sent", "file_path": f"{kw['clinic_id']}/x/report.pdf", "error_message": None}
    from app.services.lab_reports import LabReportService
    monkeypatch.setattr(LabReportService, "upload_and_send", fake_upload_and_send)
    asyncio.run(w["deliver"](w["clinic"], r["id"]))   # the real deliver_report (the fixture stubs the auto-send)
    (kw,) = calls
    assert kw["file_bytes"][:4] == b"%PDF" and kw["external_report_id"] == f"lis-{r['id']}"
    assert kw["matched_patient_id"] and kw["source"] == "lis" and kw["report_name"] == "CBC"
    assert _q(api.conn, "SELECT pdf_storage_path FROM lab_results WHERE id = %s", r["id"]) == \
        f"{w['clinic']}/x/report.pdf"
