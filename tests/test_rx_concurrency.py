"""Pharmacy OS concurrency and race conditions on real PostgreSQL (RG8; RX-63, TEST-PLAN §3).

Every race runs N independent psycopg2 connections in threads released together by a barrier, so the SQL functions'
own locks — not the test — decide the order. Asserted: the stock wall (never negative, balance = ledger), the
prescription fulfilment cap for a Schedule H1 drug sold at two counters, dispense vs the IPD final bill (no deadlock,
one invoice, no charge lost), the audit delta under live sales, and a gapless verified register chain.
"""

import json
import threading
import uuid

import psycopg2

from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)
from tests.test_migration_rx_m1 import _store
from tests.test_migration_rx_m3 import _bal, _move
from tests.test_migration_rx_m5 import draft, line, pos_world, signed_rx
from tests.test_migration_rx_m7 import counts, post, start
from tests.test_rx_ipd import _dispense, _raise, ward  # noqa: F401  (fixture)

RX_SNAP = {"registration_number": "TSMC/2012/777"}


def _race(uri, fns, timeout=60):
    """Run each fn(conn) on its own connection, all released at once. → [("ok", value) | ("err", message)]."""
    gate, out = threading.Barrier(len(fns)), [None] * len(fns)

    def run(i, fn):
        conn = psycopg2.connect(uri)
        conn.autocommit = True
        try:
            gate.wait()
            out[i] = ("ok", fn(conn))
        except psycopg2.Error as e:
            out[i] = ("err", str(e))
        finally:
            conn.close()
    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert all(not t.is_alive() for t in threads), "a racer hung (lock wait / deadlock)"
    assert not [m for k, m in out if k == "err" and "deadlock" in m], out
    return out


def _complete(w, sale, amount):
    return lambda conn: _q(conn, "SELECT rx_complete_sale(%s, %s, %s::jsonb, NULL, NULL, NULL, 'Ravi', NULL, false, false)",
                           w["clinic"], sale, json.dumps([{"mode": "upi", "amount_paise": amount}]))


def _ledger(c, w, store=None):
    return _q(c, "SELECT COALESCE(SUM(qty_units), 0) FROM rx_stock_movements WHERE store_id = %s AND batch_id = %s",
              store or w["store"], w["batch"])


def test_last_units_race_one_sale_wins(real_pg_conn, real_postgres_uri):
    c = real_pg_conn
    w = pos_world(c, stock=5)                                   # ₹120 a strip of 10 → 5 units ₹60
    sales = [draft(c, w) for _ in range(2)]
    for s in sales:
        line(c, w, s, 5)
    got = _race(real_postgres_uri, [_complete(w, s, 6000) for s in sales])
    assert sorted(k for k, _ in got) == ["err", "ok"], got
    assert "rx_insufficient_stock" in next(m for k, m in got if k == "err")
    assert _bal(c, w["store"], w["batch"]) == 0 == _ledger(c, w)
    assert _q(c, "SELECT count(*) FROM rx_sales WHERE clinic_id = %s AND status = 'completed'", w["clinic"]) == 1


def test_h1_prescription_at_two_counters_never_over_fulfils(real_pg_conn, real_postgres_uri):
    c = real_pg_conn
    w = pos_world(c, schedule="H1")
    w2 = {**w, "store": _store(c, w["clinic"], "OPD2")}
    _move(c, w["clinic"], w2["store"], w["batch"], w["item"], 100, "opening")
    rx = signed_rx(c, w["clinic"])                              # 1 TDS × 5 days = 15 units, H1: a hard cap
    sales = []
    for ww in (w, w2):                                          # one live draft per Rx × store: two counters
        s = draft(c, ww, "opd_rx", rx["rx"], rx["patient"], snapshot=RX_SNAP)
        line(c, ww, s, 10, rx_item=rx["rx_items"][0])
        sales.append(s)
    got = _race(real_postgres_uri, [_complete(w, s, 12000) for s in sales])
    assert sorted(k for k, _ in got) == ["err", "ok"], got
    assert "rx_fulfilment_exceeds_rx" in next(m for k, m in got if k == "err")
    assert _q(c, "SELECT SUM(units) FROM rx_rx_fulfilments WHERE opd_prescription_item_id = %s", rx["rx_items"][0]) == 10
    assert _bal(c, w["store"], w["batch"]) + _bal(c, w2["store"], w["batch"]) == 190


def test_ipd_dispense_vs_final_bill_serialise_without_deadlock(api, ward, real_postgres_uri):  # noqa: F811
    w, c, conn = ward, ward["clinic"], api.conn
    first = _raise(api, w).json()
    assert _dispense(api, w, first).status_code == 200            # one posted pharmacy charge to bill
    second = _raise(api, w).json()
    api.as_(w["u"]["doctor"])
    r = api.call("POST", f"/admin/ipd/admissions/{w['admission']}/discharge/initiate", c, {"discharge_type": "regular"})
    assert r.status_code == 200, r.text
    ln = str(_q(conn, "SELECT id FROM rx_indent_lines WHERE indent_id = %s", second["id"]))
    batch = str(_q(conn, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'DLATE'", c))
    lines = json.dumps([{"indent_line_id": ln, "item_id": w["item"], "batch_id": batch, "qty_units": 3}])
    got = _race(real_postgres_uri, [
        lambda k: _q(k, "SELECT rx_dispense_indent(%s, %s, %s, %s::jsonb, NULL, 'Pharma', %s)", c, second["id"],
                     w["store"], lines, uuid.uuid4().hex),
        lambda k: _q(k, "SELECT ipd_generate_final_bill(%s, %s, NULL, 'Cashier')", c, w["admission"])])
    assert [k for k, _ in got] == ["ok", "ok"], got
    inv = _q(conn, "SELECT json_agg(id) FROM opd_invoices WHERE clinic_id = %s AND admission_id = %s AND status <> 'void'",
             c, w["admission"])
    assert len(inv) == 1                                          # one bill
    charges = _q(conn, "SELECT json_agg(json_build_array(status, invoice_id)) FROM ipd_charges WHERE admission_id = %s "
                       "AND source = 'pharmacy' AND status <> 'void'", w["admission"])
    assert len(charges) == 2                                      # no charge lost or doubled
    assert all(inv_id in (None, inv[0]) for _, inv_id in charges)  # billed on that bill, or a late (unbilled) charge
    assert charges.count(["posted", inv[0]]) >= 1                 # the first dispense is always on the bill


def test_sales_during_a_counting_audit_leave_zero_variance(real_pg_conn, real_postgres_uri):
    c = real_pg_conn
    w = pos_world(c, stock=100)
    a = start(c, {**w, "central": w["store"]})                    # not frozen: the counter keeps selling
    sales = [draft(c, w) for _ in range(10)]
    for s in sales:
        line(c, w, s, 1)
    got = _race(real_postgres_uri, [_complete(w, s, 1200) for s in sales])
    assert [k for k, _ in got] == ["ok"] * 10, got
    assert _q(c, "SELECT movement_delta_since_start FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"]) == -10
    counts(c, w, a["audit_id"], [(w["batch"], 90)])               # the shelf, counted right
    assert _q(c, "SELECT variance_units FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"]) == 0
    _q(c, "SELECT rx_review_audit(%s, %s)", w["clinic"], a["audit_id"])
    post(c, w, a["audit_id"], reasons=[])
    assert _bal(c, w["store"], w["batch"]) == 90 == _ledger(c, w)


def test_register_chain_stays_gapless_under_ten_concurrent_h1_sales(real_pg_conn, real_postgres_uri):
    c = real_pg_conn
    w = pos_world(c, schedule="H1")
    sales = []
    for _ in range(10):
        rx = signed_rx(c, w["clinic"])
        s = draft(c, w, "opd_rx", rx["rx"], rx["patient"], snapshot=RX_SNAP)
        line(c, w, s, 1, rx_item=rx["rx_items"][0])
        sales.append(s)
    got = _race(real_postgres_uri, [_complete(w, s, 1200) for s in sales])
    assert [k for k, _ in got] == ["ok"] * 10, got
    seqs = _q(c, "SELECT json_agg(seq ORDER BY seq) FROM rx_register_entries WHERE clinic_id = %s AND register = 'H1'",
              w["clinic"])
    assert seqs == list(range(1, 12))                             # opening + 10 sales, no gap, no duplicate
    assert _q(c, "SELECT json_build_array(ok, verified_rows) FROM rx_register_verify(%s, 'H1')", w["clinic"]) == [True, 11]
    numbers = _q(c, "SELECT json_agg(sale_seq ORDER BY sale_seq) FROM rx_sales WHERE clinic_id = %s", w["clinic"])
    assert numbers == list(range(1, 11))                          # PHS- series contiguous too
