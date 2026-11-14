"""Diagnostic OS bench end to end (P6: W8, W9, AUDIT A5/A26): real HTTP routes, real Postgres
(tests/ipd_pg_bridge.py).

Worklist stages and TAT, LIS routes refusing DX orders and the hospital sign-off
queue hiding them, worksheet preview with a calculated parameter, entry gated on
receipt, critical value → call with read-back → approval, three-level
verification (no self-verification), batch approval of normal results only,
delta failure from a previous validated value, approval needing a doctor-linked
login, attachments, the pure stage / TAT functions and the TAT scan job.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_dx_visits_e2e import _approve as approve_test, dxw, visit as _desk_visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def bench(dxw):  # noqa: F811
    c, conn, U = dxw["clinic"], dxw["c"], dxw["api"].U
    dxw["site"] = str(_q(conn, "SELECT id FROM dx_sites WHERE clinic_id = %s", c))
    lipid, cbc = dxw["T"]["lipid"], dxw["T"]["cbc"]
    # lipid: entered LIPID + HDL, calculated RATIO; cbc: bands, criticals, a 20 % delta in both directions
    _q(conn, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, parameter_code, sort_order) VALUES "
             "(%s, %s, 'HDL', 'HDL', 2)", c, lipid)
    _q(conn, "INSERT INTO lab_test_parameters (clinic_id, lab_test_id, name, parameter_code, sort_order, formula, "
             "decimal_places) VALUES (%s, %s, 'TC/HDL ratio', 'RATIO', 3, 'LIPID / HDL', 2)", c, lipid)
    _q(conn, "UPDATE lab_test_parameters SET normal_low = 10, normal_high = 20, critical_low = 5, critical_high = 30, "
             "delta_direction = 'both', delta_limit_percent = 20 WHERE lab_test_id = %s", cbc)
    _q(conn, "UPDATE lab_tests SET tat_minutes = 240, stat_tat_minutes = 60 WHERE id IN (%s, %s)", lipid, cbc)
    for t in (lipid, cbc):
        approve_test(conn, c, t, dxw["doctor"])
    uid = lambda: str(uuid.uuid4())  # noqa: E731
    dxw["u"].update({
        "col": U("col", "staff", c, uid(), ["DX_COLLECT"], staff_role="FRONT_DESK"),
        "tech": U("tech", "staff", c, uid(), [], staff_role="LAB_OPERATOR"),
        "sup": U("sup", "staff", c, uid(), [], staff_role="LAB_SUPERVISOR"),
        "path": U("path", "staff", c, uid(), [], staff_role="PATHOLOGIST", doctor_id=dxw["doctor"]),
        "unlinked": U("path2", "staff", c, uid(), ["LAB_VALIDATE"], staff_role="PATHOLOGIST"),
    })
    return dxw


def _as(w, who):
    return w["api"].as_(w["u"][who])


def _visit(w, items, **extra):
    v = _desk_visit(w, items, **extra)
    assert v.status_code == 201, v.text
    return v.json()


def _collect_receive(w, v, receive=True):
    r = _as(w, "col").call("POST", f"/admin/dx/visits/{v['visit']['id']}/collect", w["clinic"],
                           {"samples": [{"id": s["id"]} for s in v["samples"]], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    if receive:
        for s in v["samples"]:
            rr = _as(w, "tech").call("POST", "/admin/dx/samples/receive-direct", w["clinic"],
                                     {"barcode": s["barcode"], "site_id": w["site"]})
            assert rr.status_code == 200, rr.text


def _order(v, name):
    return next(o for o in v["orders"] if o["item_name"] == name)["id"]


def _params(w, order_id):
    ws = _as(w, "tech").call("GET", f"/admin/dx/orders/{order_id}/worksheet", w["clinic"])
    assert ws.status_code == 200, ws.text
    return {p["name"]: p for p in ws.json()["parameters"]}, ws.json()


def _save(w, order_id, values: dict, who="tech", preview=False):
    p, _ = _params(w, order_id)
    body = {"values": [{"parameter_id": p[k]["parameter_id"], "value": v} for k, v in values.items()], "preview": preview}
    return _as(w, who).call("POST", f"/admin/dx/orders/{order_id}/results", w["clinic"], body)


def _levels(w, n):
    _q(w["c"], "UPDATE clinics SET dx_settings = dx_settings || jsonb_build_object('approval_levels', %s) WHERE id = %s",
       n, w["clinic"])


def _stages(w, v):
    wl = _as(w, "tech").call("GET", "/admin/dx/worklist", w["clinic"])
    assert wl.status_code == 200, wl.text
    row = next(x for x in wl.json()["visits"] if x["id"] == v["visit"]["id"])
    return {o["item_name"]: o for o in row["orders"]}, wl.json()


def _approve(w, results, who="path", batch=False):
    return _as(w, who).call("POST", "/admin/dx/results/approve", w["clinic"], {
        "items": [{"result_id": r["id"], "expected_updated_at": r["updated_at"]} for r in results], "batch": batch})


# ── worklist, LIS isolation ─────────────────────────────────────────────────

def test_worklist_stages_and_lis_isolation(bench):
    c = bench["clinic"]
    v = _visit(bench, ["cbc", "lipid"], priority="stat")
    st, wl = _stages(bench, v)
    assert {o["stage"] for o in st.values()} == {"pending_collection"}
    assert st["CBC"]["tat"]["target_minutes"] == 60 and st["CBC"]["sample"]["colour_hex"]
    assert "phone" not in str(wl)
    _collect_receive(bench, v, receive=False)
    st, _ = _stages(bench, v)
    assert st["CBC"]["stage"] == "pending_receipt"
    # entry waits for receipt
    early = _save(bench, _order(v, "CBC"), {"CBC": "14"})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "dx_sample_awaiting_receipt"
    for s in v["samples"]:
        _as(bench, "tech").call("POST", "/admin/dx/samples/receive-direct", c, {"barcode": s["barcode"],
                                                                               "site_id": bench["site"]})
    st, wl = _stages(bench, v)
    assert st["CBC"]["stage"] == "pending_entry" and wl["counts"]["pending_entry"] >= 2
    assert wl["approval_levels"] == 2
    # the hospital LIS routes refuse DX orders, and its sign-off queue never lists them (AUDIT A5)
    p, _ = _params(bench, _order(v, "CBC"))
    lis_entry = _as(bench, "tech").call("POST", f"/admin/lis/orders/{_order(v, 'CBC')}/results", c,
                                        {"values": [{"parameter_id": p["CBC"]["parameter_id"], "value": "14"}]})
    assert lis_entry.status_code == 409 and lis_entry.json()["detail"] == "dx_use_dx_route"
    r = _save(bench, _order(v, "CBC"), {"CBC": "14"}).json()
    assert _as(bench, "path").call("POST", f"/admin/lis/results/{r['id']}/validate", c,
                                   {"expected_updated_at": r["updated_at"]}).json()["detail"] == "dx_use_dx_route"
    queue = _as(bench, "path").call("GET", "/admin/lis/results", c).json()["results"]
    assert r["id"] not in [x["id"] for x in queue]
    assert _stages(bench, v)[0]["CBC"]["stage"] == "pending_approval"


# ── entry: calculated parameter, preview ────────────────────────────────────

def test_preview_and_calculated_parameter(bench):
    v = _visit(bench, ["lipid"])
    _collect_receive(bench, v)
    oid = _order(v, "Lipid Profile")
    pv = _save(bench, oid, {"Lipid Profile": "200", "HDL": "48", "TC/HDL ratio": "999"}, preview=True)
    assert pv.status_code == 200, pv.text
    by = {x["parameter_name"]: x for x in pv.json()["values"]}
    assert pv.json()["saved"] is False and by["TC/HDL ratio"]["numeric_value"] == "4.170"    # 200/48, typed value ignored
    assert by["TC/HDL ratio"]["is_calculated"] is True
    assert _q(bench["c"], "SELECT count(*) FROM lab_results WHERE order_id = %s", oid) == 0
    saved = _save(bench, oid, {"Lipid Profile": "200", "HDL": "48"})
    assert saved.status_code == 200 and saved.json()["saved"] is True
    _, ws = _params(bench, oid)
    assert {m["parameter_id"] for m in ws["result"]["meta"] if m["is_calculated"]} == \
        {p["parameter_id"] for p in ws["parameters"] if p["name"] == "TC/HDL ratio"}


# ── critical value (AUDIT A26) ──────────────────────────────────────────────

def test_critical_value_needs_a_call_before_approval(bench):
    c = bench["clinic"]
    v = _visit(bench, ["cbc"])
    _collect_receive(bench, v)
    r = _save(bench, _order(v, "CBC"), {"CBC": "35"}).json()
    assert r["has_critical_value"] is True and r["critical_event_id"]
    st, wl = _stages(bench, v)
    assert st["CBC"]["result"]["critical_pending"] is True
    assert next(x for x in wl["visits"] if x["id"] == v["visit"]["id"])["has_critical"] is True
    refused = _approve(bench, [r]).json()
    assert refused["signed"] == 0 and refused["items"][0]["code"] == "dx_critical_not_notified"
    call = _as(bench, "sup").call("POST", f"/admin/dx/critical-events/{r['critical_event_id']}/call", c,
                                  {"notified_to": "Dr. Anuradha", "notified_role": "referrer", "channel": "phone",
                                   "read_back_confirmed": True})
    assert call.status_code == 201, call.text
    assert _as(bench, "col").call("POST", f"/admin/dx/critical-events/{r['critical_event_id']}/call", c,
                                  {"notified_to": "x y", "notified_role": "other", "channel": "phone",
                                   "read_back_confirmed": True}).status_code == 403
    ok = _approve(bench, [r]).json()
    assert ok["signed"] == 1, ok
    assert _stages(bench, v)[0]["CBC"]["stage"] == "pending_release"
    signed = _q(bench["c"], "SELECT pathologist_snapshot FROM lab_results WHERE id = %s", r["id"])
    assert signed["registration_number"] and signed["name"] == "Dr. Rao"


# ── three levels ─────────────────────────────────────────────────────────────

def test_three_level_verification(bench):
    c = bench["clinic"]
    _levels(bench, 3)
    v = _visit(bench, ["cbc"])
    _collect_receive(bench, v)
    r = _save(bench, _order(v, "CBC"), {"CBC": "14"}, who="sup").json()     # entered by the supervisor
    assert _stages(bench, v)[0]["CBC"]["stage"] == "pending_verification"
    assert _approve(bench, [r]).json()["items"][0]["code"] == "dx_verification_required"
    assert _as(bench, "tech").call("POST", f"/admin/dx/results/{r['id']}/verify", c,
                                   {"expected_updated_at": r["updated_at"], "decision": "verified"}).status_code == 403
    self_v = _as(bench, "sup").call("POST", f"/admin/dx/results/{r['id']}/verify", c,
                                    {"expected_updated_at": r["updated_at"], "decision": "verified"})
    assert self_v.status_code == 403 and self_v.json()["detail"]["code"] == "dx_self_verification"
    ret = _as(bench, "path").call("POST", f"/admin/dx/results/{r['id']}/verify", c,
                                  {"expected_updated_at": r["updated_at"], "decision": "returned", "comment": "rerun"})
    assert ret.status_code == 200
    st, _ = _stages(bench, v)
    assert st["CBC"]["stage"] == "pending_verification" and st["CBC"]["result"]["returned"] is True
    r2 = _save(bench, _order(v, "CBC"), {"CBC": "15"}).json()                 # tech re-enters
    ok = _as(bench, "sup").call("POST", f"/admin/dx/results/{r2['id']}/verify", c,
                                {"expected_updated_at": r2["updated_at"], "decision": "verified"})
    assert ok.status_code == 200, ok.text
    assert _stages(bench, v)[0]["CBC"]["stage"] == "pending_approval"
    assert _approve(bench, [r2]).json()["signed"] == 1


# ── batch approval, delta, signer ───────────────────────────────────────────

def test_batch_approval_and_delta(bench):
    first = _visit(bench, ["cbc"])
    _collect_receive(bench, first)
    r0 = _save(bench, _order(first, "CBC"), {"CBC": "12"}).json()
    assert _approve(bench, [r0]).json()["signed"] == 1                      # becomes the previous value
    v = _visit(bench, ["cbc", "lipid"])
    _collect_receive(bench, v)
    jump = _save(bench, _order(v, "CBC"), {"CBC": "16"}).json()               # +33 % > 20 %: delta fails
    assert jump["values"][0]["delta_failed"] is True and jump["values"][0]["previous_value"] == "12"
    normal = _save(bench, _order(v, "Lipid Profile"), {"Lipid Profile": "150", "HDL": "50"}).json()
    st, _ = _stages(bench, v)
    assert st["CBC"]["result"]["batch_ok"] is False
    # the lipid ratio has no band: 'not_evaluated' is not 'normal', so it cannot be batch-signed either
    assert st["Lipid Profile"]["result"]["batch_ok"] is False
    out = _approve(bench, [jump, normal], batch=True).json()
    assert out["signed"] == 0 and {i["code"] for i in out["items"]} == {"dx_batch_not_normal"}
    # signing needs a doctor-linked login with a registration number
    assert _approve(bench, [jump], who="unlinked").status_code == 403
    assert _approve(bench, [jump], who="tech").status_code == 403
    one = _approve(bench, [jump]).json()
    assert one["signed"] == 1


def test_batch_signs_normal_results(bench):
    v1, v2 = _visit(bench, ["cbc"]), _visit(bench, ["cbc"])
    for v in (v1, v2):
        _collect_receive(bench, v)
    a = _save(bench, _order(v1, "CBC"), {"CBC": "14"}).json()
    b = _save(bench, _order(v2, "CBC"), {"CBC": "15"}).json()
    assert _stages(bench, v1)[0]["CBC"]["result"]["batch_ok"] is True
    out = _approve(bench, [a, b], batch=True).json()
    assert out["signed"] == 2 and out["refused"] == 0


# ── attachments ─────────────────────────────────────────────────────────────

def test_attachments(bench, monkeypatch):
    from app.services import dx_results
    store = {}

    class _Bucket:
        def upload(self, path, data, opts):
            store[path] = data

        def download(self, path):
            return store[path]

    class _Storage:
        def from_(self, bucket):
            assert bucket == dx_results.ATTACHMENT_BUCKET
            return _Bucket()
    monkeypatch.setattr(type(dx_results.supabase), "storage", property(lambda self: _Storage()), raising=False)
    c = bench["clinic"]
    v = _visit(bench, ["cbc"])
    _collect_receive(bench, v)
    r = _save(bench, _order(v, "CBC"), {"CBC": "14"}).json()
    api = _as(bench, "tech")
    url = f"/admin/dx/results/{r['id']}/attachments"
    bad = api.client.post(url, params={"clinic_id": c, "kind": "pdf"}, files={"file": ("g.png", PNG)})
    assert bad.status_code == 415
    up = api.client.post(url, params={"clinic_id": c, "kind": "graph", "caption": "Histogram"},
                         files={"file": ("g.png", PNG)})
    assert up.status_code == 201, up.text
    got = api.call("GET", f"{url}/{up.json()['id']}", c)
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"
    _approve(bench, [r])
    locked = api.client.post(url, params={"clinic_id": c, "kind": "graph"}, files={"file": ("g.png", PNG)})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "dx_attachment_locked"


# ── pure stage / TAT, scan job ──────────────────────────────────────────────

def test_stage_and_tat_functions():
    from app.services.dx import dx_stage, tat_status
    v = {"billing_mode": "patient"}
    assert dx_stage({"status": "ordered"}, {"status": "pending_collection"}, None, v,
                    {"collect_before_payment": True, "due_paise": 100}) == "awaiting_payment"
    assert dx_stage({"status": "ordered"}, {"status": "pending_collection"}, None, v) == "pending_collection"
    assert dx_stage({"status": "sample_collected"}, {"status": "in_transit"}, None, v) == "pending_receipt"
    assert dx_stage({"status": "sample_collected"}, {"status": "received"}, None, v) == "pending_entry"
    assert dx_stage({"status": "in_progress"}, {"status": "received"}, {"status": "draft"}, v) == "pending_entry"
    entered = {"status": "entered"}
    assert dx_stage({"status": "in_progress"}, None, entered, v, {"approval_levels": 3}) == "pending_verification"
    assert dx_stage({"status": "in_progress"}, None, entered, v, {"approval_levels": 3, "verified": True}) == \
        "pending_approval"
    assert dx_stage({"status": "in_progress"}, None, entered, v) == "pending_approval"
    assert dx_stage({"status": "completed"}, None, {"status": "validated"}, v) == "pending_release"
    assert dx_stage({"status": "completed"}, None, {"status": "validated"}, v, {"released": True}) == "released"
    assert dx_stage({"status": "cancelled"}, None, None, v) == "cancelled"
    now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    ago = lambda m: now - timedelta(minutes=m)  # noqa: E731
    assert tat_status(ago(30), 60, now=now) == {"elapsed_minutes": 30, "target_minutes": 60, "status": "green"}
    assert tat_status(ago(45), 60, now=now)["status"] == "amber"
    assert tat_status(ago(60), 60, now=now)["status"] == "red"
    assert tat_status(ago(90), 60, ended_at=ago(80), now=now)["elapsed_minutes"] == 10     # clock stops at sign-off
    assert tat_status(None, 60, now=now)["status"] == "none" and tat_status(ago(5), None, now=now)["status"] == "none"


def test_tat_breach_scan(bench, caplog):
    from app.services import dx
    v = _visit(bench, ["cbc"], priority="stat")
    cur = bench["c"].cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (bench["clinic"],))
    cur.execute("UPDATE dx_accessions SET created_at = now() - interval '2 hours' WHERE id = %s", (v["visit"]["id"],))
    cur.execute("COMMIT")
    with caplog.at_level("WARNING"):
        n = asyncio.run(dx.tat_breach_scan())
    assert n >= 1 and any(f"order={_order(v, 'CBC')}" in r.message for r in caplog.records)


# ── P6b: OPD / IPD lab orders on the DX bench (W15, DX-41) ──────────────────

def _opd_order(w, test="cbc"):
    """A consultation's CBC order placed through the unchanged G1 function."""
    c, clinic = w["c"], w["clinic"]
    appt = str(_q(c, """INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id,
                        doctor_name, department, appointment_date, appointment_time, status, is_walk_in, booking_type)
                        VALUES (%s, %s, '+919000000009', 'Asha', %s, 'Dr. Rao', 'Lab', CURRENT_DATE, %s, 'completed',
                                false, 'consultation') RETURNING id""",
                  clinic, w["patient"], w["doctor"], f"{8 + uuid.uuid4().int % 10:02d}:{uuid.uuid4().int % 60:02d}:00"))
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES "
                    "(%s, %s, %s, %s) RETURNING id", clinic, appt, w["patient"], w["doctor"]))
    import json
    placed = _q(c, "SELECT opd_place_orders(%s, %s, %s::jsonb, %s, NULL, 'drrao')", clinic, enc,
                json.dumps([{"order_type": "investigation", "catalog_type": "lab_test",
                             "catalog_reference_id": w["T"][test]}]), uuid.uuid4().hex)
    return placed["orders"][0]["id"]


def test_opd_order_adopted_onto_the_dx_bench(bench):
    c, conn = bench["clinic"], bench["c"]
    order = _opd_order(bench)
    bills_before = _q(conn, "SELECT count(*) FROM opd_invoice_items WHERE order_id = %s", order)
    assert _as(bench, "tech").call("POST", "/admin/dx/orders/adopt", c, {"order_ids": [order]}).status_code == 403
    bad = _as(bench, "desk").call("POST", "/admin/dx/orders/adopt", c, {"order_ids": ["not-a-uuid"]})
    assert bad.status_code == 422
    r = _as(bench, "desk").call("POST", "/admin/dx/orders/adopt", c, {"order_ids": [order]})
    assert r.status_code == 200, r.text
    out = r.json()
    v = out["visits"][0]
    assert out["adopted"] == 1 and v["visit"]["billing_mode"] == "internal" and v["visit"]["source"] == "opd"
    assert [o["id"] for o in v["orders"]] == [order] and len(v["samples"]) == 1
    # trackable on the DX worklist, grouped into a tube, worked and signed on the DX bench
    st, _ = _stages(bench, v)
    assert st["CBC"]["stage"] == "pending_collection"
    _collect_receive(bench, v)
    saved = _save(bench, order, {"CBC": "14"})
    assert saved.status_code == 200, saved.text
    res = saved.json()
    signed = _approve(bench, [res])
    assert signed.status_code == 200 and signed.json()["signed"] == 1, signed.text
    o = _q(conn, "SELECT to_jsonb(o) FROM opd_orders o WHERE id = %s", order)
    assert o["source"] == "opd" and o["encounter_id"] and o["accession_id"] == v["visit"]["id"]
    assert _q(conn, "SELECT status FROM lab_results WHERE order_id = %s", order) == "validated"   # the OPD record's result
    assert _q(conn, "SELECT count(*) FROM opd_invoice_items WHERE order_id = %s", order) == bills_before
    assert _q(conn, "SELECT count(*) FROM opd_invoices WHERE accession_id = %s", v["visit"]["id"]) == 0
    # adopting again is a no-op; the internal visit cannot be cancelled or added to from DX
    again = _as(bench, "desk").call("POST", "/admin/dx/orders/adopt", c, {"order_ids": [order]})
    assert again.status_code == 200 and again.json()["adopted"] == 0 and again.json()["skipped"] == [order]
    cancel = _as(bench, "desk").call("POST", f"/admin/dx/visits/{v['visit']['id']}/cancel", c, {"reason": "not needed"})
    assert cancel.status_code == 409 and cancel.json()["detail"]["code"] in ("dx_internal_visit", "dx_visit_has_samples")
