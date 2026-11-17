"""Pharmacy OS stock through the real /admin/rx routes on real Postgres (RX-18…RX-20).

Opening stock (dry run, landed cost with / without GSTIN, expired refused, idempotent,
closed after trading); stock / batches / FEFO / expiry / reorder reads; quarantine;
write-offs (replay, insufficient stock, expired allowed); store scope for a branch
login; a store holding stock cannot be deactivated. Every movement here is written by
an rx_* SQL function; the test never touches the ledger.
"""

import uuid
from datetime import date, timedelta

import pytest

from app.services.rx_stock import packs_loose, parse_expiry
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_rx_catalogue_api import ROWS, _code, csv_bytes, upload

OPEN_HDR = "item_code,batch_no,expiry_mm_yyyy,mrp_per_pack,purchase_rate_per_pack,gst_rate_percent,qty_units,packs,loose"


def _mmyyyy(days):
    d = date.today() + timedelta(days=days)
    return f"{d.month:02d}/{d.year}"


@pytest.fixture
def shop(api):  # noqa: F811
    clinic = _clinic(api.conn)
    assert api.owner_toggle(clinic, "pharmacy_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, str(uuid.uuid4())))
    assert upload(api, "/admin/rx/items/import/apply", clinic, csv_bytes(ROWS)).status_code == 200
    store = str(_q(api.conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", clinic))
    return {"clinic": clinic, "store": store}


def _open(api, shop, lines, dry_run=False):  # noqa: F811
    body = ("\n".join([OPEN_HDR, *lines]) + "\n").encode()
    return upload(api, "/admin/rx/stock/opening-import", shop["clinic"], body, "open.csv", store_id=shop["store"],
                  dry_run=str(dry_run).lower())


LOAD = [f"DOLO650,D-NEAR,{_mmyyyy(0)},30.50,21.00,12,30,,",
        f"DOLO650,D-LATE,{_mmyyyy(400)},31.00,21.00,12,,4,5",          # 4 strips of 15 + 5 loose = 65
        f"AUG625,A1,{_mmyyyy(300)},204.00,150.00,12,10,,"]


def test_pure_helpers():
    assert parse_expiry("03/2027") == date(2027, 3, 31) and parse_expiry("2/28") == date(2028, 2, 29)
    assert parse_expiry("13/2027") is None and parse_expiry("2027-03") is None
    assert packs_loose(65, 15) == {"packs": 4, "loose": 5} and packs_loose(7, 1) == {"packs": 7, "loose": 0}


def test_opening_dry_run_apply_idempotent_and_reads(api, shop):  # noqa: F811
    dry = _open(api, shop, LOAD, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert (dry.json()["valid"], dry.json()["units"], dry.json()["applied"]) == (True, 105, False)
    assert _q(api.conn, "SELECT count(*) FROM rx_batches WHERE clinic_id = %s", shop["clinic"]) == 0
    done = _open(api, shop, LOAD).json()
    assert (done["movements"], done["units"], done["new_batches"]) == (3, 105, 3)
    assert _open(api, shop, LOAD).json()["already_loaded"] == 3                 # re-upload writes nothing
    # no GSTIN on this clinic: landed cost includes GST → 21.00 × 1.12 / 15 = 156.8 paise per tablet
    assert _q(api.conn, "SELECT landed_unit_cost_paise_x100 FROM rx_batches WHERE batch_no = 'D-LATE' AND clinic_id = %s",
              shop["clinic"]) == 15680
    stock = {r["item"]["item_code"]: r for r in api.call("GET", "/admin/rx/stock", shop["clinic"]).json()["stock"]}
    dolo = stock["DOLO650"]
    assert (dolo["qty_units"], dolo["packs_loose"], dolo["nearest_expiry"]) == (95, {"packs": 6, "loose": 5},
                                                                              str(parse_expiry(_mmyyyy(0))))
    assert dolo["value_paise"] > 0                                                # admin sees valuation
    b = api.call("GET", "/admin/rx/batches", shop["clinic"], item_id=dolo["item"]["id"], store_id=shop["store"],
                 required_qty=40).json()
    assert [(x["batch_no"], x["expiry_status"], x["fefo_rank"]) for x in b["batches"]] == [("D-NEAR", "short_shelf", 1),
                                                                                           ("D-LATE", "ok", 2)]
    assert "landed_unit_cost_paise_x100" not in b["batches"][0]
    assert [(p["batch_no"], p["take_units"]) for p in b["fefo"]["picks"]] == [("D-NEAR", 30), ("D-LATE", 10)]
    assert b["fefo"]["shortfall_units"] == 0
    exp = api.call("GET", "/admin/rx/expiry", shop["clinic"], days=30).json()
    assert [x["batch_no"] for x in exp["buckets"]["le_30"]] == ["D-NEAR"] and exp["totals"]["expired"]["batches"] == 0
    assert api.call("GET", "/admin/rx/expiry", shop["clinic"], days=45).status_code == 422
    re = {r["item"]["item_code"]: r for r in api.call("GET", "/admin/rx/reorder", shop["clinic"]).json()["reorder"]}
    assert set(re) == {"AUG625"} and re["AUG625"]["suggested_packs"] == 10       # 10 < 20; 100 units / 10
    low = api.call("GET", "/admin/rx/stock", shop["clinic"], below_reorder="true").json()["stock"]
    assert [r["item"]["item_code"] for r in low] == ["AUG625"]
    # items list shows sellable stock
    items = {i["item_code"]: i["stock_units"] for i in api.call("GET", "/admin/rx/items", shop["clinic"]).json()["items"]}
    assert items == {"AUG625": 10, "CALPOL650": 0, "DOLO650": 95}


def test_opening_errors_refuse_the_file_and_close_after_trading(api, shop):  # noqa: F811
    bad = [LOAD[0], f"DOLO650,OLD,{_mmyyyy(-40)},30,20,12,10,,", "NOPE,X1,12/2030,10,5,12,1,,",
           f"DOLO650,D-NEAR,{_mmyyyy(0)},30.50,21.00,12,5,,", f"AUG625,A2,{_mmyyyy(300)},abc,1,7,0,,"]
    dry = _open(api, shop, bad, dry_run=True).json()
    fields = {(e["row"], e["field"]) for e in dry["errors"]}
    assert {(3, "expiry_mm_yyyy"), (4, "item_code"), (5, "batch_no"), (6, "mrp_per_pack"), (6, "gst_rate_percent"),
            (6, "qty_units")} <= fields
    r = _open(api, shop, bad)
    assert r.status_code == 422 and _code(r) == "rx_import_invalid"
    assert _q(api.conn, "SELECT count(*) FROM rx_stock_movements WHERE clinic_id = %s", shop["clinic"]) == 0
    _open(api, shop, LOAD)
    batch = str(_q(api.conn, "SELECT id FROM rx_batches WHERE batch_no = 'D-NEAR' AND clinic_id = %s", shop["clinic"]))
    w = api.call("POST", "/admin/rx/write-offs", shop["clinic"], {"store_id": shop["store"], "lines": [
        {"batch_id": batch, "qty_units": 2, "kind": "damage", "reason": "Crushed strip"}]})
    assert w.status_code == 201, w.text
    closed = _open(api, shop, [f"CALPOL650,C9,{_mmyyyy(300)},33,22,12,10,,"], dry_run=True)
    assert closed.status_code == 409 and _code(closed) == "rx_opening_closed"


def test_writeoff_block_and_store_rules(api, shop):  # noqa: F811
    _open(api, shop, LOAD)
    clinic, store = shop["clinic"], shop["store"]
    near = str(_q(api.conn, "SELECT id FROM rx_batches WHERE batch_no = 'D-NEAR' AND clinic_id = %s", clinic))
    late = str(_q(api.conn, "SELECT id FROM rx_batches WHERE batch_no = 'D-LATE' AND clinic_id = %s", clinic))
    body = {"store_id": store, "idempotency_key": "wo-2026-11-17-01",
            "lines": [{"batch_id": near, "qty_units": 5, "kind": "expiry", "reason": "Short expiry, destroyed"}]}
    first = api.call("POST", "/admin/rx/write-offs", clinic, body).json()
    replay = api.call("POST", "/admin/rx/write-offs", clinic, body).json()
    assert (first["replayed"], replay["replayed"], first["ref_id"]) == (False, True, replay["ref_id"])
    too_many = api.call("POST", "/admin/rx/write-offs", clinic, {**body, "idempotency_key": None, "lines": [
        {"batch_id": near, "qty_units": 26, "kind": "damage", "reason": "Flood"}]})
    assert too_many.status_code == 409 and _code(too_many) == "rx_insufficient_stock"
    dup = {**body, "idempotency_key": None, "lines": body["lines"] * 2}
    assert api.call("POST", "/admin/rx/write-offs", clinic, dup).status_code == 422
    assert api.call("POST", "/admin/rx/write-offs", clinic, {**body, "lines": [{**body["lines"][0], "reason": ""}]}
                    ).status_code == 422
    assert _q(api.conn, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", near) == 25
    # quarantine
    assert api.call("POST", f"/admin/rx/batches/{late}/block", clinic, {"reason": "x"}).status_code == 422
    assert api.call("POST", f"/admin/rx/batches/{late}/block", clinic, {"reason": "CDSCO NSQ alert"}).json()["is_blocked"]
    item = str(_q(api.conn, "SELECT item_id FROM rx_batches WHERE id = %s", late))
    statuses = {x["batch_no"]: x["expiry_status"] for x in
                api.call("GET", "/admin/rx/batches", clinic, item_id=item).json()["batches"]}
    assert statuses["D-LATE"] == "blocked"
    fefo = api.call("GET", "/admin/rx/batches", clinic, item_id=item, store_id=store, required_qty=100).json()["fefo"]
    assert [p["batch_no"] for p in fefo["picks"]] == ["D-NEAR"] and fefo["shortfall_units"] == 75
    assert api.call("POST", f"/admin/rx/batches/{late}/unblock", clinic, {"reason": "Cleared by lab"}).json()[
        "is_blocked"] is False
    # a store with stock cannot be deactivated
    r = api.call("PATCH", f"/admin/rx/stores/{store}", clinic, {"is_active": False})
    assert r.status_code == 409 and "stock" in r.text
    # another tenant's batch / store
    other = _clinic(api.conn)
    api.owner_toggle(other, "pharmacy_enabled", True)
    api.as_(api.U("admin2", "clinic_admin", other, str(uuid.uuid4())))
    assert api.call("POST", f"/admin/rx/batches/{late}/block", other, {"reason": "Leak test"}).status_code == 404
    assert api.call("POST", "/admin/rx/write-offs", other, body).status_code == 404
    assert api.call("GET", "/admin/rx/stock", other).json()["stock"] == []


def test_branch_login_sees_only_its_branch_stores(api, shop):  # noqa: F811
    clinic = shop["clinic"]
    _open(api, shop, LOAD)
    branch = str(_q(api.conn, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'Kukatpally') RETURNING id", clinic))
    keeper = api.U("keeper", "staff", clinic, str(uuid.uuid4()), [], staff_role="STORE_KEEPER", branch_id=branch)
    api.as_(keeper)
    assert api.call("GET", "/admin/rx/stock", clinic).json()["stock"] == []          # MAIN is clinic-wide, not theirs
    assert api.call("GET", "/admin/rx/stock", clinic, store_id=shop["store"]).status_code == 404
    assert api.call("GET", "/admin/rx/expiry", clinic, store_id=shop["store"]).status_code == 404
    late = str(_q(api.conn, "SELECT id FROM rx_batches WHERE batch_no = 'D-LATE' AND clinic_id = %s", clinic))
    assert api.call("POST", f"/admin/rx/batches/{late}/block", clinic, {"reason": "Not mine"}).status_code == 404
    stock = api.call("GET", "/admin/rx/stock", clinic).json()
    assert "value_paise" not in str(stock)                                          # no cost without RX_PURCHASE/ADMIN
