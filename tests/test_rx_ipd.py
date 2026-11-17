"""Inpatient pharmacy through the real /admin/rx/ipd routes on real Postgres (W4, W5b; ADR-RX-04).

A nurse raises a ward indent against the doctor's active order, the pharmacy dispenses FEFO with the
IPD charge written in the same transaction, the nurse receives it, a dose on the UNCHANGED MAR route is
linked to the dispensed batch, and unused units come back (charge voided and re-issued for what was kept).
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import rx_ipd
from tests.test_ipd_api_e2e import _admit, _clinic, _code, _q, _setup_hospital, api  # noqa: F401  (fixture)
from tests.test_rx_catalogue_api import ROWS, csv_bytes, upload

OPEN = "item_code,batch_no,expiry_mm_yyyy,mrp_per_pack,purchase_rate_per_pack,gst_rate_percent,qty_units\n"


def _mm(days):
    d = date.today() + timedelta(days=days)
    return f"{d.month:02d}/{d.year}"


@pytest.fixture
def ward(api):  # noqa: F811
    """IPD + Pharmacy clinic: one admitted patient with an active oral order, stock in two batches."""
    c = _clinic(api.conn)
    doctor, u, beds = _setup_hospital(api, c)
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    api.as_(u["admin"])
    assert upload(api, "/admin/rx/items/import/apply", c, csv_bytes(ROWS)).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    stock = OPEN + f"DOLO650,DNEAR,{_mm(200)},30.00,20.00,12,40\nDOLO650,DLATE,{_mm(500)},30.00,20.00,12,100\n"
    assert upload(api, "/admin/rx/stock/opening-import", c, stock.encode(), "o.csv", store_id=store,
                  dry_run="false").status_code == 200
    aid = _admit(api, c, u, doctor, beds["101"]).json()["id"]
    api.as_(u["doctor"])
    order = api.call("POST", f"/admin/ipd/admissions/{aid}/orders", c, {
        "order_type": "medication", "description": "Tab Dolo 650 TDS", "drug_name": "Dolo 650", "dose": "1 tab",
        "route": "oral", "frequency": "TDS", "idempotency_key": uuid.uuid4().hex}).json()
    pharmacist = api.U("pharma", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACIST")
    item = str(_q(api.conn, "SELECT id FROM rx_items WHERE clinic_id = %s AND item_code = 'DOLO650'", c))
    return {"clinic": c, "u": u, "doctor": doctor, "beds": beds, "admission": aid, "order": order["id"], "store": store,
            "item": item, "pharmacist": pharmacist}


def _raise(api, w, units=3, priority="routine", key=None):  # noqa: F811
    api.as_(w["u"]["nurse"])
    return api.call("POST", "/admin/rx/ipd/indents", w["clinic"], {
        "admission_id": w["admission"], "from_store_id": w["store"], "priority": priority,
        "lines": [{"ipd_order_id": w["order"], "requested_units": units, "item_id": w["item"]}],
        "idempotency_key": key or uuid.uuid4().hex})


def _dispense(api, w, indent, key=None, **line):  # noqa: F811
    api.as_(w["pharmacist"])
    return api.call("POST", f"/admin/rx/ipd/indents/{indent['id']}/dispense", w["clinic"], {
        "store_id": w["store"], "lines": [{"indent_line_id": indent["lines"][0]["id"], **line}],
        "idempotency_key": key or uuid.uuid4().hex})


def _pharmacy_charges(api, w):  # noqa: F811
    return _q(api.conn, "SELECT COALESCE(json_agg(json_build_array(quantity, unit_price_paise, status) ORDER BY created_at), "
                        "'[]') FROM ipd_charges WHERE admission_id = %s AND source = 'pharmacy' AND status <> 'void'",
              w["admission"])


def test_nurse_raises_pharmacy_dispenses_fefo_and_bills_once(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    api.as_(w["u"]["nurse"])
    got = api.call("GET", f"/admin/rx/ipd/admissions/{w['admission']}/indentable", c)
    assert got.status_code == 200, got.text
    o = got.json()["orders"][0]
    assert o["id"] == w["order"] and o["default_units"] == 3                # 1 tab TDS for 24 h
    assert w["item"] in [x["item_id"] for x in o["candidates"]]
    assert [s["id"] for s in got.json()["stores"]] == [w["store"]]
    key = uuid.uuid4().hex
    r = _raise(api, w, priority="stat", key=key)
    assert r.status_code == 201, r.text
    indent = r.json()
    assert indent["indent_number"] == f"IND-{date.today().year}-00001" and indent["priority"] == "stat"
    assert _raise(api, w, key=key).json()["id"] == indent["id"]            # replay
    # pharmacy queue: STAT first, FEFO suggests the near-expiry batch
    api.as_(w["pharmacist"])
    _raise(api, w, priority="routine")
    api.as_(w["pharmacist"])
    queue = api.call("GET", "/admin/rx/ipd/indents", c).json()["indents"]
    assert [q["priority"] for q in queue] == ["stat", "routine"] and queue[0]["bed"] == "101"
    detail = api.call("GET", f"/admin/rx/ipd/indents/{indent['id']}", c).json()
    assert detail["lines"][0]["fefo"]["picks"][0]["batch_no"] == "DNEAR"
    dkey = uuid.uuid4().hex
    first = _dispense(api, w, indent, key=dkey)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "dispensed" and body["final_bill_exists"] is False
    for _ in range(2):
        assert _dispense(api, w, indent, key=dkey).json()["replayed"] is True
    assert _pharmacy_charges(api, w) == [[3, 200, "posted"]]                 # ₹30 / strip of 15 = ₹2 a tab
    assert _q(api.conn, "SELECT b.batch_no FROM rx_dispense_lines dl JOIN rx_batches b ON b.id = dl.batch_id "
                        "WHERE dl.dispense_id = %s", body["dispense_id"]) == "DNEAR"
    slip = api.client.get(body["slip_url"], params={"clinic_id": c})
    assert slip.status_code == 200 and slip.content[:4] == b"%PDF"
    # the ward acknowledges: closed
    api.as_(w["u"]["nurse"])
    rec = api.call("POST", f"/admin/rx/ipd/dispenses/{body['dispense_id']}/receive", c)
    assert rec.status_code == 200 and rec.json()["indent_status"] == "closed"


def test_mar_dose_on_unchanged_route_links_to_the_batch(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    indent = _raise(api, w).json()
    d = _dispense(api, w, indent).json()["dispense_id"]
    dl = str(_q(api.conn, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    api.as_(w["u"]["nurse"])
    mar = api.call("POST", f"/admin/ipd/admissions/{w['admission']}/administrations", c, {
        "order_id": w["order"], "administered_at": datetime.now(timezone.utc).isoformat(), "outcome": "given",
        "dose_given": "1 tab", "idempotency_key": uuid.uuid4().hex})
    assert mar.status_code == 201, mar.text
    linked = asyncio.run(rx_ipd.link_mar(c, mar.json()["id"], dl, None))
    assert linked["rx_link"] == "linked"
    assert _q(api.conn, "SELECT b.batch_no FROM rx_mar_links m JOIN rx_dispense_lines dl ON dl.id = m.dispense_line_id "
                        "JOIN rx_batches b ON b.id = dl.batch_id WHERE m.mar_id = %s", mar.json()["id"]) == "DNEAR"
    # a wrong line never fails the dose record: the MAR row stays, the link is refused
    api.as_(w["u"]["doctor"])
    other = api.call("POST", f"/admin/ipd/admissions/{w['admission']}/orders", c, {
        "order_type": "medication", "description": "Inj Pan 40", "drug_name": "Pantoprazole", "dose": "40 mg",
        "route": "iv", "frequency": "OD", "idempotency_key": uuid.uuid4().hex}).json()
    api.as_(w["u"]["nurse"])
    mar2 = api.call("POST", f"/admin/ipd/admissions/{w['admission']}/administrations", c, {
        "order_id": other["id"], "administered_at": datetime.now(timezone.utc).isoformat(), "outcome": "given",
        "idempotency_key": uuid.uuid4().hex})
    assert mar2.status_code == 201
    assert asyncio.run(rx_ipd.link_mar(c, mar2.json()["id"], dl, None)) == {"rx_link": "refused: rx_mar_line_mismatch"}
    assert _q(api.conn, "SELECT count(*) FROM ipd_med_administrations WHERE id = %s", mar2.json()["id"]) == 1


def test_ward_return_voids_and_reissues_the_charge(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    indent = _raise(api, w, units=10).json()
    d = _dispense(api, w, indent).json()["dispense_id"]
    dl = str(_q(api.conn, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    assert _pharmacy_charges(api, w) == [[10, 200, "posted"]]
    api.as_(w["u"]["nurse"])
    api.call("POST", f"/admin/rx/ipd/dispenses/{d}/receive", c)
    key = uuid.uuid4().hex
    body = {"lines": [{"dispense_line_id": dl, "qty_units": 4, "restock": True}], "reason": "Switched to IV",
            "idempotency_key": key}
    r = api.call("POST", f"/admin/rx/ipd/dispenses/{d}/returns", c, body)
    assert r.status_code == 201, r.text
    assert r.json()["value_paise"] == 800 and r.json()["credited_paise"] == 0
    assert api.call("POST", f"/admin/rx/ipd/dispenses/{d}/returns", c, body).json()["replayed"] is True
    assert _pharmacy_charges(api, w) == [[6, 200, "posted"]]                  # bill drops by 4 × ₹2
    assert _q(api.conn, "SELECT count(*) FROM ipd_charges WHERE admission_id = %s AND status = 'void'", w["admission"]) == 1
    over = api.call("POST", f"/admin/rx/ipd/dispenses/{d}/returns", c,
                    {"lines": [{"dispense_line_id": dl, "qty_units": 7}], "reason": "Too many"})
    assert over.status_code == 409 and _code(over) == "rx_return_exceeds_dispensed"


def test_stopped_order_and_permissions(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    api.as_(w["pharmacist"])
    assert api.call("POST", "/admin/rx/ipd/indents", c, {
        "admission_id": w["admission"], "from_store_id": w["store"],
        "lines": [{"ipd_order_id": w["order"], "requested_units": 1}]}).status_code == 403     # nurses raise
    indent = _raise(api, w).json()
    api.as_(w["u"]["nurse"])
    assert api.call("POST", f"/admin/rx/ipd/indents/{indent['id']}/dispense", c, {
        "store_id": w["store"], "lines": [{"indent_line_id": indent["lines"][0]["id"]}]}).status_code == 403
    api.as_(w["u"]["billing"])
    assert api.call("GET", "/admin/rx/ipd/indents", c).status_code == 403
    api.as_(w["u"]["doctor"])
    assert api.call("POST", f"/admin/ipd/orders/{w['order']}/end", c,
                    {"status": "discontinued", "reason": "Fever settled"}).status_code == 200
    r = _dispense(api, w, indent)
    assert r.status_code == 409 and _code(r) == "rx_order_not_active"
    assert _pharmacy_charges(api, w) == []
    api.as_(w["u"]["nurse"])
    cancel = api.call("POST", f"/admin/rx/ipd/indents/{indent['id']}/cancel", c, {"reason": "Order stopped"})
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"


def test_ipd_wind_down_still_dispenses_and_ipd_off_is_refused(api, ward):  # noqa: F811
    w, c = ward, ward["clinic"]
    indent = _raise(api, w).json()
    assert api.owner_toggle(c, "ipd_enabled", False, confirm=True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    assert _q(api.conn, "SELECT ipd_state FROM clinics WHERE id = %s", c) == "WIND_DOWN"
    assert _dispense(api, w, indent).status_code == 200                       # admitted patients keep getting medicines
    other = _clinic(api.conn)
    assert api.owner_toggle(other, "pharmacy_enabled", True).status_code == 200
    invalidate_tenant_cache()
    api.as_(api.U("admin", "clinic_admin", other, str(uuid.uuid4())))
    r = api.call("GET", "/admin/rx/ipd/indents", other)
    assert r.status_code == 403 and "IPD" in r.text
