"""Eye (SP-EYE-01…03) and Dental (SP-DEN-05, SP-DEN-09) depth on real Postgres.

Optical job: only from the eye_exam_v1 of a SIGNED visit, the Rx is a snapshot (a later form cannot change it); the
board has the four columns of UI spec §7.9; moves follow the job order; "Ready" messages the patient only with the
confirm flag and names the staff member; the spectacle Rx PDF prints from the signed form. Dental lab orders validate
FDI teeth and move sent → received → fitted / remake (with a reason); compliance items show days left and state.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock

from tests.sp_world import encounter, sign, sp_world
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

EXAM = {"od": {"va": {"distance_unaided": "6/18"}, "refraction": {"sph": -1.75, "cyl": -0.5, "axis": 170, "add": 1.0}},
        "os": {"va": {"distance_unaided": "6/12"}, "refraction": {"sph": -1.5}}, "pd_mm": 62, "lens_advice": "Progressive"}


def _signed_form(api, w):  # noqa: F811
    enc, _ = encounter(w)
    api.as_(w.u.doctor)
    f = api.call("PUT", f"/admin/sp/encounters/{enc}/forms/eye_exam_v1", w.clinic, {"data": EXAM}).json()
    return enc, f


def test_optical_job_board_and_ready_message(api, monkeypatch):  # noqa: F811
    from app.services.whatsapp import whatsapp_service
    w = sp_world(api)
    enc, form = _signed_form(api, w)
    api.as_(w.u.desk)
    body = {"form_id": form["id"], "lens_desc": "PAL 1.6 AR", "frame_desc": "Titan T-200", "total_paise": 650000,
            "advance_paise": 200000, "due_on": str(date.today() + timedelta(days=5))}
    r = api.call("POST", "/admin/sp/optical/jobs", w.clinic, body)
    assert r.status_code == 409 and "Sign" in r.text                                   # unsigned visit
    sign(w, enc)
    job = api.call("POST", "/admin/sp/optical/jobs", w.clinic, body)
    assert job.status_code == 201, job.text
    job = job.json()
    assert job["status"] == "advance_paid" and job["balance_paise"] == 450000
    assert job["rx_snapshot"]["od"] == {"sph": -1.75, "cyl": -0.5, "axis": 170, "add": 1.0}
    board = api.call("GET", "/admin/sp/optical/board", w.clinic).json()
    assert list(board["columns"]) == ["advance_paid", "at_lab", "ready", "delivered"]
    assert [j["id"] for j in board["columns"]["advance_paid"]] == [job["id"]]
    mv = lambda to, **kw: api.call("POST", f"/admin/sp/optical/jobs/{job['id']}/move", w.clinic, {"to": to, **kw})  # noqa: E731
    assert mv("delivered").status_code == 409                                         # cannot skip the lab
    for to in ("sent_to_lab", "received", "qc_passed"):
        assert mv(to).status_code == 200
    assert mv("remake").status_code == 422                                            # needs a reason
    _q(w.conn, "UPDATE patients SET opted_in = true WHERE id = %s", w.patient)
    text = AsyncMock(return_value=True)
    monkeypatch.setattr(whatsapp_service, "send_text", text)
    ready = mv("ready", notify=False).json()
    assert ready["status"] == "ready" and ready["ready_confirmed_by_name"] and "notified" not in ready
    assert text.await_count == 0                                                       # nothing sent without confirm
    _q(w.conn, "UPDATE sp_optical_jobs SET status = 'qc_passed' WHERE id = %s", job["id"])
    ready = mv("ready", notify=True).json()
    assert ready["notified"]["sent"] is True and "4,500" in text.await_args.args[2]
    assert "-1.75" not in text.await_args.args[2]                                      # never a clinical value
    assert mv("delivered").json()["delivered_at"]
    pdf = api.call("GET", f"/admin/sp/forms/{form['id']}/spectacle-rx.pdf", w.clinic)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    other = sp_world(api, plan="dental", packs=("dental",))
    api.as_(other.u.admin)
    assert api.call("GET", "/admin/sp/optical/board", other.clinic).status_code == 403   # eye pack off there


def test_dental_lab_orders_and_compliance(api):  # noqa: F811
    w = sp_world(api, plan="dental", packs=("dental",))
    api.as_(w.u.doctor)
    base = {"patient_id": w.patient, "lab_name": "Smile Lab", "work_type": "crown", "shade": "A2",
            "due_on": str(date.today() - timedelta(days=1))}
    assert api.call("POST", "/admin/sp/dental/lab-orders", w.clinic, {**base, "teeth": ["19"]}).status_code == 422
    o = api.call("POST", "/admin/sp/dental/lab-orders", w.clinic, {**base, "teeth": ["36", "16", "16"]})
    assert o.status_code == 201, o.text
    assert o.json()["teeth"] == "16,36"
    oid = o.json()["id"]
    listed = api.call("GET", "/admin/sp/dental/lab-orders", w.clinic).json()["orders"]
    assert listed[0]["overdue"] is True
    mv = lambda to, **kw: api.call("POST", f"/admin/sp/dental/lab-orders/{oid}/move", w.clinic, {"to": to, **kw})  # noqa: E731
    assert mv("fitted").status_code == 409
    assert mv("received").status_code == 200 and mv("remake").status_code == 422
    assert mv("remake", reason="shade mismatch").json()["remake_reason"] == "shade mismatch"
    assert mv("received").status_code == 200 and mv("fitted").json()["fitted_at"]
    api.as_(w.u.admin)
    for days, state in ((-3, "expired"), (20, "renew_soon"), (400, "valid")):
        r = api.call("POST", "/admin/sp/compliance", w.clinic, {"kind": "aerb_registration", "label": f"RVG unit {days}",
                                                                "valid_until": str(date.today() + timedelta(days=days))})
        assert r.status_code == 201, r.text
    items = api.call("GET", "/admin/sp/compliance", w.clinic).json()["items"]
    assert [i["state"] for i in items] == ["expired", "renew_soon", "valid"]
    api.as_(w.u.desk)
    assert api.call("POST", "/admin/sp/compliance", w.clinic, {"kind": "other", "label": "x y z",
                                                               "valid_until": str(date.today())}).status_code == 403
