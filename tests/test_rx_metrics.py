"""Consumption-based reorder and stock classification on a fixed ledger, real Postgres (P10; RX-75, A7).

Out-movements (sale, IPD dispense, ward issue) over 90 IST days: 140 / 40 / 20 units of three items (values 70/20/10
% → A / B / C), one item last used 120 days ago (N, not dead) and one 200 days ago with stock (dead). An adjustment is
not consumption. Recompute never touches rx_items; apply-reorder (RX_ADMIN) does, and the change is logged.
"""

import uuid
from datetime import date

import pytest

from app.services import rx_metrics
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move


def test_formulas():
    assert [rx_metrics.adu_x1000(u) for u in (0, 1, 20, 40, 90, 140)] == [0, 11, 222, 444, 1000, 1556]
    assert [rx_metrics.fsn(a) for a in (0, 1, 1000, 1001)] == ["N", "S", "S", "F"]
    assert rx_metrics.suggestions(1556, 5, 7, 15) == (19, 43)          # ceil(18.672), 19 + ceil(23.34)
    assert rx_metrics.suggestions(0, 3, 7, 15) == (0, 0)
    assert rx_metrics.abc({"a": 70, "b": 20, "c": 10, "z": 0}) == {"a": "A", "b": "B", "c": "C", "z": "C"}
    assert rx_metrics.abc({"a": 95, "b": 5}) == {"a": "A", "b": "C"}
    assert rx_metrics.abc({}) == {}


def _backdate(conn, clinic, item, days):
    _q(conn, "SET session_replication_role = replica; UPDATE rx_stock_movements SET created_at = now() - %s * interval "
             "'1 day' WHERE clinic_id = %s AND item_id = %s AND movement_type <> 'opening'; "
             "SET session_replication_role = origin", days, clinic, item)


@pytest.fixture
def ledger(api, monkeypatch):  # noqa: F811
    conn, c = api.conn, _clinic(api.conn)
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    monkeypatch.setattr("app.routers.rx.log_admin_action", lambda **kw: _noop())
    store = str(_q(conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    _q(conn, "UPDATE rx_stores SET can_receive_grn = true WHERE id = %s", store)
    gen = _generic(conn, c)
    items, batches = {}, {}
    for code in ("FASTA", "MIDB", "SLOWC", "OLDN", "DEAD"):
        items[code] = _item(conn, c, gen, code, code.title())
        batches[code] = _batch(conn, c, items[code], "B" + code)            # ₹3 landed per unit
        _move(conn, c, store, batches[code], items[code], 1000 if code != "DEAD" else 60, "opening")
    mv = lambda code, qty, kind, reason=None: _move(conn, c, store, batches[code], items[code], qty, kind,  # noqa: E731
                                                    reason=reason)
    mv("FASTA", -100, "sale_out")
    mv("FASTA", -40, "ipd_dispense_out")
    mv("MIDB", -40, "ward_issue_out")
    mv("SLOWC", -20, "sale_out")
    mv("SLOWC", -500, "adjust_out", "Breakage")                         # not consumption
    mv("OLDN", -5, "sale_out")
    _backdate(conn, c, items["OLDN"], 120)
    mv("DEAD", -10, "sale_out")
    _backdate(conn, c, items["DEAD"], 200)
    sup = str(_q(conn, "INSERT INTO rx_suppliers (clinic_id, name, lead_time_days) VALUES (%s, 'Fast Pharma', 5) "
                       "RETURNING id", c))
    grn = str(_q(conn, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, supplier_invoice_no, supplier_invoice_date) "
                       "VALUES (%s, %s, %s, 'F-1', CURRENT_DATE) RETURNING id", c, store, sup))
    _q(conn, "INSERT INTO rx_grn_lines (clinic_id, grn_id, line_no, item_id, batch_no, expiry_date, packs, rate_pack_paise, "
             "tax_rate_bps, mrp_pack_paise) VALUES (%s, %s, 1, %s, 'G1', CURRENT_DATE + 500, 1, 3000, 1200, 4500) "
             "RETURNING id", c, grn, items["FASTA"])
    _q(conn, "UPDATE rx_grns SET invoice_total_paise = taxable_paise + cgst_paise + sgst_paise WHERE id = %s", grn)
    _q(conn, "SELECT rx_post_grn(%s, %s, NULL, 'Ravi')", c, grn)
    users = {"mgr": api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER"),
             "pharma": api.U("pharma", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACIST")}
    return {"clinic": c, "store": store, "items": items, "users": users}


async def _noop():
    return None


def test_recompute_classifies_and_suggests_without_touching_items(api, ledger):  # noqa: F811
    d, conn, c = ledger, api.conn, ledger["clinic"]
    api.as_(d["users"]["pharma"])
    assert api.call("POST", "/admin/rx/metrics/recompute", c).status_code == 403
    api.as_(d["users"]["mgr"])
    r = api.call("POST", "/admin/rx/metrics/recompute", c)
    assert r.status_code == 200 and r.json() == {"computed_on": date.today().isoformat(), "rows": 5}
    got = {code: _q(conn, "SELECT json_build_array(adu_x1000, abc, fsn, suggested_reorder_units, suggested_max_units) "
                          "FROM rx_item_metrics WHERE clinic_id = %s AND item_id = %s", c, d["items"][code])
           for code in d["items"]}
    assert got == {"FASTA": [1556, "A", "F", 19, 43],    # lead 5 (last GRN supplier) + safety 7; review 15
                   "MIDB": [444, "B", "S", 5, 12],       # default lead 3
                   "SLOWC": [222, "C", "S", 3, 7],
                   "OLDN": [0, "C", "N", 0, 0],
                   "DEAD": [0, "C", "N", 0, 0]}
    assert _q(conn, "SELECT count(*) FROM rx_items WHERE clinic_id = %s AND reorder_level_units <> 0", c) == 0
    assert api.call("POST", "/admin/rx/metrics/recompute", c).json()["rows"] == 5       # replaced, not appended
    assert _q(conn, "SELECT count(*) FROM rx_item_metrics WHERE clinic_id = %s", c) == 5

    rows = api.call("GET", "/admin/rx/metrics", c).json()
    assert rows["computed_on"] == date.today().isoformat() and len(rows["rows"]) == 5
    a = api.call("GET", "/admin/rx/metrics", c, abc="A").json()["rows"]
    assert [x["item_code"] for x in a] == ["FASTA"] and a[0]["suggested_reorder_qty_units"] == 24
    assert a[0]["current_reorder_level_units"] == 0 and a[0]["on_hand_units"] == 1000 - 140 + 10   # + the GRN pack of 10
    assert [x["item_code"] for x in api.call("GET", "/admin/rx/metrics", c, fsn="N").json()["rows"]] == ["DEAD", "OLDN"]
    dead = api.call("GET", "/admin/rx/metrics", c, dead_stock="true").json()
    assert [x["item_code"] for x in dead["rows"]] == ["DEAD"] and dead["dead_stock_value_paise"] == 50 * 300
    api.as_(d["users"]["pharma"])                                            # RX_READ may read the analytics
    assert api.call("GET", "/admin/rx/metrics", c).status_code == 200


def test_apply_reorder_is_admin_only_and_logged(api, ledger):  # noqa: F811
    d, conn, c = ledger, api.conn, ledger["clinic"]
    body = {"items": [{"item_id": d["items"]["FASTA"], "reorder_level_units": 19, "reorder_qty_units": 24}]}
    api.as_(d["users"]["pharma"])
    assert api.call("POST", "/admin/rx/metrics/apply-reorder", c, body).status_code == 403
    api.as_(d["users"]["mgr"])
    dup = {"items": body["items"] * 2}
    assert api.call("POST", "/admin/rx/metrics/apply-reorder", c, dup).status_code == 422
    r = api.call("POST", "/admin/rx/metrics/apply-reorder", c, body)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["reorder_level_units"] == 19
    assert _q(conn, "SELECT json_build_array(reorder_level_units, reorder_qty_units) FROM rx_items WHERE id = %s",
              d["items"]["FASTA"]) == [19, 24]
    log = _q(conn, "SELECT json_agg(json_build_array(field, old_value, new_value, reason) ORDER BY field) "
                   "FROM rx_item_changes WHERE item_id = %s", d["items"]["FASTA"])
    assert log == [["reorder_level_units", "0", "19", "A7 reorder suggestion applied"],
                   ["reorder_qty_units", "0", "24", "A7 reorder suggestion applied"]]


def test_nightly_job_is_registered_and_skips_off_clinics(api, ledger):  # noqa: F811
    import asyncio
    from app.services.scheduler import SchedulerService
    assert "rx_compute_metrics" in SchedulerService.__dict__
    off = _clinic(api.conn)
    asyncio.run(rx_metrics.run_nightly())
    assert _q(api.conn, "SELECT count(*) FROM rx_item_metrics WHERE clinic_id = %s", ledger["clinic"]) == 5
    assert _q(api.conn, "SELECT count(*) FROM rx_item_metrics WHERE clinic_id = %s", off) == 0
