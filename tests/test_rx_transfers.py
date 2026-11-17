"""Store transfers and ward par levels through the real /admin/rx routes on real Postgres (W7, A10, RX-42/43/78).

Central (MAIN) → ward sub-stores in two branches. The store keeper drafts (FEFO batches, rack-sorted pick list)
and dispatches; the ward nurse of that branch receives, short units are written off at the source; the other
branch's nurse sees nothing (404); controlled lines need two different managers; par levels draft (never
dispatch) a replenishment of the positive deficits only.
"""

import uuid
from datetime import date, timedelta

import pytest

from tests.test_ipd_api_e2e import _clinic, _code, _q, _setup_hospital, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_rx_catalogue_api import ROWS, csv_bytes, upload

OPEN = "item_code,batch_no,expiry_mm_yyyy,mrp_per_pack,purchase_rate_per_pack,gst_rate_percent,qty_units\n"


def _mm(days):
    d = date.today() + timedelta(days=days)
    return f"{d.month:02d}/{d.year}"


@pytest.fixture
def hosp(api):  # noqa: F811
    c = _clinic(api.conn)
    _, u, _ = _setup_hospital(api, c)
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    api.as_(u["admin"])
    assert upload(api, "/admin/rx/items/import/apply", c, csv_bytes(ROWS)).status_code == 200
    main = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s AND code = 'MAIN'", c))
    stock = OPEN + "\n".join([f"DOLO650,DNEAR,{_mm(200)},30.00,20.00,12,40", f"DOLO650,DLATE,{_mm(500)},30.00,20.00,12,100",
                              f"CALPOL650,CP1,{_mm(400)},33.00,22.00,12,60", f"AUG625,AG1,{_mm(300)},200.00,150.00,12,40"]) + "\n"
    assert upload(api, "/admin/rx/stock/opening-import", c, stock.encode(), "o.csv", store_id=main,
                  dry_run="false").status_code == 200
    ward = str(_q(api.conn, "SELECT id FROM ipd_wards WHERE clinic_id = %s LIMIT 1", c))
    br = {k: str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, %s) RETURNING id", c, k)) for k in "AB"}
    wards = {k: str(_q(api.conn, "INSERT INTO rx_stores (clinic_id, branch_id, parent_store_id, code, name, store_type, ward_id) "
                                 "VALUES (%s, %s, %s, %s, %s, 'ward_substore', %s) RETURNING id",
                       c, br[k], main, "W" + k, "Ward " + k, ward)) for k in "AB"}
    items = dict(_q(api.conn, "SELECT json_agg(json_build_array(item_code, id)) FROM rx_items WHERE clinic_id = %s", c))
    U, uid = api.U, lambda: str(uuid.uuid4())
    users = {"keeper": U("keeper", "staff", c, uid(), [], staff_role="STORE_KEEPER"),
             "mgr1": U("mgr1", "staff", c, uid(), [], staff_role="PHARMACY_MANAGER"),
             "mgr2": U("mgr2", "staff", c, uid(), [], staff_role="PHARMACY_MANAGER"),
             "nurse_a": U("nurse_a", "staff", c, uid(), ["IPD_NURSING"], staff_role="NURSE", branch_id=br["A"]),
             "nurse_b": U("nurse_b", "staff", c, uid(), ["IPD_NURSING"], staff_role="NURSE", branch_id=br["B"]),
             "cashier": U("cashier", "staff", c, uid(), ["OPD_BILLING"], staff_role="CASHIER")}
    return {"clinic": c, "main": main, "wards": wards, "items": items, "u": users}


def _draft(api, h, to, lines, who="keeper"):  # noqa: F811
    api.as_(h["u"][who])
    return api.call("POST", "/admin/rx/transfers", h["clinic"], {"from_store_id": h["main"], "to_store_id": to,
                                                                 "lines": lines})


def _bal(api, store, item):  # noqa: F811
    return _q(api.conn, "SELECT COALESCE(SUM(qty_units), 0) FROM rx_stock_balances WHERE store_id = %s AND item_id = %s",
              store, item)


def test_transfer_lifecycle_fefo_pick_list_challan_and_short_receipt(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    dolo, aug, calpol = h["items"]["DOLO650"], h["items"]["AUG625"], h["items"]["CALPOL650"]
    r = _draft(api, h, h["wards"]["A"], [{"item_id": calpol, "units": 5}, {"item_id": aug, "units": 4},
                                         {"item_id": dolo, "units": 50}])
    assert r.status_code == 201, r.text
    t = r.json()
    # FEFO: 40 from the near batch, 10 from the later one; pick list by rack (A-1, B-2, then unracked)
    assert [(ln["item"]["item_code"], ln["batch"]["batch_no"], ln["sent_units"]) for ln in t["lines"]] == [
        ("DOLO650", "DNEAR", 40), ("DOLO650", "DLATE", 10), ("AUG625", "AG1", 4), ("CALPOL650", "CP1", 5)]
    d = api.call("POST", f"/admin/rx/transfers/{t['id']}/dispatch", c)
    assert d.status_code == 200, d.text
    assert d.json()["transfer_number"] == f"STR-{date.today().year}-00001" and d.json()["status"] == "dispatched"
    assert _bal(api, h["main"], dolo) == 90
    pdf = api.client.get(d.json()["challan_url"], params={"clinic_id": c})
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # the branch-A ward nurse receives, one line short
    api.as_(h["u"]["nurse_a"])
    inc = api.call("GET", "/admin/rx/transfers", c, status="dispatched").json()
    assert [x["id"] for x in inc["transfers"]] == [t["id"]] and {s["code"] for s in inc["stores"]} == {"WA"}
    lines = d.json()["lines"]
    body = {"lines": [{"line_id": ln["id"], "received_units": ln["sent_units"] - (1 if ln["item"]["item_code"] == "AUG625" else 0),
                       **({"discrepancy_reason": "Strip torn"} if ln["item"]["item_code"] == "AUG625" else {})} for ln in lines]}
    rec = api.call("POST", f"/admin/rx/transfers/{t['id']}/receive", c, body)
    assert rec.status_code == 200, rec.text
    assert rec.json()["status"] == "partially_received"
    assert (_bal(api, h["wards"]["A"], dolo), _bal(api, h["wards"]["A"], aug), _bal(api, h["main"], aug)) == (50, 3, 36)
    assert _q(api.conn, "SELECT count(*) FROM rx_stock_movements WHERE store_id = %s AND movement_type = 'damage_writeoff_out' "
                        "AND reason LIKE '%%Strip torn%%'", h["main"]) == 1
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/receive", c, body).json()["replayed"] is True


def test_ward_store_scope_and_roles(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    t = _draft(api, h, h["wards"]["A"], [{"item_id": h["items"]["DOLO650"], "units": 5}]).json()
    api.call("POST", f"/admin/rx/transfers/{t['id']}/dispatch", c)
    body = {"lines": [{"line_id": t["lines"][0]["id"], "received_units": 5}]}
    api.as_(h["u"]["nurse_b"])                                       # the other branch's ward: not found
    assert api.call("GET", f"/admin/rx/transfers/{t['id']}", c).status_code == 404
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/receive", c, body).status_code == 404
    assert api.call("GET", "/admin/rx/transfers", c).json()["transfers"] == []
    assert api.call("GET", "/admin/rx/par-levels", c, store_id=h["wards"]["A"]).status_code == 404
    api.as_(h["u"]["nurse_a"])                                       # a nurse receives; never drafts or dispatches
    assert _draft(api, h, h["wards"]["A"], [{"item_id": h["items"]["DOLO650"], "units": 1}], "nurse_a").status_code == 403
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/dispatch", c).status_code == 403
    assert api.call("GET", "/admin/rx/par-levels", c, store_id=h["main"]).status_code == 404   # not a ward store
    api.as_(h["u"]["cashier"])
    assert api.call("GET", "/admin/rx/transfers", c).status_code == 403
    api.as_(h["u"]["keeper"])
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/cancel", c, {"reason": "Too late"}).status_code == 409


def test_controlled_transfer_two_person(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    gen = _q(api.conn, "SELECT generic_id FROM rx_items WHERE id = %s", h["items"]["DOLO650"])
    narc = str(_q(api.conn, "INSERT INTO rx_items (clinic_id, item_code, brand_name, generic_id, schedule, base_unit, pack_size, "
                            "hsn_code, tax_rate_bps, allow_loose) VALUES (%s, 'MORPH10', 'Morphine 10', %s, 'narcotic', "
                            "'tablet', 10, '30049099', 1200, true) RETURNING id", h["clinic"], gen))
    b = _batch(api.conn, h["clinic"], narc, "M1")
    _move(api.conn, h["clinic"], h["main"], b, narc, 20, "opening")
    t = _draft(api, h, h["wards"]["A"], [{"item_id": narc, "units": 5}]).json()
    assert t["controlled"] is True
    r = api.call("POST", f"/admin/rx/transfers/{t['id']}/dispatch", c)      # the keeper holds no RX_CONTROLLED
    assert r.status_code == 403 and _code(r) == "rx_controlled_permission"
    api.as_(h["u"]["mgr1"])
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/dispatch", c).status_code == 200
    body = {"lines": [{"line_id": t["lines"][0]["id"], "received_units": 5}]}
    same = api.call("POST", f"/admin/rx/transfers/{t['id']}/receive", c, body)
    assert same.status_code == 409 and _code(same) == "rx_controlled_two_person"
    api.as_(h["u"]["mgr2"])
    assert api.call("POST", f"/admin/rx/transfers/{t['id']}/receive", c, body).json()["status"] == "received"


def test_par_level_draft_only_positive_and_pick_list_rack_order(api, hosp):  # noqa: F811
    h, c = hosp, hosp["clinic"]
    dolo, aug, calpol = h["items"]["DOLO650"], h["items"]["AUG625"], h["items"]["CALPOL650"]
    wa = h["wards"]["A"]
    api.as_(h["u"]["nurse_a"])                                       # the ward sets its own pars
    r = api.call("POST", "/admin/rx/par-levels", c, {"store_id": wa, "rows": [
        {"item_id": calpol, "par_units": 6}, {"item_id": dolo, "par_units": 30}, {"item_id": aug, "par_units": 2}]})
    assert r.status_code == 200, r.text
    _draft(api, h, wa, [{"item_id": aug, "units": 5}])               # the ward already holds 5 Augmentin (> par 2)
    t0 = api.call("GET", "/admin/rx/transfers", c).json()["transfers"][0]
    api.call("POST", f"/admin/rx/transfers/{t0['id']}/dispatch", c)
    det = api.call("GET", f"/admin/rx/transfers/{t0['id']}", c).json()
    api.as_(h["u"]["nurse_a"])
    api.call("POST", f"/admin/rx/transfers/{t0['id']}/receive", c,
             {"lines": [{"line_id": det["lines"][0]["id"], "received_units": 5}]})
    par = api.call("GET", "/admin/rx/par-levels", c, store_id=wa).json()["par_levels"]
    assert [(p["item"]["item_code"], p["deficit_units"]) for p in par] == [("DOLO650", 30), ("AUG625", 0), ("CALPOL650", 6)]
    assert api.call("POST", "/admin/rx/par-levels/replenish", c, {"store_id": wa}).status_code == 403   # keeper's job
    api.as_(h["u"]["keeper"])
    out = api.call("POST", "/admin/rx/par-levels/replenish", c, {"store_id": wa}).json()
    t = out["transfer"]
    assert t["status"] == "draft" and t["source"] == "par" and t["from_store_id"] == h["main"]
    assert [(ln["item"]["item_code"], ln["sent_units"]) for ln in t["lines"]] == [("DOLO650", 30), ("CALPOL650", 6)]
    again = api.call("POST", "/admin/rx/par-levels/replenish", c, {"store_id": wa}).json()["transfer"]
    assert again["id"] == t["id"]                                    # refreshed, never doubled
    assert _q(api.conn, "SELECT count(*) FROM rx_transfers WHERE clinic_id = %s AND source = 'par'", c) == 1
