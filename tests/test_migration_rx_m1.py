"""Migration 134 (Pharmacy OS foundation, RX-M1) against real PostgreSQL.

clinics.rx_state / rx_settings defaults and CHECK; rx_stores keys, shapes and the
composite branch / parent FKs (no cross-tenant store); the store guard (identity
immutable, never deleted outside the purge); one active central store per branch;
RLS forced; the settings CAS; the owner purge; a guarded rollback + idempotent
re-apply. HTTP behaviour: tests/test_rx_feature_gate.py.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "134_rx_foundation.sql", MIG / "rollback" / "134_rollback_rx_foundation.sql"


def unwind_134(cur) -> None:
    """Inside an open transaction: purge every pharmacy clinic (the only way the
    rollback allows) and run the 134 rollback. Newest first: unwind_133 must call
    this before its own rollback, because rx_stores_branch_fk sits on 103's
    uq_branches_clinic_id_id; later RX migrations' unwinds call it after their own."""
    from tests.test_migration_rx_m2 import unwind_135
    unwind_135(cur)  # newest first: 135/136 rows hang off rx_stores
    cur.execute("SELECT to_regclass('public.rx_stores') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT rx_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM rx_stores) x")
    cur.execute(DOWN.read_text(encoding="utf-8"))


def _q(conn, sql, *args):
    cur = conn.cursor()
    cur.execute(sql, args)
    out = cur.fetchone()[0] if cur.description else None
    cur.close()
    return out


def _err(conn, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(conn, sql, *args)
    return str(e.value)


def _clinic(c, name="RX Hospital"):
    return str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'polyclinic', true) "
                     "RETURNING id", name, "+9178" + uuid.uuid4().hex[:8]))


def _branch(c, clinic, name="Main"):
    return str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, %s) RETURNING id", clinic, name))


def _store(c, clinic, code="MAIN", store_type="retail_counter", branch=None, parent=None, can_sell=None):
    sell = store_type == "retail_counter" if can_sell is None else can_sell
    return str(_q(c, "INSERT INTO rx_stores (clinic_id, branch_id, parent_store_id, code, name, store_type, can_sell) "
                     "VALUES (%s, %s, %s, %s, 'Main Pharmacy', %s, %s) RETURNING id",
                  clinic, branch, parent, code, store_type, sell))


def test_existing_clinics_are_off_with_empty_settings(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    assert _q(c, "SELECT rx_state FROM clinics WHERE id = %s", clinic) == "OFF"
    assert _q(c, "SELECT rx_settings FROM clinics WHERE id = %s", clinic) == {}
    assert "clinics_rx_state_check" in _err(c, "UPDATE clinics SET rx_state = 'ON' WHERE id = %s", clinic)
    assert "clinics_rx_state_check" in _err(c, "UPDATE clinics SET rx_settings = '[]' WHERE id = %s", clinic)
    assert "rx_state" in _err(c, "UPDATE clinics SET rx_state = NULL WHERE id = %s", clinic)
    for state in ("ACTIVE", "WIND_DOWN", "OFF"):
        _q(c, "UPDATE clinics SET rx_state = %s WHERE id = %s", state, clinic)


def test_store_keys_and_shapes(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    _store(c, clinic, "OPD1")
    assert "rx_stores_code_key" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                           "VALUES (%s, 'OPD1', 'Again', 'central')", clinic)
    assert "rx_stores_code_check" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                             "VALUES (%s, 'opd 2', 'Bad code', 'central')", clinic)
    assert "rx_stores_store_type_check" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                                   "VALUES (%s, 'X1', 'Bad type', 'warehouse')", clinic)
    assert "rx_stores_name_check" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                             "VALUES (%s, 'X1', ' ', 'central')", clinic)
    # only a retail counter sells; a ward sub-store needs its ward and nothing else has one
    assert "rx_stores_sell_shape" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type, can_sell) "
                                             "VALUES (%s, 'C1', 'Central', 'central', true)", clinic)
    assert "rx_stores_ward_shape" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                             "VALUES (%s, 'W1', 'Ward 3', 'ward_substore')", clinic)
    assert "rx_stores_ward_shape" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type, ward_id) "
                                             "VALUES (%s, 'T1', 'OT', 'ot_tray', %s)", clinic, str(uuid.uuid4()))
    # the same code is fine in another clinic
    other = _clinic(c, "Other")
    _store(c, other, "OPD1")


def test_store_cannot_sit_on_another_clinics_branch_or_store(real_pg_conn):
    c = real_pg_conn
    mine, theirs = _clinic(c), _clinic(c, "Theirs")
    their_branch = _branch(c, theirs)
    their_store = _store(c, theirs, "THEIRS")
    assert "rx_stores_branch_fk" in _err(c, "INSERT INTO rx_stores (clinic_id, branch_id, code, name, store_type) "
                                            "VALUES (%s, %s, 'LEAK', 'Leak', 'central')", mine, their_branch)
    assert "rx_stores_parent_fk" in _err(c, "INSERT INTO rx_stores (clinic_id, parent_store_id, code, name, "
                                            "store_type) VALUES (%s, %s, 'LEAK', 'Leak', 'ot_tray')", mine, their_store)


def test_one_active_central_store_per_branch(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    b1, b2 = _branch(c, clinic, "One"), _branch(c, clinic, "Two")
    first = _store(c, clinic, "C1", "central", b1)
    assert "uq_rx_stores_one_central" in _err(c, "INSERT INTO rx_stores (clinic_id, branch_id, code, name, "
                                                 "store_type) VALUES (%s, %s, 'C2', 'Second', 'central')", clinic, b1)
    _store(c, clinic, "C3", "central", b2)                     # another branch: fine
    _store(c, clinic, "C4", "central")                         # one clinic-wide central store …
    assert "uq_rx_stores_one_central" in _err(c, "INSERT INTO rx_stores (clinic_id, code, name, store_type) "
                                                 "VALUES (%s, 'C5', 'Second wide', 'central')", clinic)  # … only one
    _q(c, "UPDATE rx_stores SET is_active = false WHERE id = %s", first)
    _store(c, clinic, "C6", "central", b1)                     # replaces the deactivated one


def test_store_guard_identity_and_no_delete(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    store = _store(c, clinic, "OPD1")
    for col, val in (("code", "'OPD2'"), ("store_type", "'central'"), ("clinic_id", f"'{_clinic(c, 'X')}'")):
        assert f"rx_store_field_locked:{col}" in _err(c, f"UPDATE rx_stores SET {col} = {val} WHERE id = %s", store)
    before = _q(c, "SELECT updated_at FROM rx_stores WHERE id = %s", store)
    _q(c, "UPDATE rx_stores SET name = 'Renamed', drug_licence_no = '20B-TS-1234', is_active = false WHERE id = %s",
       store)
    assert _q(c, "SELECT updated_at FROM rx_stores WHERE id = %s", store) > before
    assert "rx_store_delete_refused" in _err(c, "DELETE FROM rx_stores WHERE id = %s", store)
    assert "rx_stores_no_self_parent" in _err(c, "UPDATE rx_stores SET parent_store_id = id WHERE id = %s", store)


def test_rls_forced_and_api_roles_locked_out(real_pg_conn):
    c = real_pg_conn
    assert _q(c, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'rx_stores'::regclass")
    assert _q(c, "SELECT count(*) FROM pg_policies WHERE tablename = 'rx_stores'") >= 1
    for role in ("anon", "authenticated"):
        if _q(c, "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", role):
            assert not _q(c, "SELECT has_table_privilege(%s, 'rx_stores', 'SELECT')", role)
            assert not _q(c, "SELECT has_function_privilege(%s, 'rx_purge_clinic(uuid)', 'EXECUTE')", role)


def test_settings_cas_bumps_version_and_refuses_a_stale_write(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    s1 = _q(c, "SELECT rx_update_settings(%s, %s::jsonb, 0)", clinic, json.dumps({"return_days": 5}))
    assert s1 == {"return_days": 5, "version": 1}
    s2 = _q(c, "SELECT rx_update_settings(%s, %s::jsonb, 1)", clinic, json.dumps({"return_days": 3}))
    assert s2["version"] == 2 and s2["return_days"] == 3
    assert "rx_settings_conflict" in _err(c, "SELECT rx_update_settings(%s, %s::jsonb, 1)", clinic,
                                          json.dumps({"return_days": 9}))
    assert _q(c, "SELECT rx_settings FROM clinics WHERE id = %s", clinic) == s2
    assert "rx_settings_invalid" in _err(c, "SELECT rx_update_settings(%s, '[]'::jsonb, 2)", clinic)
    assert "rx_clinic_not_found" in _err(c, "SELECT rx_update_settings(%s, '{}'::jsonb, 0)", str(uuid.uuid4()))


def test_purge_removes_only_that_clinics_stores(real_pg_conn):
    c = real_pg_conn
    a, b = _clinic(c), _clinic(c, "Keep")
    parent = _store(c, a, "CENTRAL", "central")
    _store(c, a, "OT1", "ot_tray", parent=parent)              # child + parent go in one statement
    _store(c, b, "MAIN")
    _q(c, "UPDATE clinics SET rx_state = 'ACTIVE', rx_settings = '{\"version\": 3}' WHERE id = %s", a)
    assert _q(c, "SELECT rx_purge_clinic(%s)", a)["stores"] == 2
    assert _q(c, "SELECT count(*) FROM rx_stores WHERE clinic_id = %s", a) == 0
    assert _q(c, "SELECT count(*) FROM rx_stores WHERE clinic_id = %s", b) == 1
    assert _q(c, "SELECT rx_state FROM clinics WHERE id = %s", a) == "OFF"
    assert _q(c, "SELECT rx_settings FROM clinics WHERE id = %s", a) == {}
    assert _q(c, "SELECT current_setting('kriya.rx_purge_clinic', true)") in (None, "")


def test_rx_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('rx_purging', 'rx_raise', 'rx_purge_clinic', 'rx_update_settings',
                                                 'rx_guard_store')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0


def test_no_dependency_on_other_module_tables(real_pg_conn):
    """134 references only clinics, branches (via 103's composite unique index) and itself — never IPD/DX tables."""
    refs = _q(real_pg_conn, """SELECT array_agg(DISTINCT confrelid::regclass::text) FROM pg_constraint
                                WHERE conrelid = 'rx_stores'::regclass AND contype = 'f'""")
    assert set(refs) <= {"clinics", "branches", "rx_stores"}


def test_rollback_refuses_stores_then_reapplies(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    _store(c, clinic, "MAIN")
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "134_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_134(cur)
        cur.execute("SELECT to_regclass('public.rx_stores')")
        assert cur.fetchone() == (None,)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'clinics' "
                    "AND column_name IN ('rx_state', 'rx_settings')")
        assert cur.fetchone() == (0,)
        for _ in range(2):   # idempotent re-apply
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT rx_state FROM clinics WHERE id = %s", (clinic,))
        assert cur.fetchone() == ("OFF",)
    finally:
        cur.execute("ROLLBACK")
