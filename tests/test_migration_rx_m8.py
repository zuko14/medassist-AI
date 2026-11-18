"""Migration 141 (Pharmacy OS statutory registers, RX-M8) against real PostgreSQL.

Every ledger movement of a scheduled item appends a register row at commit: GRN in (supplier), counter sale out
(patient + prescriber), IPD dispense out (patient + ordering doctor), transfers, audit variances; unscheduled items
write nothing. seq is gapless per clinic × register; the hash chain verifies, and a single mutated / removed row is
reported at its seq. Rows are function-only and append-only. Narcotic empties need two people; the batch recall
trace covers sales (phone masked), IPD (ward, bed, MAR) and transfers. Purge v8; guarded rollback + re-apply.
HTTP behaviour: tests/test_rx_registers.py.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _err, _q
from tests.test_migration_rx_m4 import _grn, _line, _post, _world
from tests.test_migration_rx_m5 import complete, draft as sale_draft, line as sale_line, pos_world, signed_rx
from tests.test_migration_rx_m6 import dispense, ipd_world, raise_indent
from tests.test_migration_rx_m7 import A, B, dispatch, draft as transfer_draft, receive, world as transfer_world

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "141_rx_registers.sql", MIG / "rollback" / "141_rollback_rx_registers.sql"


def unwind_141(cur) -> None:
    """Inside an open transaction: purge every clinic with register entries and run the 141 rollback."""
    cur.execute("SELECT to_regclass('public.rx_register_entries') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")      # register appends still pending in this transaction
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM rx_register_entries) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def entries(c, clinic, register=None):
    return _q(c, "SELECT COALESCE(json_agg(to_jsonb(e) - 'row_hash' - 'prev_hash' ORDER BY register, seq), '[]') "
                 "FROM rx_register_entries e WHERE clinic_id = %s AND (%s::text IS NULL OR register = %s)",
              clinic, register, register)


def verify(c, clinic, register):
    return _q(c, "SELECT json_build_array(ok, verified_rows, first_bad_seq) FROM rx_register_verify(%s, %s)",
              clinic, register)


def schedule(c, item, sched):
    _q(c, "UPDATE rx_items SET schedule = %s WHERE id = %s", sched, item)


# ── wiring: each writer appends the right row ────────────────────────────────

def test_grn_in_with_supplier_and_unscheduled_items_write_nothing(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    g = _grn(c, w, total=92610)
    _line(c, w, g)
    _post(c, w, g)
    assert entries(c, w["clinic"]) == []                            # schedule 'none': no register
    w2 = _world(c)
    schedule(c, w2["item"], "H1")
    g2 = _grn(c, w2, "INV-77", total=92610)
    _line(c, w2, g2)
    _post(c, w2, g2)
    [e] = entries(c, w2["clinic"])
    assert (e["register"], e["seq"], e["direction"], e["movement_type"], e["qty_units"], e["balance_after_units"]) == \
        ("H1", 1, "in", "grn_in", 120, 120)
    assert e["supplier_snapshot"]["name"] == "Sri Balaji Pharma" and e["supplier_snapshot"]["invoice_no"] == "INV-77"
    assert e["doc_ref"].startswith("GRN-") and e["dispensed_by_name"] == "Ravi"
    assert e["patient_snapshot"] is None and e["prescriber_snapshot"] is None


def test_counter_sale_out_has_patient_and_prescriber_never_a_phone(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c, schedule="H1")
    _q(c, "UPDATE clinics SET rx_settings = rx_settings || '{\"pharmacist_reg_no\": \"TSPC/1234\"}' WHERE id = %s", w["clinic"])
    rx = signed_rx(c, w["clinic"])
    s = sale_draft(c, w, "opd_rx", rx["rx"], rx["patient"],
                   snapshot={"name": "Dr. Meena", "registration_number": "TSMC/2012/777", "registration_council": "TSMC"})
    sale_line(c, w, s, 10, rx_item=rx["rx_items"][0])
    complete(c, w, s, [{"mode": "upi", "amount_paise": 12000}])
    opening, out = entries(c, w["clinic"], "H1")
    assert (opening["seq"], opening["direction"], opening["balance_after_units"]) == (1, "in", 100)
    assert (out["seq"], out["direction"], out["qty_units"], out["balance_after_units"]) == (2, "out", 10, 90)
    assert out["patient_snapshot"]["name"] == "Lakshmi Devi" and out["patient_snapshot"]["age"] == 54
    assert out["prescriber_snapshot"] == {"name": "Dr. Meena", "registration_number": "TSMC/2012/777",
                                          "council": "TSMC", "address": None}
    assert out["doc_ref"].startswith("PHS-") and out["pharmacist_reg_no"] == "TSPC/1234"
    assert rx["phone"][-8:] not in json.dumps(entries(c, w["clinic"]))


def test_ipd_dispense_out_names_patient_admission_and_ordering_doctor(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    schedule(c, w["item"], "H1")
    dispense(c, w, raise_indent(c, w)["indent_id"])
    out = entries(c, w["clinic"], "H1")[-1]
    assert (out["direction"], out["movement_type"], out["qty_units"], out["balance_after_units"]) == \
        ("out", "ipd_dispense_out", 6, 1994)
    assert out["patient_snapshot"]["name"] == "Ravi Kumar" and out["patient_snapshot"]["admission"].startswith("IPD-")
    assert (out["prescriber_snapshot"]["name"], out["prescriber_snapshot"]["registration_number"]) == ("Dr. Rao", "KMC/2015/1")
    assert out["doc_ref"].startswith("IND-")


def test_transfer_rows_per_store_with_running_balances(real_pg_conn):
    c = real_pg_conn
    w = transfer_world(c, schedule="narcotic")
    t = transfer_draft(c, w, 12)
    dispatch(c, w, t, controlled=True)
    receive(c, w, t, 10, "Ampoule broken", actor=B, controlled=True)
    rows = [(e["movement_type"], e["direction"], e["qty_units"], e["store_id"] == w["central"], e["balance_after_units"])
            for e in entries(c, w["clinic"], "narcotic")]
    assert rows == [("opening", "in", 100, True, 100), ("transfer_out", "adjust", 12, True, 88),
                    ("transfer_in", "in", 10, False, 10), ("transfer_in", "in", 2, True, 90),
                    ("damage_writeoff_out", "writeoff", 2, True, 88)]
    assert [e["seq"] for e in entries(c, w["clinic"], "narcotic")] == [1, 2, 3, 4, 5]
    assert verify(c, w["clinic"], "narcotic") == [True, 5, None]


# ── chain ────────────────────────────────────────────────────────────────────

def test_rows_are_function_only_and_append_only(real_pg_conn):
    c = real_pg_conn
    w = transfer_world(c, schedule="X")
    e = entries(c, w["clinic"])[0]
    assert "rx_append_only" in _err(c, "UPDATE rx_register_entries SET qty_units = 1 WHERE id = %s", e["id"])
    assert "rx_append_only" in _err(c, "DELETE FROM rx_register_entries WHERE id = %s", e["id"])
    assert "rx_register_via_function_only" in _err(
        c, "INSERT INTO rx_register_entries (clinic_id, register, seq, entry_date, direction, movement_type, item_id, "
           "batch_id, store_id, qty_units, balance_after_units, source_table, source_id, dispensed_by_name, row_hash) "
           "SELECT clinic_id, register, 99, entry_date, direction, movement_type, item_id, batch_id, store_id, 1, 1, "
           "source_table, source_id, 'x', row_hash FROM rx_register_entries WHERE id = %s", e["id"])


def test_tamper_detected_at_the_mutated_seq(real_pg_conn):
    c = real_pg_conn
    w = transfer_world(c, schedule="X")
    for _ in range(3):
        dispatch(c, w, transfer_draft(c, w, 5), controlled=True)
    assert verify(c, w["clinic"], "X") == [True, 4, None]
    assert verify(c, w["clinic"], "H1") == [True, 0, None]           # an empty register is a valid chain
    cur = c.cursor()
    for tamper in ("UPDATE rx_register_entries SET qty_units = 50 WHERE clinic_id = %s AND seq = 3",
                   "UPDATE rx_register_entries SET patient_snapshot = '{\"name\": \"Someone\"}' WHERE clinic_id = %s AND seq = 3",
                   "DELETE FROM rx_register_entries WHERE clinic_id = %s AND seq = 3"):
        cur.execute("BEGIN")
        try:   # a DBA bypassing the guards (replica role skips triggers) is exactly what the chain must catch
            cur.execute("SET LOCAL session_replication_role = replica")
            cur.execute(tamper, (w["clinic"],))
            cur.execute("SELECT json_build_array(ok, verified_rows, first_bad_seq) FROM rx_register_verify(%s, 'X')",
                        (w["clinic"],))
            assert cur.fetchone()[0] == [False, 2, 3]
        finally:
            cur.execute("ROLLBACK")
    assert verify(c, w["clinic"], "X") == [True, 4, None]


def test_seq_is_per_clinic_and_register(real_pg_conn):
    c = real_pg_conn
    a, b = transfer_world(c, schedule="X"), transfer_world(c, schedule="X")
    dispatch(c, a, transfer_draft(c, a, 5), controlled=True)
    assert [e["seq"] for e in entries(c, a["clinic"], "X")] == [1, 2]
    assert [e["seq"] for e in entries(c, b["clinic"], "X")] == [1]


# ── A11 empties, A6 recall trace ─────────────────────────────────────────────

def test_narcotic_empties_two_person_and_capped(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    dispense(c, w, raise_indent(c, w)["indent_id"])
    dl = str(_q(c, "SELECT id FROM rx_dispense_lines WHERE clinic_id = %s", w["clinic"]))
    empties = "SELECT rx_record_empties(%s, %s, %s, %s, %s)"
    assert "rx_empties_not_controlled" in _err(c, empties, w["clinic"], dl, 1, A, B)
    schedule(c, w["item"], "narcotic")
    assert "rx_empties_two_person" in _err(c, empties, w["clinic"], dl, 1, A, A)
    assert "rx_dispense_line_not_found" in _err(c, empties, w["clinic"], str(uuid.uuid4()), 1, A, B)
    out = _q(c, empties, w["clinic"], dl, 4, A, B)
    assert (out["empties_returned_units"], out["outstanding_units"]) == (4, 2)
    assert "rx_empties_exceed_dispensed" in _err(c, empties, w["clinic"], dl, 3, A, B)
    assert _q(c, empties, w["clinic"], dl, 2, B, A)["outstanding_units"] == 0


def test_batch_trace_covers_sale_ipd_mar_transfer_and_on_hand(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    dispense(c, w, raise_indent(c, w)["indent_id"])
    t = _q(c, "SELECT rx_batch_trace(%s, %s)", w["clinic"], w["batch"])
    [ipd] = t["ipd"]
    assert (ipd["patient_name"], ipd["ward"], ipd["bed"], ipd["qty_units"], ipd["mar_doses"]) == \
        ("Ravi Kumar", "General Ward", "B1", 6, 0)
    assert t["on_hand"] == [{"store_id": w["store"], "store_code": "CENTRAL", "store_name": "Main Pharmacy",
                             "qty_units": 1994}]
    assert _q(c, "SELECT rx_batch_trace(%s, %s)", w["clinic"], str(uuid.uuid4())) is None
    assert _q(c, "SELECT rx_batch_trace(%s, %s)", str(uuid.uuid4()), w["batch"]) is None   # another clinic's batch

    p = pos_world(c)
    rx = signed_rx(c, p["clinic"])
    s = sale_draft(c, p, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
    sale_line(c, p, s, 5, rx_item=rx["rx_items"][0])
    complete(c, p, s, [{"mode": "upi", "amount_paise": 6000}])
    [sale] = _q(c, "SELECT rx_batch_trace(%s, %s)", p["clinic"], p["batch"])["sales"]
    assert sale["patient_name"] == "Lakshmi Devi" and sale["qty_units"] == 5
    assert sale["phone_masked"] == "XXXXXX" + rx["phone"][-4:]
    assert rx["phone"][-8:] not in json.dumps(sale)

    tw = transfer_world(c)
    dispatch(c, tw, transfer_draft(c, tw, 10))
    [tr] = _q(c, "SELECT rx_batch_trace(%s, %s)", tw["clinic"], tw["batch"])["transfers"]
    assert (tr["from_store"], tr["to_store"], tr["sent_units"], tr["status"]) == ("CENTRAL", "OT1", 10, "dispatched")


# ── purge, rollback ──────────────────────────────────────────────────────────

def test_purge_v8_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, b = transfer_world(c, schedule="X"), transfer_world(c, schedule="X")
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert out["register_entries"] == 1
    assert len(entries(c, b["clinic"])) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "141_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_141(cur)
        cur.execute("SELECT to_regclass('public.rx_register_entries'), "
                    "(SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_rx_sm_register')")
        assert cur.fetchone() == (None, 0)
        cur.execute("SELECT prosrc LIKE '%%rx_par_levels%%' AND prosrc NOT LIKE '%%rx_register_entries%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                            # purge v7 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_m8_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_register_guard_insert', 'rx_register_hash', 'rx_register_append',
                                                 'rx_register_verify', 'rx_patient_snap', 'rx_sm_register',
                                                 'rx_record_empties', 'rx_batch_trace')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
