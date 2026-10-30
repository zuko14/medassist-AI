"""Migration 120 (service master, ICD-10 reference) against real PostgreSQL.

Idempotent backfill of the legacy JSON catalogue, fail-open reads (master row
wins, legacy-only codes still resolve), order placement and GST line tax
reading the master, ICD-10 seed and ranking, and a guarded rollback +
idempotent re-apply. HTTP behaviour: tests/test_masters_api_e2e.py.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_115_orders import _place, _q, _rows, world as _orders_world  # noqa: F401  (fixture)

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP = MIG / "120_clinical_coding_and_masters.sql"
DOWN = MIG / "rollback" / "120_down.sql"
XRAY = {"order_type": "investigation", "catalog_type": "service_catalog", "catalog_code": "XRAY_CHEST"}


def _master(c, clinic, code, price, **kw):
    cols = {"clinic_id": clinic, "service_code": code, "name": kw.pop("name", code.title()), "category": "radiology",
            "standard_price_paise": price, **kw}
    return str(_q(c, f"INSERT INTO billable_services ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                     "RETURNING id", *cols.values()))


def test_schema_and_rls(real_pg_conn):
    names = {r[0] for r in _rows(real_pg_conn, "SELECT conname FROM pg_constraint WHERE conrelid = 'billable_services'::regclass")}
    assert {"billable_services_clinic_code_key", "billable_services_clinic_id_id_key", "billable_services_branch_fk"} <= names
    for t in ("billable_services", "icd10_codes"):
        assert _q(real_pg_conn, "SELECT relforcerowsecurity FROM pg_class WHERE relname = %s", t) is True


def test_backfill_is_idempotent_and_tolerates_malformed_legacy_items(_orders_world):  # noqa: F811
    c, clinic = _orders_world["c"], _orders_world["clinic"]
    items = [{"code": "XRAY_CHEST", "name": "X-ray chest PA", "item_type": "diagnostic", "price_paise": 45000},
             {"name": "Dressing", "price": "150.50", "item_type": "nursing", "tax_rate_bps": 999},
             {"code": "bad code!", "name": "ECG", "price_paise": "abc", "hsn_sac": "x", "active": False},
             {"name": "   "}, "not-an-object"]
    assert _q(c, "SELECT opd_import_service_catalog(%s, %s::jsonb, false)", clinic, json.dumps(items)) == 3
    assert _q(c, "SELECT opd_import_service_catalog(%s, %s::jsonb, false)", clinic, json.dumps(items)) == 0
    rows = {r[0]: r[1:] for r in _rows(c, """SELECT name, service_code, category, standard_price_paise, hsn_sac,
                                              tax_rate_bps, is_active FROM billable_services WHERE clinic_id = %s""", clinic)}
    assert rows["X-ray chest PA"] == ("XRAY_CHEST", "diagnostic", 45000, "999312", 0, True)
    assert rows["Dressing"][1:] == ("nursing", 15050, "999312", 0, True) and rows["Dressing"][0].startswith("SVC_")
    assert rows["ECG"][1:] == ("procedure", 0, "999312", 0, False)   # bad code / price / SAC -> safe defaults
    # overwrite = the legacy settings API writing through
    items[0]["price_paise"] = 50000
    _q(c, "SELECT opd_import_service_catalog(%s, %s::jsonb, true)", clinic, json.dumps(items[:1]))
    assert _q(c, "SELECT standard_price_paise FROM billable_services WHERE clinic_id = %s AND service_code = 'XRAY_CHEST'",
              clinic) == 50000


def test_reads_fail_open_master_wins_and_legacy_only_codes_resolve(_orders_world):  # noqa: F811
    c, clinic = _orders_world["c"], _orders_world["clinic"]
    legacy = _q(c, "SELECT opd_catalog_item(%s, 'XRAY_CHEST')", clinic)       # world's clinic has JSON only
    assert legacy["source"] == "legacy" and legacy["price_paise"] == 45000
    assert _q(c, "SELECT opd_catalog_item(%s, 'NOPE')", clinic) is None
    _master(c, clinic, "XRAY_CHEST", 52000, name="X-ray chest PA (digital)")
    _master(c, clinic, "USG_ABD", 90000, name="USG abdomen")
    master = _q(c, "SELECT opd_catalog_item(%s, 'XRAY_CHEST')", clinic)
    assert (master["source"], master["price_paise"], master["item_type"]) == ("master", 52000, "diagnostic")
    codes = [i["code"] for i in _q(c, "SELECT opd_service_catalog(%s)", clinic)]
    assert sorted(codes) == ["USG_ABD", "XRAY_CHEST"]                            # no duplicate legacy entry


def test_order_placement_prices_from_the_master_and_refuses_inactive(_orders_world):  # noqa: F811
    w = _orders_world
    sid = _master(w["c"], w["clinic"], "XRAY_CHEST", 52000)
    order = _place(w["c"], w, [XRAY])["orders"][0]
    assert (order["unit_price_paise"], order["catalog_code"]) == (52000, "XRAY_CHEST")
    _q(w["c"], "UPDATE billable_services SET is_active = false WHERE id = %s", sid)
    with pytest.raises(psycopg2.Error) as e:   # inactive in the master even though the JSON says active
        _place(w["c"], w, [XRAY])
    assert "opd_order_catalog_not_found" in str(e.value)


def test_gst_line_tax_reads_the_master_rate(_orders_world):  # noqa: F811
    w, c = _orders_world, _orders_world["c"]
    _q(c, """UPDATE clinics SET opd_settings = opd_settings || '{"gstin": "36AABCU9603R1ZM"}'::jsonb WHERE id = %s""",
       w["clinic"])
    _master(c, w["clinic"], "LASER_HAIR", 100000, category="procedure", hsn_sac="999722", tax_rate_bps=1800)
    inv = str(_q(c, """INSERT INTO opd_invoices (clinic_id, appointment_id, patient_id, status, subtotal_paise)
                       VALUES (%s, %s, %s, 'draft', 0) RETURNING id""", w["clinic"], w["appt"], w["patient"]))
    _q(c, """INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, description,
             quantity, unit_price_paise) VALUES (%s, %s, 1, 'procedure', 'LASER_HAIR', 'Laser', 1, 100000)""",
       w["clinic"], inv)
    assert _rows(c, "SELECT hsn_sac, tax_rate_bps, cgst_paise, sgst_paise FROM opd_invoice_items WHERE invoice_id = %s",
                 inv) == [("999722", 1800, 9000, 9000)]


def test_code_is_immutable_and_branch_must_be_the_clinics(_orders_world):  # noqa: F811
    c, clinic = _orders_world["c"], _orders_world["clinic"]
    sid = _master(c, clinic, "PHYSIO_30", 50000, category="physiotherapy")
    with pytest.raises(psycopg2.Error) as e:
        _q(c, "UPDATE billable_services SET service_code = 'PHYSIO_45' WHERE id = %s", sid)
    assert "opd_service_code_immutable" in str(e.value)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _master(c, clinic, "BAD_TAX", 100, tax_rate_bps=1000)
    other = str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('O', %s, 'polyclinic', true) "
                      "RETURNING id", "+9187" + uuid.uuid4().hex[:8]))
    foreign_branch = str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, 'X') RETURNING id", other))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _master(c, clinic, "XB", 100, branch_id=foreign_branch)


def test_icd10_seed_and_ranking(real_pg_conn):
    c = real_pg_conn
    assert _q(c, "SELECT count(*) FROM icd10_codes WHERE is_common") >= 300
    assert _q(c, "SELECT count(*) FROM icd10_codes WHERE chapter_number IS NULL") == 0
    assert _rows(c, "SELECT chapter_number FROM icd10_codes WHERE code IN ('U07.1', 'V89.2', 'Z00.0') ORDER BY code") \
        == [(22,), (20,), (21,)]
    search = lambda q, n=5: [r[0] for r in _rows(c, "SELECT code FROM icd10_search(%s, %s)", q, n)]  # noqa: E731
    assert search("j06")[0] == "J06.9" and search("I10")[0] == "I10"
    assert "K64.9" in search("piles") and search("hypertension")[0] in ("I10", "I11.9", "I12.9", "O13")
    assert search("") == [] and search("%") == [] and search("_") == []          # wildcards are literal
    assert len(search("a", 500)) == 50                                             # limit is clamped


def test_rollback_refuses_master_only_services_then_reapplies(_orders_world):  # noqa: F811
    c = _orders_world["c"]
    _master(c, _orders_world["clinic"], "MASTER_ONLY", 100)
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "120_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        from tests.test_migration_122_ot import unwind_122
        unwind_122(cur)   # 122 references billable_services: newest rollback first
        cur.execute("DELETE FROM billable_services")
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.billable_services'), to_regprocedure('opd_catalog_item(uuid,text)'), "
                    "to_regclass('public.icd10_codes')")
        assert cur.fetchone() == (None, None, None)
        # the restored 115 placement reads the JSON catalogue again
        cur.execute("SELECT prosrc LIKE '%%opd_settings -> ''service_catalog''%%' FROM pg_proc WHERE proname = 'opd_place_orders'")
        assert cur.fetchone() == (True,)
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM icd10_codes")
        assert cur.fetchone()[0] >= 300
    finally:
        cur.execute("ROLLBACK")
