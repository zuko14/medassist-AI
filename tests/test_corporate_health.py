"""Corporate employee-health insights (migration 095).

Report text below is SYNTHETIC — the layout of the lab's LIS reports
(header per page, value line = name value unit range, extra range bands on
following lines) with invented patients. No real report is committed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.routers.admin import AdminUser, _enforce_corporate_viewer_scope, verify_credentials
from app.services import corporate_health as ch
from app.services.tenant import corporate_health_enabled

CLINIC = "11111111-1111-1111-1111-111111111111"
OTHER_CLINIC = "22222222-2222-2222-2222-222222222222"
COMPANY = "33333333-3333-3333-3333-333333333333"
OTHER_COMPANY = "44444444-4444-4444-4444-444444444444"
REPORT = "55555555-5555-5555-5555-555555555555"
DIAG = {"id": CLINIC, "plan": "diagstream", "features": {"corporate_health": True}}

ADMIN = AdminUser(username="lab_admin", role="clinic_admin", clinic_id=CLINIC, user_id="u1")
DESK = AdminUser(username="desk", role="staff", clinic_id=CLINIC, user_id="u2", staff_role="STAFF")
MANAGER = AdminUser(username="mgr", role="staff", clinic_id=CLINIC, user_id="u3",
                    staff_role="CUSTOM_ROLE", permissions=["CORPORATE_HEALTH_MANAGE"])
VIEWER = AdminUser(username="acme.hr", role="staff", clinic_id=CLINIC, user_id="v1",
                   staff_role="CORPORATE_VIEWER")


def _header(name, age, sex, bill):
    return (f"Patient Name : {name} Collected : Sep 07, 2026, 08:12 a.m.\n"
            f"Age : {age} years ({sex}) Received : Sep 07, 2026, 08:14 a.m.\n"
            f"Referral : SELF Reported : Sep 07, 2026, 09:17 a.m.\n"
            f"Bill ID : {bill} Sample ID :\n1000525026\n")


def report_text(name="MR. TEST EMPLOYEE", age=55, sex="Male", bill="900001", values=None):
    """A full 18-test package report. `values` overrides value strings by key."""
    male = sex == "Male"
    v = {"hb": "15.1", "esr": "10", "fbs": "124", "a1c": "6.3", "tc": "160", "tg": "143", "tbil": "0.6",
         "sgot": "20", "sgpt": "18", "tp": "7.3", "cr": "1.2", "urea": "29.6", "vitd": "34.3",
         "b12": "281", "ca": "9.5", "t3": "1.38", "t4": "10.34", "tsh": "5.45"}
    v.update(values or {})
    p1 = _header(name, age, sex, bill) + f"""CLINICAL BIOCHEMISTRY
Glucose Fasting (FBS)
Test Description Value(s) Unit(s) Reference Range
TAIYO AARUSH EXECUTIVE
Glucose Fasting (FBS) {v['fbs']} mg/dL 70 - 110
Method: Enzymatic UV - Hexokinase
**END OF REPORT**
HbA1c (Glycated Hemoglobin)
Glycosylated Hb (HBA1C) {v['a1c']} % Non-diabetic: 4.0 - 6.0 %
Method: High Performance Liquid Chromotography Pre-diabetic: 6.0 - 6.5 %
Diabetic: > 6.5 %
Mean Blood Glucose 134.11
**END OF REPORT**
LIPID PROFILE
Total Cholesterol {v['tc']} mg/dL Desirable level | < 200
Method: CHO-POD Borderline High | 200-239
High | >or = 240
Triglycerides {v['tg']} mg/dL Normal < 150
Method: Enzymatic- GPO-POD Borderline-High : 150 - 199
HDL Cholesterol 40 mg/dL 40 - 60
LDL Cholesterol 91 mg/dL Optimal < 100
Method: Calculated Near / Above Optimal 100-129
**END OF REPORT**
"""
    p2 = _header(name, age, sex, bill) + f"""LFT- LIVER FUNCTION TEST
Total Bilirubin {v['tbil']} mg/dL Adults : 0.3 - 1.2
Method: 3, 5 Dichloro Phenyl Diazonium Tetra Fluoro Borate 0 - 1 Day : 1.4 - 10.0
15 Days to 17 Yrs : 0.0 - 1.0
Direct-Bilirubin 0.1 mg/dL 0.0 - 0.4
AST(SGOT) {v['sgot']} U/L {'<35' if male else '<31'}
ALT (SGPT) {v['sgpt']} U/L {'<45' if male else '<34'}
Total Protein {v['tp']} g/dL 6.6 - 8.3
**END OF REPORT**
RFT-2 (Renal Function Test - 2)
Creatinine {v['cr']} mg/dl 0.4 - 1.4
Blood Urea {v['urea']} mg/dl 15 - 38
Blood Urea Nitrogen (BUN) 13.8 mg/dl 7 - 18
**END OF REPORT**
Vitamin D - 25(OH)D
Vitamin D (25 - Hydroxy) {v['vitd']} ng/mL Deficiency: < 20
Method: CLIA Insufficiency: 20 - <30
Sufficiency: 30 - 100
Upper Safety:>100
**END OF REPORT**
Vitamin B12 (Cobalamin)
Vitamin B12 (Serum) {v['b12']} pg/mL 120 - 914
**END OF REPORT**
Calcium (Ca)
Calcium {v['ca']} mg/dl 8.8 - 10.8
**END OF REPORT**
"""
    p3 = _header(name, age, sex, bill) + f"""TFT(1) - Thyroid Function Test (T3, T4, TSH)
Triiodothyronine (T3) {v['t3']} ng/mL 20 Years above : 0.58 - 1.78
Method: CLIA 1 to 5 days:- 0.73 - 2.88
Thyroxine (T4) {v['t4']} ug/dL 16 Years above : 4.82 - 15.65
Thyroid Stimulating Hormone (TSH) {v['tsh']} uIU/mL 18 Years Above : 0.38 - 5.33
Method: CLIA First trimester : 0.18 - 2.99
**END OF REPORT**
CBC (Complete Blood Count)
Hemoglobin {v['hb']} gm% {'13 - 17' if male else '11 - 15'}
PCV/HCT 44.8 vol% 34 - 51
Total RBC Count 5.13 mill /cu.mm 4.50 - 5.50
**END OF REPORT**
ESR (Erythrocyte Sedimentation Rate)
ESR - Erythrocyte Sedimentation Rate {v['esr']} mm/hr {'0 - 10' if male else '0 - 15'}
Method: Westergren
Blood Glucose-Post Prandial 184 mg/dL 80 - 140
**END OF REPORT**
"""
    return p1 + p2 + p3


def status(parsed):
    return {k: r["s"] for k, r in parsed["results"].items()}


# ═══════ parser: every package test, from the printed ranges ═══════


def test_parses_all_18_tests_with_the_printed_ranges():
    p = ch.parse_report_text(report_text())
    assert (p["employee_name"], p["sex"], p["age_years"], p["bill_id"], p["collected_on"]) == \
        ("TEST EMPLOYEE", "M", 55, "900001", "2026-09-07")
    assert p["missing"] == [] and p["warnings"] == []
    assert status(p) == {
        "hemoglobin": "normal", "esr": "normal", "fbs": "high", "hba1c": "high",
        "total_cholesterol": "normal", "triglycerides": "normal", "total_bilirubin": "normal",
        "sgot": "normal", "sgpt": "normal", "total_protein": "normal", "creatinine": "normal",
        "blood_urea": "normal", "vitamin_d": "normal", "vitamin_b12": "normal", "calcium": "normal",
        "t3": "normal", "t4": "normal", "tsh": "high",
    }
    assert p["results"]["hemoglobin"] == {"v": 15.1, "u": "gm%", "ref": "13 - 17", "lo": 13.0, "hi": 17.0, "s": "normal"}


def test_vitamin_d_uses_the_sufficiency_band_not_the_first_printed_band():
    vd = ch.parse_report_text(report_text(values={"vitd": "25.1"}))["results"]["vitamin_d"]
    assert (vd["lo"], vd["hi"], vd["s"], vd["ref"]) == (30.0, 100.0, "low", "Sufficiency: 30 - 100")


def test_female_ranges_come_from_the_female_report():
    # ESR 12: normal for a woman (0-15), high for a man (0-10). Hb 12: normal (11-15) vs low (13-17).
    over = {"esr": "12", "hb": "12.0", "sgot": "33"}
    f = status(ch.parse_report_text(report_text(sex="Female", values=over)))
    m = status(ch.parse_report_text(report_text(sex="Male", values=over)))
    assert (f["esr"], f["hemoglobin"], f["sgot"]) == ("normal", "normal", "high")
    assert (m["esr"], m["hemoglobin"], m["sgot"]) == ("high", "low", "normal")


@pytest.mark.parametrize("key, value, expected", [
    ("esr", "10", "normal"),          # "0 - 10" is inclusive
    ("fbs", "110", "normal"), ("fbs", "110.1", "high"), ("fbs", "69.9", "low"),
    ("sgot", "35", "high"),           # "<35" excludes 35
    ("tc", "199", "normal"), ("tc", "200", "high"),
    ("tg", "150", "high"),
    ("a1c", "6.0", "normal"), ("a1c", "6.1", "high"),
    ("vitd", "30", "normal"), ("vitd", "100.5", "high"),
], ids=lambda x: str(x))
def test_boundaries(key, value, expected):
    names = {"esr": "esr", "fbs": "fbs", "sgot": "sgot", "tc": "total_cholesterol",
             "tg": "triglycerides", "a1c": "hba1c", "vitd": "vitamin_d"}
    assert status(ch.parse_report_text(report_text(values={key: value})))[names[key]] == expected


def test_adult_only_range_is_unclassified_for_a_minor():
    p = ch.parse_report_text(report_text(age=17))
    assert status(p)["t3"] == "unclassified" and status(p)["tsh"] == "unclassified"
    assert status(p)["t4"] == "normal"  # "16 Years above" does apply at 17
    assert any("from age 20" in w for w in p["warnings"])


def test_unexpected_unit_is_not_classified():
    p = ch.parse_report_text(report_text().replace("Creatinine 1.2 mg/dl", "Creatinine 106 umol/L"))
    assert status(p)["creatinine"] == "unclassified"
    assert any("unexpected unit" in w for w in p["warnings"])


def test_conflicting_duplicate_value_is_not_classified():
    p = ch.parse_report_text(report_text() + "\nHemoglobin 9.0 gm% 13 - 17\n")
    assert status(p)["hemoglobin"] == "unclassified"


def test_missing_tests_are_listed_not_invented():
    text = "\n".join(ln for ln in report_text().splitlines() if not ln.startswith("Calcium 9.5"))
    p = ch.parse_report_text(text)
    assert "calcium" not in p["results"] and p["missing"] == ["calcium"]


@pytest.mark.parametrize("text, reason", [
    (report_text() + _header("MR. SOMEONE ELSE", 40, "Male", "900001"), "more than one patient name"),
    (report_text().replace("Bill ID : 900001", "Bill ID : 900002", 1), "more than one bill id"),
    (report_text().replace("Patient Name :", "Name -"), "Patient name not found"),
    (report_text().replace("years (Male)", "years"), "age and sex not found"),
    (_header("MR. X", 30, "Male", "1") + "Nothing useful here", "None of the package tests"),
], ids=["two-patients", "two-bills", "no-name", "no-sex", "no-tests"])
def test_unusable_reports_are_rejected_with_a_reason(text, reason):
    with pytest.raises(ch.ReportRejected, match=reason):
        ch.parse_report_text(text)


@pytest.mark.parametrize("text, expected", [
    ("13 - 17", (13.0, True, 17.0, True)), ("20 - <30", (20.0, True, 30.0, False)),
    ("<35", (None, True, 35.0, False)), ("<=67", (None, True, 67.0, True)),
    (">or = 240", (240.0, True, None, True)), ("> 6.5", (6.5, False, None, True)),
    ("17 - 13", None), ("Clear", None), ("", None),
])
def test_parse_range(text, expected):
    b = ch._parse_range(text)
    assert (None if b is None else (b["lo"], b["lo_incl"], b["hi"], b["hi_incl"])) == expected


# ═══════ PDF layer ═══════


def _pdf(lines):
    """A minimal one-page PDF with a text layer (no PDF library needed)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    ops = "BT /F1 8 Tf 20 800 Td 10 TL " + " ".join(f"({esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


def test_real_pdf_round_trip():
    p = ch.parse_pdf(_pdf(report_text().splitlines()[:40]))
    assert p["bill_id"] == "900001" and status(p)["fbs"] == "high" and status(p)["total_cholesterol"] == "normal"


@pytest.mark.parametrize("data, reason", [
    (b"not a pdf", "Not a PDF"), (b"%PDF-1.4 garbage", "could not be opened"), (b"", "Not a PDF"),
])
def test_bad_files_are_rejected(data, reason):
    with pytest.raises(ch.ReportRejected, match=reason):
        ch.extract_pdf_text(data)


def test_pdf_without_text_is_rejected():
    with pytest.raises(ch.ReportRejected, match="no text layer"):
        ch.extract_pdf_text(_pdf([]))


# ═══════ aggregation ═══════


def _row(sex, age, **statuses):
    return {"sex": sex, "age_years": age, "collected_on": "2026-09-07",
            "results": {k: {"v": 1.0, "u": "x", "s": s, "ref": "r"} for k, s in statuses.items()}}


ROWS = [
    _row("M", 25, hemoglobin="normal", fbs="high"),
    _row("M", 45, hemoglobin="low", fbs="normal"),
    _row("F", 45, hemoglobin="normal", fbs="unclassified"),
    _row("F", 62, hemoglobin="normal"),
]


def test_insights_counts_and_percentages():
    out = ch.build_insights(ROWS)
    s = out["summary"]
    assert (s["employees"], s["male"], s["female"], s["avg_age"]) == (4, 2, 2, 44.2)
    assert (s["with_abnormal"], s["with_abnormal_pct"]) == (2, 50.0)
    assert s["age_distribution"] == {"<30": 1, "30-39": 0, "40-49": 2, "50-59": 0, "60+": 1}
    hb = next(p for p in out["params"] if p["key"] == "hemoglobin")
    assert (hb["tested"], hb["normal"], hb["low"], hb["high"], hb["abnormal_pct"]) == (4, 3, 1, 0, 25.0)
    assert hb["by_sex"]["M"] == {"normal": 1, "low": 1, "high": 0, "unclassified": 0}
    assert hb["by_age"]["40-49"]["low"] == 1
    fbs = next(p for p in out["params"] if p["key"] == "fbs")
    # the unclassified result is counted as tested but excluded from the percentage
    assert (fbs["tested"], fbs["unclassified"], fbs["abnormal_pct"]) == (3, 1, 50.0)


def test_insights_filters():
    assert ch.build_insights(ROWS, sex="F")["summary"]["employees"] == 2
    assert ch.build_insights(ROWS, band="40-49")["summary"]["employees"] == 2
    assert ch.build_insights(ROWS, sex="M", band="60+")["summary"]["employees"] == 0


def test_pct_rounds_half_up():
    assert ch.pct(1, 8) == 12.5 and ch.pct(1, 3) == 33.3 and ch.pct(2, 3) == 66.7 and ch.pct(1, 0) == 0.0
    assert ch.pct(1, 400) == 0.3  # 0.25 -> 0.3; round() would give 0.2


def test_insights_from_parsed_reports_end_to_end():
    rows = [ch.parse_report_text(report_text(bill=str(i), **kw)) for i, kw in enumerate([
        {}, {"sex": "Female", "age": 32, "values": {"hb": "10.2"}}, {"age": 61, "values": {"tsh": "2.1"}}])]
    out = ch.build_insights(rows)
    tsh = next(p for p in out["params"] if p["key"] == "tsh")
    # default fixture TSH 5.45 is high (> 5.33); only the third report overrides it to 2.1
    assert (tsh["high"], tsh["normal"], tsh["abnormal_pct"]) == (2, 1, 66.7)
    hb = next(p for p in out["params"] if p["key"] == "hemoglobin")
    assert hb["by_sex"]["F"]["low"] == 1 and hb["unit"] == "gm%"


# ═══════ feature gate ═══════


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": "diagstream", "features": {"corporate_health": True}}, True),
    ({"plan": "diagbooking", "features": {"corporate_health": True}}, True),
    ({"plan": "diagstream", "features": {}}, False),                          # opt-in only
    ({"plan": "diagstream", "features": {"corporate_health": "yes"}}, False),
    ({"plan": "enterprise", "features": {"corporate_health": True}}, False),  # never via wildcard
    ({"plan": "dental", "features": {"corporate_health": True}}, False),
    (None, False),
])
def test_corporate_health_enabled(clinic, expected):
    assert corporate_health_enabled(clinic) is expected


# ═══════ company-viewer confinement (the security boundary) ═══════


def _req(method, path):
    r = MagicMock()
    r.method = method
    r.url.path = path
    return r


@pytest.mark.parametrize("method, path", [
    ("GET", "/admin/me"), ("PUT", "/admin/change-password"),
    ("GET", "/admin/corporate-health/companies"),
    ("GET", f"/admin/corporate-health/companies/{COMPANY}/insights"),
])
def test_viewer_may_reach_its_dashboard(method, path):
    assert _enforce_corporate_viewer_scope(_req(method, path), VIEWER) is VIEWER


@pytest.mark.parametrize("method, path", [
    ("GET", "/admin/patients"), ("GET", "/admin/appointments"), ("GET", "/admin/lab-reports"),
    ("GET", "/admin/stats"), ("GET", "/admin/staff"), ("POST", "/admin/staff"),
    ("GET", f"/admin/corporate-health/companies/{COMPANY}/reports"),
    ("POST", f"/admin/corporate-health/companies/{COMPANY}/reports"),
    ("POST", "/admin/corporate-health/companies"),
    ("DELETE", f"/admin/corporate-health/companies/{COMPANY}"),
    ("GET", f"/admin/corporate-health/companies/{COMPANY}/viewers"),
    ("POST", "/admin/me"),
    ("GET", "/fhir/Patient"),
])
def test_viewer_is_refused_everything_else(method, path):
    with pytest.raises(HTTPException) as e:
        _enforce_corporate_viewer_scope(_req(method, path), VIEWER)
    assert e.value.status_code == 403


def test_other_accounts_are_untouched_by_the_viewer_guard():
    for user in (ADMIN, DESK, MANAGER):
        assert _enforce_corporate_viewer_scope(_req("GET", "/admin/patients"), user) is user


def test_guard_runs_inside_verify_credentials_for_real_requests():
    """No dependency override: a viewer session cookie against a patient route."""
    with patch("app.routers.admin.resolve_admin_session", AsyncMock(return_value=VIEWER)):
        c = TestClient(app)
        c.cookies.set("kriya_admin_session", "tok")
        r = c.get("/admin/patients", params={"clinic_id": CLINIC})
    assert r.status_code == 403 and "company's health dashboard" in r.text


# ═══════ router ═══════


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


client = TestClient(app)
R = "app.routers.corporate_health"
BOUND = MagicMock(data=[{"corporate_client_id": COMPANY, "is_active": True, "staff_role": "CORPORATE_VIEWER"}])


def _enabled(clinic=DIAG):
    return patch(f"{R}.get_clinic_by_id", AsyncMock(return_value=clinic))


def _company(name="Acme", active=True):
    return patch(f"{R}.ch.get_company", AsyncMock(return_value={"id": COMPANY, "name": name, "is_active": active}))


def test_feature_off_is_refused(as_user):
    as_user(ADMIN)
    with _enabled({"id": CLINIC, "plan": "diagstream", "features": {}}):
        r = client.get("/admin/corporate-health/companies", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_front_desk_without_the_grant_is_refused(as_user):
    as_user(DESK)
    with _enabled():
        assert client.get("/admin/corporate-health/companies", params={"clinic_id": CLINIC}).status_code == 403
        assert client.post("/admin/corporate-health/companies", params={"clinic_id": CLINIC},
                           json={"name": "Acme"}).status_code == 403


def test_cross_tenant_is_refused(as_user):
    as_user(ADMIN)
    assert client.get("/admin/corporate-health/companies", params={"clinic_id": OTHER_CLINIC}).status_code == 403


def test_viewer_sees_only_its_own_company(as_user):
    as_user(VIEWER)
    lister = AsyncMock(return_value=[{"id": COMPANY, "name": "Acme", "is_active": True, "reports": 3}])
    with _enabled(), patch(f"{R}.sb", AsyncMock(return_value=BOUND)), patch(f"{R}.ch.list_companies", lister):
        r = client.get("/admin/corporate-health/companies", params={"clinic_id": CLINIC})
    assert r.status_code == 200
    assert r.json() == {"companies": [{"id": COMPANY, "name": "Acme"}], "can_manage": False}
    assert lister.call_args.kwargs == {"only_id": COMPANY}


def test_viewer_cannot_open_another_companys_insights(as_user):
    as_user(VIEWER)
    with _enabled(), patch(f"{R}.sb", AsyncMock(return_value=BOUND)), \
            patch(f"{R}.ch.fetch_rows", AsyncMock()) as rows:
        r = client.get(f"/admin/corporate-health/companies/{OTHER_COMPANY}/insights", params={"clinic_id": CLINIC})
    assert r.status_code == 404
    rows.assert_not_called()


@pytest.mark.parametrize("row", [
    None, {"corporate_client_id": COMPANY, "is_active": False, "staff_role": "CORPORATE_VIEWER"},
    {"corporate_client_id": None, "is_active": True, "staff_role": "CORPORATE_VIEWER"},
], ids=["no-row", "deactivated", "company-deleted"])
def test_unlinked_viewer_sees_nothing(as_user, row):
    as_user(VIEWER)
    with _enabled(), patch(f"{R}.sb", AsyncMock(return_value=MagicMock(data=[row] if row else []))):
        r = client.get(f"/admin/corporate-health/companies/{COMPANY}/insights", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_viewer_insights_happy_path(as_user):
    as_user(VIEWER)
    with _enabled(), patch(f"{R}.sb", AsyncMock(return_value=BOUND)), _company(), \
            patch(f"{R}.ch.fetch_rows", AsyncMock(return_value=ROWS)):
        r = client.get(f"/admin/corporate-health/companies/{COMPANY}/insights",
                       params={"clinic_id": CLINIC, "sex": "M"})
    assert r.status_code == 200
    assert r.json()["company"] == {"id": COMPANY, "name": "Acme"} and r.json()["summary"]["employees"] == 2


def test_disabled_company_is_hidden_from_its_viewer(as_user):
    as_user(VIEWER)
    with _enabled(), patch(f"{R}.sb", AsyncMock(return_value=BOUND)), _company(active=False):
        r = client.get(f"/admin/corporate-health/companies/{COMPANY}/insights", params={"clinic_id": CLINIC})
    assert r.status_code == 403


def test_bad_age_band_is_422(as_user):
    as_user(ADMIN)
    with _enabled(), _company():
        r = client.get(f"/admin/corporate-health/companies/{COMPANY}/insights",
                       params={"clinic_id": CLINIC, "age_band": "20-25"})
    assert r.status_code == 422


def test_upload_reports_per_file_outcomes(as_user):
    as_user(MANAGER)
    outcomes = [{"status": "accepted", "id": REPORT}, {"status": "rejected", "reason": "Not a PDF file."}]
    with _enabled(), _company(), patch(f"{R}.ch.ingest_pdf", AsyncMock(side_effect=outcomes)) as ingest, \
            patch(f"{R}.log_admin_action", AsyncMock()):
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/reports", params={"clinic_id": CLINIC},
                        files=[("files", ("a.pdf", b"%PDF-a", "application/pdf")),
                               ("files", ("b.pdf", b"nope", "application/pdf"))])
    assert r.status_code == 200
    assert r.json()["counts"] == {"accepted": 1, "duplicate": 0, "rejected": 1, "error": 0}
    assert ingest.call_args_list[0].args == (CLINIC, COMPANY, b"%PDF-a", "mgr")


def test_upload_database_failure_is_reported_per_file(as_user):
    as_user(ADMIN)
    with _enabled(), _company(), patch(f"{R}.ch.ingest_pdf", AsyncMock(side_effect=RuntimeError("db down"))), \
            patch(f"{R}.log_admin_action", AsyncMock()):
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/reports", params={"clinic_id": CLINIC},
                        files=[("files", ("a.pdf", b"%PDF-a", "application/pdf"))])
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "error"
    assert "db down" not in r.text  # internals are logged, not returned


def test_upload_batch_limit(as_user):
    as_user(ADMIN)
    with _enabled(), _company():
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/reports", params={"clinic_id": CLINIC},
                        files=[("files", (f"{i}.pdf", b"%PDF", "application/pdf")) for i in range(6)])
    assert r.status_code == 400


def test_upload_to_unknown_company_is_404(as_user):
    as_user(ADMIN)
    with _enabled(), patch(f"{R}.ch.get_company", AsyncMock(return_value=None)):
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/reports", params={"clinic_id": CLINIC},
                        files=[("files", ("a.pdf", b"%PDF", "application/pdf"))])
    assert r.status_code == 404


def test_delete_all_requires_the_company_name(as_user):
    as_user(ADMIN)
    with _enabled(), _company("Acme Ltd"), patch(f"{R}.ch.delete_reports", AsyncMock(return_value=7)) as deleter, \
            patch(f"{R}.log_admin_action", AsyncMock()):
        bad = client.delete(f"/admin/corporate-health/companies/{COMPANY}/reports",
                            params={"clinic_id": CLINIC, "confirm": "acme"})
        ok = client.delete(f"/admin/corporate-health/companies/{COMPANY}/reports",
                           params={"clinic_id": CLINIC, "confirm": " acme  LTD "})
    assert bad.status_code == 400 and ok.json() == {"success": True, "deleted": 7}
    deleter.assert_awaited_once_with(CLINIC, COMPANY)


def test_viewer_creation_needs_staff_create_for_delegated_managers(as_user):
    as_user(MANAGER)
    with _enabled():
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/viewers", params={"clinic_id": CLINIC},
                        json={"username": "acme.hr", "password": "longenough1"})
    assert r.status_code == 403


def test_viewer_creation_binds_role_and_company(as_user):
    as_user(ADMIN)
    sb_mock = AsyncMock(side_effect=[MagicMock(data=[]), MagicMock(data=[{"id": "new"}])])
    with _enabled(), _company(), patch(f"{R}.sb", sb_mock), patch(f"{R}.supabase") as fake, \
            patch(f"{R}.log_admin_action", AsyncMock()):
        r = client.post(f"/admin/corporate-health/companies/{COMPANY}/viewers", params={"clinic_id": CLINIC},
                        json={"username": "acme.hr", "password": "longenough1"})
    assert r.status_code == 200
    row = fake.table.return_value.insert.call_args.args[0]
    assert {k: row[k] for k in ("clinic_id", "role", "staff_role", "permissions", "branch_id", "corporate_client_id")} == {
        "clinic_id": CLINIC, "role": "staff", "staff_role": "CORPORATE_VIEWER", "permissions": [],
        "branch_id": None, "corporate_client_id": COMPANY}
    assert row["password_hash"].startswith("$2")


def test_generic_staff_routes_cannot_mint_or_convert_viewers(as_user):
    as_user(ADMIN)
    r = client.post("/admin/staff", params={"clinic_id": CLINIC},
                    json={"username": "x.hr", "password": "longenough1", "staff_role": "CORPORATE_VIEWER"})
    assert r.status_code == 422
    target = {"id": "s1", "clinic_id": CLINIC, "role": "staff", "staff_role": "CORPORATE_VIEWER",
              "permissions": [], "branch_id": None, "is_active": True, "username": "acme.hr"}
    with patch("app.routers.admin.sb", AsyncMock(return_value=MagicMock(data=[target]))):
        assert client.put("/admin/staff/s1", params={"clinic_id": CLINIC}, json={"staff_role": "STAFF"}).status_code == 422
        assert client.put("/admin/staff/s1", params={"clinic_id": CLINIC},
                          json={"extra_permissions": ["REPORTS_VIEW"]}).status_code == 422
    desk = {**target, "staff_role": "STAFF", "username": "desk"}
    with patch("app.routers.admin.sb", AsyncMock(return_value=MagicMock(data=[desk]))):
        assert client.put("/admin/staff/s1", params={"clinic_id": CLINIC},
                          json={"staff_role": "CORPORATE_VIEWER"}).status_code == 422


# ═══════ persistence: data minimisation + duplicate guard ═══════


@pytest.mark.asyncio
async def test_ingest_skips_a_report_already_uploaded():
    with patch("app.database.sb", AsyncMock(return_value=MagicMock(data=[{"id": REPORT}]))), \
            patch("app.database.supabase", MagicMock()) as fake, \
            patch.object(ch, "parse_pdf", return_value=ch.parse_report_text(report_text())):
        out = await ch.ingest_pdf(CLINIC, COMPANY, b"%PDF", "mgr")
    assert out["status"] == "duplicate" and out["bill_id"] == "900001"
    fake.table.return_value.insert.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_stores_no_name_and_no_file():
    sb_mock = AsyncMock(side_effect=[MagicMock(data=[]), MagicMock(data=[{"id": REPORT}])])
    with patch("app.database.sb", sb_mock), patch("app.database.supabase", MagicMock()) as fake, \
            patch.object(ch, "parse_pdf", return_value=ch.parse_report_text(report_text())):
        out = await ch.ingest_pdf(CLINIC, COMPANY, b"%PDF", "mgr")
    row = fake.table.return_value.insert.call_args.args[0]
    assert out["status"] == "accepted" and out["employee_name"] == "TEST EMPLOYEE"  # shown once to the uploader
    assert set(row) == {"clinic_id", "corporate_client_id", "sex", "age_years", "bill_id", "collected_on",
                        "results", "file_sha256", "parser_version", "uploaded_by"}
    assert "TEST EMPLOYEE" not in repr(row)


@pytest.mark.asyncio
async def test_ingest_race_on_unique_index_is_a_duplicate_not_an_error():
    sb_mock = AsyncMock(side_effect=[MagicMock(data=[]),
                                     Exception('duplicate key value violates unique constraint "uq_corporate_reports_bill"')])
    with patch("app.database.sb", sb_mock), patch("app.database.supabase", MagicMock()), \
            patch.object(ch, "parse_pdf", return_value=ch.parse_report_text(report_text())):
        out = await ch.ingest_pdf(CLINIC, COMPANY, b"%PDF", "mgr")
    assert out["status"] == "duplicate"


@pytest.mark.asyncio
async def test_ingest_rejects_without_touching_the_database():
    with patch("app.database.sb", AsyncMock()) as sb_mock:
        out = await ch.ingest_pdf(CLINIC, COMPANY, b"not a pdf", "mgr")
    assert out == {"status": "rejected", "reason": "Not a PDF file."}
    sb_mock.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_clinic_scope_is_refused():
    with pytest.raises(ValueError):
        await ch.fetch_rows("default", COMPANY)


@pytest.mark.asyncio
async def test_fetch_rows_pages_past_1000():
    pages = [MagicMock(data=[{"id": i} for i in range(1000)]), MagicMock(data=[{"id": "last"}])]
    with patch("app.database.sb", AsyncMock(side_effect=pages)), patch("app.database.supabase", MagicMock()):
        rows = await ch.fetch_rows(CLINIC, COMPANY)
    assert len(rows) == 1001


# ═══════ owner toggle ═══════


@pytest.mark.parametrize("plan, expected", [("diagstream", 200), ("diagbooking", 200), ("dental", 400)])
def test_owner_can_enable_only_on_diagnostic_plans(plan, expected):
    from app.routers import platform

    sb_mock = AsyncMock(side_effect=[MagicMock(data=[{"features": {}, "plan": plan}]),
                                     MagicMock(data=[{"id": CLINIC}])])
    app.dependency_overrides[platform.verify_owner_credentials] = lambda: AdminUser("owner", role="super_admin")
    try:
        with patch.object(platform, "sb", sb_mock), patch.object(platform, "log_admin_action", AsyncMock()):
            r = client.patch(f"/platform/clinics/{CLINIC}/features",
                             json={"feature": "corporate_health", "enabled": True})
    finally:
        app.dependency_overrides.pop(platform.verify_owner_credentials, None)
    assert r.status_code == expected
