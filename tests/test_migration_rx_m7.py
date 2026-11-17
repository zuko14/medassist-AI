"""Migration 140 (Pharmacy OS transfers, stock audits, par levels, RX-M7) against real PostgreSQL.

Transfers: function-only writes, draft replace, dispatch moves stock out of the source with an STR- number
(numbered last; replay), receipt into the target, short units written off against the source without
touching its balance twice, blocked batches never leave, controlled lines two-person. Audits: snapshot,
blind counts set per batch, found stock joins the audit, movements during the count accumulate on the line
(the audit's own posting does not), a frozen store sells nothing, every line counted before review, reasons
on every variance, adjust movements bring stock to the count, second person above the value threshold, one
open audit per store, cancel unfreezes. Par levels set / removed. Purge v7; guarded rollback + re-apply.
HTTP behaviour: tests/test_rx_transfers.py, tests/test_rx_audit.py.
"""

import json
import uuid
from datetime import date
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _bal, _batch, _move

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "140_rx_transfers_audits.sql", MIG / "rollback" / "140_rollback_rx_transfers_audits.sql"
A, B = str(uuid.uuid4()), str(uuid.uuid4())          # two staff ids


def unwind_140(cur) -> None:
    """Inside an open transaction: purge every clinic with transfers, audits or par levels; run the 140 rollback."""
    cur.execute("SELECT to_regclass('public.rx_transfers') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_transfers UNION "
                "SELECT clinic_id FROM rx_stock_audits UNION SELECT clinic_id FROM rx_par_levels) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def world(c, stock=100, schedule="none"):
    clinic = _clinic(c, "RX Transfer Hospital")
    central = _store(c, clinic, "CENTRAL", "central")
    tray = _store(c, clinic, "OT1", "ot_tray", parent=central)
    kw = {"allow_loose": True} if schedule in ("X", "narcotic") else {}
    item = _item(c, clinic, _generic(c, clinic), "PCM650", "Dolo 650", schedule=schedule, rack_location="A-1", **kw)
    batch = _batch(c, clinic, item, "B1")
    _move(c, clinic, central, batch, item, stock, "opening")
    return {"clinic": clinic, "central": central, "tray": tray, "item": item, "batch": batch}


def draft(c, w, units=10, tid=None, frm=None, to=None, batch=None):
    return _q(c, "SELECT rx_save_transfer(%s, %s, %s, %s, %s::jsonb, 'note', 'manual', %s, 'Keeper')", w["clinic"], tid,
              frm or w["central"], to or w["tray"],
              json.dumps([{"item_id": w["item"], "batch_id": batch or w["batch"], "sent_units": units}]), A)["transfer_id"]


def dispatch(c, w, t, actor=A, controlled=False):
    return _q(c, "SELECT rx_dispatch_transfer(%s, %s, %s, 'Keeper', %s)", w["clinic"], t, actor, controlled)


def receive(c, w, t, got, reason=None, actor=B, controlled=False):
    line = str(_q(c, "SELECT id FROM rx_transfer_lines WHERE transfer_id = %s", t))
    return _q(c, "SELECT rx_receive_transfer(%s, %s, %s::jsonb, %s, 'Nurse', %s)", w["clinic"], t,
              json.dumps([{"line_id": line, "received_units": got, "discrepancy_reason": reason}]), actor, controlled)


def start(c, w, store=None, scope="full", freeze=False, actor=A, rack=None, items=None):
    return _q(c, "SELECT rx_start_audit(%s, %s, %s, %s, %s::uuid[], %s, %s, 'Manager')", w["clinic"], store or w["central"],
              scope, rack, items, freeze, actor)


def counts(c, w, audit, rows):
    return _q(c, "SELECT rx_record_counts(%s, %s, %s::jsonb)", w["clinic"], audit,
              json.dumps([{"batch_id": b, "counted_units": n} for b, n in rows]))


def post(c, w, audit, reasons=None, actor=A, approver=None):
    if reasons is None:
        reasons = [{"line_id": str(i), "variance_reason": "breakage"} for i in
                   _q(c, "SELECT COALESCE(array_agg(id::text), '{}') FROM rx_stock_audit_lines WHERE audit_id = %s "
                         "AND variance_units <> 0", audit)]
    return _q(c, "SELECT rx_post_audit(%s, %s, %s::jsonb, %s, 'Manager', %s, 'Approver')", w["clinic"], audit,
              json.dumps(reasons), actor, approver)


# ── transfers ────────────────────────────────────────────────────────────────

def test_transfer_lifecycle_and_short_writeoff(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    assert "rx_write_via_function_only" in _err(c, "INSERT INTO rx_transfers (clinic_id, from_store_id, to_store_id) "
                                                   "VALUES (%s, %s, %s)", w["clinic"], w["central"], w["tray"])
    t = draft(c, w, 10)
    assert draft(c, w, 12, tid=t) == t                              # a draft is replaced in place
    assert _q(c, "SELECT sent_units FROM rx_transfer_lines WHERE transfer_id = %s", t) == 12
    assert "rx_transfer_same_store" in _err(c, "SELECT rx_save_transfer(%s, NULL, %s, %s, %s::jsonb, NULL, 'manual', "
                                                     "NULL, 'K')", w["clinic"], w["central"], w["central"],
                                                  json.dumps([{"item_id": w["item"], "batch_id": w["batch"], "sent_units": 1}]))
    with pytest.raises(psycopg2.Error, match="rx_transfer_not_dispatched"):
        receive(c, w, t, 12)
    out = dispatch(c, w, t)
    assert out["transfer_number"] == f"STR-{date.today().year}-00001" and out["replayed"] is False
    assert dispatch(c, w, t)["replayed"] is True
    assert (_bal(c, w["central"], w["batch"]), _bal(c, w["tray"], w["batch"])) == (88, 0)
    with pytest.raises(psycopg2.Error, match="rx_document_locked"):
        draft(c, w, 1, tid=t)
    with pytest.raises(psycopg2.Error, match="rx_discrepancy_reason_required"):
        receive(c, w, t, 10)
    got = receive(c, w, t, 10, "Strip torn in transit")
    assert (got["status"], got["short_units"]) == ("partially_received", 2)
    assert receive(c, w, t, 10, "again")["replayed"] is True
    # the source was debited once (at dispatch); the 2 short units are on paper back in and out as a write-off
    assert (_bal(c, w["central"], w["batch"]), _bal(c, w["tray"], w["batch"])) == (88, 10)
    moves = _q(c, "SELECT json_agg(json_build_array(movement_type, qty_units, store_id = %s) ORDER BY created_at) "
                  "FROM rx_stock_movements WHERE ref_table = 'rx_transfer_lines' AND clinic_id = %s", w["central"], w["clinic"])
    assert moves == [["transfer_out", -12, True], ["transfer_in", 10, False], ["transfer_in", 2, True],
                     ["damage_writeoff_out", -2, True]]
    assert "rx_write_via_function_only" in _err(c, "UPDATE rx_transfers SET status = 'draft' WHERE id = %s", t)


def test_blocked_batch_never_leaves_and_cancel_is_draft_only(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    t = draft(c, w)
    _q(c, "UPDATE rx_batches SET is_blocked = true, block_reason = 'Recall notice' WHERE id = %s", w["batch"])
    with pytest.raises(psycopg2.Error, match="rx_batch_blocked"):
        dispatch(c, w, t)
    assert _q(c, "SELECT status FROM rx_transfers WHERE id = %s", t) == "draft"          # nothing half-done
    out = _q(c, "SELECT rx_cancel_transfer(%s, %s, 'Recalled batch')", w["clinic"], t)
    assert out["status"] == "cancelled"
    with pytest.raises(psycopg2.Error, match="rx_document_locked"):
        _q(c, "SELECT rx_cancel_transfer(%s, %s, 'again')", w["clinic"], t)


def test_controlled_transfer_two_person(real_pg_conn):
    c = real_pg_conn
    w = world(c, schedule="narcotic")
    t = draft(c, w, 5)
    with pytest.raises(psycopg2.Error, match="rx_controlled_permission"):
        dispatch(c, w, t, controlled=False)
    assert dispatch(c, w, t, controlled=True)["controlled"] is True
    with pytest.raises(psycopg2.Error, match="rx_controlled_permission"):
        receive(c, w, t, 5, actor=B, controlled=False)
    with pytest.raises(psycopg2.Error, match="rx_controlled_two_person"):
        receive(c, w, t, 5, actor=A, controlled=True)
    assert receive(c, w, t, 5, actor=B, controlled=True)["status"] == "received"
    assert _q(c, "SELECT (has_controlled, received_by::text) FROM rx_transfers WHERE id = %s", t) == f"(t,{B})"


# ── audits ───────────────────────────────────────────────────────────────────

def test_audit_variance_with_concurrent_movements(real_pg_conn):
    c = real_pg_conn
    w = world(c, stock=100)
    a = start(c, w)
    assert a["audit_number"] == f"AUD-{date.today().year}-00001" and a["lines"] == 1
    _move(c, w["clinic"], w["central"], w["batch"], w["item"], -3, "sale_out")          # a sale while counting
    assert _q(c, "SELECT movement_delta_since_start FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"]) == -3
    counts(c, w, a["audit_id"], [(w["batch"], 95)])
    assert counts(c, w, a["audit_id"], [(w["batch"], 96)])["counted"] == 1             # re-sent: the count is SET
    assert _q(c, "SELECT variance_units FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"]) == -1   # 96 − 100 − (−3)
    _q(c, "SELECT rx_review_audit(%s, %s)", w["clinic"], a["audit_id"])
    with pytest.raises(psycopg2.Error, match="rx_audit_not_counting"):
        counts(c, w, a["audit_id"], [(w["batch"], 1)])
    with pytest.raises(psycopg2.Error, match="rx_variance_reason_required"):
        post(c, w, a["audit_id"], reasons=[])
    out = post(c, w, a["audit_id"])
    assert (out["adjust_out"], out["variance_value_paise"]) == (1, 300)                # 1 unit × ₹3.00 landed
    assert _bal(c, w["central"], w["batch"]) == 96                                      # the shelf, now the system
    assert _q(c, "SELECT movement_delta_since_start FROM rx_stock_audit_lines WHERE audit_id = %s", a["audit_id"]) == -3
    assert post(c, w, a["audit_id"])["replayed"] is True


def test_found_stock_scope_and_uncounted(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    other = _item(c, w["clinic"], _generic(c, w["clinic"], "Cetirizine", "10 mg"), "CTZ10", "Okacet", rack_location="B-2")
    ob = _batch(c, w["clinic"], other, "O1")
    a = start(c, w, scope="rack", rack="A-1")
    with pytest.raises(psycopg2.Error, match="rx_audit_out_of_scope"):
        counts(c, w, a["audit_id"], [(ob, 4)])
    with pytest.raises(psycopg2.Error, match="rx_audit_uncounted:1"):
        _q(c, "SELECT rx_review_audit(%s, %s)", w["clinic"], a["audit_id"])
    with pytest.raises(psycopg2.Error, match="rx_audit_already_open"):
        start(c, w)
    _q(c, "SELECT rx_cancel_audit(%s, %s, 'Wrong rack')", w["clinic"], a["audit_id"])
    full = start(c, w)
    out = counts(c, w, full["audit_id"], [(w["batch"], 100), (ob, 4)])                 # 4 strips nobody received
    assert (out["added"], out["uncounted"]) == (1, 0)
    _q(c, "SELECT rx_review_audit(%s, %s)", w["clinic"], full["audit_id"])
    assert post(c, w, full["audit_id"], [{"line_id": str(_q(c, "SELECT id FROM rx_stock_audit_lines WHERE batch_id = %s",
                                                          ob)), "variance_reason": "found"}])["adjust_in"] == 1
    assert _bal(c, w["central"], ob) == 4


def test_freeze_blocks_sales_until_cancelled(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    a = start(c, w, freeze=True)
    with pytest.raises(psycopg2.Error, match="rx_store_frozen_for_audit"):
        _move(c, w["clinic"], w["central"], w["batch"], w["item"], -1, "sale_out")
    with pytest.raises(psycopg2.Error, match="rx_store_frozen_for_audit"):
        _move(c, w["clinic"], w["central"], w["batch"], w["item"], -1, "ipd_dispense_out")
    _move(c, w["clinic"], w["tray"], w["batch"], w["item"], 5, "opening")             # another store is not frozen
    _move(c, w["clinic"], w["tray"], w["batch"], w["item"], -1, "sale_out")
    _q(c, "SELECT rx_cancel_audit(%s, %s, 'Count postponed')", w["clinic"], a["audit_id"])
    _move(c, w["clinic"], w["central"], w["batch"], w["item"], -1, "sale_out")
    assert _bal(c, w["central"], w["batch"]) == 99


def test_second_approver_above_threshold(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    _q(c, "UPDATE clinics SET rx_settings = '{\"audit_approval_paise\": 1000}'::jsonb WHERE id = %s", w["clinic"])
    a = start(c, w, actor=A)["audit_id"]
    counts(c, w, a, [(w["batch"], 90)])                                                 # −10 × ₹3 = ₹30 > ₹10
    _q(c, "SELECT rx_review_audit(%s, %s)", w["clinic"], a)
    for approver in (None, A):                                                          # nobody, or the starter
        with pytest.raises(psycopg2.Error, match="rx_audit_needs_approver"):
            post(c, w, a, actor=B, approver=approver)
    with pytest.raises(psycopg2.Error, match="rx_audit_needs_approver"):
        post(c, w, a, actor=B, approver=B)                                              # nor the poster
    third = str(uuid.uuid4())
    out = post(c, w, a, actor=B, approver=third)
    assert out["variance_value_paise"] == 3000 and out["approved_by"] == third
    assert "rx_stock_audits_second_person" in _err(c, "SELECT set_config('kriya.rx_tx', %s, true); "
                                                      "UPDATE rx_stock_audits SET approved_by = started_by WHERE id = %s",
                                                   w["clinic"], a)


def test_par_levels_set_and_remove(real_pg_conn):
    c = real_pg_conn
    w = world(c)
    out = _q(c, "SELECT rx_set_par_levels(%s, %s, %s::jsonb, %s)", w["clinic"], w["tray"],
             json.dumps([{"item_id": w["item"], "par_units": 30}]), A)
    assert out == {"set": 1, "removed": 0}
    _q(c, "SELECT rx_set_par_levels(%s, %s, %s::jsonb, %s)", w["clinic"], w["tray"],
       json.dumps([{"item_id": w["item"], "par_units": 40}]), A)
    assert _q(c, "SELECT par_units FROM rx_par_levels WHERE store_id = %s", w["tray"]) == 40
    _q(c, "SELECT rx_set_par_levels(%s, %s, %s::jsonb, %s)", w["clinic"], w["tray"],
       json.dumps([{"item_id": w["item"], "par_units": 0}]), A)
    assert _q(c, "SELECT count(*) FROM rx_par_levels WHERE store_id = %s", w["tray"]) == 0


def test_purge_v7_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, b = world(c), world(c)
    for w in (a, b):
        dispatch(c, w, draft(c, w))
        start(c, w)
        _q(c, "SELECT rx_set_par_levels(%s, %s, %s::jsonb, NULL)", w["clinic"], w["tray"],
           json.dumps([{"item_id": w["item"], "par_units": 5}]))
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert (out["transfers"], out["audits"]) == (1, 1)
    assert _q(c, "SELECT count(*) FROM rx_transfers WHERE clinic_id = %s", b["clinic"]) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "140_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_140(cur)
        cur.execute("SELECT to_regclass('public.rx_transfers'), (SELECT count(*) FROM pg_trigger "
                    "WHERE tgname LIKE 'trg_rx_sm_audit%%')")
        assert cur.fetchone() == (None, 0)
        cur.execute("SELECT prosrc LIKE '%%rx_mar_links%%' AND prosrc NOT LIKE '%%rx_transfers%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                            # purge v6 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_m7_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_guard_fn_only', 'rx_sm_audit_freeze', 'rx_sm_audit_delta',
                                                 'rx_save_transfer', 'rx_dispatch_transfer', 'rx_receive_transfer',
                                                 'rx_cancel_transfer', 'rx_set_par_levels', 'rx_audit_in_scope',
                                                 'rx_start_audit', 'rx_record_counts', 'rx_review_audit',
                                                 'rx_post_audit', 'rx_cancel_audit')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
