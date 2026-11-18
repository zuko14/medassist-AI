"""Migration 142 (Pharmacy OS refills, alerts, integrity checks, item metrics, RX-M9) against real PostgreSQL.

Four tables with RLS forced; one plan per sold line and one open request per plan; the alert dedupe index (one per
entity per day, STAT indents exempt); rx_ledger_check catches a balance that disagrees with the ledger;
rx_alerts_scan writes low-stock / near-expiry / expired-on-hand / ledger / chain-break alerts once per day;
rx_expiry_return_plan groups batches by the original GRN supplier's return window. Purge v9; guarded rollback +
double re-apply. Service behaviour: tests/test_rx_refills.py, tests/test_rx_alerts.py.
"""

import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move
from tests.test_migration_rx_m4 import _grn, _line, _post, _world
from tests.test_migration_rx_m5 import complete, draft, line, pos_world

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "142_rx_refills_alerts.sql", MIG / "rollback" / "142_rollback_rx_refills_alerts.sql"
TODAY = date.today()


def unwind_142(cur) -> None:
    """Inside an open transaction: purge every clinic with refill plans / requests and run the 142 rollback."""
    cur.execute("SELECT to_regclass('public.rx_refill_plans') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_refill_plans UNION "
                "SELECT clinic_id FROM rx_refill_requests) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def scan(c, clinic, day=TODAY, near=90):
    return _q(c, "SELECT rx_alerts_scan(%s, %s, %s)", clinic, day, near)


def alerts(c, clinic):
    return sorted(_q(c, "SELECT COALESCE(json_agg(kind), '[]') FROM rx_alerts WHERE clinic_id = %s", clinic))


def sold_line(c):
    """A completed counter sale of a chronic item: (world, sale_item_id)."""
    w = pos_world(c)
    _q(c, "UPDATE rx_items SET is_chronic = true WHERE id = %s", w["item"])
    s = draft(c, w, "otc", walkin="Asha K")
    si = line(c, w, s, 10)
    complete(c, w, s, [{"mode": "upi", "amount_paise": 12000}])
    return w, si


def plan(c, w, si, status="scheduled"):
    pat = str(_q(c, "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Asha K', %s) RETURNING id",
                 w["clinic"], "+9196" + uuid.uuid4().hex[:8]))
    return str(_q(c, "INSERT INTO rx_refill_plans (clinic_id, patient_id, item_id, sale_item_id, units_per_day, covered_until, "
                     "remind_on, status, opt_in_by, opt_in_at) VALUES (%s, %s, %s, %s, 2, %s, %s, %s, %s, now()) RETURNING id",
                  w["clinic"], pat, w["item"], si, TODAY + timedelta(days=5), TODAY + timedelta(days=2), status,
                  str(uuid.uuid4()))), pat


def test_refill_tables_one_plan_per_line_one_open_request(real_pg_conn):
    c = real_pg_conn
    w, si = sold_line(c)
    p, pat = plan(c, w, si)
    assert "rx_refill_plans_sale_item_key" in _err(
        c, "INSERT INTO rx_refill_plans (clinic_id, patient_id, item_id, sale_item_id, units_per_day, covered_until, "
           "remind_on, opt_in_by, opt_in_at) VALUES (%s, %s, %s, %s, 1, CURRENT_DATE, CURRENT_DATE, %s, now())",
        w["clinic"], pat, w["item"], si, str(uuid.uuid4()))
    assert "units_per_day" in _err(c, "UPDATE rx_refill_plans SET units_per_day = 0 WHERE id = %s", p)
    assert "rx_refill_plans_dates" in _err(c, "UPDATE rx_refill_plans SET remind_on = covered_until + 1 WHERE id = %s", p)
    req = "INSERT INTO rx_refill_requests (clinic_id, plan_id, patient_id, channel) VALUES (%s, %s, %s, 'whatsapp') RETURNING id"
    r1 = str(_q(c, req, w["clinic"], p, pat))
    assert "uq_rx_refill_requests_open" in _err(c, req, w["clinic"], p, pat)
    assert "rx_refill_requests_reject_reason" in _err(c, "UPDATE rx_refill_requests SET status = 'rejected' WHERE id = %s", r1)
    assert "rx_refill_requests_packed_sale" in _err(c, "UPDATE rx_refill_requests SET status = 'packed' WHERE id = %s", r1)
    _q(c, "UPDATE rx_refill_requests SET status = 'rejected', rejected_reason = 'Rx expired' WHERE id = %s", r1)
    assert _q(c, req, w["clinic"], p, pat)                           # a closed request frees the slot
    other = _clinic(c, "Other")
    assert "rx_refill_requests_plan_fk" in _err(c, req, other, p, pat)   # a plan of another clinic


def test_alert_dedupe_per_entity_per_day_stat_exempt(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    store = _store(c, clinic)
    ins = "INSERT INTO rx_alerts (clinic_id, store_id, kind, alert_date) VALUES (%s, %s, %s, %s)"
    _q(c, ins, clinic, store, "ledger_mismatch", TODAY)
    assert "uq_rx_alerts_dedupe" in _err(c, ins, clinic, store, "ledger_mismatch", TODAY)
    _q(c, ins, clinic, store, "ledger_mismatch", TODAY + timedelta(days=1))
    for _ in range(2):
        _q(c, ins, clinic, store, "stat_indent", TODAY)              # every STAT indent is its own alert
    assert "rx_alerts_ack_shape" in _err(c, "UPDATE rx_alerts SET acknowledged_at = now() WHERE clinic_id = %s", clinic)


def test_scan_writes_each_alert_once_a_day(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    store = _store(c, clinic)
    g = _generic(c, clinic)
    low = _item(c, clinic, g, "LOW1", "Low Item", reorder_level_units=50)
    exp = _item(c, clinic, g, "EXP1", "Expiring Item")
    _move(c, clinic, store, _batch(c, clinic, low, "L1"), low, 20, "opening")
    near = _batch(c, clinic, exp, "N1", expiry=TODAY + timedelta(days=30))
    _move(c, clinic, store, near, exp, 5, "opening")
    old = _batch(c, clinic, exp, "OLD", expiry=TODAY + timedelta(days=400))
    _move(c, clinic, store, old, exp, 5, "opening")
    out = scan(c, clinic)
    assert (out["low_stock"], out["expiry"], out["ledger_mismatch"], out["register_chain_break"]) == (1, 1, [], [])
    assert alerts(c, clinic) == ["low_stock", "near_expiry"]
    assert scan(c, clinic)["low_stock"] == 0 and alerts(c, clinic) == ["low_stock", "near_expiry"]   # same day: no repeat
    later = scan(c, clinic, TODAY + timedelta(days=31))              # the near batch has now expired on the shelf
    assert later["expiry"] == 1 and "expired_on_hand" in alerts(c, clinic)


def test_ledger_check_and_chain_break_alert(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    store = _store(c, clinic)
    item = _item(c, clinic, _generic(c, clinic), "X1", "Controlled", schedule="X", allow_loose=True)
    batch = _batch(c, clinic, item, "B1")
    _move(c, clinic, store, batch, item, 10, "opening")
    _move(c, clinic, store, batch, item, 5, "opening", ref=str(uuid.uuid4()))
    assert _q(c, "SELECT count(*) FROM rx_ledger_check(%s)", clinic) == 0
    _q(c, "SET session_replication_role = replica; UPDATE rx_stock_balances SET qty_units = 99 WHERE batch_id = %s; "
          "UPDATE rx_register_entries SET qty_units = 1 WHERE clinic_id = %s AND seq = 2; "
          "SET session_replication_role = origin", batch, clinic)
    [mis] = _q(c, "SELECT json_agg(x) FROM rx_ledger_check(%s) x", clinic)
    assert (mis["balance_units"], mis["ledger_units"]) == (99, 15)
    out = scan(c, clinic)
    assert out["ledger_mismatch"][0]["balance_units"] == 99
    assert out["register_chain_break"] == [{"register": "X", "first_bad_seq": 2, "verified_rows": 1}]
    assert alerts(c, clinic) == ["ledger_mismatch", "register_chain_break"]


def test_expiry_return_plan_by_supplier_window(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    _q(c, "UPDATE rx_suppliers SET return_window_days = 90 WHERE id = %s", w["supplier"])
    g = _grn(c, w, total=92610)
    _line(c, w, g, batch="IN", expiry=TODAY + timedelta(days=60))     # inside the 90-day window
    _post(c, w, g)
    g2 = _grn(c, w, "INV-2", total=92610)
    _line(c, w, g2, batch="OUT", expiry=TODAY + timedelta(days=200))  # not yet
    _post(c, w, g2)
    [grp] = _q(c, "SELECT rx_expiry_return_plan(%s, %s, %s)", w["clinic"], w["store"], TODAY)
    assert grp["supplier_name"] == "Sri Balaji Pharma" and grp["return_window_days"] == 90
    assert [(ln["batch_no"], ln["days_left"], ln["qty_units"], ln["drafted_units"]) for ln in grp["lines"]] == [("IN", 60, 120, 0)]
    batch = grp["lines"][0]["batch_id"]
    srn = str(_q(c, "INSERT INTO rx_supplier_returns (clinic_id, store_id, supplier_id, reason) VALUES (%s, %s, %s, "
                    "'near_expiry') RETURNING id", w["clinic"], w["store"], w["supplier"]))
    _q(c, "INSERT INTO rx_supplier_return_lines (clinic_id, return_id, batch_id, qty_units) VALUES (%s, %s, %s, 40)",
       w["clinic"], srn, batch)
    assert _q(c, "SELECT rx_expiry_return_plan(%s, %s, %s)", w["clinic"], w["store"], TODAY)[0]["lines"][0]["drafted_units"] == 40
    assert _q(c, "SELECT rx_expiry_return_plan(%s, %s, %s)", w["clinic"], w["store"], TODAY - timedelta(days=100)) == []


def test_purge_v9_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    (a, sa), (b, sb_) = sold_line(c), sold_line(c)
    for w, si in ((a, sa), (b, sb_)):
        plan(c, w, si)
        scan(c, w["clinic"])
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert out["refill_plans"] == 1
    assert _q(c, "SELECT count(*) FROM rx_refill_plans WHERE clinic_id = %s", b["clinic"]) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "142_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_142(cur)
        cur.execute("SELECT to_regclass('public.rx_refill_plans'), to_regclass('public.rx_alerts'), "
                    "to_regclass('public.rx_register_entries') IS NOT NULL")
        assert cur.fetchone() == (None, None, True)
        cur.execute("SELECT prosrc LIKE '%%rx_register_entries%%' AND prosrc NOT LIKE '%%rx_refill_plans%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                            # purge v8 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_m9_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_ledger_check', 'rx_alerts_scan', 'rx_expiry_return_plan')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
