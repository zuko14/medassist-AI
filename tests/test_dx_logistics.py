"""Diagnostic OS logistics and outsourcing end to end (P5: W6, W13 data part): real HTTP routes,
real Postgres (tests/ipd_pg_bridge.py).

Outsource partners and routing rules (CRUD, tie refused, preview), tubes split by
destination at the desk with "Outsourced to" on the orders, a runner's batch from
a collection site to the hub (scan, dispatch, stability timers), the hub's receipt
with missing and unexpected tubes, site scope on batches, data minimisation for a
processing-site login (no phone / address / money), the runner's confinement, the
pure stability calculator and the stability scan job.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.test_dx_visits_e2e import dxw, visit as _desk_visit  # noqa: F401  (fixture)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)


@pytest.fixture
def net(dxw):  # noqa: F811
    """Main (collection + processing, from provisioning) and a processing-only hub."""
    c, conn, U = dxw["clinic"], dxw["c"], dxw["api"].U
    dxw["site"] = str(_q(conn, "SELECT id FROM dx_sites WHERE clinic_id = %s", c))
    dxw["hub_branch"] = str(_q(conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Hub') RETURNING id", c))
    dxw["hub"] = str(_q(conn, "INSERT INTO dx_sites (clinic_id, branch_id, kind, code, display_name) VALUES "
                              "(%s, %s, 'processing', 'HUB', 'Central hub') RETURNING id", c, dxw["hub_branch"]))
    uid = lambda: str(uuid.uuid4())  # noqa: E731
    dxw["u"].update({
        "col": U("col", "staff", c, uid(), ["DX_COLLECT"], staff_role="FRONT_DESK", branch_id=dxw["branch"]),
        "runner": U("runner", "staff", c, uid(), [], staff_role="LOGISTICS_RUNNER", branch_id=dxw["branch"]),
        "hubtech": U("hubtech", "staff", c, uid(), [], staff_role="LAB_OPERATOR", branch_id=dxw["hub_branch"]),
        "pinned_admin": U("padmin", "staff", c, uid(), ["DX_ADMIN"], branch_id=dxw["branch"]),
    })
    return dxw


def visit(w, items, **extra):
    """Registered at Main (the clinic has two sites, so the desk names the branch)."""
    return _desk_visit(w, items, branch_id=w["branch"], **extra)


def _as(w, who):
    return w["api"].as_(w["u"][who])


def _collect(w, v):
    r = _as(w, "col").call("POST", f"/admin/dx/visits/{v['visit']['id']}/collect", w["clinic"],
                           {"samples": [{"id": s["id"]} for s in v["samples"]], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()


def _hub_default(w):
    r = _as(w, "admin").call("POST", "/admin/dx/routing-rules", w["clinic"], {"to_site_id": w["hub"]})
    assert r.status_code == 201, r.text
    return r.json()


# ── partners and routing rules (DX-32, DX-34) ────────────────────────────────

def test_partners_rules_and_preview(net):
    c, api = net["clinic"], _as(net, "admin")
    p = api.call("POST", "/admin/dx/outsource-partners", c, {"name": "RefLab", "legal_name": "RefLab Pvt Ltd",
                                                             "address": "Hyderabad", "nabl_certificate_no": "MC-9"})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    assert api.call("POST", "/admin/dx/outsource-partners", c, {"name": "reflab"}).status_code == 409
    assert api.call("PUT", f"/admin/dx/outsource-partners/{pid}", c, {"contact_phone": "+919800000001"}).json()[
        "contact_phone"] == "+919800000001"
    # rules: one target, never a collection-only site, ties refused
    assert api.call("POST", "/admin/dx/routing-rules", c, {"to_site_id": net["hub"],
                                                           "outsource_partner_id": pid}).status_code == 422
    coll_branch = str(_q(net["c"], "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Spoke') RETURNING id", c))
    coll = str(_q(net["c"], "INSERT INTO dx_sites (clinic_id, branch_id, kind, code, display_name) VALUES "
                            "(%s, %s, 'collection', 'SPK', 'Spoke') RETURNING id", c, coll_branch))
    assert api.call("POST", "/admin/dx/routing-rules", c, {"to_site_id": coll}).status_code == 422
    dept = api.call("POST", "/admin/dx/routing-rules", c, {"department_id": net["dept"], "to_site_id": net["hub"],
                                                           "priority": 20})
    assert dept.status_code == 201
    tie = api.call("POST", "/admin/dx/routing-rules", c, {"department_id": net["dept"], "outsource_partner_id": pid,
                                                          "priority": 20})
    assert tie.status_code == 409 and "priority" in tie.json()["detail"]
    test_rule = api.call("POST", "/admin/dx/routing-rules", c, {"lab_test_id": net["T"]["hba1c"],
                                                                "outsource_partner_id": pid})
    assert test_rule.status_code == 201
    pv = api.call("POST", "/admin/dx/routing-rules/preview", c,
                  {"from_site_id": net["site"], "lab_test_ids": [net["T"]["cbc"], net["T"]["hba1c"]]})
    assert pv.status_code == 200, pv.text
    by = {r["name"]: r for r in pv.json()["routes"]}
    assert by["CBC"]["matched"] == "department" and by["CBC"]["destination"] == "Central hub"
    assert by["HbA1c"]["matched"] == "test" and by["HbA1c"]["destination"] == "RefLab"
    assert len(api.call("GET", "/admin/dx/routing-rules", c).json()["rules"]) == 2
    upd = api.call("PUT", f"/admin/dx/routing-rules/{dept.json()['id']}", c,
                   {"department_id": net["dept"], "to_site_id": net["site"], "priority": 30})
    assert upd.status_code == 200 and upd.json()["priority"] == 30
    assert api.call("DELETE", f"/admin/dx/routing-rules/{test_rule.json()['id']}", c).status_code == 200
    assert api.call("DELETE", f"/admin/dx/routing-rules/{test_rule.json()['id']}", c).status_code == 404
    # routing is clinic-wide configuration; a technician cannot touch it
    assert _as(net, "pinned_admin").call("POST", "/admin/dx/routing-rules", c, {"to_site_id": net["hub"]}).status_code == 403
    assert _as(net, "hubtech").call("GET", "/admin/dx/routing-rules", c).status_code == 403


def test_desk_registration_splits_and_marks_outsourced(net):
    c = net["clinic"]
    pid = _as(net, "admin").call("POST", "/admin/dx/outsource-partners", c, {"name": "RefLab"}).json()["id"]
    _as(net, "admin").call("POST", "/admin/dx/routing-rules", c, {"lab_test_id": net["T"]["hba1c"],
                                                                  "outsource_partner_id": pid})
    v = visit(net, ["cbc", "hba1c"])
    assert v.status_code == 201, v.text
    v = v.json()
    assert len(v["samples"]) == 2                                      # both EDTA, two destinations
    got = _as(net, "desk").call("GET", f"/admin/dx/visits/{v['visit']['id']}", c).json()
    assert {o["item_name"]: o["outsourced_to"] for o in got["orders"]} == {"CBC": None, "HbA1c": "RefLab"}
    q = _as(net, "col").call("GET", "/admin/dx/collection/queue", c).json()["visits"]
    tubes = next(x for x in q if x["id"] == v["visit"]["id"])["samples"]
    assert sorted(t["outsourced_to"] or "" for t in tubes) == ["", "RefLab"]


# ── batches (DX-33, W6) ──────────────────────────────────────────────────────

def test_runner_batch_to_hub_and_receipt_with_discrepancy(net):
    c = net["clinic"]
    _hub_default(net)
    v1, v2 = visit(net, ["cbc"]).json(), visit(net, ["lipid"]).json()
    _collect(net, v1)
    _collect(net, v2)
    codes = [v1["samples"][0]["barcode"], v2["samples"][0]["barcode"]]
    run = _as(net, "runner")
    lst = run.call("GET", "/admin/dx/batches", c)
    assert lst.status_code == 200 and lst.json()["my_site_ids"] == [net["site"]]
    assert {s["id"] for s in lst.json()["sites"]} == {net["site"], net["hub"]}
    b = run.call("POST", "/admin/dx/batches", c, {"to_site_id": net["hub"], "carrier_name": "Ravi", "temp_c": 4})
    assert b.status_code == 201, b.text
    bid = b.json()["id"]
    assert b.json()["from_site_id"] == net["site"] and b.json()["batch_number"].startswith("TRN-")
    add = run.call("POST", f"/admin/dx/batches/{bid}/samples", c, {"barcodes": codes})
    assert add.status_code == 200 and add.json()["added"] == 2
    st = add.json()["samples"][0]["stability"]
    assert st["status"] == "normal" and st["elapsed_hours"] >= 0
    bad = run.call("POST", f"/admin/dx/batches/{bid}/samples", c, {"barcodes": ["NOPE999"]})
    assert bad.status_code == 404 and bad.json()["detail"]["reason"] == "NOPE999"
    # the hub cannot dispatch the runner's batch; the runner cannot receive
    assert _as(net, "hubtech").call("POST", f"/admin/dx/batches/{bid}/dispatch", c, {}).status_code == 403
    run = _as(net, "runner")           # as_() swaps one shared login
    d = run.call("POST", f"/admin/dx/batches/{bid}/dispatch", c, {"temp_c": 5, "notes": "cool box"})
    assert d.status_code == 200 and {s["status"] for s in d.json()["samples"]} == {"in_transit"}
    assert run.call("POST", f"/admin/dx/batches/{bid}/receive", c, {"barcodes": codes}).status_code == 403
    hub = _as(net, "hubtech")
    incoming = hub.call("GET", "/admin/dx/batches", c, status="dispatched").json()["batches"]
    assert [x["id"] for x in incoming] == [bid] and incoming[0]["sample_count"] == 2
    r = hub.call("POST", f"/admin/dx/batches/{bid}/receive", c,
                 {"barcodes": [codes[0], "STRAY001"], "temp_c": 6, "discrepancy_notes": "one tube short"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "received_with_discrepancy"
    assert r.json()["discrepancy"] == {"received": [codes[0]], "missing": [codes[1]], "damaged": [],
                                       "unexpected": ["STRAY001"]}
    tl = _as(net, "runner").call("GET", f"/admin/dx/samples/{codes[0]}/track", c)
    assert tl.status_code == 200, tl.text
    kinds = [e["kind"] for e in tl.json()["timeline"]]
    assert kinds[-2:] == ["dispatched", "received"]
    assert tl.json()["timeline"][-1]["details"]["batch_number"] == b.json()["batch_number"]


def test_batch_site_scope(net):
    c = net["clinic"]
    _hub_default(net)
    bid = _as(net, "runner").call("POST", "/admin/dx/batches", c, {"to_site_id": net["hub"]}).json()["id"]
    other_branch = str(_q(net["c"], "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Far') RETURNING id", c))
    _q(net["c"], "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, 'FAR', 'Far')", c,
       other_branch)
    stranger = net["api"].U("far", "staff", c, str(uuid.uuid4()), [], staff_role="LOGISTICS_RUNNER",
                            branch_id=other_branch)
    api = net["api"].as_(stranger)
    assert api.call("GET", f"/admin/dx/batches/{bid}", c).status_code == 404
    assert api.call("GET", "/admin/dx/batches", c).json()["batches"] == []
    assert api.call("POST", "/admin/dx/batches", c, {"from_site_id": net["site"],
                                                     "to_site_id": net["hub"]}).status_code == 404
    # a desk login (no DX_LOGISTICS) cannot open a batch
    assert _as(net, "desk").call("POST", "/admin/dx/batches", c, {"to_site_id": net["hub"]}).status_code == 403


def test_processing_site_user_sees_no_phone(net):
    c = net["clinic"]
    _hub_default(net)
    v = visit(net, ["cbc"], external_ref="IP-778",
              delivery_prefs={"whatsapp": "+919811122233", "email": "asha@example.com"}).json()
    vid = v["visit"]["id"]
    desk = _as(net, "desk").call("GET", f"/admin/dx/visits/{vid}", c).json()
    assert desk["visit"]["external_ref"] == "IP-778" and desk["invoices"] is not None
    hub = _as(net, "hubtech").call("GET", f"/admin/dx/visits/{vid}", c)
    assert hub.status_code == 200, hub.text      # reached through the tube's processing site
    body = hub.text
    assert "+919811122233" not in body and "asha@example.com" not in body and "IP-778" not in body
    assert hub.json()["invoices"] == [] and all("price_paise" not in i for i in hub.json()["items"])
    assert hub.json()["visit"]["patient_snapshot"]["name"] == "Asha"
    _collect(net, v)
    bid = _as(net, "runner").call("POST", "/admin/dx/batches", c, {"to_site_id": net["hub"]}).json()["id"]
    _as(net, "runner").call("POST", f"/admin/dx/batches/{bid}/samples", c, {"barcodes": [v["samples"][0]["barcode"]]})
    batch = _as(net, "hubtech").call("GET", f"/admin/dx/batches/{bid}", c)
    phone = _q(net["c"], "SELECT phone FROM patients WHERE id = %s", net["patient"])
    assert batch.status_code == 200 and phone not in batch.text and "phone" not in batch.text
    assert "paise" not in batch.text
    s = batch.json()["samples"][0]
    assert (s["patient_name"], s["gender"], s["tests"]) == ("Asha", "female", ["CBC"])


def test_runner_confined_to_logistics():
    from app.routers.admin import _enforce_dx_confined_scope
    runner = SimpleNamespace(staff_role="LOGISTICS_RUNNER", username="r")

    def req(method, path):
        return SimpleNamespace(method=method, url=SimpleNamespace(path=path))
    for m, p in (("GET", "/admin/dx/batches"), ("POST", "/admin/dx/batches/x/dispatch"),
                 ("GET", "/admin/dx/samples/260700000001/track"), ("GET", "/admin/dx/outsource-partners"),
                 ("GET", "/admin/me")):
        assert _enforce_dx_confined_scope(req(m, p), runner) is runner
    for m, p in (("GET", "/admin/dx/visits"), ("GET", "/admin/dx/visits/x"), ("GET", "/admin/patients"),
                 ("GET", "/admin/dx/routing-rules")):
        with pytest.raises(HTTPException) as e:
            _enforce_dx_confined_scope(req(m, p), runner)
        assert e.value.status_code == 403


# ── stability (pure) and the scan job ────────────────────────────────────────

def test_stability_calculator():
    from app.services.dx import calculate_sample_stability as calc
    now = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    at = lambda h: now - timedelta(hours=h)  # noqa: E731
    assert calc(at(2), 8, 24, False, now) == {"elapsed_hours": 2.0, "remaining_hours": 6.0, "status": "normal"}
    assert calc(at(6), 8, 24, False, now)["status"] == "amber"            # exactly 75 %
    assert calc(at(8), 8, 24, False, now) == {"elapsed_hours": 8.0, "remaining_hours": 0.0, "status": "expired"}
    assert calc(at(8), 8, 24, True, now)["status"] == "normal"            # cold window
    assert calc(at(8), 8, None, True, now)["status"] == "expired"         # no cold window: room hours
    assert calc(at(99), None, None, False, now) == {"elapsed_hours": 99.0, "remaining_hours": None, "status": "normal"}
    assert calc(at(1).isoformat().replace("+00:00", "Z"), 8, None, False, now)["status"] == "normal"
    assert calc(now + timedelta(minutes=5), 8, None, False, now)["elapsed_hours"] == 0.0   # clock skew


def test_stability_scan_flags_old_tubes(net, caplog):
    from app.services import dx
    conn, c = net["c"], net["clinic"]
    _q(conn, "UPDATE dx_containers SET stability_room_hours = 4 WHERE id = %s", net["edta"])
    v = visit(net, ["cbc"]).json()
    _collect(net, v)
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("SELECT set_config('kriya.dx_tx', %s, true)", (c,))
    cur.execute("UPDATE dx_samples SET collected_at = now() - interval '3 hours 30 minutes' WHERE id = %s",
                (v["samples"][0]["id"],))
    cur.execute("COMMIT")
    with caplog.at_level("WARNING"):
        n = asyncio.run(dx.stability_scan())
    assert n >= 1
    assert any(f"barcode={v['samples'][0]['barcode']} status=amber" in r.message for r in caplog.records)
