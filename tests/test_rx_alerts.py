"""Daily alerts, nightly integrity checks, STAT indent alerts and the expiry-return planner on real Postgres
(RX-56, RX-76 / A8).

The scan writes each alert once per entity per day and skips clinics whose pharmacy is OFF; a balance that disagrees
with the ledger and a tampered register chain each raise a P1 admin notification; alerts list / acknowledge through
the routes; a STAT indent alerts the pharmacy; the planner groups shelf batches by the GRN supplier's return window
and only ever leads to a DRAFT supplier return.
"""

import uuid
from datetime import date, timedelta

import pytest

from app.services import rx_alerts
from tests.test_ipd_api_e2e import _clinic, _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m4 import GSTIN, _grn, _line, _post
from tests.test_rx_ipd import _raise, ward  # noqa: F401  (fixture)

TODAY = date.today()


@pytest.fixture
def shop(api):  # noqa: F811
    c, conn = _clinic(api.conn), api.conn
    assert api.owner_toggle(c, "pharmacy_enabled", True).status_code == 200
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    store = str(_q(conn, "SELECT id FROM rx_stores WHERE clinic_id = %s", c))
    g = _generic(conn, c)
    low = _item(conn, c, g, "LOW1", "Low Item", reorder_level_units=50)
    _move(conn, c, store, _batch(conn, c, low, "L1"), low, 20, "opening")
    ctrl = _item(conn, c, g, "CTRL1", "Controlled", schedule="X", allow_loose=True)
    cb = _batch(conn, c, ctrl, "C1")
    _move(conn, c, store, cb, ctrl, 10, "opening")
    _move(conn, c, store, cb, ctrl, 5, "opening", ref=str(uuid.uuid4()))
    mgr = api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER")
    return {"clinic": c, "store": store, "low": low, "ctrl": ctrl, "ctrl_batch": cb, "mgr": mgr}


def _kinds(api, c):  # noqa: F811
    return sorted(_q(api.conn, "SELECT COALESCE(json_agg(kind), '[]') FROM rx_alerts WHERE clinic_id = %s", c))


def _p1(api, c):  # noqa: F811
    return _q(api.conn, "SELECT COALESCE(json_agg(title ORDER BY title), '[]') FROM admin_notifications "
                        "WHERE clinic_id = %s AND title LIKE '[P1]%%'", c)


@pytest.mark.asyncio
async def test_daily_dedupe_and_off_clinics_skipped(api, shop):  # noqa: F811
    s = shop
    off = _clinic(api.conn)                                          # a clinic that never enabled the pharmacy
    for _ in range(2):
        await rx_alerts.run_scan()
    assert _kinds(api, s["clinic"]) == ["low_stock"]
    assert _kinds(api, off) == []
    assert _p1(api, s["clinic"]) == []
    await rx_alerts.scan_clinic({"id": s["clinic"], "rx_settings": {}}, TODAY + timedelta(days=1))
    assert _kinds(api, s["clinic"]) == ["low_stock", "low_stock"]  # a new day, a new alert


@pytest.mark.asyncio
async def test_ledger_mismatch_and_chain_break_raise_p1(api, shop):  # noqa: F811
    s = shop
    _q(api.conn, "SET session_replication_role = replica; UPDATE rx_stock_balances SET qty_units = 99 WHERE batch_id = %s; "
                 "UPDATE rx_register_entries SET qty_units = 1 WHERE clinic_id = %s AND register = 'X' AND seq = 2; "
                 "SET session_replication_role = origin", s["ctrl_batch"], s["clinic"])
    stats = await rx_alerts.run_scan()
    assert stats["integrity_failures"] >= 2
    assert _kinds(api, s["clinic"]) == ["ledger_mismatch", "low_stock", "register_chain_break"]
    assert _p1(api, s["clinic"]) == ["[P1] Drug register tampering detected (X)", "[P1] Pharmacy stock ledger mismatch"]
    payload = _q(api.conn, "SELECT payload FROM rx_alerts WHERE clinic_id = %s AND kind = 'ledger_mismatch'", s["clinic"])
    assert (payload["balance_units"], payload["ledger_units"]) == (99, 15)


@pytest.mark.asyncio
async def test_alert_list_and_acknowledge(api, shop):  # noqa: F811
    s, c = shop, shop["clinic"]
    await rx_alerts.scan_clinic({"id": c, "rx_settings": {}})
    api.as_(s["mgr"])
    [a] = api.call("GET", "/admin/rx/alerts", c).json()["alerts"]
    assert a["kind"] == "low_stock" and a["payload"]["reorder_level_units"] == 50
    assert api.call("GET", "/admin/rx/alerts", c, kind="near_expiry").json()["alerts"] == []
    ack = api.call("POST", f"/admin/rx/alerts/{a['id']}/ack", c)
    assert ack.status_code == 200 and ack.json()["acknowledged_by"] == s["mgr"].user_id
    assert api.call("GET", "/admin/rx/alerts", c).json()["alerts"] == []
    assert [x["id"] for x in api.call("GET", "/admin/rx/alerts", c, ack="true").json()["alerts"]] == [a["id"]]
    assert api.call("POST", f"/admin/rx/alerts/{a['id']}/ack", c).json()["acknowledged_by"] == s["mgr"].user_id
    assert api.call("POST", f"/admin/rx/alerts/{uuid.uuid4()}/ack", c).status_code == 404
    api.as_(api.U("anon", "staff", c, "env-admin", [], staff_role="PHARMACY_MANAGER"))     # no named login
    other = _q(api.conn, "INSERT INTO rx_alerts (clinic_id, kind, alert_date) VALUES (%s, 'cold_chain', CURRENT_DATE) "
                         "RETURNING id", c)
    assert api.call("POST", f"/admin/rx/alerts/{other}/ack", c).status_code == 403


def test_stat_indent_alerts_the_pharmacy(api, ward):  # noqa: F811
    w = ward
    assert _raise(api, w, priority="stat").status_code == 201
    assert _raise(api, w, priority="stat").status_code == 201        # every STAT indent alerts, no dedupe
    _raise(api, w, priority="routine")
    rows = _q(api.conn, "SELECT json_agg(payload ORDER BY created_at) FROM rx_alerts WHERE clinic_id = %s "
                        "AND kind = 'stat_indent'", w["clinic"])
    assert len(rows) == 2 and rows[0]["indent_number"].startswith("IND-") and rows[0]["bed"] == "101"
    assert _q(api.conn, "SELECT count(*) FROM admin_notifications WHERE clinic_id = %s AND title LIKE 'STAT indent%%'",
              w["clinic"]) == 2


def test_expiry_return_planner_groups_by_supplier_and_drafts_only(api, shop):  # noqa: F811
    s, c, conn = shop, shop["clinic"], api.conn
    _q(conn, "UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || jsonb_build_object('gstin', %s::text) "
             "WHERE id = %s", GSTIN, c)
    _q(conn, "UPDATE rx_stores SET can_receive_grn = true WHERE id = %s", s["store"])
    item = _item(conn, c, _generic(conn, c, "Cetirizine", "10 mg"), "CET10", "Cetzine")
    w = {"clinic": c, "store": s["store"], "item": item}
    for name, window in (("Sri Balaji Pharma", 90), ("Deccan Medico", 30)):
        w["supplier"] = str(_q(conn, "INSERT INTO rx_suppliers (clinic_id, name, gstin, state_code, return_window_days) "
                                     "VALUES (%s, %s, %s, '36', %s) RETURNING id", c, name,
                               "36AAACB1234F1Z5" if window == 90 else "36AAACD5678G1Z9", window))
        g = _grn(c=conn, w=w, invoice="INV-" + name[:3], total=92610)
        _line(conn, w, g, batch="B" + name[:3].upper(), expiry=TODAY + timedelta(days=60))
        _post(conn, w, g)
    from app.services.tenant import invalidate_tenant_cache
    invalidate_tenant_cache()
    api.as_(s["mgr"])
    plan = api.call("GET", "/admin/rx/expiry/return-plan", c, store_id=s["store"])
    assert plan.status_code == 200, plan.text
    [grp] = plan.json()["suppliers"]                                 # 60 days left: inside 90, outside 30
    assert grp["supplier_name"] == "Sri Balaji Pharma" and [ln["batch_no"] for ln in grp["lines"]] == ["BSRI"]
    ln = grp["lines"][0]
    before = _q(conn, "SELECT count(*) FROM rx_supplier_returns WHERE clinic_id = %s", c)
    assert before == 0                                               # the plan itself drafts nothing
    d = api.call("POST", "/admin/rx/supplier-returns", c, {"store_id": s["store"], "supplier_id": grp["supplier_id"],
                                                           "reason": "near_expiry",
                                                           "lines": [{"batch_id": ln["batch_id"], "qty_units": ln["qty_units"]}]})
    assert d.status_code == 201, d.text
    assert _q(conn, "SELECT status FROM rx_supplier_returns WHERE id = %s", d.json()["id"]) == "draft"
    again = api.call("GET", "/admin/rx/expiry/return-plan", c, store_id=s["store"]).json()["suppliers"][0]["lines"][0]
    assert again["drafted_units"] == ln["qty_units"]                 # the UI hides what is already drafted
    assert api.call("GET", "/admin/rx/expiry/return-plan", c, store_id=str(uuid.uuid4())).status_code == 404
