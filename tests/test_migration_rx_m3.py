"""Migration 136 (Pharmacy OS batches + stock ledger, RX-M3) against real PostgreSQL.

ADR-RX-01/02 invariants: the ledger is written only inside an rx_* function, never
updated or deleted; the balance is a projection nobody can write directly; no movement
can take a batch below zero (rx_insufficient_stock); an expired or blocked batch never
leaves for a patient / ward, but can be written off or returned; balance == SUM(ledger)
after 1 000 random movements; IST date boundary; batch identity immutable; opening stock
(dry run, idempotent re-apply, expired refused, closed after trading starts); write-off
replay; FEFO view order; purge v3; guarded rollback + idempotent re-apply.
HTTP behaviour: tests/test_rx_stock_api.py.
"""

import json
import random
import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "136_rx_stock_ledger.sql", MIG / "rollback" / "136_rollback_rx_stock_ledger.sql"


def unwind_136(cur) -> None:
    """Inside an open transaction: purge every clinic with stock rows and run the 136 rollback."""
    cur.execute("SELECT to_regclass('public.rx_batches') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_batches UNION "
                "SELECT clinic_id FROM rx_stock_movements) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _world(c, name="RX Ledger"):
    clinic = _clinic(c, name)
    store = _store(c, clinic, "MAIN")
    item = _item(c, clinic, _generic(c, clinic))
    return clinic, store, item


def _batch(c, clinic, item, no="B1", expiry=None, mrp=4500, blocked=False):
    expiry = expiry or (date.today() + timedelta(days=400))
    return str(_q(c, "INSERT INTO rx_batches (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise, "
                     "landed_unit_cost_paise_x100, is_blocked, block_reason) VALUES (%s, %s, %s, %s, %s, 30000, %s, %s) "
                     "RETURNING id", clinic, item, no, expiry, mrp, blocked, "Recall notice" if blocked else None))


def _move(c, clinic, store, batch, item, qty, mtype, ref=None, reason=None):
    """What an rx_* function does: set kriya.rx_tx for its transaction, then insert."""
    _q(c, "SELECT set_config('kriya.rx_tx', %s, true); "
          "INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, "
          "ref_id, unit_cost_paise_x100, actor_name, reason) VALUES (%s, %s, %s, %s, %s, %s, 'test', %s, 30000, 'T', %s)",
       clinic, clinic, store, batch, item, qty, mtype, ref or str(uuid.uuid4()), reason)


def _bal(c, store, batch):
    return _q(c, "SELECT COALESCE(SUM(qty_units), 0) FROM rx_stock_balances WHERE store_id = %s AND batch_id = %s",
              store, batch)


def test_ledger_only_through_functions_and_append_only(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    batch = _batch(c, clinic, item)
    assert "rx_movement_via_function_only" in _err(
        c, "INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, "
           "ref_id, unit_cost_paise_x100, actor_name) VALUES (%s, %s, %s, %s, 10, 'grn_in', 'x', %s, 0, 'T')",
        clinic, store, batch, item, str(uuid.uuid4()))
    assert "rx_movement_via_function_only" in _err(       # another clinic's flag does not count
        c, "SELECT set_config('kriya.rx_tx', %s, true); INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, "
           "item_id, qty_units, movement_type, ref_table, ref_id, unit_cost_paise_x100, actor_name) VALUES "
           "(%s, %s, %s, %s, 10, 'grn_in', 'x', %s, 0, 'T')", str(uuid.uuid4()), clinic, store, batch, item,
        str(uuid.uuid4()))
    _move(c, clinic, store, batch, item, 100, "grn_in")
    assert _bal(c, store, batch) == 100
    assert "rx_append_only:rx_stock_movements" in _err(c, "UPDATE rx_stock_movements SET qty_units = 999 WHERE batch_id = %s", batch)
    assert "rx_append_only:rx_stock_movements" in _err(c, "DELETE FROM rx_stock_movements WHERE batch_id = %s", batch)
    for sql in ("UPDATE rx_stock_balances SET qty_units = 999 WHERE batch_id = %s",
                "DELETE FROM rx_stock_balances WHERE batch_id = %s"):
        assert "rx_balance_via_ledger_only" in _err(c, sql, batch)
    assert "rx_balance_via_ledger_only" in _err(c, "INSERT INTO rx_stock_balances (clinic_id, store_id, batch_id, item_id, "
                                                   "qty_units) VALUES (%s, %s, %s, %s, 5)", clinic, _store(c, clinic, "S2"),
                                                batch, item)
    assert _bal(c, store, batch) == 100
    # sign and reason rules
    assert "rx_sm_sign" in _err(c, "SELECT set_config('kriya.rx_tx', %s, true); INSERT INTO rx_stock_movements (clinic_id, "
                                   "store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id, "
                                   "unit_cost_paise_x100, actor_name) VALUES (%s, %s, %s, %s, 5, 'sale_out', 'x', %s, 0, "
                                   "'T')", clinic, clinic, store, batch, item, str(uuid.uuid4()))
    with pytest.raises(psycopg2.Error, match="rx_sm_reason_for_adjust"):
        _move(c, clinic, store, batch, item, -1, "adjust_out")


def test_movement_must_match_batch_item_and_tenant(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    other_item = _item(c, clinic, _generic(c, clinic, "Ibuprofen", "400 mg"), "IBU400", "Brufen 400")
    batch = _batch(c, clinic, item)
    with pytest.raises(psycopg2.Error, match="rx_batch_item_mismatch"):
        _move(c, clinic, store, batch, other_item, 10, "grn_in")
    theirs, their_store, their_item = _world(c, "Theirs")
    with pytest.raises(psycopg2.Error, match="rx_sm_batch_fk|rx_batch_item_mismatch"):
        _move(c, theirs, their_store, batch, their_item, 10, "grn_in")       # my batch in their ledger
    with pytest.raises(psycopg2.Error, match="rx_sm_store_fk"):
        _move(c, clinic, their_store, batch, item, 10, "grn_in")             # their store in my ledger


def test_negative_stock_is_impossible(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    batch = _batch(c, clinic, item)
    _move(c, clinic, store, batch, item, 10, "grn_in")
    with pytest.raises(psycopg2.Error, match="rx_insufficient_stock"):
        _move(c, clinic, store, batch, item, -11, "sale_out")
    with pytest.raises(psycopg2.Error, match="rx_insufficient_stock"):       # never into a store that has none
        _move(c, clinic, _store(c, clinic, "WARD"), batch, item, -1, "transfer_out")
    _move(c, clinic, store, batch, item, -10, "sale_out")
    assert _bal(c, store, batch) == 0
    assert _q(c, "SELECT count(*) FROM rx_stock_movements WHERE batch_id = %s", batch) == 2


def test_expired_and_blocked_batches_never_leave_for_patients(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    today = _q(c, "SELECT rx_ist_date(now())")
    expired = _batch(c, clinic, item, "OLD", today - timedelta(days=1))
    last_day = _batch(c, clinic, item, "TODAY", today)
    blocked = _batch(c, clinic, item, "RECALL", blocked=True)
    for b in (expired, last_day, blocked):
        _move(c, clinic, store, b, item, 20, "opening")
    for mtype in ("sale_out", "ipd_dispense_out", "transfer_out", "ward_issue_out"):
        with pytest.raises(psycopg2.Error, match="rx_batch_expired"):
            _move(c, clinic, store, expired, item, -1, mtype)
        with pytest.raises(psycopg2.Error, match="rx_batch_blocked"):
            _move(c, clinic, store, blocked, item, -1, mtype)
    _move(c, clinic, store, last_day, item, -1, "sale_out")                # expiry date itself is still sellable
    _move(c, clinic, store, expired, item, -5, "supplier_return_out")      # how expired stock leaves
    res = _q(c, "SELECT rx_post_writeoff(%s, %s, %s, %s::jsonb, NULL, 'Ravi')", clinic, store, str(uuid.uuid4()),
             json.dumps([{"batch_id": expired, "qty_units": 15, "kind": "expiry", "reason": "Expired 10/2026"},
                         {"batch_id": blocked, "qty_units": 20, "kind": "damage", "reason": "Recall destroyed"}]))
    assert (res["movements"], res["units"]) == (2, 35)
    assert (_bal(c, store, expired), _bal(c, store, blocked)) == (0, 0)
    assert _q(c, "SELECT current_setting('kriya.rx_tx', true)") in (None, "")


def test_ist_date_boundary_in_sql(real_pg_conn):
    c = real_pg_conn
    assert str(_q(c, "SELECT rx_ist_date('2027-03-31 18:29:59+00')")) == "2027-03-31"
    assert str(_q(c, "SELECT rx_ist_date('2027-03-31 18:30:00+00')")) == "2027-04-01"   # IST midnight


def test_batch_identity_is_immutable(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    batch = _batch(c, clinic, item)
    for col, val in (("mrp_pack_paise", 9999), ("batch_no", "B2"), ("expiry_date", "2030-01-31"),
                     ("landed_unit_cost_paise_x100", 1)):
        assert "rx_batch_field_locked" in _err(c, f"UPDATE rx_batches SET {col} = %s WHERE id = %s", val, batch)
    assert "rx_batches_block_reason" in _err(c, "UPDATE rx_batches SET is_blocked = true WHERE id = %s", batch)
    _q(c, "UPDATE rx_batches SET is_blocked = true, block_reason = 'CDSCO alert 12/26' WHERE id = %s", batch)
    _q(c, "UPDATE rx_batches SET is_blocked = false, block_reason = NULL WHERE id = %s", batch)
    assert "rx_batch_delete_refused" in _err(c, "DELETE FROM rx_batches WHERE id = %s", batch)
    # the same batch number at a new MRP is a different batch (DPCO revision)
    _batch(c, clinic, item, "B1", _q(c, "SELECT expiry_date FROM rx_batches WHERE id = %s", batch), mrp=4800)
    assert "rx_batches_identity_key" in _err(
        c, "INSERT INTO rx_batches (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise, landed_unit_cost_paise_x100) "
           "SELECT clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise, 1 FROM rx_batches WHERE id = %s", batch)


def test_balance_equals_ledger_after_1000_random_movements(real_pg_conn):
    c = real_pg_conn
    clinic, s1, item = _world(c)
    s2 = _store(c, clinic, "WARD", "ot_tray")
    batches = [_batch(c, clinic, item, f"R{i}") for i in range(4)]
    rng = random.Random(1350136)
    refused = 0
    for _ in range(1000):
        qty = rng.randint(1, 40)
        mtype = rng.choice(["grn_in", "sale_out", "transfer_in", "transfer_out", "sale_return_in", "ipd_dispense_out"])
        try:
            _move(c, clinic, rng.choice([s1, s2]), rng.choice(batches), item, qty if mtype.endswith("_in") else -qty, mtype)
        except psycopg2.Error as e:
            assert "rx_insufficient_stock" in str(e)
            refused += 1
    assert refused > 0
    drift = _q(c, """SELECT count(*) FROM (
                       SELECT store_id, batch_id, SUM(qty_units) s FROM rx_stock_movements WHERE clinic_id = %s
                        GROUP BY 1, 2) l
                     FULL JOIN (SELECT store_id, batch_id, qty_units FROM rx_stock_balances WHERE clinic_id = %s) b
                       USING (store_id, batch_id)
                    WHERE COALESCE(l.s, 0) <> COALESCE(b.qty_units, 0)""", clinic, clinic)
    assert drift == 0
    assert _q(c, "SELECT min(qty_units) FROM rx_stock_balances WHERE clinic_id = %s", clinic) >= 0


def _opening(c, clinic, store, rows, apply):
    return _q(c, "SELECT rx_opening_stock(%s, %s, %s::jsonb, %s, NULL, 'Opening')", clinic, store, json.dumps(rows), apply)


def test_opening_stock_dry_run_idempotent_and_closed_after_trading(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    exp = str(date.today() + timedelta(days=300))
    rows = [{"item_code": "PCM500", "batch_no": "OP1", "expiry_date": exp, "mrp_pack_paise": 3500, "ptr_pack_paise": 2400,
             "landed_unit_cost_paise_x100": 24000, "qty_units": 120, "_row": 2},
            {"item_code": "PCM500", "batch_no": "OP2", "expiry_date": exp, "mrp_pack_paise": 3500, "ptr_pack_paise": 2400,
             "landed_unit_cost_paise_x100": 24000, "qty_units": 30, "_row": 3}]
    dry = _opening(c, clinic, store, rows, False)
    assert (dry["new_batches"], dry["units"], dry["applied"]) == (2, 150, False)
    assert _q(c, "SELECT count(*) FROM rx_batches WHERE clinic_id = %s", clinic) == 0
    done = _opening(c, clinic, store, rows, True)
    assert (done["movements"], done["units"], done["applied"]) == (2, 150, True)
    again = _opening(c, clinic, store, rows, True)
    assert (again["movements"], again["already_loaded"], again["new_batches"]) == (0, 2, 0)
    assert _q(c, "SELECT SUM(qty_units) FROM rx_stock_balances WHERE store_id = %s", store) == 150
    # an expired or unknown row refuses the whole file
    bad = [{**rows[0], "batch_no": "OP3", "_row": 2},
           {**rows[0], "batch_no": "OLD", "expiry_date": str(date.today() - timedelta(days=1)), "_row": 3},
           {**rows[0], "item_code": "NOPE", "_row": 4}]
    res = _opening(c, clinic, store, bad, True)
    assert res["applied"] is False and [e["row"] for e in res["errors"]] == [3, 4]
    assert "rx_batch_expired" in res["errors"][0]["error"] and "rx_item_not_found" in res["errors"][1]["error"]
    assert _q(c, "SELECT count(*) FROM rx_batches WHERE clinic_id = %s AND batch_no = 'OP3'", clinic) == 0
    # once the store trades, opening stock is closed (it could hide shrinkage)
    batch = _q(c, "SELECT id FROM rx_batches WHERE clinic_id = %s AND batch_no = 'OP1'", clinic)
    _move(c, clinic, store, str(batch), item, -1, "sale_out")
    with pytest.raises(psycopg2.Error, match="rx_opening_closed"):
        _opening(c, clinic, store, rows, False)
    with pytest.raises(psycopg2.Error, match="rx_store_not_found"):
        _opening(c, _clinic(c, "Other"), store, rows, False)


def test_writeoff_replay_and_validation(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    batch = _batch(c, clinic, item)
    _move(c, clinic, store, batch, item, 10, "grn_in")
    ref = str(uuid.uuid4())
    line = [{"batch_id": batch, "qty_units": 3, "kind": "damage", "reason": "Strip torn"}]
    first = _q(c, "SELECT rx_post_writeoff(%s, %s, %s, %s::jsonb, NULL, 'R')", clinic, store, ref, json.dumps(line))
    second = _q(c, "SELECT rx_post_writeoff(%s, %s, %s, %s::jsonb, NULL, 'R')", clinic, store, ref, json.dumps(line))
    assert (first["replayed"], second["replayed"]) == (False, True)
    assert _bal(c, store, batch) == 7
    for bad, code in (({"qty_units": 0}, "rx_qty_invalid"), ({"kind": "theft"}, "rx_writeoff_kind_invalid"),
                      ({"reason": ""}, "rx_sm_reason_for_adjust"), ({"qty_units": 8}, "rx_insufficient_stock")):
        with pytest.raises(psycopg2.Error, match=code):
            _q(c, "SELECT rx_post_writeoff(%s, %s, %s, %s::jsonb, NULL, 'R')", clinic, store, str(uuid.uuid4()),
               json.dumps([{**line[0], **bad}]))
    with pytest.raises(psycopg2.Error, match="rx_batch_not_found"):            # another clinic's batch
        _q(c, "SELECT rx_post_writeoff(%s, %s, %s, %s::jsonb, NULL, 'R')", _clinic(c, "X"), store, str(uuid.uuid4()),
           json.dumps(line))
    assert _bal(c, store, batch) == 7


def test_fefo_and_item_stock_views(real_pg_conn):
    c = real_pg_conn
    clinic, store, item = _world(c)
    today = date.today()
    late = _batch(c, clinic, item, "LATE", today + timedelta(days=500))
    soon = _batch(c, clinic, item, "SOON", today + timedelta(days=40))
    old = _batch(c, clinic, item, "OLD", today - timedelta(days=3))
    blocked = _batch(c, clinic, item, "BLK", today + timedelta(days=10), blocked=True)
    for b, q in ((late, 50), (soon, 20), (old, 5), (blocked, 7)):
        _move(c, clinic, store, b, item, q, "opening")
    fefo = _q(c, "SELECT json_agg(batch_no ORDER BY fefo_rank) FROM rx_v_fefo WHERE store_id = %s AND item_id = %s",
              store, item)
    assert fefo == ["SOON", "LATE"]
    row = _q(c, "SELECT row_to_json(v) FROM rx_v_item_stock v WHERE store_id = %s AND item_id = %s", store, item)
    assert (row["qty_units"], row["sellable_units"], row["nearest_expiry"]) == (82, 70, str(today + timedelta(days=40)))
    assert row["value_paise"] == 82 * 300


def test_purge_v3_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, sa, ia = _world(c)
    b, sb_, ib = _world(c, "Keep")
    _move(c, a, sa, _batch(c, a, ia), ia, 5, "opening")
    _move(c, b, sb_, _batch(c, b, ib), ib, 5, "opening")
    out = _q(c, "SELECT rx_purge_clinic(%s)", a)
    assert (out["movements"], out["items"], out["stores"]) == (1, 1, 1)
    assert _q(c, "SELECT count(*) FROM rx_stock_balances WHERE clinic_id = %s", b) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "136_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_136(cur)
        cur.execute("SELECT to_regclass('public.rx_stock_movements'), to_regclass('public.rx_v_fefo'), "
                    "to_regclass('public.rx_items') IS NOT NULL")
        assert cur.fetchone() == (None, None, True)
        cur.execute("SELECT prosrc LIKE '%%rx_items%%' AND prosrc NOT LIKE '%%rx_batches%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                                # purge v2 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_ledger_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_in_tx', 'rx_ist_date', 'rx_guard_batch', 'rx_sm_guard_insert',
                                                 'rx_sm_expiry_lock', 'rx_sm_apply', 'rx_guard_balance',
                                                 'rx_post_writeoff', 'rx_opening_stock', 'rx_purge_clinic')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
