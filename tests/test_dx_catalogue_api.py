"""Diagnostic OS catalogue end to end (P2: DX-15, DX-16): real HTTP routes, real Postgres.

Masters (departments, containers, signatories + signature), the test master with
DX fields, parameters with formulas validated at save, panels/packages, approval
by digest (doctor-linked approver, readiness, CAS, cleared by edits), formula
check, the seed library (idempotent, inactive drafts, name matching) and the
CSV / XLSX parameter import with its dry run.
"""

import io
import uuid

import pytest

from app.services import dx_catalogue, dx_seed
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _lab(conn, plan="diagstream"):
    return str(_q(conn, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('Cat Lab', %s, %s, true) "
                        "RETURNING id", "+9172" + uuid.uuid4().hex[:8], plan))


def _doctor(conn, clinic, reg="TSMC/2010/11"):
    return str(_q(conn, """INSERT INTO doctors (clinic_id, name, department, specialization, registration_number,
                           is_active) VALUES (%s, 'Dr. Lakshmi', 'Lab', 'Pathology', %s, true) RETURNING id""",
                  clinic, reg))


@pytest.fixture
def lab(api):  # noqa: F811
    clinic = _lab(api.conn)
    assert api.owner_toggle(clinic, "dx_enabled", True).status_code == 200
    doc, unreg = _doctor(api.conn, clinic), _doctor(api.conn, clinic, None)
    U, uid = api.U, lambda: str(uuid.uuid4())
    return {"clinic": clinic, "doctor": doc, "unreg": unreg,
            "admin": U("admin", "clinic_admin", clinic, uid()),
            "director": U("director", "staff", clinic, uid(), ["DX_CATALOGUE", "DX_CATALOGUE_APPROVE"],
                          staff_role="PATHOLOGIST", doctor_id=doc),
            "tech": U("tech", "staff", clinic, uid(), [], staff_role="LAB_OPERATOR"),
            "desk": U("desk", "staff", clinic, uid(), ["OPD_FRONT_DESK"], staff_role="FRONT_DESK")}


def _masters(api, lab):
    api.as_(lab["admin"])
    dept = api.call("POST", "/admin/dx/departments", lab["clinic"],
                    {"code": "BIO", "name": "Biochemistry", "report_heading": "CLINICAL BIOCHEMISTRY"})
    cont = api.call("POST", "/admin/dx/containers", lab["clinic"],
                    {"code": "01", "name": "Plain / Red top", "colour_hex": "#C0392B", "sample_type": "serum",
                     "default_volume_ml": 5, "stability_room_hours": 8})
    assert dept.status_code == 201 and cont.status_code == 201, (dept.text, cont.text)
    return dept.json(), cont.json()


def _lipid(api, lab, dept, cont):
    api.as_(lab["admin"])
    t = api.call("POST", "/admin/dx/tests", lab["clinic"],
                 {"name": "Lipid Profile", "price_paise": 55000, "test_code": "LIPID", "department_id": dept["id"],
                  "container_id": cont["id"], "method": "Enzymatic", "tat_minutes": 240})
    assert t.status_code == 201, t.text
    tid = t.json()["id"]
    for body in ({"name": "Total cholesterol", "unit": "mg/dL", "parameter_code": "TC", "normal_high": 200,
                  "high_exclusive": True, "decimal_places": 0, "sort_order": 1},
                 {"name": "HDL", "unit": "mg/dL", "parameter_code": "HDL", "normal_low": 40, "normal_high": 60,
                  "decimal_places": 0, "sort_order": 2},
                 {"name": "Triglycerides", "unit": "mg/dL", "parameter_code": "TG", "normal_high": 150,
                  "high_exclusive": True, "decimal_places": 0, "sort_order": 3},
                 {"name": "LDL", "unit": "mg/dL", "parameter_code": "LDL", "formula": "TC - HDL - TG / 5",
                  "rounding_mode": "truncate", "decimal_places": 0, "sort_order": 4}):
        r = api.call("POST", f"/admin/dx/tests/{tid}/parameters", lab["clinic"], body)
        assert r.status_code == 201, r.text
    return tid


# ── masters ──────────────────────────────────────────────────────────────────

def test_catalogue_is_closed_until_dx_is_enabled(api):  # noqa: F811
    clinic = _lab(api.conn)
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    for path in ("/admin/dx/departments", "/admin/dx/tests", "/admin/dx/seed/preview"):
        assert api.call("GET", path, clinic).status_code == 403, path


def test_departments_and_containers(api, lab):  # noqa: F811
    dept, cont = _masters(api, lab)
    c = lab["clinic"]
    assert api.call("POST", "/admin/dx/departments", c, {"code": "BIO", "name": "Dup", "report_heading": "DUP"}
                    ).status_code == 409
    assert api.call("POST", "/admin/dx/containers", c, {"code": "1", "name": "Bad", "colour_hex": "#FFFFFF",
                                                       "sample_type": "serum"}).status_code == 422
    assert api.call("POST", "/admin/dx/containers", c, {"code": "07", "name": "EDTA", "colour_hex": "purple",
                                                       "sample_type": "whole_blood"}).status_code == 422
    r = api.call("PUT", f"/admin/dx/containers/{cont['id']}", c, {"stability_cold_hours": 168, "colour_hex": "#B03A2E"})
    assert r.status_code == 200 and r.json()["stability_cold_hours"] == 168
    assert api.call("PUT", f"/admin/dx/departments/{dept['id']}", c, {}).status_code == 422
    assert api.call("PUT", f"/admin/dx/departments/{dept['id']}", c, {"is_active": False}).json()["is_active"] is False
    assert [d["code"] for d in api.call("GET", "/admin/dx/departments", c).json()["departments"]] == ["BIO"]
    done = {i["key"]: i["done"] for i in api.call("GET", "/admin/dx/setup", c).json()["checklist"]}
    assert (done["departments"], done["containers"], done["signatories"]) == (False, True, False)   # BIO deactivated
    # read for DX_READ (technician), write only for DX_CATALOGUE / DX_ADMIN
    api.as_(lab["tech"])
    assert api.call("GET", "/admin/dx/containers", c).status_code == 200
    assert api.call("POST", "/admin/dx/departments", c, {"code": "HAEM", "name": "Haem", "report_heading": "HAEM"}
                    ).status_code == 403
    api.as_(lab["desk"])
    assert api.call("GET", "/admin/dx/containers", c).status_code == 403


def test_signatories_need_a_registered_doctor_and_a_png(api, lab, monkeypatch):  # noqa: F811
    c = lab["clinic"]
    api.as_(lab["admin"])
    r = api.call("POST", "/admin/dx/signatories", c, {"doctor_id": lab["unreg"], "designation": "Pathologist",
                                                     "qualification_text": "MD"})
    assert r.status_code == 422 and "registration" in r.text
    s = api.call("POST", "/admin/dx/signatories", c, {"doctor_id": lab["doctor"], "designation": "Consultant Pathologist",
                                                     "qualification_text": "MD Pathology"})
    assert s.status_code == 201, s.text
    assert api.call("POST", "/admin/dx/signatories", c, {"doctor_id": lab["doctor"], "designation": "Again",
                                                        "qualification_text": "MD"}).status_code == 409
    listed = api.call("GET", "/admin/dx/signatories", c).json()["signatories"]
    assert listed[0]["doctor_name"] == "Dr. Lakshmi" and listed[0]["registration_number"]

    uploads = []

    class _Bucket:
        def upload(self, path, data, opts):
            uploads.append((path, len(data), opts))

    class _Storage:
        def from_(self, bucket):
            assert bucket == dx_catalogue.SIGNATURE_BUCKET
            return _Bucket()
    monkeypatch.setattr(type(dx_catalogue.supabase), "storage", property(lambda self: _Storage()), raising=False)
    sid = s.json()["id"]
    url = f"/admin/dx/signatories/{sid}/signature"
    bad = api.client.post(url, params={"clinic_id": c}, files={"file": ("s.jpg", b"\xff\xd8\xff" + b"0" * 10)})
    assert bad.status_code == 415
    big = api.client.post(url, params={"clinic_id": c}, files={"file": ("s.png", PNG + b"0" * 400_000)})
    assert big.status_code == 413
    ok = api.client.post(url, params={"clinic_id": c}, files={"file": ("s.png", PNG)})
    assert ok.status_code == 200, ok.text
    path = ok.json()["signature_asset_path"]
    assert path.startswith(f"{c}/dx-assets/signatures/") and path.endswith(".png") and uploads[0][0] == path


# ── test master, parameters, components ─────────────────────────────────────

def test_test_master_and_formula_validation_at_save(api, lab):  # noqa: F811
    dept, cont = _masters(api, lab)
    tid = _lipid(api, lab, dept, cont)
    c = lab["clinic"]
    view = api.call("GET", f"/admin/dx/tests/{tid}", c).json()
    assert view["test"]["is_active"] is False and view["test"]["approved_at"] is None   # a draft, not bookable
    assert len(view["digest"]) == 64 and view["issues"] == []
    assert [p["parameter_code"] for p in view["parameters"]] == ["TC", "HDL", "TG", "LDL"]
    for body, token in (({"name": "X", "parameter_code": "X", "formula": "TC - NOPE"}, "NOPE"),
                        ({"name": "X", "parameter_code": "X", "formula": "__import__('os')"}, "__import__"),
                        ({"name": "X", "formula": "TC + 1"}, None)):
        r = api.call("POST", f"/admin/dx/tests/{tid}/parameters", c, body)
        assert r.status_code == 422, (body, r.text)
        if token:
            assert r.json()["detail"]["token"] == token
    # a loop between calculated parameters is refused at save
    api.call("POST", f"/admin/dx/tests/{tid}/parameters", c, {"name": "A", "parameter_code": "A", "formula": "TC + 1"})
    a = [p for p in api.call("GET", f"/admin/dx/tests/{tid}", c).json()["parameters"] if p["parameter_code"] == "A"][0]
    r = api.call("PUT", f"/admin/dx/tests/{tid}/parameters/{a['id']}", c,
                 {"name": "A", "parameter_code": "A", "formula": "B + 1"})
    assert r.status_code == 422
    api.call("POST", f"/admin/dx/tests/{tid}/parameters", c, {"name": "B", "parameter_code": "B", "formula": "LDL * 2"})
    r = api.call("PUT", f"/admin/dx/tests/{tid}/parameters/{a['id']}", c,
                 {"name": "A", "parameter_code": "A", "formula": "B + 1"})
    assert r.status_code == 200, r.text
    b = [p for p in api.call("GET", f"/admin/dx/tests/{tid}", c).json()["parameters"] if p["parameter_code"] == "B"][0]
    r = api.call("PUT", f"/admin/dx/tests/{tid}/parameters/{b['id']}", c,
                 {"name": "B", "parameter_code": "B", "formula": "A * 2"})
    assert r.status_code == 422 and "loop" in r.json()["detail"]["message"]
    # DX fields on the test
    r = api.call("PUT", f"/admin/dx/tests/{tid}", c, {"interpretation": "Fasting 10-12 h", "loinc_code": "57698-3",
                                                      "kind": None})
    assert r.status_code == 200 and r.json()["interpretation"] == "Fasting 10-12 h" and r.json()["kind"] == "test"
    assert api.call("PUT", f"/admin/dx/tests/{tid}", c, {"loinc_code": "bad"}).status_code == 422
    listed = api.call("GET", "/admin/dx/tests", c, q="lipid").json()["tests"]
    assert [t["test_code"] for t in listed] == ["LIPID"]


def test_panels_and_packages(api, lab):  # noqa: F811
    dept, cont = _masters(api, lab)
    c = lab["clinic"]
    api.as_(lab["admin"])

    def mk(name, kind="test"):
        r = api.call("POST", "/admin/dx/tests", c, {"name": name, "price_paise": 10000, "kind": kind})
        assert r.status_code == 201, r.text
        return r.json()["id"]
    t3, tsh, panel, package = mk("T3"), mk("TSH"), mk("Thyroid Profile", "panel"), mk("Health Package", "package")
    for parent, child in ((panel, t3), (panel, tsh), (package, panel)):
        assert api.call("POST", f"/admin/dx/tests/{parent}/components", c, {"child_test_id": child}).status_code == 201
    r = api.call("POST", f"/admin/dx/tests/{t3}/components", c, {"child_test_id": tsh})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "dx_component_parent_kind"
    r = api.call("POST", f"/admin/dx/tests/{panel}/components", c, {"child_test_id": mk("Other panel", "panel")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "dx_component_depth"
    assert api.call("POST", f"/admin/dx/tests/{panel}/components", c, {"child_test_id": t3}).status_code == 409
    r = api.call("POST", f"/admin/dx/tests/{panel}/parameters", c, {"name": "X"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_parameters_on_group"
    assert api.call("PUT", f"/admin/dx/tests/{panel}", c, {"kind": "test"}).json()["detail"]["code"] == "dx_kind_locked"
    comps = api.call("GET", f"/admin/dx/tests/{panel}/components", c).json()["components"]
    assert sorted(x["child"]["name"] for x in comps) == ["T3", "TSH"]
    assert api.call("DELETE", f"/admin/dx/tests/{panel}/components/{tsh}", c).status_code == 200
    assert api.call("DELETE", f"/admin/dx/tests/{panel}/components/{tsh}", c).status_code == 404


# ── approval ─────────────────────────────────────────────────────────────────

def test_approval_needs_a_registered_doctor_a_ready_test_and_the_reviewed_digest(api, lab):  # noqa: F811
    dept, cont = _masters(api, lab)
    tid = _lipid(api, lab, dept, cont)
    c = lab["clinic"]
    digest = api.call("GET", f"/admin/dx/tests/{tid}", c).json()["digest"]
    # a clinic admin without a doctor link cannot sign off a test
    r = api.as_(lab["admin"]).call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": digest})
    assert r.status_code == 403 and "doctor" in r.text
    unreg = api.U("dir2", "staff", c, str(uuid.uuid4()), ["DX_CATALOGUE_APPROVE"], doctor_id=lab["unreg"])
    assert api.as_(unreg).call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": digest}).status_code == 422
    assert api.as_(lab["tech"]).call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": digest}).status_code == 403
    # someone edits after the director loaded the test: 409, nothing approved
    api.as_(lab["admin"]).call("PUT", f"/admin/dx/tests/{tid}", c, {"method": "CHO-POD"})
    api.as_(lab["director"])
    r = api.call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": digest})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_test_changed"
    view = api.call("GET", f"/admin/dx/tests/{tid}", c).json()
    r = api.call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": view["digest"]})
    assert r.status_code == 200, r.text
    assert r.json()["approved_by"] == lab["doctor"]
    assert api.call("GET", f"/admin/dx/tests/{tid}", c).json()["test"]["approved_at"]
    assert [t["id"] for t in api.call("GET", "/admin/dx/tests", c, approved=True).json()["tests"]] == [tid]
    # editing a band clears the approval
    tc = [p for p in view["parameters"] if p["parameter_code"] == "TC"][0]
    api.call("PUT", f"/admin/dx/tests/{tid}/parameters/{tc['id']}", c,
             {"name": "Total cholesterol", "unit": "mg/dL", "parameter_code": "TC", "normal_high": 190,
              "decimal_places": 0})
    assert api.call("GET", f"/admin/dx/tests/{tid}", c).json()["test"]["approved_at"] is None


def test_an_incomplete_test_cannot_be_approved(api, lab):  # noqa: F811
    c = lab["clinic"]
    api.as_(lab["director"])
    tid = api.call("POST", "/admin/dx/tests", c, {"name": "Bare test", "price_paise": 100}).json()["id"]
    view = api.call("GET", f"/admin/dx/tests/{tid}", c).json()
    assert set(view["issues"]) == {"Choose a department.", "Choose a container.", "Add at least one parameter."}
    r = api.call("POST", f"/admin/dx/tests/{tid}/approve", c, {"digest": view["digest"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dx_test_not_ready"
    pkg = api.call("POST", "/admin/dx/tests", c, {"name": "Empty pkg", "price_paise": 100, "kind": "package"}).json()
    assert api.call("GET", f"/admin/dx/tests/{pkg['id']}", c).json()["issues"] == ["Add at least one member test."]


def test_formula_check(api, lab):  # noqa: F811
    api.as_(lab["director"])
    r = api.call("POST", "/admin/dx/formula/check", lab["clinic"],
                 {"formula": "TC - HDL - TG / 5", "codes": ["TC", "HDL", "TG"], "inputs": {"TC": 134, "TG": 162,
                                                                                         "HDL": 40},
                  "decimal_places": 0, "rounding_mode": "truncate"}).json()
    assert r == {"valid": True, "dependencies": ["HDL", "TC", "TG"], "raw": "61.6", "value": "61", "missing": []}
    r = api.call("POST", "/admin/dx/formula/check", lab["clinic"],
                 {"formula": "egfr_ckd_epi_2021(CREAT)", "codes": ["CREAT"], "inputs": {"CREAT": 1.0},
                  "patient": {"age_years": 50, "is_female": False}, "decimal_places": 0}).json()
    assert r["value"] == "92"
    bad = api.call("POST", "/admin/dx/formula/check", lab["clinic"], {"formula": "TC.real", "codes": ["TC"]}).json()
    assert bad["valid"] is False and bad["token"] == "Attribute"


def test_another_tenant_cannot_reach_this_catalogue(api, lab):  # noqa: F811
    dept, cont = _masters(api, lab)
    tid = _lipid(api, lab, dept, cont)
    other = _lab(api.conn)
    api.owner_toggle(other, "dx_enabled", True)
    api.as_(api.U("admin2", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("GET", f"/admin/dx/tests/{tid}", other).status_code == 404
    assert api.call("GET", f"/admin/dx/tests/{tid}", lab["clinic"]).status_code in (403, 404)
    assert api.call("PUT", f"/admin/dx/containers/{cont['id']}", other, {"name": "Hijack"}).status_code == 404


# ── seed library ─────────────────────────────────────────────────────────────

def test_seed_is_idempotent_inactive_and_unapproved(api, lab):  # noqa: F811
    c = lab["clinic"]
    # the clinic already sells "Lipid Profile" for WhatsApp booking: matched by name, price and bands kept
    own = str(_q(api.conn, "INSERT INTO lab_tests (clinic_id, name, price_paise, is_active) VALUES (%s, "
                           "'lipid profile', 49900, true) RETURNING id", c))
    api.as_(lab["admin"])
    pre = api.call("GET", "/admin/dx/seed/preview", c).json()
    seed_n = len(dx_seed.load_seed()["tests"])
    assert pre["total"] == seed_n >= 140 and pre["name_match"] == 1 and pre["new"] == seed_n - 1
    assert "indicative" in pre["source"]
    out = api.call("POST", "/admin/dx/seed/apply", c, {}).json()
    assert out["created"] == seed_n - 1 and out["matched_by_name"] == 1 and out["conflicts"] == []
    want_params = sum(len(dx_seed._param_rows("x", t["parameters"])) for t in dx_seed.load_seed()["tests"])
    want_links = sum(len(t["components"]) for t in dx_seed.load_seed()["tests"])
    assert (out["departments"], out["containers"], out["parameters"], out["components"]) == (8, 11, want_params,
                                                                                            want_links)
    again = api.call("POST", "/admin/dx/seed/apply", c, {}).json()
    assert (again["created"], again["parameters"], again["components"], again["departments"]) == (0, 0, 0, 0)
    assert again["already_present"] == seed_n
    assert _q(api.conn, "SELECT count(*) FROM lab_tests WHERE clinic_id = %s AND test_code IS NOT NULL AND "
                        "(is_active OR approved_at IS NOT NULL) AND id <> %s", c, own) == 0
    row = _q(api.conn, "SELECT to_jsonb(t) FROM lab_tests t WHERE id = %s", own)
    assert (row["test_code"], row["price_paise"], row["is_active"]) == ("LIPID", 49900, True)
    lipid = api.call("GET", f"/admin/dx/tests/{own}", c).json()
    assert [p["parameter_code"] for p in lipid["parameters"]][:3] == ["TC", "TG", "HDL"]
    cbc = _q(api.conn, "SELECT id::text FROM lab_tests WHERE clinic_id = %s AND test_code = 'CBC'", c)
    assert api.call("GET", f"/admin/dx/tests/{cbc}", c).json()["issues"] == []          # ready for review
    pkg = _q(api.conn, "SELECT id::text FROM lab_tests WHERE clinic_id = %s AND test_code = 'EXEC_HEALTH'", c)
    assert len(api.call("GET", f"/admin/dx/tests/{pkg}/components", c).json()["components"]) == 12


def test_seed_subset_pulls_in_members(api, lab):  # noqa: F811
    api.as_(lab["admin"])
    out = api.call("POST", "/admin/dx/seed/apply", lab["clinic"], {"test_keys": ["THYROID"]}).json()
    assert out["created"] == 4 and out["components"] == 3                  # panel + T3, T4, TSH
    assert api.call("POST", "/admin/dx/seed/apply", lab["clinic"], {"test_keys": ["NOPE"]}).status_code == 422


# ── parameter import ─────────────────────────────────────────────────────────

CSV_OK = ("test_code,parameter_code,name,unit,gender,min_age_days,max_age_days,normal_low,normal_high,decimal_places\n"
          "VITX,VITX,Vitamin X,ng/mL,male,6570,43800,10,50,1\n"
          "VITX,VITX,Vitamin X,ng/mL,female,6570,43800,8,40,1\n")


def _vitx(api, lab):
    api.as_(lab["admin"])
    return api.call("POST", "/admin/dx/tests", lab["clinic"], {"name": "Vitamin X", "price_paise": 900,
                                                               "test_code": "VITX"}).json()["id"]


def _upload(api, path, clinic, content, name="bands.csv"):
    return api.client.post(path, params={"clinic_id": clinic}, files={"file": (name, content)})


def test_import_dry_run_reports_every_problem_and_apply_writes_nothing_until_clean(api, lab):  # noqa: F811
    tid = _vitx(api, lab)
    c = lab["clinic"]
    bad = CSV_OK + ("NOPE,X,Ghost,,all,0,43800,1,2,2\n"
                    "VITX,VITX,Vitamin X,ng/mL,male,10000,20000,abc,50,1\n"
                    "VITX,VITX,Vitamin X,ng/mL,all,0,43800,60,50,1\n")
    r = _upload(api, "/admin/dx/import/dry-run", c, bad.encode()).json()
    assert (r["rows"], r["valid"], r["invalid"]) == (5, 2, 3)
    errs = {row["row"]: " ".join(row["errors"]) for row in r["report"]}
    assert "not a test of this clinic" in errs[4]
    assert "not a number" in errs[5] and "overlaps another band" in errs[5]
    assert "ordered" in errs[6]
    applied = _upload(api, "/admin/dx/import/apply", c, bad.encode())
    assert applied.status_code == 422
    assert _q(api.conn, "SELECT count(*) FROM lab_test_parameters WHERE lab_test_id = %s", tid) == 0
    ok = _upload(api, "/admin/dx/import/apply", c, CSV_OK.encode())
    assert ok.status_code == 200 and ok.json()["written"] == 2
    again = _upload(api, "/admin/dx/import/dry-run", c, CSV_OK.encode()).json()
    assert again["invalid"] == 2 and "existing band" in again["report"][0]["errors"][0]


def test_import_reads_xlsx_and_validates_formulas(api, lab):  # noqa: F811
    import openpyxl
    _vitx(api, lab)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Test Code", "Parameter Code", "Name", "Formula"])
    ws.append(["VITX", "VITX", "Vitamin X", ""])
    ws.append(["VITX", "VITX2", "Vitamin X doubled", "VITX * 2"])
    ws.append(["VITX", "BAD", "Bad", "VITX.real"])
    buf = io.BytesIO()
    wb.save(buf)
    r = _upload(api, "/admin/dx/import/dry-run", lab["clinic"], buf.getvalue(), "bands.xlsx").json()
    assert (r["rows"], r["invalid"]) == (3, 1) and "formula" in r["report"][2]["errors"][0]
    assert _upload(api, "/admin/dx/import/dry-run", lab["clinic"], b"MZ\x90\x00", "x.exe").status_code == 400
    assert _upload(api, "/admin/dx/import/dry-run", lab["clinic"], b"name\nx\n").status_code == 422   # no test_code


def test_seed_file_is_well_formed():
    seed = dx_seed.load_seed()
    from app.services import dx_formula
    codes = [t["code"] for t in seed["tests"]]
    assert len(codes) == len(set(codes)) and len(codes) >= 140
    for t in seed["tests"]:
        params = {p["code"] for p in t["parameters"]}
        formulas = {p["code"]: p["formula"] for p in t["parameters"] if p["formula"]}
        for f in formulas.values():
            dx_formula.validate_formula(f, params)
        dx_formula.evaluation_order(formulas)
        assert t["kind"] == "test" or (not t["parameters"] and t["components"])
        assert t["container"] in {c["code"] for c in seed["containers"]} | {None}
