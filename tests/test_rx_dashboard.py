"""Pharmacy dashboard and the lost-sale call list through the real /admin/rx routes on real Postgres (P9; RX-62, RX-80).

KPIs for the day (sales, returns, ward indents with STAT, refill requests, items below reorder, near-expiry and expired
stock by cost value, open alerts, register chain status); gross margin only for RX_ADMIN. The call list holds
yesterday's signed prescriptions the pharmacy never billed and the part-filled ones, phones masked; the reveal is
RX_DISPENSE only and audited; nothing on the list sends a message.
"""

import uuid
from datetime import date, timedelta

import pytest

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m5 import complete, draft, line, signed_rx
from tests.test_rx_ipd import _raise, ward  # noqa: F401  (fixture)

TODAY = date.today()


@pytest.fixture
def dash(api, ward, monkeypatch):  # noqa: F811
    w, c, conn = ward, ward["clinic"], api.conn
    audits = []

    async def _log(**kw):
        audits.append(kw)
    monkeypatch.setattr("app.routers.rx.log_admin_action", _log)
    dnear = str(_q(conn, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'DNEAR'", c))
    wd = {"clinic": c, "store": w["store"], "item": w["item"], "batch": dnear}
    s = draft(conn, wd, "otc", walkin="Asha K")
    line(conn, wd, s, 15)                                            # one strip of Dolo 650: ₹30
    complete(conn, wd, s, [{"mode": "upi", "amount_paise": 3000}])
    assert _raise(api, w, priority="stat").status_code == 201
    _q(conn, "UPDATE rx_items SET reorder_level_units = 500 WHERE id = %s", w["item"])
    old = _batch(conn, c, w["item"], "OLD1", expiry=TODAY - timedelta(days=10), mrp=3000)
    _move(conn, c, w["store"], old, w["item"], 5, "opening")        # 5 tabs at ₹300 cost each (30000 x100)
    soon = _batch(conn, c, w["item"], "SOON1", expiry=TODAY + timedelta(days=30), mrp=3000)
    _move(conn, c, w["store"], soon, w["item"], 10, "opening")
    users = {"mgr": api.U("mgr", "staff", c, str(uuid.uuid4()), [], staff_role="PHARMACY_MANAGER"),
             "pharma": w["pharmacist"],
             "keeper": api.U("keeper", "staff", c, str(uuid.uuid4()), [], staff_role="STORE_KEEPER")}
    return {**w, "sale": s, "users": users, "audits": audits}


def _get(api, d, path, user, **params):  # noqa: F811
    api.as_(d["users"][user])
    r = api.call("GET", path, d["clinic"], **params)
    assert r.status_code == 200, r.text
    return r.json()


def test_dashboard_kpis_and_margin_only_for_rx_admin(api, dash):  # noqa: F811
    d = dash
    k = _get(api, d, "/admin/rx/dashboard", "mgr")
    assert (k["sales_today_paise"], k["sales_count"], k["returns_count"]) == (3000, 1, 0)
    assert (k["pending_indents_count"], k["stat_indents_count"]) == (1, 1)
    assert k["low_stock_count"] == 1                                 # Dolo 650 below 500
    assert (k["expired_on_hand_count"], k["expired_on_hand_value_paise"]) == (1, 1500)
    assert k["near_expiry_value_paise"] == 3000 and k["near_expiry_batches"] == 1
    assert k["register_chain_status"]["ok"] is True and k["pending_refill_requests_count"] == 0
    taxable = _q(api.conn, "SELECT taxable_paise FROM rx_sale_items WHERE sale_id = %s", d["sale"])
    cost = 15 * _q(api.conn, "SELECT landed_unit_cost_paise_x100 FROM rx_batches WHERE batch_no = 'DNEAR' "
                             "AND clinic_id = %s", d["clinic"]) // 100
    assert k["gross_margin_paise"] == taxable - cost and k["gross_margin_bps"] == (taxable - cost) * 10000 // taxable
    p = _get(api, d, "/admin/rx/dashboard", "pharma")
    assert p["gross_margin_paise"] is None and p["gross_margin_bps"] is None and p["sales_count"] == 1
    assert _get(api, d, "/admin/rx/dashboard", "mgr", date=str(TODAY - timedelta(days=1)))["sales_count"] == 0
    _q(api.conn, "INSERT INTO rx_alerts (clinic_id, kind, alert_date, payload) VALUES (%s, 'register_chain_break', "
                 "CURRENT_DATE, '{\"register\": \"X\", \"first_bad_seq\": 4}')", d["clinic"])
    chain = _get(api, d, "/admin/rx/dashboard", "keeper")["register_chain_status"]
    assert chain["ok"] is False and chain["breaks"][0]["register"] == "X"
    api.as_(d["u"]["billing"])
    assert api.call("GET", "/admin/rx/dashboard", d["clinic"]).status_code == 403


def _signed_yesterday(api, c, lines):  # noqa: F811
    rx = signed_rx(api.conn, c, lines=lines, patient_name="Lakshmi " + uuid.uuid4().hex[:4])
    _q(api.conn, "SET session_replication_role = replica; UPDATE opd_prescriptions SET signed_at = now() - interval "
                 "'1 day' WHERE id = %s; SET session_replication_role = origin", rx["rx"])
    return rx


def test_lost_sale_list_masked_and_audited_reveal(api, dash):  # noqa: F811
    d, c, conn = dash, dash["clinic"], api.conn
    unbilled = _signed_yesterday(api, c, (("Dolo 650", "tablet", "1", "TDS", 5),))
    partial = _signed_yesterday(api, c, (("Dolo 650", "tablet", "1", "BD", 30),))
    billed = _signed_yesterday(api, c, (("Dolo 650", "tablet", "1", "OD", 2),))
    dlate = str(_q(conn, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'DLATE'", c))
    wd = {"clinic": c, "store": d["store"], "item": d["item"], "batch": dlate}
    for rx, qty in ((partial, 10), (billed, 2)):
        s = draft(conn, wd, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
        line(conn, wd, s, qty, rx_item=rx["rx_items"][0])
        complete(conn, wd, s, [{"mode": "upi", "amount_paise": qty * 200}])
    out = _get(api, d, "/admin/rx/lost-sales", "keeper")
    assert out["date"] == str(TODAY - timedelta(days=1))
    rows = {r["prescription_id"]: r for r in out["rows"]}
    assert set(rows) == {unbilled["rx"], partial["rx"]}             # the fully billed one is not on the list
    assert rows[unbilled["rx"]]["type"] == "unbilled_rx" and rows[unbilled["rx"]]["items"][0]["pending_units"] == 15
    assert rows[partial["rx"]]["type"] == "partial_fulfilment" and rows[partial["rx"]]["items"][0]["pending_units"] == 50
    assert rows[partial["rx"]]["doctor_name"] == "Dr. Meena"
    for r in rows.values():
        assert r["phone_masked"].startswith("+91XXXXXX") and r["phone_masked"][-4:] in (unbilled["phone"], partial["phone"])[
            0 if r["prescription_id"] == unbilled["rx"] else 1]
    assert unbilled["phone"] not in str(out) and partial["phone"] not in str(out)
    assert _get(api, d, "/admin/rx/lost-sales", "keeper", date=str(TODAY))["rows"] == []
    path = f"/admin/rx/lost-sales/{unbilled['rx']}/reveal-phone"
    api.as_(d["users"]["keeper"])                                    # RX_READ only: no reveal
    assert api.call("POST", path, c).status_code == 403
    api.as_(d["users"]["pharma"])
    got = api.call("POST", path, c)
    assert got.status_code == 200 and got.json()["phone"] == unbilled["phone"]
    assert [(a["action"], a["resource_id"]) for a in d["audits"] if a["action"] == "rx_lost_sale_phone_reveal"] == [
        ("rx_lost_sale_phone_reveal", unbilled["rx"])]
    assert api.call("POST", f"/admin/rx/lost-sales/{uuid.uuid4()}/reveal-phone", c).status_code == 404
    assert not _q(conn, "SELECT count(*) FROM rx_refill_requests WHERE clinic_id = %s", c)     # nothing was sent / queued
