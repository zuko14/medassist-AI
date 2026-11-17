"""Migration 139 (Pharmacy OS inpatient pharmacy, RX-M6) against real PostgreSQL.

ipd_charges.source widened (keeps 116's 'package', validated) and no rx trigger on any IPD table
(VERIFICATION D2 / D9); indent lines only for ACTIVE MEDICATION orders of the same, still-admitted stay;
function-only writes; IND- numbering + idempotent replay; dispense moves stock, prices from the batch and
writes exactly one charge per line however often it is retried; >999 units split into charges; a stopped
order blocks the dispense (re-read under lock, no trigger); over-dispense refused; receive closes; MAR
links validated and cascaded from MAR; ward return voids + re-issues an unbilled charge, or credit-notes
an issued bill; purge v6 (pharmacy charges stay on the bill); guarded rollback + idempotent re-apply.
HTTP behaviour: tests/test_rx_ipd.py.
"""

import json
import uuid
from datetime import date
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _err, _q, _store
from tests.test_migration_rx_m2 import _generic, _item
from tests.test_migration_rx_m3 import _bal, _batch, _move

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "139_rx_ipd.sql", MIG / "rollback" / "139_rollback_rx_ipd.sql"


def unwind_139(cur) -> None:
    """Inside an open transaction: purge every clinic with indents and run the 139 rollback. Pharmacy charges
    belong to patient bills and outlive the RX purge; the owner's IPD purge (run after the RX purge, as the
    platform does) removes them so older rollbacks (116_down) can narrow ipd_charges.source again."""
    cur.execute("SELECT to_regclass('public.rx_ipd_indents') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM rx_ipd_indents) x")
    cur.execute("SELECT ipd_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM ipd_charges "
                "WHERE source = 'pharmacy') x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


# ── world: an IPD + pharmacy clinic, one admitted patient with orders, a central store with stock ──

def ipd_world(c, stock=2000, pack=10, mrp=4500):
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state, ipd_state,
                                               ipd_settings)
                          VALUES ('RX IPD Hospital', %s, 'multispecialty', true,
                                  '{"opd_enabled": true, "ipd_enabled": true}'::jsonb, 'READY', 'ACTIVE',
                                  '{"bed_tariffs": {"general": 150000}}'::jsonb) RETURNING id""",
                    "+9177" + uuid.uuid4().hex[:8]))
    doctor = str(_q(c, "INSERT INTO doctors (clinic_id, name, department, specialization, registration_number, "
                       "registration_council) VALUES (%s, 'Dr. Rao', 'Medicine', 'Physician', 'KMC/2015/1', 'KMC') "
                       "RETURNING id", clinic))
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name, ward_type, gender_restriction) "
                     "VALUES (%s, %s, 'General Ward', 'general', 'any') RETURNING id", clinic, "W" + uuid.uuid4().hex[:5]))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B1', 'general') "
                    "RETURNING id", clinic, ward))
    patient = str(_q(c, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on) "
                        "VALUES (%s, 'Ravi Kumar', %s, 'male', 40, CURRENT_DATE) RETURNING id",
                     clinic, "+9190" + uuid.uuid4().hex[:8]))
    adm = admit(c, clinic, patient, doctor, bed)
    store = _store(c, clinic, "CENTRAL", "central")
    item = _item(c, clinic, _generic(c, clinic, "Ceftriaxone", "1 g", "injection"), "CEF1G", "Monocef 1g",
                 base_unit="vial", pack_size=pack)
    batch = _batch(c, clinic, item, "C1", mrp=mrp)
    _move(c, clinic, store, batch, item, stock, "opening")
    med = order(c, clinic, adm, doctor)
    return {"clinic": clinic, "doctor": doctor, "ward": ward, "bed": bed, "patient": patient, "admission": adm,
            "store": store, "item": item, "batch": batch, "order": med}


def admit(c, clinic, patient, doctor, bed):
    payload = json.dumps({"patient_id": patient, "admitting_doctor_id": doctor, "reason": "Pneumonia"})
    return _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic, payload, bed)["id"]


def order(c, clinic, adm, doctor, kind="medication"):
    if kind != "medication":
        return str(_q(c, "INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, ordered_by_doctor_id, "
                         "ordered_by_name) VALUES (%s, %s, %s, 'Soft diet', %s, 'Dr. Rao') RETURNING id",
                      clinic, adm, kind, doctor))
    return str(_q(c, "INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, drug_name, dose, route, "
                     "frequency, ordered_by_doctor_id, ordered_by_name) VALUES (%s, %s, 'medication', 'Inj Ceftriaxone', "
                     "'Ceftriaxone', '1 g', 'iv', 'BD', %s, 'Dr. Rao') RETURNING id", clinic, adm, doctor))


def raise_indent(c, w, lines=None, idem=None, priority="routine", adm=None):
    lines = lines if lines is not None else [{"ipd_order_id": w["order"], "requested_units": 6, "item_id": w["item"]}]
    return _q(c, "SELECT rx_raise_indent(%s, %s, %s, %s, %s::jsonb, %s, NULL, 'Nurse Asha')", w["clinic"],
              adm or w["admission"], w["store"], priority, json.dumps(lines), idem)


def line_of(c, indent):
    return str(_q(c, "SELECT id FROM rx_indent_lines WHERE indent_id = %s ORDER BY line_no LIMIT 1", indent))


def dispense(c, w, indent, qty=6, idem=None, line=None, **extra):
    lines = [{"indent_line_id": line or line_of(c, indent), "item_id": w["item"], "batch_id": w["batch"],
              "qty_units": qty, **extra}]
    return _q(c, "SELECT rx_dispense_indent(%s, %s, %s, %s::jsonb, NULL, 'Pharm Ravi', %s)", w["clinic"], indent,
              w["store"], json.dumps(lines), idem)


def charges(c, adm, live=True):
    return _q(c, "SELECT COALESCE(json_agg(json_build_object('ref', source_ref, 'qty', quantity, 'price', unit_price_paise, "
                 "'status', status, 'source', source, 'invoice', invoice_id) ORDER BY created_at, source_ref), '[]') "
                 "FROM ipd_charges WHERE admission_id = %s AND category = 'pharmacy'" + (" AND status <> 'void'" if live else ""),
              adm)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_ipd_tables_untouched_and_source_widened(real_pg_conn):
    c = real_pg_conn
    defn = _q(c, "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ipd_charges_source_check' "
                 "AND convalidated")
    assert all(s in defn for s in ("manual", "bed_day", "order", "package", "pharmacy"))
    assert _q(c, """SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                    WHERE t.tgrelid IN ('ipd_charges'::regclass, 'ipd_orders'::regclass, 'ipd_admissions'::regclass,
                                        'ipd_med_administrations'::regclass) AND p.proname LIKE 'rx\\_%%'""") == 0
    # every FK into an IPD table cascades / sets null, so the IPD owner purge never meets a pharmacy row
    assert dict(_q(c, """SELECT json_agg(json_build_array(conname, confdeltype)) FROM pg_constraint
                          WHERE contype = 'f' AND conrelid::regclass::text LIKE 'rx\\_%%'
                            AND confrelid::regclass::text LIKE 'ipd\\_%%'""")) == {
        "rx_ipd_indents_admission_fk": "c", "rx_ipd_indents_ward_fk": "n", "rx_indent_lines_order_fk": "c",
        "rx_dispense_lines_charge_fk": "n", "rx_mar_links_mar_fk": "c"}


def test_indent_requires_active_med_order_of_the_same_stay(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    diet = order(c, w["clinic"], w["admission"], w["doctor"], "diet")
    with pytest.raises(psycopg2.Error, match="rx_order_not_medication"):
        raise_indent(c, w, [{"ipd_order_id": diet, "requested_units": 1}])
    other = ipd_world(c)
    with pytest.raises(psycopg2.Error, match="rx_order_not_found|rx_indent_lines_order_fk"):
        raise_indent(c, w, [{"ipd_order_id": other["order"], "requested_units": 1}])
    stopped = order(c, w["clinic"], w["admission"], w["doctor"])
    _q(c, "UPDATE ipd_orders SET status = 'discontinued', ended_at = now(), end_reason = 'Course done' WHERE id = %s", stopped)
    with pytest.raises(psycopg2.Error, match="rx_order_not_active"):
        raise_indent(c, w, [{"ipd_order_id": stopped, "requested_units": 1}])
    # numbered last, IND-<year>-00001, and a retry with the same key is the same indent
    first = raise_indent(c, w, idem="ind-key-0001", priority="stat")
    assert first["indent_number"] == f"IND-{date.today().year}-00001" and first["replayed"] is False
    assert raise_indent(c, w, idem="ind-key-0001")["indent_id"] == first["indent_id"]
    assert _q(c, "SELECT ward_id FROM rx_ipd_indents WHERE id = %s", first["indent_id"]) == w["ward"]
    assert _q(c, "SELECT count(*) FROM rx_ipd_indents WHERE clinic_id = %s", w["clinic"]) == 1   # failures left nothing


def test_writes_only_through_functions(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    assert "rx_write_via_function_only" in _err(c, "INSERT INTO rx_ipd_indents (clinic_id, admission_id, from_store_id, "
                                                   "raised_by_name) VALUES (%s, %s, %s, 'x')",
                                                w["clinic"], w["admission"], w["store"])
    ind = raise_indent(c, w)["indent_id"]
    assert "rx_write_via_function_only" in _err(c, "UPDATE rx_ipd_indents SET status = 'closed' WHERE id = %s", ind)
    assert "rx_write_via_function_only" in _err(c, "UPDATE rx_indent_lines SET dispensed_units = 6 WHERE indent_id = %s", ind)
    assert "rx_append_only" in _err(c, "DELETE FROM rx_ipd_indents WHERE id = %s", ind)


def test_dispense_posts_charge_once_and_moves_stock(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w)["indent_id"]
    outs = [dispense(c, w, ind, idem="disp-key-0001") for _ in range(3)]
    assert outs[0]["replayed"] is False and all(o["replayed"] for o in outs[1:])
    assert outs[0]["total_paise"] == 6 * 450 and outs[0]["final_bill_exists"] is False       # MRP 45.00 / 10 per vial
    assert charges(c, w["admission"]) == [{"ref": f"rx:dl:{_q(c, 'SELECT id FROM rx_dispense_lines WHERE dispense_id = %s', outs[0]['dispense_id'])}",
                                           "qty": 6, "price": 450, "status": "posted", "source": "pharmacy", "invoice": None}]
    assert _bal(c, w["store"], w["batch"]) == 2000 - 6
    assert _q(c, "SELECT movement_type FROM rx_stock_movements WHERE ref_table = 'rx_dispense_lines' AND clinic_id = %s",
              w["clinic"]) == "ipd_dispense_out"
    assert _q(c, "SELECT status FROM rx_ipd_indents WHERE id = %s", ind) == "dispensed"
    assert _q(c, "SELECT ipd_charge_id IS NOT NULL FROM rx_dispense_lines WHERE dispense_id = %s", outs[0]["dispense_id"])
    with pytest.raises(psycopg2.Error, match="rx_indent_not_open"):     # nothing left to dispense
        dispense(c, w, ind, 1)


def test_partial_over_dispense_autopost_off_and_split_charges(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    _q(c, "UPDATE clinics SET rx_settings = '{\"ipd_autopost\": false}'::jsonb WHERE id = %s", w["clinic"])
    ind = raise_indent(c, w, [{"ipd_order_id": w["order"], "requested_units": 1500}])["indent_id"]
    with pytest.raises(psycopg2.Error, match="rx_indent_lines_dispensed_le_requested"):
        dispense(c, w, ind, 1501)
    dispense(c, w, ind, 500)
    assert _q(c, "SELECT status FROM rx_ipd_indents WHERE id = %s", ind) == "partially_dispensed"
    dispense(c, w, ind, 1000)
    got = charges(c, w["admission"])
    assert [(x["qty"], x["status"]) for x in got] == [(500, "proposed"), (999, "proposed"), (1, "proposed")]
    assert got[2]["ref"] == got[1]["ref"] + ":p2"


def test_stopped_order_or_discharged_stay_blocks_dispense(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w)["indent_id"]
    _q(c, "UPDATE ipd_orders SET status = 'discontinued', ended_at = now(), end_reason = 'Switched to oral' WHERE id = %s",
       w["order"])
    with pytest.raises(psycopg2.Error, match="rx_order_not_active"):
        dispense(c, w, ind)
    assert _bal(c, w["store"], w["batch"]) == 2000 and charges(c, w["admission"]) == []
    # the pharmacist closes the stale line instead
    _q(c, "SELECT rx_dispense_indent(%s, %s, %s, %s::jsonb, NULL, 'P', NULL)", w["clinic"], ind, w["store"],
       json.dumps([{"indent_line_id": line_of(c, ind), "not_available": True}]))
    assert _q(c, "SELECT status FROM rx_ipd_indents WHERE id = %s", ind) == "cancelled"
    # a stay that does not hold a bed cannot raise
    other = str(_q(c, "INSERT INTO patients (clinic_id, name, phone, gender, age_years, age_recorded_on) "
                      "VALUES (%s, 'Sita', %s, 'female', 30, CURRENT_DATE) RETURNING id",
                   w["clinic"], "+9191" + uuid.uuid4().hex[:8]))
    pending = _q(c, "SELECT ipd_admit(%s, %s::jsonb, NULL, NULL, 'desk')", w["clinic"],
                 json.dumps({"patient_id": other, "admitting_doctor_id": w["doctor"], "reason": "Elective"}))
    assert pending["status"] == "pending"
    with pytest.raises(psycopg2.Error, match="rx_admission_not_active"):
        raise_indent(c, w, adm=pending["id"])


def test_substitute_must_share_generic_with_reason(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    same = _item(c, w["clinic"], _q(c, "SELECT generic_id FROM rx_items WHERE id = %s", w["item"]), "CEF1GB", "Oframax 1g",
                 base_unit="vial")
    sb = _batch(c, w["clinic"], same, "O1")
    _move(c, w["clinic"], w["store"], sb, same, 10, "opening")
    ind = raise_indent(c, w)["indent_id"]
    sub = {"indent_line_id": line_of(c, ind), "item_id": same, "batch_id": sb, "qty_units": 2}
    run = lambda e: _q(c, "SELECT rx_dispense_indent(%s, %s, %s, %s::jsonb, NULL, 'P', NULL)", w["clinic"], ind, w["store"],
                       json.dumps([e]))
    with pytest.raises(psycopg2.Error, match="rx_substitution_reason_required"):
        run(sub)
    run({**sub, "substitution_reason": "Monocef out of stock"})
    assert _q(c, "SELECT substituted_from_item_id FROM rx_dispense_lines WHERE item_id = %s", same) == w["item"]


def test_receive_closes_and_mar_link_is_validated(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w)["indent_id"]
    d = dispense(c, w, ind)["dispense_id"]
    first = _q(c, "SELECT rx_receive_dispense(%s, %s, NULL, 'Nurse Asha')", w["clinic"], d)
    assert first["indent_status"] == "closed" and first["replayed"] is False
    assert _q(c, "SELECT rx_receive_dispense(%s, %s, NULL, 'Nurse Asha')", w["clinic"], d)["replayed"] is True
    dl = str(_q(c, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    mar = str(_q(c, "INSERT INTO ipd_med_administrations (clinic_id, admission_id, order_id, outcome, administered_at, "
                    "recorded_by_name) VALUES (%s, %s, %s, 'given', now(), 'Nurse Asha') RETURNING id",
                 w["clinic"], w["admission"], w["order"]))
    assert _q(c, "SELECT rx_link_mar(%s, %s, %s, NULL)", w["clinic"], mar, dl)["replayed"] is False
    assert _q(c, "SELECT rx_link_mar(%s, %s, %s, NULL)", w["clinic"], mar, dl)["replayed"] is True
    other_order = order(c, w["clinic"], w["admission"], w["doctor"])
    mar2 = str(_q(c, "INSERT INTO ipd_med_administrations (clinic_id, admission_id, order_id, outcome, administered_at, "
                     "recorded_by_name) VALUES (%s, %s, %s, 'given', now(), 'Nurse Asha') RETURNING id",
                  w["clinic"], w["admission"], other_order))
    with pytest.raises(psycopg2.Error, match="rx_mar_line_mismatch"):
        _q(c, "SELECT rx_link_mar(%s, %s, %s, NULL)", w["clinic"], mar2, dl)
    assert "rx_write_via_function_only" in _err(c, "INSERT INTO rx_mar_links (clinic_id, mar_id, dispense_line_id) "
                                                   "VALUES (%s, %s, %s)", w["clinic"], mar2, dl)


def test_return_voids_and_reissues_unbilled_charge(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w, [{"ipd_order_id": w["order"], "requested_units": 10, "item_id": w["item"]}])["indent_id"]
    d = dispense(c, w, ind, 10)["dispense_id"]
    dl = str(_q(c, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    rid = str(uuid.uuid4())
    ret = lambda lines, r=rid: _q(c, "SELECT rx_ipd_return(%s, %s, %s::jsonb, 'Course changed', %s, NULL, 'Nurse Asha', false)",
                                  w["clinic"], d, json.dumps(lines), r)
    out = ret([{"dispense_line_id": dl, "qty_units": 4, "restock": True}])
    assert (out["value_paise"], out["credited_paise"], out["replayed"]) == (4 * 450, 0, False)
    assert ret([{"dispense_line_id": dl, "qty_units": 4, "restock": True}])["replayed"] is True    # retry = same return
    live = charges(c, w["admission"])
    assert [(x["qty"], x["ref"]) for x in live] == [(6, f"rx:dl:{dl}:r:{rid}")]
    assert [x["status"] for x in charges(c, w["admission"], live=False)].count("void") == 1
    assert _bal(c, w["store"], w["batch"]) == 2000 - 10 + 4
    # opened strip: back in and straight out as damage; the last units leave no charge
    ret([{"dispense_line_id": dl, "qty_units": 6, "restock": False}], str(uuid.uuid4()))
    assert charges(c, w["admission"]) == [] and _bal(c, w["store"], w["batch"]) == 2000 - 10 + 4
    with pytest.raises(psycopg2.Error, match="rx_return_exceeds_dispensed"):
        ret([{"dispense_line_id": dl, "qty_units": 1, "restock": True}], str(uuid.uuid4()))
    assert _q(c, "SELECT returned_units FROM rx_indent_lines WHERE indent_id = %s", ind) == 10


def test_return_after_issued_bill_issues_credit_note(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w)["indent_id"]
    d = dispense(c, w, ind)["dispense_id"]
    dl = str(_q(c, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    cur = c.cursor()
    cur.execute("SELECT ipd_initiate_discharge(%s, %s, 'regular', NULL, 'dr')", (w["clinic"], w["admission"]))
    cur.execute("SELECT ipd_accrue_bed_charges(%s, %s, NULL, 'billing')", (w["clinic"], w["admission"]))
    cur.execute("SELECT array_agg(id) FROM ipd_charges WHERE admission_id = %s AND status = 'proposed'", (w["admission"],))
    cur.execute("SELECT ipd_post_charges(%s, %s, %s::uuid[], NULL, 'billing')", (w["clinic"], w["admission"], cur.fetchone()[0]))
    cur.execute("SELECT ipd_generate_final_bill(%s, %s, NULL, 'billing')", (w["clinic"], w["admission"]))
    inv = cur.fetchone()[0]["invoice_id"]
    assert charges(c, w["admission"])[0]["invoice"] == inv                      # the pharmacy line is on the bill
    ret = lambda: _q(c, "SELECT rx_ipd_return(%s, %s, %s::jsonb, 'Not used', %s, NULL, 'N', false)", w["clinic"], d,
                     json.dumps([{"dispense_line_id": dl, "qty_units": 2, "restock": True}]), str(uuid.uuid4()))
    with pytest.raises(psycopg2.Error, match="opd_credit_note_invoice_state"):   # a draft bill is not credited
        ret()
    cur.execute("UPDATE opd_invoices SET status = 'issued' WHERE id = %s", (inv,))
    out = ret()
    assert out["credited_paise"] == 2 * 450 and len(out["credit_notes"]) == 1
    assert _q(c, "SELECT credited_paise FROM opd_invoices WHERE id = %s", inv) == 900
    assert len(charges(c, w["admission"])) == 1                                  # the billed charge is never edited


def test_cancel_only_undispensed_lines(real_pg_conn):
    c = real_pg_conn
    w = ipd_world(c)
    ind = raise_indent(c, w)["indent_id"]
    dispense(c, w, ind, 2)
    with pytest.raises(psycopg2.Error, match="rx_reason_required"):
        _q(c, "SELECT rx_cancel_indent(%s, %s, 'x', NULL)", w["clinic"], ind)
    out = _q(c, "SELECT rx_cancel_indent(%s, %s, 'Patient shifted to ICU', NULL)", w["clinic"], ind)
    assert (out["lines_cancelled"], out["status"]) == (1, "dispensed")
    assert _q(c, "SELECT dispensed_units FROM rx_indent_lines WHERE indent_id = %s", ind) == 2


def test_ipd_owner_purge_cascades_pharmacy_rows(real_pg_conn):
    """ipd_purge_clinic is unchanged and never meets an RX row it cannot delete (MAR links, lines, indents)."""
    c = real_pg_conn
    w = ipd_world(c)
    d = dispense(c, w, raise_indent(c, w)["indent_id"])["dispense_id"]
    dl = str(_q(c, "SELECT id FROM rx_dispense_lines WHERE dispense_id = %s", d))
    mar = str(_q(c, "INSERT INTO ipd_med_administrations (clinic_id, admission_id, order_id, outcome, administered_at, "
                    "recorded_by_name) VALUES (%s, %s, %s, 'given', now(), 'N') RETURNING id",
                 w["clinic"], w["admission"], w["order"]))
    _q(c, "SELECT rx_link_mar(%s, %s, %s, NULL)", w["clinic"], mar, dl)
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("SELECT ipd_purge_clinic(%s)", (w["clinic"],))
        cur.execute("SELECT (SELECT count(*) FROM rx_ipd_indents WHERE clinic_id = %s) + "
                    "(SELECT count(*) FROM rx_dispense_lines WHERE clinic_id = %s) + "
                    "(SELECT count(*) FROM rx_mar_links WHERE clinic_id = %s)", (w["clinic"],) * 3)
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM rx_stores WHERE clinic_id = %s", (w["clinic"],))
        assert cur.fetchone() == (1,)                               # the pharmacy itself is RX's purge, not IPD's
    finally:
        cur.execute("ROLLBACK")


def test_purge_v6_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, b = ipd_world(c), ipd_world(c)
    for w in (a, b):
        dispense(c, w, raise_indent(c, w)["indent_id"])
    out = _q(c, "SELECT rx_purge_clinic(%s)", a["clinic"])
    assert (out["indents"], out["dispenses"]) == (1, 1)
    assert len(charges(c, a["admission"])) == 1                     # the patient's bill keeps its pharmacy line
    assert _q(c, "SELECT count(*) FROM rx_ipd_indents WHERE clinic_id = %s", b["clinic"]) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "139_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    source_check = "SELECT pg_get_constraintdef(oid) LIKE '%%pharmacy%%' FROM pg_constraint WHERE conname = 'ipd_charges_source_check'"
    cur.execute("BEGIN")
    try:   # RX purged but the patients' bills kept: the CHECK must stay wide
        cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM rx_ipd_indents) x")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute(source_check)
        assert cur.fetchone() == (True,)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_139(cur)
        cur.execute("SELECT to_regclass('public.rx_ipd_indents'), to_regclass('public.rx_sales')")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT prosrc LIKE '%%rx_overrides%%' AND prosrc NOT LIKE '%%rx_ipd_indents%%' FROM pg_proc "
                    "WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (True,)                            # purge v5 restored
        cur.execute(source_check)
        assert cur.fetchone() == (False,)                           # no pharmacy charge left: the 116 CHECK is back
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
    finally:
        cur.execute("ROLLBACK")


def test_rx_ipd_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_guard_ipd_doc', 'rx_check_indent_line', 'rx_lock_admission_active',
                                                 'rx_ipd_unit_price', 'rx_ipd_post_charge', 'rx_indent_refresh',
                                                 'rx_raise_indent', 'rx_dispense_indent', 'rx_cancel_indent',
                                                 'rx_receive_dispense', 'rx_link_mar', 'rx_ipd_return')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
