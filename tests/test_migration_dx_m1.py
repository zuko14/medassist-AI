"""Migration 125 (Diagnostic OS foundation, DX-M1) against real PostgreSQL.

clinics.dx_state / dx_settings defaults and CHECK, dx_sites keys and the
composite branch FK (no cross-tenant site), RLS forced, the settings CAS,
the owner purge, and a guarded rollback + idempotent re-apply.
HTTP behaviour: tests/test_dx_feature_gate.py.
"""

import json
import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "125_dx_os_foundation.sql", MIG / "rollback" / "125_rollback_dx_os_foundation.sql"


def unwind_125(cur) -> None:
    """Inside an open transaction: purge every DX clinic (the only way the
    rollback allows) and run the 125 rollback. Rollback tests of earlier
    migrations call this first (via unwind_124), since 125 sits on their objects."""
    from tests.test_migration_dx_m2 import unwind_126
    unwind_126(cur)   # newest first: 126 redefines dx_purge_clinic and sits on lab_tests
    cur.execute("SELECT to_regclass('public.dx_sites') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT dx_purge_clinic(x.clinic_id) FROM (SELECT DISTINCT clinic_id FROM dx_sites) x")
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


def _clinic(c, name="DX Lab"):
    return str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES (%s, %s, 'diagstream', true) "
                     "RETURNING id", name, "+9177" + uuid.uuid4().hex[:8]))


def _branch(c, clinic, name="Main"):
    return str(_q(c, "INSERT INTO branches (clinic_id, name) VALUES (%s, %s) RETURNING id", clinic, name))


def _site(c, clinic, branch, code="MAIN"):
    return str(_q(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) VALUES (%s, %s, %s, 'Main lab') "
                     "RETURNING id", clinic, branch, code))


def test_existing_clinics_are_off_with_empty_settings(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    assert _q(c, "SELECT dx_state FROM clinics WHERE id = %s", clinic) == "OFF"
    assert _q(c, "SELECT dx_settings FROM clinics WHERE id = %s", clinic) == {}
    assert "clinics_dx_state_check" in _err(c, "UPDATE clinics SET dx_state = 'ON' WHERE id = %s", clinic)
    assert "clinics_dx_state_check" in _err(c, "UPDATE clinics SET dx_settings = '[]' WHERE id = %s", clinic)
    for state in ("ACTIVE", "WIND_DOWN", "OFF"):
        _q(c, "UPDATE clinics SET dx_state = %s WHERE id = %s", state, clinic)


def test_site_keys_and_checks(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    b1, b2 = _branch(c, clinic, "One"), _branch(c, clinic, "Two")
    _site(c, clinic, b1, "HUB1")
    assert "dx_sites_clinic_branch_key" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) "
                                                   "VALUES (%s, %s, 'HUB2', 'Again')", clinic, b1)
    assert "dx_sites_clinic_code_key" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) "
                                                 "VALUES (%s, %s, 'HUB1', 'Dup code')", clinic, b2)
    assert "dx_sites_code_check" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) "
                                            "VALUES (%s, %s, 'hub-2', 'Bad code')", clinic, b2)
    assert "dx_sites_kind_check" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name, kind) "
                                            "VALUES (%s, %s, 'HUB2', 'Bad kind', 'warehouse')", clinic, b2)
    assert "dx_sites_display_name_check" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) "
                                                    "VALUES (%s, %s, 'HUB2', ' ')", clinic, b2)
    # the same code is fine in another clinic
    other = _clinic(c, "Other Lab")
    _site(c, other, _branch(c, other), "HUB1")


def test_site_cannot_sit_on_another_clinics_branch(real_pg_conn):
    c = real_pg_conn
    mine, theirs = _clinic(c), _clinic(c, "Theirs")
    their_branch = _branch(c, theirs)
    assert "dx_sites_branch_fk" in _err(c, "INSERT INTO dx_sites (clinic_id, branch_id, code, display_name) "
                                           "VALUES (%s, %s, 'XX', 'Leak')", mine, their_branch)


def test_rls_forced_and_api_roles_locked_out(real_pg_conn):
    c = real_pg_conn
    assert _q(c, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'dx_sites'::regclass")
    assert _q(c, "SELECT count(*) FROM pg_policies WHERE tablename = 'dx_sites'") >= 1
    for role in ("anon", "authenticated"):
        if _q(c, "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)", role):
            assert not _q(c, "SELECT has_table_privilege(%s, 'dx_sites', 'SELECT')", role)
            assert not _q(c, "SELECT has_function_privilege(%s, 'dx_purge_clinic(uuid)', 'EXECUTE')", role)


def test_settings_cas_bumps_version_and_refuses_a_stale_write(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    s1 = _q(c, "SELECT dx_update_settings(%s, %s::jsonb, 0)", clinic, json.dumps({"approval_levels": 2}))
    assert s1 == {"approval_levels": 2, "version": 1}
    s2 = _q(c, "SELECT dx_update_settings(%s, %s::jsonb, 1)", clinic, json.dumps({"approval_levels": 3}))
    assert s2["version"] == 2 and s2["approval_levels"] == 3
    # a second admin still holding version 1 is refused; nothing changes
    assert "dx_settings_conflict" in _err(c, "SELECT dx_update_settings(%s, %s::jsonb, 1)", clinic,
                                          json.dumps({"approval_levels": 2}))
    assert _q(c, "SELECT dx_settings FROM clinics WHERE id = %s", clinic) == s2
    assert "dx_settings_invalid" in _err(c, "SELECT dx_update_settings(%s, '[]'::jsonb, 2)", clinic)
    assert "dx_clinic_not_found" in _err(c, "SELECT dx_update_settings(%s, '{}'::jsonb, 0)", str(uuid.uuid4()))


def test_purge_removes_only_that_clinics_sites(real_pg_conn):
    c = real_pg_conn
    a, b = _clinic(c), _clinic(c, "Keep")
    _site(c, a, _branch(c, a))
    _site(c, b, _branch(c, b))
    _q(c, "UPDATE clinics SET dx_state = 'ACTIVE' WHERE id = %s", a)
    assert _q(c, "SELECT dx_purge_clinic(%s)", a)["sites"] == 1
    assert _q(c, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", a) == 0
    assert _q(c, "SELECT count(*) FROM dx_sites WHERE clinic_id = %s", b) == 1
    assert _q(c, "SELECT dx_state FROM clinics WHERE id = %s", a) == "OFF"
    assert _q(c, "SELECT current_setting('kriya.dx_purge_clinic', true)") in (None, "")


def test_dx_functions_pin_search_path(real_pg_conn):
    rows = _q(real_pg_conn, """SELECT count(*) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
                                 AND proname IN ('dx_purging', 'dx_purge_clinic', 'dx_update_settings')
                                 AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x
                                                 WHERE x LIKE 'search_path=%%')""")
    assert rows == 0


def test_rollback_refuses_sites_then_reapplies(real_pg_conn):
    c = real_pg_conn
    clinic = _clinic(c)
    _site(c, clinic, _branch(c, clinic))
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        with pytest.raises(psycopg2.Error) as e:
            cur.execute(DOWN.read_text(encoding="utf-8"))
        assert "125_down" in str(e.value)
    finally:
        cur.execute("ROLLBACK")
    cur.execute("BEGIN")
    try:
        unwind_125(cur)
        cur.execute("SELECT to_regclass('public.dx_sites')")
        assert cur.fetchone() == (None,)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'clinics' "
                    "AND column_name IN ('dx_state', 'dx_settings')")
        assert cur.fetchone() == (0,)
        for _ in range(2):   # idempotent re-apply
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT dx_state FROM clinics WHERE id = %s", (clinic,))
        assert cur.fetchone() == ("OFF",)
    finally:
        cur.execute("ROLLBACK")
