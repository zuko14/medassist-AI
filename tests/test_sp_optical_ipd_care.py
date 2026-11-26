"""SP-EYE-02 optical stock, SP-EYE-05 IOL estimate → IPD package, CC one-tap wiring, derma severity scores.

Stock: items with an append-only ledger; a job issues its frame + lens when it first goes to the lab (out of stock
refuses the move and changes nothing), a remake issues another lens, a cancel after issue returns both; write-off needs
a reason; the ledger cannot be edited. IOL → IPD: only an accepted estimate, at the accepted option's total, applied to
the patient's own admission through the IPD package path; the pinned IPD / RX suites are not touched. CC wiring: the
care-pack list answers from signed activations only, prefills facts Kriya already holds, and makes no CC query when
Continuous Care is off. Derma: PASI, SCORAD, MASI, SALT are arithmetic on entered components.
"""

from datetime import date, timedelta

import psycopg2
import pytest

from app.services import sp_forms
from tests.sp_world import encounter, sign, sp_world
from tests.test_ipd_api_e2e import _admit, _q, _setup_hospital, api  # noqa: F401  (fixture)
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)

EXAM = {"od": {"refraction": {"sph": -1.75}}, "os": {"refraction": {"sph": -1.5}}}


def _signed_form(api, w):  # noqa: F811
    enc, _ = encounter(w)
    api.as_(w.u.doctor)
    f = api.call("PUT", f"/admin/sp/encounters/{enc}/forms/eye_exam_v1", w.clinic, {"data": EXAM}).json()
    sign(w, enc)
    return f


def test_optical_stock_follows_the_job(api):  # noqa: F811
    w = sp_world(api)
    api.as_(w.u.admin)
    frame = api.call("POST", "/admin/sp/optical/items", w.clinic, {"sku": "FR-TITAN", "kind": "frame", "name": "Titan T-200",
                                                                   "barcode": "8901234567", "reorder_level": 1}).json()
    lens = api.call("POST", "/admin/sp/optical/items", w.clinic, {"sku": "LN-PAL16", "kind": "lens", "name": "PAL 1.6 AR"}).json()
    assert api.call("POST", "/admin/sp/optical/items", w.clinic, {"sku": "FR-TITAN", "kind": "frame", "name": "dup"}).status_code == 409
    api.as_(w.u.desk)
    assert api.call("POST", f"/admin/sp/optical/items/{frame['id']}/stock", w.clinic, {"delta": 2, "reason": "receive"}).json()["qty_on_hand"] == 2
    assert api.call("POST", f"/admin/sp/optical/items/{lens['id']}/stock", w.clinic, {"delta": 1, "reason": "receive"}).status_code == 200
    assert api.call("POST", f"/admin/sp/optical/items/{lens['id']}/stock", w.clinic, {"delta": -1, "reason": "write_off"}).status_code == 403
    form = _signed_form(api, w)
    api.as_(w.u.desk)
    bad = api.call("POST", "/admin/sp/optical/jobs", w.clinic, {"form_id": form["id"], "lens_desc": "PAL", "total_paise": 500000,
                                                               "lens_item_id": frame["id"]})
    assert bad.status_code == 422                                                       # a frame is not a lens
    job = api.call("POST", "/admin/sp/optical/jobs", w.clinic, {"form_id": form["id"], "lens_desc": "PAL", "total_paise": 500000,
                                                               "advance_paise": 100000, "frame_item_id": frame["id"],
                                                               "lens_item_id": lens["id"]}).json()
    mv = lambda to, **kw: api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": to, **kw})  # noqa: E731
    assert mv("sent_to_lab").status_code == 200
    qty = lambda i: _q(w.conn, "SELECT qty_on_hand FROM sp_optical_items WHERE id = %s", i)  # noqa: E731
    assert (qty(frame["id"]), qty(lens["id"])) == (1, 0)
    assert mv("received").status_code == 200
    assert mv("remake", reason="axis wrong").status_code == 200
    r = mv("sent_to_lab")
    assert r.status_code == 409 and "stock" in r.text                                    # no lens left for the remake
    assert _q(w.conn, "SELECT status FROM sp_optical_jobs WHERE id = %s", job["id"]) == "remake"   # nothing changed
    api.call("POST", f"/admin/sp/optical/items/{lens['id']}/stock", w.clinic, {"delta": 1, "reason": "receive"})
    assert mv("sent_to_lab").status_code == 200 and qty(lens["id"]) == 0
    assert _q(w.conn, "SELECT count(*) FROM sp_optical_stock_moves WHERE job_id = %s AND reason = 'issue_job'", job["id"]) == 3
    with pytest.raises(psycopg2.Error, match="sp_optical_moves_append_only"):
        _q(w.conn, "UPDATE sp_optical_stock_moves SET delta = 5 WHERE item_id = %s", frame["id"])
    items = {i["sku"]: i for i in api.call("GET", "/admin/sp/optical/items", w.clinic).json()["items"]}
    assert items["FR-TITAN"]["low"] is True and items["FR-TITAN"]["qty_on_hand"] == 1
    assert len(api.call("GET", "/admin/sp/optical/items", w.clinic, q="8901234").json()["items"]) == 1
    api.as_(w.u.admin)
    assert api.call("POST", f"/admin/sp/optical/items/{frame['id']}/stock", w.clinic,
                    {"delta": -1, "reason": "write_off"}).status_code == 422             # a reason is required
    assert api.call("POST", f"/admin/sp/optical/items/{frame['id']}/stock", w.clinic,
                    {"delta": -1, "reason": "write_off", "note": "broken hinge"}).json()["qty_on_hand"] == 0


def test_cancel_after_issue_returns_stock(api):  # noqa: F811
    w = sp_world(api)
    api.as_(w.u.admin)
    lens = api.call("POST", "/admin/sp/optical/items", w.clinic, {"sku": "LN-1", "kind": "lens", "name": "SV 1.5"}).json()
    api.call("POST", f"/admin/sp/optical/items/{lens['id']}/stock", w.clinic, {"delta": 1, "reason": "receive"})
    form = _signed_form(api, w)
    api.as_(w.u.admin)
    job = api.call("POST", "/admin/sp/optical/jobs", w.clinic, {"form_id": form["id"], "lens_desc": "SV", "total_paise": 100000,
                                                               "advance_paise": 50000, "lens_item_id": lens["id"]}).json()
    api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": "sent_to_lab"})
    api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": "received"})
    api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": "remake", "reason": "chipped edge"})
    assert api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": "cancelled"}).status_code == 200
    assert _q(w.conn, "SELECT qty_on_hand FROM sp_optical_items WHERE id = %s", lens["id"]) == 1


def test_accepted_iol_estimate_becomes_an_ipd_package(api):  # noqa: F811
    w = sp_world(api)
    doctor, u, beds = _setup_hospital(api, w.clinic)
    api.as_(w.u.admin)
    est = api.call("POST", "/admin/sp/estimates", w.clinic, {
        "patient_id": w.patient, "specialty": "eye", "title": "Cataract surgery — right eye", "valid_until": str(date.today() + timedelta(days=20)),
        "options": [{"code": "mono", "label": "Monofocal", "lines": [{"label": "Surgery", "amount_paise": 1800000},
                                                                     {"label": "IOL", "amount_paise": 700000}]},
                    {"code": "toric", "label": "Toric", "lines": [{"label": "Surgery + toric IOL", "amount_paise": 4500000}]}]}).json()
    assert api.call("POST", f"/admin/sp/estimates/{est['id']}/convert-ipd", w.clinic, {}).status_code == 409   # not accepted
    api.call("POST", f"/admin/sp/estimates/{est['id']}/respond", w.clinic, {"option": "mono"})
    other = _admit(api, w.clinic, u, doctor, beds["101"]).json()                       # another patient's stay
    api.as_(w.u.admin)
    assert api.call("POST", f"/admin/sp/estimates/{est['id']}/convert-ipd", w.clinic,
                    {"admission_id": other["id"]}).status_code == 404
    mine = _admit(api, w.clinic, u, doctor, beds["102"], patient=w.patient).json()
    api.as_(w.u.admin)
    r = api.call("POST", f"/admin/sp/estimates/{est['id']}/convert-ipd", w.clinic, {"admission_id": mine["id"]})
    assert r.status_code == 201, r.text
    pkg = r.json()["ipd_package"]
    assert pkg["price_paise"] == 2500000 and pkg["name"].endswith("Monofocal") and r.json()["applied"]
    row = _q(w.conn, "SELECT row_to_json(e) FROM sp_estimates e WHERE id = %s", est["id"])
    assert (row["status"], row["converted_ipd_package_id"], row["converted_sale_id"]) == ("converted", pkg["id"], None)
    assert _q(w.conn, "SELECT count(*) FROM ipd_admission_packages WHERE admission_id = %s AND status = 'active'", mine["id"]) == 1
    api.as_(w.u.desk)
    assert api.call("POST", f"/admin/sp/estimates/{est['id']}/convert-ipd", w.clinic, {}).status_code == 403


def test_care_packs_wiring(api, spy):  # noqa: F811
    w = sp_world(api, plan="womenchild", packs=("anc", "imm"))
    api.as_(w.u.doctor)
    before = len(spy)
    off = api.call("GET", "/admin/sp/care-packs", w.clinic, specialty="anc", patient_id=w.patient).json()
    assert off == {"care": False, "packs": []} and not [p for p in spy[before:] if "/cc_" in p]
    assert api.call("GET", "/admin/sp/care-packs", w.clinic, specialty="eye").status_code == 403   # eye pack off here
    assert api.owner_toggle(w.clinic, "care_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    edd = date.today() + timedelta(days=60)
    api.call("POST", "/admin/sp/anc/pregnancies", w.clinic, {"patient_id": w.patient, "edd": str(edd), "edd_source": "dating_scan",
                                                             "gravida": 1})
    on = api.call("GET", "/admin/sp/care-packs", w.clinic, specialty="anc", patient_id=w.patient).json()
    assert on["care"] is True and [p["code"] for p in on["packs"]] == ["anc", "postnatal"]
    anc = on["packs"][0]
    assert anc["active"] is False and anc["facts"] == {"edd": str(edd)}                  # no signed activation yet


def test_derma_scores():
    _, c = sp_forms.validate("derma_exam_v1", {
        "scorad": {"extent_pct": 30, "intensity": {"erythema": 2, "oedema": 1, "oozing": 0, "excoriation": 2,
                                                   "lichenification": 1, "dryness": 2}, "pruritus": 6, "sleep_loss": 4},
        "masi": {"forehead": {"area": 2, "darkness": 2, "homogeneity": 1}, "chin": {"area": 1, "darkness": 1, "homogeneity": 1}},
        "salt": {"vertex": 50, "right": 20, "left": 10, "posterior": 0}})
    assert c["scorad"] == round(30 / 5 + 7 * 8 / 2 + 6 + 4, 1) == 44.0 and c["scorad_objective"] == 34.0
    assert c["masi"] == round(0.3 * 2 * 3 + 0.1 * 1 * 2, 1) == 2.0
    assert c["salt"] == round(0.4 * 50 + 0.18 * 20 + 0.18 * 10, 1) == 25.4
    from fastapi import HTTPException
    for bad in ({"scorad": {"intensity": {"erythema": 4}}}, {"masi": {"nose": {}}}, {"salt": {"vertex": 120}}):
        with pytest.raises(HTTPException):
            sp_forms.validate("derma_exam_v1", bad)
    lines = sp_forms.pdf_lines({"form_code": "derma_exam_v1", "data": {}, "computed": c})
    assert lines[0] == "SCORAD 44.0 · MASI 2.0 · SALT 25.4"
