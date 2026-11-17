"""Migration 137 (Pharmacy OS purchase, RX-M4) against real PostgreSQL.

GRN line maths (TEST-PLAN §4.3 vectors, with and without GSTIN), freight split, IGST;
PO send numbering and draft-only edits; GRN posting (batches, grn_in movements, PO
received / status, idempotent, total within ±99 paise, duplicate supplier invoice,
expired batch refused, posted GRN locked); void only while stock is unconsumed;
supplier return of an expired batch with debit-note rate from its GRN line; purge v4;
guarded rollback + idempotent re-apply.
"""

import uuid
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "137_rx_purchase.sql", MIG / "rollback" / "137_rollback_rx_purchase.sql"
GSTIN = "36AABCU9603R1ZM"


def unwind_137(cur) -> None:
    """Inside an open transaction: purge every clinic with purchase rows and run the 137 rollback."""
    cur.execute("SELECT to_regclass('public.rx_suppliers') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_suppliers UNION "
                "SELECT clinic_id FROM rx_grns UNION SELECT clinic_id FROM rx_purchase_orders) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _world(c, gstin=GSTIN):
    clinic = _clinic(c)
    if gstin:
        _q(c, "UPDATE clinics SET opd_settings = COALESCE(opd_settings, '{}'::jsonb) || jsonb_build_object('gstin', %s) "
              "WHERE id = %s", gstin, clinic)
    store = _store(c, clinic, "CENTRAL", "central")
    _q(c, "UPDATE rx_stores SET can_receive_grn = true WHERE id = %s", store)
    item = _item(c, clinic, _generic(c, clinic))                         # pack_size 10
    supplier = str(_q(c, "INSERT INTO rx_suppliers (clinic_id, name, gstin, state_code) VALUES (%s, 'Sri Balaji Pharma', "
                         "'36AAACB1234F1Z5', '36') RETURNING id", clinic))
    return {"clinic": clinic, "store": store, "item": item, "supplier": supplier}


def _grn(c, w, invoice="INV-1", po=None, freight=0, total=0, interstate=False):
    return str(_q(c, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, po_id, supplier_invoice_no, "
                     "supplier_invoice_date, freight_paise, invoice_total_paise, is_interstate) "
                     "VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, %s, %s, %s) RETURNING id",
                  w["clinic"], w["store"], w["supplier"], po, invoice, freight, total, interstate))


def _line(c, w, grn, n=1, batch="B1", packs=10, free=2, rate=10000, sch=1000, cd=200, tax=500, mrp=15000,
          expiry=None, po_line=None, item=None):
    return str(_q(c, "INSERT INTO rx_grn_lines (clinic_id, grn_id, line_no, item_id, po_line_id, batch_no, expiry_date, "
                     "packs, free_packs, rate_pack_paise, scheme_discount_bps, cash_discount_bps, tax_rate_bps, "
                     "mrp_pack_paise) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                  w["clinic"], grn, n, item or w["item"], po_line, batch, expiry or date.today() + timedelta(days=500),
                  packs, free, rate, sch, cd, tax, mrp))


def _row(c, table, rid):
    return _q(c, f"SELECT row_to_json(t) FROM {table} t WHERE id = %s", rid)


def _post(c, w, grn):
    return _q(c, "SELECT rx_post_grn(%s, %s, NULL, 'Ravi')", w["clinic"], grn)


def test_landed_cost_vectors_with_and_without_gstin(real_pg_conn):
    c = real_pg_conn
    with_gst = _world(c)
    line = _row(c, "rx_grn_lines", _line(c, with_gst, _grn(c, with_gst)))
    assert (line["taxable_paise"], line["tax_paise"], line["landed_unit_cost_paise_x100"]) == (88200, 4410, 73500)
    no_gst = _world(c, gstin=None)
    line = _row(c, "rx_grn_lines", _line(c, no_gst, _grn(c, no_gst)))
    assert line["landed_unit_cost_paise_x100"] == 77175                  # GST is cost without input credit
    # header totals follow the lines; intra-state splits CGST / SGST
    g = _row(c, "rx_grns", _q(c, "SELECT grn_id FROM rx_grn_lines WHERE id = %s", line["id"]))
    assert (g["taxable_paise"], g["cgst_paise"], g["sgst_paise"], g["igst_paise"]) == (88200, 2205, 2205, 0)


def test_interstate_igst_and_freight_split(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    grn = _grn(c, w, interstate=True, freight=1001)
    a = _line(c, w, grn, 1, "A1", packs=1, free=0, rate=10000, sch=0, cd=0, tax=1200)
    b = _line(c, w, grn, 2, "A2", packs=2, free=0, rate=10000, sch=0, cd=0, tax=1200)
    g = _row(c, "rx_grns", grn)
    assert (g["igst_paise"], g["cgst_paise"], g["sgst_paise"]) == (3600, 0, 0)
    shares = [_row(c, "rx_grn_lines", x)["freight_share_paise"] for x in (a, b)]
    assert shares == [334, 667] and sum(shares) == 1001                   # largest remainder, nothing lost
    assert _row(c, "rx_grn_lines", a)["landed_unit_cost_paise_x100"] == (10334 * 100 + 5) // 10
    _q(c, "UPDATE rx_grns SET is_interstate = false, freight_paise = 0 WHERE id = %s", grn)
    g = _row(c, "rx_grns", grn)
    assert (g["igst_paise"], g["cgst_paise"] + g["sgst_paise"]) == (0, 3600)
    assert _row(c, "rx_grn_lines", b)["freight_share_paise"] == 0


def test_po_send_numbering_and_draft_only_edits(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    po = str(_q(c, "INSERT INTO rx_purchase_orders (clinic_id, store_id, supplier_id) VALUES (%s, %s, %s) RETURNING id",
                w["clinic"], w["store"], w["supplier"]))
    with pytest.raises(psycopg2.Error, match="rx_po_empty"):
        _q(c, "SELECT rx_send_po(%s, %s)", w["clinic"], po)
    pl = str(_q(c, "INSERT INTO rx_po_lines (clinic_id, po_id, line_no, item_id, ordered_packs) VALUES (%s, %s, 1, %s, 10) "
                   "RETURNING id", w["clinic"], po, w["item"]))
    first = _q(c, "SELECT rx_send_po(%s, %s)", w["clinic"], po)
    year = date.today().year
    assert first == {"po_number": f"PO-{year}-00001", "replayed": False}
    assert _q(c, "SELECT rx_send_po(%s, %s)", w["clinic"], po)["replayed"] is True
    assert "rx_document_locked" in _err(c, "UPDATE rx_po_lines SET ordered_packs = 99 WHERE id = %s", pl)
    assert "rx_document_locked" in _err(c, "UPDATE rx_purchase_orders SET notes = 'x' WHERE id = %s", po)
    assert "rx_document_locked" in _err(c, "INSERT INTO rx_po_lines (clinic_id, po_id, line_no, item_id, ordered_packs) "
                                           "VALUES (%s, %s, 2, %s, 1)", w["clinic"], po, w["item"])
    assert "rx_po_cancel_reason" in _err(c, "UPDATE rx_purchase_orders SET status = 'cancelled' WHERE id = %s", po)
    # receive 6 of 10 → partially received; then the rest → received
    g1 = _grn(c, w, "P-1", po, total=0)
    _line(c, w, g1, packs=6, free=0, sch=0, cd=0, po_line=pl)
    _q(c, "UPDATE rx_grns SET invoice_total_paise = taxable_paise + cgst_paise + sgst_paise WHERE id = %s", g1)
    _post(c, w, g1)
    assert _q(c, "SELECT status FROM rx_purchase_orders WHERE id = %s", po) == "partially_received"
    g2 = _grn(c, w, "P-2", po)
    _line(c, w, g2, batch="B2", packs=4, free=0, sch=0, cd=0, po_line=pl)
    _q(c, "UPDATE rx_grns SET invoice_total_paise = taxable_paise + cgst_paise + sgst_paise WHERE id = %s", g2)
    _post(c, w, g2)
    assert _q(c, "SELECT status FROM rx_purchase_orders WHERE id = %s", po) == "received"
    assert _q(c, "SELECT received_packs FROM rx_po_lines WHERE id = %s", pl) == 10
    # a GRN line must belong to the GRN's PO and item
    other_po = str(_q(c, "INSERT INTO rx_purchase_orders (clinic_id, store_id, supplier_id) VALUES (%s, %s, %s) "
                         "RETURNING id", w["clinic"], w["store"], w["supplier"]))
    with pytest.raises(psycopg2.Error, match="rx_grn_po_line_mismatch"):
        _line(c, w, _grn(c, w, "P-3", other_po), po_line=pl)


def test_post_grn_creates_batches_and_stock_idempotently(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    grn = _grn(c, w, total=92610)                                         # 88200 + 4410 = 92610
    line = _line(c, w, grn)
    out = _post(c, w, grn)
    assert out["grn_number"] == f"GRN-{date.today().year}-00001" and out["units"] == 120 and out["round_off_paise"] == 0
    assert _post(c, w, grn)["replayed"] is True
    batch = _q(c, "SELECT batch_id FROM rx_grn_lines WHERE id = %s", line)
    b = _row(c, "rx_batches", batch)
    assert (b["landed_unit_cost_paise_x100"], b["mrp_pack_paise"], b["ptr_pack_paise"]) == (73500, 15000, 10000)
    assert str(b["first_grn_line_id"]) == line
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", batch) == 120
    assert _q(c, "SELECT movement_type || ':' || qty_units FROM rx_stock_movements WHERE batch_id = %s", batch) == "grn_in:120"
    # a posted GRN and its lines are frozen
    assert "rx_document_locked" in _err(c, "UPDATE rx_grns SET freight_paise = 5 WHERE id = %s", grn)
    assert "rx_document_locked" in _err(c, "UPDATE rx_grn_lines SET packs = 1 WHERE id = %s", line)
    assert "rx_document_locked" in _err(c, "DELETE FROM rx_grns WHERE id = %s", grn)
    # the same batch again (same MRP) tops up the same batch row
    g2 = _grn(c, w, "INV-2", total=92610)
    _line(c, w, g2)
    _post(c, w, g2)
    assert _q(c, "SELECT count(*) FROM rx_batches WHERE clinic_id = %s", w["clinic"]) == 1
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", batch) == 240


def test_post_guards(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    _grn(c, w, "DUP")
    assert "rx_grns_invoice_key" in _err(c, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, supplier_invoice_no, "
                                            "supplier_invoice_date) VALUES (%s, %s, %s, 'DUP', CURRENT_DATE)",
                                         w["clinic"], w["store"], w["supplier"])
    with pytest.raises(psycopg2.Error, match="rx_grn_expired_batch"):
        _line(c, w, _grn(c, w, "EXP"), expiry=date.today() - timedelta(days=1))
    with pytest.raises(psycopg2.Error, match="rx_grn_empty"):
        _post(c, w, _grn(c, w, "EMPTY"))
    grn = _grn(c, w, "OFF", total=92610 + 100)                            # ₹1.00 off
    _line(c, w, grn)
    with pytest.raises(psycopg2.Error, match=r"rx_grn_total_mismatch:100"):
        _post(c, w, grn)
    assert _q(c, "SELECT count(*) FROM rx_stock_movements WHERE clinic_id = %s", w["clinic"]) == 0
    _q(c, "UPDATE rx_grns SET invoice_total_paise = 92610 - 99 WHERE id = %s", grn)
    assert _post(c, w, grn)["round_off_paise"] == -99                     # within ±99 paise: the round-off
    counter = _store(c, w["clinic"], "OPD1", "ot_tray")
    g = str(_q(c, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, supplier_invoice_no, supplier_invoice_date) "
                  "VALUES (%s, %s, %s, 'TRAY', CURRENT_DATE) RETURNING id", w["clinic"], counter, w["supplier"]))
    _line(c, w, g, sch=0, cd=0, free=0, rate=0, tax=0)
    with pytest.raises(psycopg2.Error, match="rx_store_cannot_receive"):
        _post(c, w, g)
    with pytest.raises(psycopg2.Error, match="rx_grn_not_found"):
        _q(c, "SELECT rx_post_grn(%s, %s, NULL, 'x')", _clinic(c, "Other"), grn)


def _move(c, clinic, store, batch, item, qty, mtype):
    _q(c, "SELECT set_config('kriya.rx_tx', %s, true); INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, "
          "item_id, qty_units, movement_type, ref_table, ref_id, unit_cost_paise_x100, actor_name) "
          "VALUES (%s, %s, %s, %s, %s, %s, 'test', %s, 0, 'T')", clinic, clinic, store, batch, item, qty, mtype,
       str(uuid.uuid4()))


def test_void_only_while_stock_unconsumed(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    grn = _grn(c, w, total=92610)
    line = _line(c, w, grn)
    _post(c, w, grn)
    batch = _q(c, "SELECT batch_id FROM rx_grn_lines WHERE id = %s", line)
    _move(c, w["clinic"], w["store"], batch, w["item"], -1, "sale_out")
    with pytest.raises(psycopg2.Error, match="rx_grn_stock_consumed"):
        _q(c, "SELECT rx_void_grn(%s, %s, 'Wrong supplier', NULL, 'M')", w["clinic"], grn)
    _move(c, w["clinic"], w["store"], batch, w["item"], 1, "sale_return_in")
    with pytest.raises(psycopg2.Error, match="rx_reason_required"):
        _q(c, "SELECT rx_void_grn(%s, %s, '', NULL, 'M')", w["clinic"], grn)
    _q(c, "SELECT rx_void_grn(%s, %s, 'Wrong supplier', NULL, 'M')", w["clinic"], grn)
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", batch) == 0
    assert _q(c, "SELECT status || ':' || void_reason FROM rx_grns WHERE id = %s", grn) == "void:Wrong supplier"
    assert _q(c, "SELECT rx_void_grn(%s, %s, 'Again', NULL, 'M')", w["clinic"], grn)["replayed"] is True


def test_supplier_return_of_expired_batch(real_pg_conn):
    c = real_pg_conn
    w = _world(c)
    grn = _grn(c, w, total=92610)
    line = _line(c, w, grn)
    _post(c, w, grn)
    batch = _q(c, "SELECT batch_id FROM rx_grn_lines WHERE id = %s", line)
    _q(c, "UPDATE rx_batches SET is_blocked = true, block_reason = 'Recall 12/26' WHERE id = %s", batch)
    srn = str(_q(c, "INSERT INTO rx_supplier_returns (clinic_id, store_id, supplier_id, reason) VALUES (%s, %s, %s, "
                    "'recall') RETURNING id", w["clinic"], w["store"], w["supplier"]))
    rl = str(_q(c, "INSERT INTO rx_supplier_return_lines (clinic_id, return_id, batch_id, item_id, qty_units, "
                   "rate_pack_paise, tax_rate_bps) VALUES (%s, %s, %s, %s, 50, 0, 0) RETURNING id",
                w["clinic"], srn, batch, str(uuid.uuid4())))
    filled = _row(c, "rx_supplier_return_lines", rl)
    assert (filled["item_id"], filled["rate_pack_paise"], filled["tax_rate_bps"]) == (w["item"], 10000, 500)
    assert "rx_srn_credit_after_dispatch" in _err(c, "UPDATE rx_supplier_returns SET credit_ref = 'CN-1' WHERE id = %s", srn)
    out = _q(c, "SELECT rx_supplier_return(%s, %s, NULL, 'Ravi')", w["clinic"], srn)
    assert out["srn_number"] == f"SRN-{date.today().year}-00001" and out["units"] == 50
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", batch) == 70
    _q(c, "UPDATE rx_supplier_returns SET credit_received_paise = 52500, credit_ref = 'CN/88' WHERE id = %s", srn)
    assert "rx_document_locked" in _err(c, "UPDATE rx_supplier_returns SET reason = 'other' WHERE id = %s", srn)
    assert "rx_document_locked" in _err(c, "UPDATE rx_supplier_return_lines SET qty_units = 1 WHERE id = %s", rl)
    srn2 = str(_q(c, "INSERT INTO rx_supplier_returns (clinic_id, store_id, supplier_id, reason) VALUES (%s, %s, %s, "
                     "'excess') RETURNING id", w["clinic"], w["store"], w["supplier"]))
    _q(c, "INSERT INTO rx_supplier_return_lines (clinic_id, return_id, batch_id, item_id, qty_units, rate_pack_paise, "
          "tax_rate_bps) VALUES (%s, %s, %s, %s, 71, 0, 0)", w["clinic"], srn2, batch, w["item"])
    with pytest.raises(psycopg2.Error, match="rx_insufficient_stock"):
        _q(c, "SELECT rx_supplier_return(%s, %s, NULL, 'Ravi')", w["clinic"], srn2)


def test_cross_tenant_references_impossible(real_pg_conn):
    c = real_pg_conn
    a, b = _world(c), _world(c)
    assert "rx_grns_supplier_fk" in _err(c, "INSERT INTO rx_grns (clinic_id, store_id, supplier_id, supplier_invoice_no, "
                                            "supplier_invoice_date) VALUES (%s, %s, %s, 'X', CURRENT_DATE)",
                                         a["clinic"], a["store"], b["supplier"])
    with pytest.raises(psycopg2.Error, match="rx_grn_lines_item_fk"):
        _line(c, a, _grn(c, a, "Y"), item=b["item"])
    assert "rx_po_store_fk" in _err(c, "INSERT INTO rx_purchase_orders (clinic_id, store_id, supplier_id) "
                                       "VALUES (%s, %s, %s)", a["clinic"], b["store"], a["supplier"])
    assert "rx_suppliers_gstin_check" in _err(c, "INSERT INTO rx_suppliers (clinic_id, name, gstin) VALUES (%s, 'Bad', "
                                                 "'36aaacb1234f1z5')", a["clinic"])
    assert "uq_rx_suppliers_gstin" in _err(c, "INSERT INTO rx_suppliers (clinic_id, name, gstin, state_code) VALUES "
                                              "(%s, 'Dup', '36AAACB1234F1Z5', '36')", a["clinic"])


def test_purge_v4_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, b = _world(c), _world(c)
    for w in (a, b):
        g = _grn(c, w, total=92610)
        _line(c, w, g)
        _post(c, w, g)
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert (out["grns"], out["movements"]) == (1, 1)
    assert _q(c, "SELECT count(*) FROM rx_suppliers WHERE clinic_id = %s", a["clinic"]) == 0
    assert _q(c, "SELECT count(*) FROM rx_grns WHERE clinic_id = %s", b["clinic"]) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "137_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_137(cur)
        cur.execute("SELECT to_regclass('public.rx_grns'), to_regclass('public.rx_batches') IS NOT NULL")
        assert cur.fetchone() == (None, True)
        cur.execute("SELECT prosrc LIKE '%%rx_batches%%' AND prosrc NOT LIKE '%%rx_grns%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                                 # purge v3 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_purchase_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_guard_purchase_doc', 'rx_guard_purchase_line', 'rx_grn_line_calc',
                                                 'rx_grn_refresh', 'rx_grn_lines_after', 'rx_grn_header_after',
                                                 'rx_srn_line_fill', 'rx_send_po', 'rx_po_refresh_status',
                                                 'rx_post_grn', 'rx_void_grn', 'rx_supplier_return')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
