"""S2 forms engine (SP-S2-01) + eye / dental / derma form schemas on real Postgres.

Validators: Snellen enumerations and computed logMAR, 0.25 D steps, an axis with every cylinder, spherical equivalent;
FDI teeth by dentition (primary for 14+ needs staff confirmation), surfaces, DMFT; six-site perio and its means; PASI
from entered components. API: save / overwrite / list / history; frozen once the encounter is signed (DB trigger, so a
direct UPDATE is refused too). PDF: a prescription without form rows is byte-identical to the renderer before Specialty
OS (golden hash of the committed renderer, CreationDate and /ID removed); with rows it prints an examination block.
"""

import asyncio
import copy
import hashlib
import re

import psycopg2
import pytest
from fastapi import HTTPException

from app.services import sp_forms
from tests.sp_world import encounter, sign, sp_world
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

GOLDEN_RX_SHA256 = "d12afb2526bc82c6a2fc360df99cb7a1f9f784d2cb5527abf70f361dea9ad79c"


def _v(code, data, **ctx):
    return sp_forms.validate(code, data, ctx)


def test_eye_exam_validation_and_computed_values():
    clean, comp = _v("eye_exam_v1", {
        "od": {"va": {"distance_unaided": "6/60", "distance_aided": "6/6", "near": "N6"},
               "refraction": {"method": "subjective", "sph": -2.25, "cyl": -0.75, "axis": 180, "add": 1.5},
               "iop": {"mmhg": 16, "method": "nct", "time": "10:30"}, "fundus": {"cdr": 0.3, "macula": " normal "}},
        "os": {"va": {"distance_unaided": "6/9"}, "refraction": {"sph": 1.0}},
        "pd_mm": 63, "diagnosis": [{"icd10": "h52.1", "eye": "ou"}]})
    assert comp["od"] == {"logmar_distance_unaided": 1.0, "logmar_distance_aided": 0.0, "sph_equivalent": -2.625}
    assert comp["os"]["logmar_distance_unaided"] == 0.18 and comp["os"]["sph_equivalent"] == 1.0
    assert clean["od"]["fundus"]["macula"] == "normal" and clean["diagnosis"] == [{"icd10": "H52.1", "eye": "ou"}]
    for bad, msg in (({"od": {"refraction": {"sph": -2.1}}}, "steps of 0.25"),
                     ({"od": {"refraction": {"sph": -1, "cyl": -0.5}}}, "axis: needed"),
                     ({"od": {"va": {"distance_unaided": "20/20"}}}, "must be one of"),
                     ({"od": {"refraction": {"axis": 181, "cyl": -1}}}, "between 0 and 180"),
                     ({"od": {"iop": {"mmhg": 16, "time": "25:00"}}}, "HH:MM"),
                     ({"od": {"colour": "x"}}, "unknown field"),
                     ({"diagnosis": [{"icd10": "cataract"}]}, "ICD-10")):
        with pytest.raises(HTTPException) as e:
            _v("eye_exam_v1", bad)
        assert e.value.status_code == 422 and msg in e.value.detail["message"], bad


def test_dental_chart_perio_and_derma_scores():
    clean, comp = _v("dental_chart_v1", {"teeth": {"16": {"surfaces": {"O": "caries", "M": "filled"}, "planned": ["rct", "crown"]},
                                                   "36": {"status": "missing"}, "11": {"surfaces": {"I": "filled"}}}},
                     age_years=30)
    assert comp["dmft"] == {"d": 1, "m": 1, "f": 1, "total": 3} and comp["planned_teeth"] == ["16"]
    assert list(clean["teeth"]) == ["11", "16", "36"]
    with pytest.raises(HTTPException):
        _v("dental_chart_v1", {"teeth": {"19": {}}})                               # not an FDI tooth
    with pytest.raises(HTTPException):
        _v("dental_chart_v1", {"teeth": {"36": {"status": "missing", "mobility": 2}}})
    with pytest.raises(HTTPException) as e:
        _v("dental_chart_v1", {"dentition": "primary", "teeth": {"55": {}}}, age_years=20)
    assert "staff confirmation" in e.value.detail["message"]
    assert _v("dental_chart_v1", {"dentition": "primary", "teeth": {"55": {}}}, age_years=20, staff_confirmed_primary=True)
    _, perio = _v("dental_perio_v1", {"teeth": {"16": {"pd": [3, 2, 5, 4, 3, 6], "bop": [True] * 3 + [False] * 3}}})
    assert perio == {"mean_pd_mm": 3.8, "bop_pct": 50, "sites_ge_5mm": 2}
    _, pasi = _v("derma_exam_v1", {"pasi": {"head": {"erythema": 2, "induration": 2, "desquamation": 1, "area": 2},
                                            "lower": {"erythema": 3, "induration": 3, "desquamation": 3, "area": 4}}})
    assert pasi == {"pasi": round(0.1 * 5 * 2 + 0.4 * 9 * 4, 1)}                    # 1.0 + 14.4 = 15.4
    with pytest.raises(HTTPException):
        _v("derma_procedure_v1", {"procedure": "prp", "test_patch": "positive"})   # describe the reaction


def test_api_save_freeze_and_history(api):  # noqa: F811
    w = sp_world(api)
    enc, _ = encounter(w)
    api.as_(w.u.doctor)
    path = f"/admin/sp/encounters/{enc}/forms/eye_exam_v1"
    r = api.call("PUT", path, w.clinic, {"data": {"od": {"refraction": {"sph": -1.25}}}})
    assert r.status_code == 200, r.text
    assert r.json()["computed"]["od"]["sph_equivalent"] == -1.25
    r = api.call("PUT", path, w.clinic, {"data": {"od": {"refraction": {"sph": -1.5}}}})   # overwrite, still one row
    assert r.json()["data"]["od"]["refraction"]["sph"] == -1.5
    assert len(api.call("GET", f"/admin/sp/encounters/{enc}/forms", w.clinic).json()["forms"]) == 1
    assert api.call("PUT", f"/admin/sp/encounters/{enc}/forms/dental_chart_v1", w.clinic,
                    {"data": {}}).status_code == 403                                # dental pack is off here
    assert api.call("PUT", f"/admin/sp/encounters/{enc}/forms/nope_v1", w.clinic, {"data": {}}).status_code == 404
    api.as_(w.u.desk)
    assert api.call("PUT", path, w.clinic, {"data": {}}).status_code == 403          # desk is not clinical
    sign(w, enc)
    api.as_(w.u.doctor)
    assert api.call("PUT", path, w.clinic, {"data": {"od": {"refraction": {"sph": -2}}}}).status_code == 409
    assert api.call("DELETE", path, w.clinic).status_code == 409
    with pytest.raises(psycopg2.Error, match="sp_form_frozen"):
        _q(w.conn, "UPDATE opd_encounter_forms SET data = '{}'::jsonb WHERE encounter_id = %s", enc)
    hist = api.call("GET", f"/admin/sp/patients/{w.patient}/forms/eye_exam_v1", w.clinic).json()["forms"]
    assert [h["data"]["od"]["refraction"]["sph"] for h in hist] == [-1.5]


def _norm(pdf: bytes) -> str:
    pdf = re.sub(rb"/CreationDate \(D:[^)]*\)", b"", pdf)
    return hashlib.sha256(re.sub(rb"/ID \[<[0-9A-F]+><[0-9A-F]+>\]", b"", pdf)).hexdigest()


def _rx():
    from tests.test_opd_pdf import SAMPLE_ITEMS, SAMPLE_SIGNED_RX
    rx = copy.deepcopy(SAMPLE_SIGNED_RX)
    rx.update(id="0b6f1c2e-0000-4000-8000-000000000001", clinic_id="0b6f1c2e-0000-4000-8000-000000000002")
    rx["signer_snapshot"]["doctor_id"] = "0b6f1c2e-0000-4000-8000-000000000003"
    return rx, SAMPLE_ITEMS


def test_golden_prescription_pdf_is_byte_identical_without_form_rows():
    from app.services.opd_pdf import render_prescription_pdf
    rx, items = _rx()
    assert _norm(render_prescription_pdf(rx, items)) == GOLDEN_RX_SHA256
    assert _norm(render_prescription_pdf(rx, items, None)) == GOLDEN_RX_SHA256
    assert _norm(render_prescription_pdf(rx, items, [])) == GOLDEN_RX_SHA256
    with_form = render_prescription_pdf(rx, items, [{"form_code": "eye_exam_v1", "data": {
        "od": {"va": {"distance_unaided": "6/12"}, "refraction": {"sph": -1.0, "cyl": -0.5, "axis": 90},
               "iop": {"mmhg": 14, "method": "nct"}}}, "computed": {}}])
    assert _norm(with_form) != GOLDEN_RX_SHA256 and with_form[:4] == b"%PDF"


def test_forms_for_print_makes_no_query_without_the_module():
    from app.services import sp
    clinic = {"id": "c1", "plan": "eye", "features": {"opd_enabled": True}}          # Specialty OS not switched on
    assert asyncio.run(sp.forms_for_print(clinic, "e1")) is None
    assert asyncio.run(sp.forms_for_print(None, "e1")) is None


def test_prescription_route_prints_the_form_block(api, monkeypatch):  # noqa: F811
    w = sp_world(api)
    enc, appt = encounter(w)
    api.as_(w.u.doctor)
    assert api.call("PUT", f"/admin/sp/encounters/{enc}/forms/eye_exam_v1", w.clinic,
                    {"data": {"od": {"refraction": {"sph": -1.0}}}}).status_code == 200
    seen = {}
    import app.routers.opd as opd_router

    def spy(rx, items, forms=None):
        seen["forms"] = forms
        return b"%PDF-1.4 spy"
    monkeypatch.setattr(opd_router, "render_prescription_pdf", spy)
    rx = str(_q(w.conn, "INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id, status) "
                        "VALUES (%s, %s, %s, %s, %s, 'draft') RETURNING id", w.clinic, enc, appt, w.patient, w.doctor))
    api.as_(w.u.admin)
    r = api.call("GET", f"/admin/opd/prescriptions/{rx}/pdf", w.clinic)
    assert r.status_code == 200, r.text
    assert [f["form_code"] for f in seen["forms"]] == ["eye_exam_v1"]
