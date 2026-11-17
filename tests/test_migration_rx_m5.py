"""Migration 138 (Pharmacy OS counter sales, RX-M5) against real PostgreSQL.

GST split / loose price / closed frequency-map vectors in SQL; lines priced only from the
batch (client values ignored), discount spread + round-off; rx_complete_sale (stock, payments,
number, idempotent replay, payment mismatch, cash needs an open shift, superseded Rx, H1 hard
cap, schedule preconditions, controlled permission and external-Rx reuse); function-only and
append-only tables; returns (sold-value refund, exact remainder, restock vs write-off, credit
note, window); same-day void; opd_close_shift counts pharmacy cash (D1) and is unchanged
without it; purge v5; rollback restores the 116 opd_close_shift body; idempotent re-apply.
"""

import json
import uuid
from datetime import date
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _batch, _move

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "138_rx_pos.sql", MIG / "rollback" / "138_rollback_rx_pos.sql"
GSTIN = "36AABCU9603R1ZO"


def unwind_138(cur) -> None:
    """Inside an open transaction: purge every clinic with sales and run the 138 rollback."""
    from tests.test_migration_rx_m6 import unwind_139
    unwind_139(cur)  # newest first: 139's dispense lines point at batches / stores and its purge v6 names 138 tables
    cur.execute("SELECT to_regclass('public.rx_sales') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_sales UNION "
                "SELECT clinic_id FROM rx_overrides) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


# ── world: a GST-registered clinic, a selling counter with stock, a signed OPD Rx ──

def pos_world(c, gstin=GSTIN, schedule="none", stock=100, pack=10, mrp=12000):
    clinic = _clinic(c, "RX POS Hospital")
    _q(c, "UPDATE clinics SET opd_state = 'READY', opd_settings = COALESCE(opd_settings, '{}'::jsonb) "
          "|| jsonb_build_object('gstin', %s::text) WHERE id = %s", gstin, clinic)
    store = _store(c, clinic, "OPD1", "retail_counter")
    item = _item(c, clinic, _generic(c, clinic), "PCM650", "Dolo 650", schedule=schedule, pack_size=pack)
    batch = _batch(c, clinic, item, "B1", mrp=mrp)
    _move(c, clinic, store, batch, item, stock, "opening")
    return {"clinic": clinic, "store": store, "item": item, "batch": batch}


def signed_rx(c, clinic, lines=(("Dolo 650", "tablet", "1", "TDS", 5),), reg="TSMC/2012/777", patient_name="Lakshmi Devi",
              token=None):
    """A signed OPD prescription the way the OPD routes leave it (103 functions)."""
    doc = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, registration_number, "
                    "registration_council, is_active) VALUES (%s, 'Dr. Meena', 'Medicine', 'Physician', %s, 'TSMC', true) "
                    "RETURNING id", clinic, reg))
    phone = "+9194" + str(uuid.uuid4().int)[:8]
    pat = str(_q(c, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on) "
                    "VALUES (%s, %s, %s, 'female', 54, CURRENT_DATE) RETURNING id", clinic, patient_name, phone))
    appt = str(_q(c, "INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id, doctor_name, "
                     "department, appointment_date, appointment_time, status, queue_status, is_walk_in, booking_type, "
                     "token_number) VALUES (%s, %s, %s, %s, %s, 'Dr. Meena', 'Medicine', CURRENT_DATE, '10:00', "
                     "'confirmed', 'waiting', false, 'consultation', %s) RETURNING id",
                  clinic, pat, phone, patient_name, doc, token or 100 + uuid.uuid4().int % 9000))
    admin = str(_q(c, "INSERT INTO clinic_admins (clinic_id, username, password_hash, role, staff_role, doctor_id, is_active) "
                      "VALUES (%s, %s, 'x', 'staff', 'DOCTOR', %s, true) RETURNING id",
                   clinic, "dr_" + uuid.uuid4().hex[:8], doc))
    snap = json.dumps({"name": "Dr. Meena", "registration_number": reg, "registration_council": "TSMC"})
    enc = str(_q(c, "INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id) VALUES (%s, %s, %s, %s) "
                    "RETURNING id", clinic, appt, pat, doc))
    _q(c, "SELECT id FROM opd_sign_encounter(%s, %s, %s, %s, %s::jsonb)", clinic, enc, admin, doc, snap)
    rx = str(_q(c, "INSERT INTO opd_prescriptions (clinic_id, encounter_id, appointment_id, patient_id, doctor_id) "
                   "VALUES (%s, %s, %s, %s, %s) RETURNING id", clinic, enc, appt, pat, doc))
    items = []
    for n, (drug, form, dosage, freq, days) in enumerate(lines, start=1):
        items.append(str(_q(c, "INSERT INTO opd_prescription_items (clinic_id, prescription_id, line_no, drug_name, "
                               "formulation, dosage, frequency, duration_days, instructions) VALUES "
                               "(%s, %s, %s, %s, %s, %s, %s, %s, 'after food') RETURNING id",
                            clinic, rx, n, drug, form, dosage, freq, days)))
    _q(c, "SELECT id FROM opd_sign_prescription(%s, %s, %s, %s, %s::jsonb, '{\"clinic_name\": \"RX POS\"}'::jsonb, "
          "%s::jsonb, '[]'::jsonb)", clinic, rx, admin, doc, snap,
       json.dumps({"patient_name": patient_name, "mrn": "004211"}))
    return {"rx": rx, "rx_items": items, "patient": pat, "doctor": doc, "appointment": appt, "phone": phone}


def open_shift(c, clinic, cashier=None, float_paise=0):
    cashier = cashier or str(uuid.uuid4())
    return str(_q(c, "INSERT INTO opd_cashier_shifts (clinic_id, cashier_admin_id, cashier_name, opening_float_paise) "
                     "VALUES (%s, %s, 'Ravi', %s) RETURNING id", clinic, cashier, float_paise)), cashier


def draft(c, w, kind="otc", rx=None, patient=None, walkin=None, snapshot=None):
    return str(_q(c, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind, opd_prescription_id, patient_id, walkin_name, "
                     "prescriber_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
                  w["clinic"], w["store"], kind, rx, patient, walkin, json.dumps(snapshot) if snapshot else None))


def line(c, w, sale, qty, n=1, rx_item=None, item=None, batch=None, fake_gross=None):
    cols = "clinic_id, sale_id, line_no, item_id, batch_id, qty_units, opd_prescription_item_id"
    vals = [w["clinic"], sale, n, item or w["item"], batch or w["batch"], qty, rx_item]
    if fake_gross is not None:
        cols += ", line_gross_paise"
        vals.append(fake_gross)
    return str(_q(c, f"INSERT INTO rx_sale_items ({cols}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING id", *vals))


def complete(c, w, sale, payments, shift=None, idem=None, actor=None, override=None, admin=False, controlled=False):
    return _q(c, "SELECT rx_complete_sale(%s, %s, %s::jsonb, %s, %s, %s, 'Ravi', %s, %s, %s)", w["clinic"], sale,
              json.dumps(payments), shift, idem, actor, override, admin, controlled)


def _sale(c, sale):
    return _q(c, "SELECT row_to_json(s) FROM rx_sales s WHERE id = %s", sale)


# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gross,rate,out", [
    (10500, 500, (10000, 250, 250)), (11200, 1200, (10000, 600, 600)), (11800, 1800, (10000, 900, 900)),
    (9999, 500, (9523, 238, 238)), (1, 500, (1, 0, 0)), (10000, 0, (10000, 0, 0)),
])
def test_gst_split_vectors(real_pg_conn, gross, rate, out):
    assert _q(real_pg_conn, "SELECT row_to_json(x) FROM rx_gst_split_inclusive(%s, %s, true) x", gross, rate) == \
        dict(zip(("taxable", "cgst", "sgst"), out))
    assert _q(real_pg_conn, "SELECT row_to_json(x) FROM rx_gst_split_inclusive(%s, %s, false) x", gross, rate) == \
        {"taxable": gross, "cgst": 0, "sgst": 0}


def test_unit_price_and_frequency_map(real_pg_conn):
    c = real_pg_conn
    assert _q(c, "SELECT rx_unit_price(12000, 10, 3)") == 3600 and _q(c, "SELECT rx_unit_price(9999, 15, 7)") == 4666
    for args, want in ((("1", "TDS", 5, "tablet"), 15), (("1/2", "BD", 10, "tablet"), 10), (("2 tabs", "1-0-1", 3, "capsule"), 12),
                       (("1", "SOS", 5, "tablet"), None), (("10 ml", "TDS", 5, "syrup"), None), (("1", "OD", None, "tablet"), None),
                       (("as directed", "BD", 5, "tablet"), None), (("0.5", "QID", 3, "tablet"), 6)):
        assert _q(c, "SELECT rx_rx_units(%s, %s, %s, %s)", *args) == want, args


def test_lines_priced_from_batch_only_with_discount_and_round_off(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c)                                                     # MRP ₹120 / strip of 10, GST 12 %
    sale = draft(c, w)
    a = line(c, w, sale, 3, fake_gross=1)                                # a client price is ignored
    row = _q(c, "SELECT row_to_json(x) FROM rx_sale_items x WHERE id = %s", a)
    assert (row["line_gross_paise"], row["mrp_pack_paise"], row["tax_rate_bps"], row["hsn_code"]) == (3600, 12000, 1200, "30049099")
    assert (row["taxable_paise"], row["cgst_paise"] + row["sgst_paise"]) == (3214, 386)
    line(c, w, sale, 7, n=2)                                             # 8400
    _q(c, "UPDATE rx_sales SET discount_paise = 1001 WHERE id = %s", sale)
    s = _sale(c, sale)
    assert (s["gross_paise"], s["discount_paise"], s["round_off_paise"], s["net_paise"]) == (12000, 1001, 1, 11000)
    shares = _q(c, "SELECT array_agg(line_discount_paise ORDER BY line_no) FROM rx_sale_items WHERE sale_id = %s", sale)
    assert shares == [300, 701] and s["taxable_paise"] + s["cgst_paise"] + s["sgst_paise"] == 12000 - 1001
    assert "rx_whole_packs_only" in _err(c, "UPDATE rx_items SET allow_loose = false WHERE id = %s; UPDATE rx_sale_items "
                                            "SET qty_units = 4 WHERE id = %s", w["item"], a)


def test_complete_sale_moves_stock_takes_payment_and_numbers_last(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c)
    shift, cashier = open_shift(c, w["clinic"])
    sale = draft(c, w)
    line(c, w, sale, 15)                                                 # 18000
    with pytest.raises(psycopg2.Error, match="rx_payment_mismatch:100"):
        complete(c, w, sale, [{"mode": "upi", "amount_paise": 17900}])
    with pytest.raises(psycopg2.Error, match="rx_sale_payments_cash_needs_shift"):
        complete(c, w, sale, [{"mode": "cash", "amount_paise": 18000}])
    out = complete(c, w, sale, [{"mode": "cash", "amount_paise": 10000}, {"mode": "upi", "amount_paise": 8000,
                                                                         "reference": "UTR1"}], shift, "idem-sale-0001",
                   cashier)
    assert out["sale_number"] == f"PHS-{date.today().year}-000001" and out["replayed"] is False
    assert complete(c, w, sale, [], shift, "idem-sale-0001", cashier)["replayed"] is True
    s = _sale(c, sale)
    assert (s["status"], s["paid_paise"], s["is_tax_invoice"], s["clinic_gstin"]) == ("completed", 18000, True, GSTIN)
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", w["batch"]) == 85
    # completed sales, lines, payments are frozen; payments only through the functions
    assert "rx_document_locked" in _err(c, "UPDATE rx_sales SET discount_paise = 5 WHERE id = %s", sale)
    assert "rx_document_locked" in _err(c, "UPDATE rx_sale_items SET qty_units = 1 WHERE sale_id = %s", sale)
    assert "rx_document_locked" in _err(c, "DELETE FROM rx_sales WHERE id = %s", sale)
    assert "rx_write_via_function_only" in _err(c, "INSERT INTO rx_sale_payments (clinic_id, sale_id, mode, amount_paise) "
                                                   "VALUES (%s, %s, 'upi', 1)", w["clinic"], sale)
    assert "rx_append_only" in _err(c, "DELETE FROM rx_sale_payments WHERE sale_id = %s", sale)
    # stock short / closed shift
    s2 = draft(c, w)
    line(c, w, s2, 86)
    with pytest.raises(psycopg2.Error, match="rx_insufficient_stock"):
        complete(c, w, s2, [{"mode": "upi", "amount_paise": 103200}])
    _q(c, "UPDATE opd_cashier_shifts SET status = 'closed', closed_at = now(), expected_cash_paise = 0, "
          "declared_cash_paise = 0 WHERE id = %s", shift)
    _q(c, "UPDATE rx_sale_items SET qty_units = 1 WHERE sale_id = %s", s2)
    with pytest.raises(psycopg2.Error, match="rx_shift_not_open"):
        complete(c, w, s2, [{"mode": "cash", "amount_paise": 1200}], shift)


def test_rx_superseded_and_fulfilment_caps(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c, schedule="H1")
    rx = signed_rx(c, w["clinic"])                                        # 1 TDS × 5 days = 15 units
    s1 = draft(c, w, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
    line(c, w, s1, 10, rx_item=rx["rx_items"][0])
    complete(c, w, s1, [{"mode": "upi", "amount_paise": 12000}])
    s2 = draft(c, w, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
    line(c, w, s2, 6, rx_item=rx["rx_items"][0])                          # 10 + 6 > 15, H1: hard
    with pytest.raises(psycopg2.Error, match="rx_fulfilment_exceeds_rx"):
        complete(c, w, s2, [{"mode": "upi", "amount_paise": 7200}])
    _q(c, "UPDATE rx_sale_items SET qty_units = 5 WHERE sale_id = %s", s2)
    complete(c, w, s2, [{"mode": "upi", "amount_paise": 6000}])
    assert _q(c, "SELECT SUM(units) FROM rx_rx_fulfilments WHERE opd_prescription_item_id = %s", rx["rx_items"][0]) == 15
    # a superseded Rx cannot be sold against
    s3 = draft(c, w, "opd_rx", rx["rx"], rx["patient"], snapshot={"registration_number": "TSMC/2012/777"})
    line(c, w, s3, 1)
    _q(c, "UPDATE opd_prescriptions SET status = 'superseded', superseded_at = now() WHERE id = %s", rx["rx"])
    with pytest.raises(psycopg2.Error, match="rx_prescription_superseded"):
        complete(c, w, s3, [{"mode": "upi", "amount_paise": 1200}])


def test_schedule_preconditions_and_controlled(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c, schedule="H")
    otc = draft(c, w)
    line(c, w, otc, 10)
    with pytest.raises(psycopg2.Error, match="rx_schedule_needs_rx"):
        complete(c, w, otc, [{"mode": "upi", "amount_paise": 12000}])
    _q(c, "UPDATE rx_items SET schedule = 'X' WHERE id = %s", w["item"])
    ext = {"external": True, "name": "Dr. Outside", "registration_number": "KMC/99/1", "rx_date": str(date.today()),
           "patient_name": "Ramesh K"}
    x1 = draft(c, w, "otc", walkin="Ramesh K", snapshot=ext)
    line(c, w, x1, 1)
    with pytest.raises(psycopg2.Error, match="rx_controlled_permission"):
        complete(c, w, x1, [{"mode": "upi", "amount_paise": 1200}])
    complete(c, w, x1, [{"mode": "upi", "amount_paise": 1200}], controlled=True)
    x2 = draft(c, w, "otc", walkin="ramesh  k.", snapshot={**ext, "patient_name": "ramesh  k."})   # same Rx again (X2)
    line(c, w, x2, 1)
    with pytest.raises(psycopg2.Error, match="rx_external_rx_reused"):
        complete(c, w, x2, [{"mode": "upi", "amount_paise": 1200}], controlled=True)
    bare = draft(c, w, "otc", walkin="No Rx", snapshot={"registration_number": "TSMC/1"})
    line(c, w, bare, 1)
    with pytest.raises(psycopg2.Error, match="rx_controlled_requires_inhouse_rx"):
        complete(c, w, bare, [{"mode": "upi", "amount_paise": 1200}], controlled=True)


def test_discount_cap_and_single_use_override(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c)
    actor, approver = str(uuid.uuid4()), str(uuid.uuid4())
    sale = draft(c, w)
    line(c, w, sale, 10)                                                  # 12000; cap 10 % = 1200
    _q(c, "UPDATE rx_sales SET discount_paise = 2000 WHERE id = %s", sale)
    with pytest.raises(psycopg2.Error, match="rx_discount_over_limit"):
        complete(c, w, sale, [{"mode": "upi", "amount_paise": 10000}], actor=actor)
    assert "rx_overrides_two_people" in _err(c, "INSERT INTO rx_overrides (clinic_id, action, sale_id, amount_paise, reason, "
                                                "approver_admin_id, approver_name, requester_admin_id, expires_at) VALUES "
                                                "(%s, 'discount_over_cap', %s, 2000, 'Staff family', %s, 'M', %s, "
                                                "now() + interval '60 seconds')", w["clinic"], sale, actor, actor)
    ov = str(_q(c, "INSERT INTO rx_overrides (clinic_id, action, sale_id, amount_paise, reason, approver_admin_id, "
                   "approver_name, requester_admin_id, expires_at) VALUES (%s, 'discount_over_cap', %s, 2000, 'Staff "
                   "family', %s, 'Manager', %s, clock_timestamp() + interval '60 seconds') RETURNING id",
                w["clinic"], sale, approver, actor))
    out = complete(c, w, sale, [{"mode": "upi", "amount_paise": 10000}], actor=actor, override=ov)
    assert out["override_used"] is True
    assert _q(c, "SELECT consumed_at IS NOT NULL FROM rx_overrides WHERE id = %s", ov)
    assert "rx_append_only:rx_overrides" in _err(c, "UPDATE rx_overrides SET consumed_at = NULL WHERE id = %s", ov)
    s2 = draft(c, w)
    line(c, w, s2, 10)
    _q(c, "UPDATE rx_sales SET discount_paise = 2000 WHERE id = %s", s2)
    with pytest.raises(psycopg2.Error, match="rx_discount_over_limit"):                     # used once already
        complete(c, w, s2, [{"mode": "upi", "amount_paise": 10000}], actor=actor, override=ov)


def _return(c, w, sale, lines, mode="upi", shift=None, actor=None, admin=False, idem=None):
    return _q(c, "SELECT rx_record_return(%s, %s, %s::jsonb, 'unopened_unused', %s, %s, %s, %s, 'Ravi', %s, NULL)",
              w["clinic"], sale, json.dumps(lines), mode, shift, idem, actor, admin)


def test_returns_refund_sold_value_and_never_more(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c, mrp=10000)                                           # ₹100 / strip of 10
    shift, cashier = open_shift(c, w["clinic"])
    sale = draft(c, w)
    item = line(c, w, sale, 3)                                            # 3000
    _q(c, "UPDATE rx_sales SET discount_paise = 100 WHERE id = %s", sale)   # net 2900 (round-off 0)
    complete(c, w, sale, [{"mode": "cash", "amount_paise": 2900}], shift, actor=cashier)
    r1 = _return(c, w, sale, [{"sale_item_id": item, "qty_units": 1, "restock": True}], "cash", shift, cashier,
                 idem="ret-idem-0001")
    assert (r1["amount_paise"], r1["return_number"]) == (967, f"PRT-{date.today().year}-00001")   # 2900/3 half-up
    assert _return(c, w, sale, [{"sale_item_id": item, "qty_units": 1, "restock": True}], "cash", shift, cashier,
                   idem="ret-idem-0001")["replayed"] is True
    with pytest.raises(psycopg2.Error, match="rx_return_exceeds_sold"):
        _return(c, w, sale, [{"sale_item_id": item, "qty_units": 3, "restock": True}], "cash", shift, cashier)
    with pytest.raises(psycopg2.Error, match="rx_refund_exceeds_tender"):
        _return(c, w, sale, [{"sale_item_id": item, "qty_units": 1, "restock": True}], "card")
    r2 = _return(c, w, sale, [{"sale_item_id": item, "qty_units": 2, "restock": False}], "credit_note", actor=cashier)
    assert r2["amount_paise"] == 2900 - 967                              # last units take the exact remainder
    assert _q(c, "SELECT cn_number FROM rx_credit_notes WHERE id = %s", r2["credit_note_id"]) == f"PCN-{date.today().year}-00001"
    s = _sale(c, sale)
    assert (s["refunded_paise"], s["credited_paise"]) == (967, 1933)
    moves = _q(c, "SELECT json_agg(movement_type || ':' || qty_units ORDER BY created_at) FROM rx_stock_movements "
                  "WHERE batch_id = %s AND movement_type <> 'opening'", w["batch"])
    assert moves == ["sale_out:-3", "sale_return_in:1", "sale_return_in:2", "damage_writeoff_out:-2"]
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", w["batch"]) == 98
    assert "rx_write_via_function_only" in _err(c, "INSERT INTO rx_credit_notes (clinic_id, sale_id, cn_year, cn_seq, "
                                                   "amount_paise, tax_paise, reason) VALUES (%s, %s, 2026, 99, 1, 0, 'x')",
                                                w["clinic"], sale)
    # window: a sale completed 8 days ago (default return_days 7) needs RX_ADMIN or an override
    old = draft(c, w)
    oi = line(c, w, old, 1)
    complete(c, w, old, [{"mode": "upi", "amount_paise": 1000}])
    _q(c, "SELECT set_config('kriya.rx_tx', %s, true); UPDATE rx_sales SET completed_at = now() - interval '8 days' "
          "WHERE id = %s", w["clinic"], old)
    with pytest.raises(psycopg2.Error, match="rx_return_window_closed"):
        _return(c, w, old, [{"sale_item_id": oi, "qty_units": 1, "restock": True}])
    assert _return(c, w, old, [{"sale_item_id": oi, "qty_units": 1, "restock": True}], admin=True)["amount_paise"] == 1000


def test_void_same_day_only_and_close_shift_counts_pharmacy_cash(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c)
    shift, cashier = open_shift(c, w["clinic"], float_paise=50000)
    sale = draft(c, w)
    line(c, w, sale, 10)
    complete(c, w, sale, [{"mode": "cash", "amount_paise": 12000}], shift, actor=cashier)
    voided = draft(c, w)
    line(c, w, voided, 5)
    complete(c, w, voided, [{"mode": "cash", "amount_paise": 6000}], shift, actor=cashier)
    with pytest.raises(psycopg2.Error, match="rx_reason_required"):
        _q(c, "SELECT rx_void_sale(%s, %s, '', %s, NULL, 'M')", w["clinic"], voided, shift)
    _q(c, "SELECT rx_void_sale(%s, %s, 'Wrong patient', %s, NULL, 'M')", w["clinic"], voided, shift)
    assert _sale(c, voided)["status"] == "void"
    assert _q(c, "SELECT qty_units FROM rx_stock_balances WHERE batch_id = %s", w["batch"]) == 90
    # expected cash = float 50000 + 12000 + 6000 − 6000 (void refund); the Z-report reads the same number
    assert _q(c, "SELECT rx_shift_expected_cash(%s, %s)", w["clinic"], shift) == 62000
    closed = _q(c, "SELECT row_to_json(x) FROM opd_close_shift(%s, %s, %s, 62000, NULL) x", w["clinic"], shift, cashier)
    assert (closed["expected_cash_paise"], closed["variance_paise"]) == (62000, 0)
    # a shift without pharmacy cash is computed exactly as before (the extra term is 0)
    plain, cashier2 = open_shift(c, w["clinic"], float_paise=777)
    assert _q(c, "SELECT expected_cash_paise FROM opd_close_shift(%s, %s, %s, 0, NULL)", w["clinic"], plain, cashier2) == 777
    # a void on another day, or after a return, is refused
    s3 = draft(c, w)
    i3 = line(c, w, s3, 1)
    complete(c, w, s3, [{"mode": "upi", "amount_paise": 1200}])
    _return(c, w, s3, [{"sale_item_id": i3, "qty_units": 1, "restock": True}])
    with pytest.raises(psycopg2.Error, match="rx_void_has_returns"):
        _q(c, "SELECT rx_void_sale(%s, %s, 'Too late', NULL, NULL, 'M')", w["clinic"], s3)


def test_drafts_one_live_per_rx_store_and_tenant_fks(real_pg_conn):
    c = real_pg_conn
    w = pos_world(c)
    rx = signed_rx(c, w["clinic"])
    draft(c, w, "opd_rx", rx["rx"], rx["patient"])
    assert "uq_rx_sales_one_live_per_rx" in _err(c, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind, opd_prescription_id, "
                                                    "patient_id) VALUES (%s, %s, 'opd_rx', %s, %s)",
                                                 w["clinic"], w["store"], rx["rx"], rx["patient"])
    other = pos_world(c)
    assert "rx_sales_rx_fk" in _err(c, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind, opd_prescription_id, "
                                       "walkin_name) VALUES (%s, %s, 'opd_rx', %s, 'Leak')", other["clinic"], other["store"],
                                    rx["rx"])
    assert "rx_sales_has_person" in _err(c, "INSERT INTO rx_sales (clinic_id, store_id, sale_kind) VALUES (%s, %s, 'staff')",
                                         w["clinic"], w["store"])
    with pytest.raises(psycopg2.Error, match="rx_batch_not_found"):
        line(c, w, draft(c, w), 1, batch=other["batch"])


def test_purge_v5_and_rollback_restores_116_close_shift(real_pg_conn):
    c = real_pg_conn
    a, b = pos_world(c), pos_world(c)
    for w in (a, b):
        s = draft(c, w)
        line(c, w, s, 1)
        complete(c, w, s, [{"mode": "upi", "amount_paise": 1200}])
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert (out["sales"], out["movements"]) == (1, 2)
    assert _q(c, "SELECT count(*) FROM rx_sales WHERE clinic_id = %s", b["clinic"]) == 1
    body_116 = (MIG / "116_ipd_finance.sql").read_text(encoding="utf-8")
    body_116 = body_116[body_116.index("CREATE OR REPLACE FUNCTION opd_close_shift("):]
    body_116 = body_116[body_116.index("AS $$") + 5:body_116.index("END $$;") + 3]
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "138_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_138(cur)
        cur.execute("SELECT to_regclass('public.rx_sales'), prosrc, proconfig FROM pg_proc WHERE proname = 'opd_close_shift'")
        reg, src, conf = cur.fetchone()
        assert reg is None and src.strip() == body_116.strip() and "search_path=public, pg_temp" in conf
        cur.execute("SELECT prosrc LIKE '%%rx_grns%%' AND prosrc NOT LIKE '%%rx_sales%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                                 # purge v4 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_pos_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_gst_split_inclusive', 'rx_unit_price', 'rx_rx_units', 'rx_require_tx',
                                                 'rx_guard_sale', 'rx_sale_line_calc', 'rx_sale_refresh',
                                                 'rx_sale_lines_after', 'rx_sale_header_after', 'rx_guard_override',
                                                 'rx_consume_override', 'rx_complete_sale', 'rx_record_return',
                                                 'rx_void_sale', 'opd_close_shift', 'rx_shift_expected_cash')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
