"""Migration 115 (investigation & procedure orders) against real PostgreSQL.

Schema shape, tenant-safe composite FKs, the order guard, the bill-line guard,
IPD order mirroring, ten concurrent placements under one key, and a clean
rollback + idempotent re-apply. HTTP behaviour: tests/test_orders_api_e2e.py.
"""

import json
import threading
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "115_investigation_and_procedure_orders.sql"
DOWN = MIG / "rollback" / "115_investigation_and_procedure_orders.down.sql"


def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


@pytest.fixture
def world(real_pg_conn):
    c = real_pg_conn
    clinic = str(_q(c, """INSERT INTO clinics (name, whatsapp_number, plan, is_active, features, opd_state, opd_settings)
                          VALUES ('Orders Clinic', %s, 'polyclinic', true, '{"opd_enabled": true}', 'READY', %s::jsonb)
                          RETURNING id""", "+9193" + uuid.uuid4().hex[:8],
                    json.dumps({"service_catalog": [{"code": "XRAY_CHEST", "name": "X-ray chest PA",
                                                     "item_type": "diagnostic", "price_paise": 45000}]})))
    doctor = str(_q(c, """INSERT INTO doctors (clinic_id, name, department, specialization, is_active)
                          VALUES (%s, 'Dr. Rao', 'Medicine', 'Physician', true) RETURNING id""", clinic))
    patient = str(_q(c, """INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'Ravi Kumar', %s) RETURNING id""",
                     clinic, "+9192" + uuid.uuid4().hex[:8]))
    appt = str(_q(c, """INSERT INTO appointments (clinic_id, patient_id, patient_phone, patient_name, doctor_id,
                        doctor_name, department, appointment_date, appointment_time, status, is_walk_in, booking_type)
                        VALUES (%s, %s, '+919000000001', 'Ravi Kumar', %s, 'Dr. Rao', 'Medicine', '2026-11-02',
                                %s, 'completed', false, 'consultation') RETURNING id""",
                  clinic, patient, doctor, f"{9 + uuid.uuid4().int % 9:02d}:{uuid.uuid4().int % 60:02d}:00"))
    enc = str(_q(c, """INSERT INTO opd_encounters (clinic_id, appointment_id, patient_id, doctor_id)
                       VALUES (%s, %s, %s, %s) RETURNING id""", clinic, appt, patient, doctor))
    cbc = str(_q(c, """INSERT INTO lab_tests (clinic_id, name, sample_type, price_paise)
                       VALUES (%s, 'CBC', 'EDTA blood', 35000) RETURNING id""", clinic))
    return {"c": c, "clinic": clinic, "doctor": doctor, "patient": patient, "appt": appt, "enc": enc, "cbc": cbc}


def _place(conn, w, items, key=None):
    return _q(conn, "SELECT opd_place_orders(%s, %s, %s::jsonb, %s, NULL, 'drrao')",
              w["clinic"], w["enc"], json.dumps(items), key or uuid.uuid4().hex)


CBC = lambda w: {"order_type": "investigation", "catalog_type": "lab_test", "catalog_reference_id": w["cbc"]}  # noqa: E731


def test_schema_constraints_and_indexes_exist(real_pg_conn):
    names = {r[0] for r in _rows(real_pg_conn, "SELECT conname FROM pg_constraint WHERE conrelid = 'opd_orders'::regclass")}
    assert {"opd_orders_clinic_id_id_key", "opd_orders_source_shape", "opd_orders_catalog_shape",
            "opd_orders_cancel_consistent", "opd_orders_sample_consistent", "opd_orders_encounter_fk",
            "opd_orders_patient_fk", "opd_orders_doctor_fk", "opd_orders_ipd_order_fk"} <= names
    idx = {r[0] for r in _rows(real_pg_conn, "SELECT indexname FROM pg_indexes WHERE tablename = 'opd_orders'")}
    assert {"uq_opd_orders_idempotency", "uq_opd_orders_ipd_order", "idx_opd_orders_worklist"} <= idx
    assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'opd_orders'") is True


def _rows(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchall()
    cur.close()
    return out


def test_placement_snapshots_catalogue_price_and_sample(world):
    out = _place(world["c"], world, [CBC(world), {"order_type": "investigation", "catalog_type": "service_catalog",
                                                  "catalog_code": "XRAY_CHEST", "urgency": "stat"}])
    assert out["replayed"] is False and len(out["orders"]) == 2
    cbc, xray = out["orders"]
    assert (cbc["item_name"], cbc["unit_price_paise"], cbc["sample_type"]) == ("CBC", 35000, "EDTA blood")
    assert (xray["item_name"], xray["unit_price_paise"], xray["urgency"]) == ("X-ray chest PA", 45000, "stat")
    # a later price change never reaches a placed order
    _q(world["c"], "UPDATE lab_tests SET price_paise = 99900 WHERE id = %s", world["cbc"])
    assert _q(world["c"], "SELECT unit_price_paise FROM opd_orders WHERE id = %s", cbc["id"]) == 35000


def test_cross_tenant_patient_is_rejected_by_the_composite_fk(world):
    other = str(_q(world["c"], """INSERT INTO clinics (name, whatsapp_number, plan, is_active)
                                  VALUES ('Other', %s, 'polyclinic', true) RETURNING id""", "+9191" + uuid.uuid4().hex[:8]))
    foreign = str(_q(world["c"], "INSERT INTO patients (clinic_id, name, phone) VALUES (%s, 'X', %s) RETURNING id",
                     other, "+9190" + uuid.uuid4().hex[:8]))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _q(world["c"], """INSERT INTO opd_orders (clinic_id, source, encounter_id, appointment_id, patient_id, doctor_id,
                          order_type, catalog_type, item_name, ordered_by_name)
                          VALUES (%s, 'opd', %s, %s, %s, %s, 'investigation', 'custom', 'ECG', 'x')""",
           world["clinic"], world["enc"], world["appt"], foreign, world["doctor"])


def test_guard_locks_content_billing_state_and_deletes(world):
    oid = _place(world["c"], world, [CBC(world)])["orders"][0]["id"]
    for sql, code in (("UPDATE opd_orders SET unit_price_paise = 1 WHERE id = %s", "opd_record_locked"),
                      ("UPDATE opd_orders SET status = 'billed' WHERE id = %s", "opd_order_billing_is_derived"),
                      ("UPDATE opd_orders SET status = 'completed', resulted_at = now() WHERE id = %s",
                       "opd_order_sample_required"),
                      ("DELETE FROM opd_orders WHERE id = %s", "opd_append_only")):
        with pytest.raises(psycopg2.Error) as e:
            _q(world["c"], sql, oid)
        assert code in str(e.value)
    # no barcode, no collection
    with pytest.raises(psycopg2.errors.CheckViolation):
        _q(world["c"], "UPDATE opd_orders SET status = 'sample_collected', sample_collected_at = now() WHERE id = %s", oid)


def test_bill_line_follows_issue_and_void_and_is_unique(world):
    c = world["c"]
    inv = str(_q(c, """INSERT INTO opd_invoices (clinic_id, appointment_id, patient_id, status, subtotal_paise)
                       VALUES (%s, %s, %s, 'draft', 0) RETURNING id""", world["clinic"], world["appt"], world["patient"]))
    out = _place(c, world, [CBC(world)])
    assert out["billing"]["invoice_id"] == inv and out["billing"]["added"] == 1   # auto-synced to the open draft
    oid = out["orders"][0]["id"]
    assert _q(c, "SELECT subtotal_paise FROM opd_invoices WHERE id = %s", inv) == 35000
    with pytest.raises(psycopg2.Error) as e:   # a second live line for the same order
        _q(c, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity,
                 unit_price_paise, order_id) VALUES (%s, %s, 9, 'diagnostic', 'CBC', 1, 35000, %s)""",
           world["clinic"], inv, oid)
    assert "opd_order_already_billed" in str(e.value)
    _q(c, "UPDATE opd_invoices SET status = 'issued' WHERE id = %s", inv)
    assert _q(c, "SELECT status FROM opd_orders WHERE id = %s", oid) == "billed"
    _q(c, "UPDATE opd_invoices SET status = 'void', voided_at = now(), void_reason = 'Raised in error' WHERE id = %s", inv)
    assert _q(c, "SELECT status FROM opd_orders WHERE id = %s", oid) == "ordered"


def test_ten_concurrent_placements_with_one_key_insert_once(world, real_postgres_uri):
    key = "conc-" + uuid.uuid4().hex[:12]
    results, errors = [], []
    barrier = threading.Barrier(10)

    def worker():
        conn = psycopg2.connect(real_postgres_uri)
        conn.autocommit = True
        try:
            barrier.wait()
            results.append(_place(conn, world, [CBC(world)], key=key))
        except Exception as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert _q(world["c"], "SELECT count(*) FROM opd_orders WHERE clinic_id = %s", world["clinic"]) == 1
    assert len({r["orders"][0]["id"] for r in results}) == 1
    assert sum(1 for r in results if not r["replayed"]) == 1


def test_ipd_investigation_order_is_mirrored_and_charged(world):
    c, clinic = world["c"], world["clinic"]
    ward = str(_q(c, "INSERT INTO ipd_wards (clinic_id, code, name) VALUES (%s, %s, 'Ward') RETURNING id",
                  clinic, "W" + uuid.uuid4().hex[:5]))
    bed = str(_q(c, "INSERT INTO ipd_beds (clinic_id, ward_id, code, bed_category) VALUES (%s, %s, 'B1', 'general') "
                    "RETURNING id", clinic, ward))
    admitted = _q(c, "SELECT ipd_admit(%s, %s::jsonb, %s, NULL, 'desk')", clinic,
                  json.dumps({"patient_id": world["patient"], "admitting_doctor_id": world["doctor"], "reason": "Fever"}),
                  bed)
    adm = str(admitted["id"] if isinstance(admitted, dict) else admitted)
    order = str(_q(c, """INSERT INTO ipd_orders (clinic_id, admission_id, order_type, description, lab_test_id, priority,
                         ordered_by_doctor_id, ordered_by_name) VALUES (%s, %s, 'investigation', 'CBC', %s, 'stat', %s, 'drrao')
                         RETURNING id""", clinic, adm, world["cbc"], world["doctor"]))
    row = _rows(c, "SELECT source, status, item_name, unit_price_paise, urgency FROM opd_orders WHERE ipd_order_id = %s", order)
    assert row == [("ipd", "ordered", "CBC", 35000, "stat")]
    assert _rows(c, "SELECT status, unit_price_paise FROM ipd_charges WHERE source_ref = %s", f"order:{order}") \
        == [("proposed", 35000)]
    _q(c, """UPDATE ipd_orders SET status = 'discontinued', ended_at = now(), end_reason = 'Not needed'
             WHERE id = %s""", order)
    assert _q(c, "SELECT status FROM opd_orders WHERE ipd_order_id = %s", order) == "cancelled"
    assert _q(c, "SELECT status FROM ipd_charges WHERE source_ref = %s", f"order:{order}") == "void"
    _q(c, "SELECT ipd_purge_clinic(%s)", clinic)   # cascades through the mirrored row
    assert _q(c, "SELECT count(*) FROM opd_orders WHERE clinic_id = %s", clinic) == 0


def test_rollback_refuses_open_orders_then_reapplies(world):
    _place(world["c"], world, [CBC(world)])
    cur = world["c"].cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "115_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        # the shared test DB holds other suites' orders: the owner purge is the only path that removes them
        cur.execute("SELECT ipd_purge_clinic(clinic_id) FROM (SELECT DISTINCT clinic_id FROM opd_orders) x")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.opd_orders'), to_regprocedure('opd_place_orders(uuid,uuid,jsonb,text,uuid,text)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'opd_invoice_items' AND column_name = 'order_id'")
        assert cur.fetchone()[0] == 0
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE proname IN ('opd_place_orders', 'opd_bill_orders', 'opd_cancel_order',
                       'opd_guard_order', 'ipd_order_to_worklist') AND NOT EXISTS
                       (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
