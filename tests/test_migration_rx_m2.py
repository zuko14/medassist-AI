"""Migration 135 (Pharmacy OS catalogue, RX-M2) against real PostgreSQL.

Keys and composite FKs (no cross-tenant generic / manufacturer / barcode); barcode
unique per clinic only; item guard (immutables, schedule only upward, reason for a
tax change, never deleted); change log written and append-only; the transactional
import (dry run writes nothing, one bad row undoes all, re-apply is a no-op); the
deterministic resolver; purge v2; guarded rollback + idempotent re-apply.
HTTP behaviour: tests/test_rx_catalogue_api.py.
"""

import json
from pathlib import Path

import psycopg2
import pytest

from tests.test_migration_rx_m1 import _clinic, _err, _q

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "135_rx_catalogue.sql", MIG / "rollback" / "135_rollback_rx_catalogue.sql"


def unwind_135(cur) -> None:
    """Inside an open transaction: unwind 136 (newest first), purge, run the 135 rollback."""
    from tests.test_migration_rx_m3 import unwind_136
    unwind_136(cur)
    cur.execute("SELECT to_regclass('public.rx_items') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT clinic_id FROM rx_items UNION "
                "SELECT clinic_id FROM rx_generics UNION SELECT clinic_id FROM rx_manufacturers) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _generic(c, clinic, name="Paracetamol", strength="500 mg", form="tablet"):
    return str(_q(c, "INSERT INTO rx_generics (clinic_id, name, strength, dosage_form) VALUES (%s, %s, %s, %s) "
                     "RETURNING id", clinic, name, strength, form))


def _item(c, clinic, generic, code="PCM500", brand="Calpol 500", schedule="none", **kw):
    cols = {"clinic_id": clinic, "item_code": code, "brand_name": brand, "generic_id": generic, "schedule": schedule,
            "base_unit": "tablet", "pack_size": 10, "hsn_code": "30049099", "tax_rate_bps": 1200, **kw}
    return str(_q(c, f"INSERT INTO rx_items ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                  *cols.values()))


def _patch(c, clinic, item, patch, reason=None):
    return _q(c, "SELECT rx_update_item(%s, %s, %s::jsonb, NULL, 'Asha', %s)", clinic, item, json.dumps(patch), reason)


def test_keys_and_composite_fks(real_pg_conn):
    c = real_pg_conn
    mine, theirs = _clinic(c), _clinic(c, "Theirs")
    g = _generic(c, mine)
    assert "rx_generics_class_key" in _err(c, "INSERT INTO rx_generics (clinic_id, name, strength, dosage_form) "
                                              "VALUES (%s, 'Paracetamol', '500 mg', 'tablet')", mine)
    _q(c, "INSERT INTO rx_manufacturers (clinic_id, name) VALUES (%s, 'Cipla')", mine)
    assert "uq_rx_manufacturers_name" in _err(c, "INSERT INTO rx_manufacturers (clinic_id, name) VALUES (%s, 'CIPLA')", mine)
    their_g = _generic(c, theirs)
    assert "rx_items_generic_fk" in _err(c, "INSERT INTO rx_items (clinic_id, item_code, brand_name, generic_id, "
                                            "base_unit, pack_size, hsn_code, tax_rate_bps) VALUES (%s, 'LEAK', 'Leak', "
                                            "%s, 'tablet', 10, '3004', 1200)", mine, their_g)
    assert "rx_items_drug_has_generic" in _err(c, "INSERT INTO rx_items (clinic_id, item_code, brand_name, base_unit, "
                                                  "pack_size, hsn_code, tax_rate_bps) VALUES (%s, 'NOGEN', 'No generic', "
                                                  "'tablet', 10, '3004', 1200)", mine)
    assert "rx_items_tax_rate_bps_check" in _err(c, "INSERT INTO rx_items (clinic_id, item_code, brand_name, item_kind, "
                                                    "base_unit, pack_size, hsn_code, tax_rate_bps) VALUES (%s, 'BAD', "
                                                    "'Bad tax', 'otc', 'piece', 1, '3004', 1000)", mine)
    item = _item(c, mine, g)
    assert "rx_items_code_key" in _err(c, "INSERT INTO rx_items (clinic_id, item_code, brand_name, generic_id, base_unit, "
                                          "pack_size, hsn_code, tax_rate_bps) VALUES (%s, 'PCM500', 'Dup', %s, "
                                          "'tablet', 10, '3004', 1200)", mine, g)
    # barcode: unique per clinic, the same code is fine in another clinic
    _q(c, "INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode) VALUES (%s, %s, '8901234567890')", mine, item)
    assert "rx_item_barcodes_barcode_key" in _err(c, "INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode) "
                                                     "VALUES (%s, %s, '8901234567890')", mine, item)
    their_item = _item(c, theirs, their_g)
    _q(c, "INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode) VALUES (%s, %s, '8901234567890')", theirs, their_item)
    assert "rx_item_barcodes_item_fk" in _err(c, "INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode) "
                                                 "VALUES (%s, %s, '1111111111')", mine, their_item)
    assert "rx_item_aliases_alias_norm_check" in _err(c, "INSERT INTO rx_item_aliases (clinic_id, item_id, alias_norm, "
                                                         "source) VALUES (%s, %s, 'Calpol-500', 'manual')", mine, item)


def test_item_guard_immutables_and_schedule_only_up(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    item = _item(c, clinic, _generic(c, clinic), schedule="H")
    for col, val in (("item_code", "NEW"), ("base_unit", "capsule")):
        assert f"rx_item_field_locked:{col}" in _err(c, f"UPDATE rx_items SET {col} = %s WHERE id = %s", val, item)
    for down in ("none", "G"):
        assert "rx_item_field_locked:schedule" in _err(c, "UPDATE rx_items SET schedule = %s WHERE id = %s", down, item)
    _q(c, "UPDATE rx_items SET schedule = 'H1' WHERE id = %s", item)                 # upgrade allowed
    _q(c, "UPDATE rx_items SET schedule = 'X' WHERE id = %s", item)
    assert "rx_item_field_locked:schedule" in _err(c, "UPDATE rx_items SET schedule = 'narcotic' WHERE id = %s", item)
    assert "rx_item_delete_refused" in _err(c, "DELETE FROM rx_items WHERE id = %s", item)


def test_tax_change_needs_reason_and_every_change_is_logged(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    item = _item(c, clinic, _generic(c, clinic))
    assert "rx_item_reason_required" in _err(c, "UPDATE rx_items SET tax_rate_bps = 500 WHERE id = %s", item)
    with pytest.raises(psycopg2.Error, match="rx_item_reason_required"):
        _patch(c, clinic, item, {"tax_rate_bps": 500})
    out = _patch(c, clinic, item, {"tax_rate_bps": 500, "hsn_code": "3004", "rack_location": "A-3"}, "GST notification 5/2026")
    assert (out["tax_rate_bps"], out["hsn_code"], out["rack_location"]) == (500, "3004", "A-3")
    rows = _q(c, "SELECT json_agg(json_build_array(field, old_value, new_value, changed_by_name, reason) ORDER BY field) "
                 "FROM rx_item_changes WHERE item_id = %s", item)
    assert rows == [["hsn_code", "30049099", "3004", "Asha", "GST notification 5/2026"],
                    ["rack_location", None, "A-3", "Asha", "GST notification 5/2026"],
                    ["tax_rate_bps", "1200", "500", "Asha", "GST notification 5/2026"]]
    assert _q(c, "SELECT current_setting('kriya.rx_reason', true)") in (None, "")
    assert "rx_append_only:rx_item_changes" in _err(c, "UPDATE rx_item_changes SET reason = 'x' WHERE item_id = %s", item)
    assert "rx_append_only:rx_item_changes" in _err(c, "DELETE FROM rx_item_changes WHERE item_id = %s", item)
    with pytest.raises(psycopg2.Error, match="rx_item_not_found"):
        _patch(c, _clinic(c, "Other"), item, {"brand_name": "Leak"})              # another clinic's item


IMPORT_ROW = {"item_code": "AUG625", "brand_name": "Augmentin 625 Duo", "generic_name": "Amoxicillin + Clavulanic acid",
              "strength": "500 mg + 125 mg", "dosage_form": "tablet", "route": "oral", "manufacturer": "GSK",
              "item_kind": "drug", "schedule": "H", "base_unit": "tablet", "pack_size": 10, "pack_label": "strip",
              "allow_loose": True, "hsn_code": "30042019", "tax_rate_bps": 1200, "reorder_level_units": 50,
              "reorder_qty_units": 200, "is_chronic": False, "is_cold_chain": False, "rack_location": None,
              "barcode": "8901234500011", "symbology": "ean13", "_row": 2}


def _import(c, clinic, rows, apply):
    return _q(c, "SELECT rx_import_items(%s, %s::jsonb, %s, NULL, 'Import')", clinic, json.dumps(rows), apply)


def test_import_dry_run_apply_idempotent_and_atomic(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    second = {**IMPORT_ROW, "item_code": "AUG625B", "brand_name": "Clavam 625", "manufacturer": "gsk",
              "barcode": "8901234500028", "_row": 3}
    dry = _import(c, clinic, [IMPORT_ROW, second], False)
    assert (dry["would_create"], dry["errors"], dry["applied"]) == (2, [], False)
    assert _q(c, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", clinic) == 0           # nothing written
    assert _q(c, "SELECT count(*) FROM rx_generics WHERE clinic_id = %s", clinic) == 0
    done = _import(c, clinic, [IMPORT_ROW, second], True)
    assert (done["would_create"], done["applied"]) == (2, True)
    assert _q(c, "SELECT count(*) FROM rx_generics WHERE clinic_id = %s", clinic) == 1          # generic on first sight
    assert _q(c, "SELECT count(*) FROM rx_manufacturers WHERE clinic_id = %s", clinic) == 1     # case-insensitive
    again = _import(c, clinic, [IMPORT_ROW, second], True)
    assert (again["would_create"], again["would_update"], again["unchanged"]) == (0, 0, 2)
    assert _q(c, "SELECT count(*) FROM rx_item_changes WHERE clinic_id = %s", clinic) == 0
    # an update goes through the guard and is logged with the import reason
    upd = _import(c, clinic, [{**IMPORT_ROW, "tax_rate_bps": 500}], True)
    assert upd["would_update"] == 1
    assert _q(c, "SELECT reason FROM rx_item_changes WHERE clinic_id = %s", clinic) == "item master import"
    # one bad row (schedule downgrade / barcode on another item) undoes the whole file
    bad = [{**IMPORT_ROW, "item_code": "NEW1", "brand_name": "Fresh", "barcode": None, "_row": 2},
           {**IMPORT_ROW, "schedule": "none", "_row": 3},
           {**second, "barcode": "8901234500011", "_row": 4}]
    res = _import(c, clinic, bad, True)
    assert res["applied"] is False and [e["row"] for e in res["errors"]] == [3, 4]
    assert "rx_item_field_locked:schedule" in res["errors"][0]["error"]
    assert "rx_barcode_taken" in res["errors"][1]["error"]
    assert _q(c, "SELECT count(*) FROM rx_items WHERE clinic_id = %s AND item_code = 'NEW1'", clinic) == 0


def test_resolver_is_deterministic_and_tenant_scoped(real_pg_conn):
    c = real_pg_conn
    clinic, other = _clinic(c), _clinic(c, "Other")
    g = _generic(c, clinic, "Pantoprazole + Domperidone", "40 mg + 10 mg", "capsule")
    pand = _item(c, clinic, g, "PAND", "Pan-D")
    pan40 = _item(c, clinic, _generic(c, clinic, "Pantoprazole", "40 mg"), "PAN40", "Pan 40")
    _item(c, clinic, g, "PANTOD", "Pantocid DSR")
    _q(c, "INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode) VALUES (%s, %s, '8900000000017')", clinic, pan40)
    _q(c, "INSERT INTO rx_item_aliases (clinic_id, item_id, alias_norm, source) VALUES (%s, %s, 'pan d caps', "
          "'pharmacist_confirmed')", clinic, pand)
    _item(c, other, _generic(c, other, "Pantoprazole", "40 mg"), "PAN40", "Pan 40")

    def ids(q):
        return [(r["item_code"], r["source"]) for r in _q(c, "SELECT rx_resolve_items(%s, %s, 5)", clinic, q)]

    assert ids("8900000000017") == [("PAN40", "barcode")]
    assert ids("Pan-D Caps")[0] == ("PAND", "alias")
    assert ids("pan d") == [("PAND", "brand")]                         # "Pan-D" exact; Pantocid does not contain it
    first = ids("pan")
    assert first == ids("pan") == ids("PAN ")                          # same input → same order, every time
    assert {code for code, _ in first} == {"PAND", "PAN40", "PANTOD"}  # never the other clinic's Pan 40 twice
    assert ids("pan40") == [("PAN40", "code")]
    assert ids("x") == [] and ids("") == [] and ids("%%") == []
    _q(c, "UPDATE rx_items SET is_active = false WHERE id = %s", pan40)
    assert "PAN40" not in {code for code, _ in ids("pan")}


def test_purge_v2_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    a, b = _clinic(c), _clinic(c, "Keep")
    _import(c, a, [IMPORT_ROW], True)
    _import(c, b, [IMPORT_ROW], True)
    assert _q(c, "SELECT rx_purge_clinic(%s)", a)["items"] == 1
    assert _q(c, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", a) == 0
    assert _q(c, "SELECT count(*) FROM rx_generics WHERE clinic_id = %s", a) == 0
    assert _q(c, "SELECT count(*) FROM rx_items WHERE clinic_id = %s", b) == 1
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        from tests.test_migration_rx_m3 import unwind_136
        unwind_136(cur)
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "135_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_135(cur)
        cur.execute("SELECT to_regclass('public.rx_items'), to_regprocedure('rx_normalize(text)')")
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT prosrc LIKE '%%rx_items%%' FROM pg_proc WHERE proname = 'rx_purge_clinic'")
        assert cur.fetchone() == (False,)                               # purge v1 restored
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT count(*) FROM rx_items")
        assert cur.fetchone() == (0,)
    finally:
        cur.execute("ROLLBACK")


def test_rx_catalogue_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_normalize', 'rx_schedule_rank', 'rx_guard_item',
                                                 'rx_guard_append_only', 'rx_update_item', 'rx_import_items',
                                                 'rx_resolve_items')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0
