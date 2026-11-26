"""Migration 143 (Revenue Guard, RI-01) against real PostgreSQL.

Two tables with RLS enabled + forced; one open finding per (rule, source row) and one exemption record; the state
check (open / resolved / dismissed shapes); no trigger on any existing table; idempotent re-apply; rollback drops
everything it added and re-applies. Rule behaviour: tests/test_ri_rules.py.
"""

import uuid
from pathlib import Path

import psycopg2
import pytest

MIG = Path(__file__).resolve().parents[1] / "migrations"
UP, DOWN = MIG / "143_revenue_guard.sql", MIG / "rollback" / "143_rollback_revenue_guard.sql"


def _q(c, sql, *args):
    cur = c.cursor()
    cur.execute(sql, args)
    return cur.fetchone()[0] if cur.description else None


def _err(c, sql, *args) -> str:
    with pytest.raises(psycopg2.Error) as e:
        _q(c, sql, *args)
    return str(e.value)


def _clinic(c):
    return str(_q(c, "INSERT INTO clinics (name, whatsapp_number, plan, is_active) VALUES ('RI', %s, 'polyclinic', true) "
                     "RETURNING id", "+9181" + uuid.uuid4().hex[:8]))


def test_tables_rls_and_registration(real_pg_conn):
    from app.tenancy import TENANT_OWNED_TABLES
    c = real_pg_conn
    for t in ("ri_runs", "ri_findings"):
        assert t in TENANT_OWNED_TABLES
        assert _q(c, "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = %s::regclass", t)
    assert _q(c, "SELECT column_default FROM information_schema.columns WHERE table_name = 'clinics' "
                 "AND column_name = 'ri_settings'") == "'{}'::jsonb"
    # zero triggers on any table: ri_* have none, and 143 added none to an existing table
    assert _q(c, "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN "
                 "('ri_runs'::regclass, 'ri_findings'::regclass)") == 0
    assert "CREATE TRIGGER" not in UP.read_text(encoding="utf-8")
    assert _q(c, """SELECT count(*) FROM pg_proc WHERE proname = 'ri_scan' AND NOT EXISTS (
                      SELECT 1 FROM unnest(coalesce(proconfig, '{}')) x WHERE x LIKE 'search_path=%%')""") == 0


def test_one_open_finding_per_source_and_state_shapes(real_pg_conn):
    c = real_pg_conn
    clinic, src = _clinic(c), str(uuid.uuid4())
    ins = ("INSERT INTO ri_findings (clinic_id, rule_code, kind, source_table, source_id, service_date, first_seen_on, "
           "last_seen_on) VALUES (%s, 'order_done_not_billed', 'leakage', 'opd_orders', %s, CURRENT_DATE, CURRENT_DATE, "
           "CURRENT_DATE) RETURNING id")
    f = str(_q(c, ins, clinic, src))
    assert "uq_ri_findings_open" in _err(c, ins, clinic, src)
    assert "ri_findings_state" in _err(c, "UPDATE ri_findings SET status = 'dismissed', resolution = 'dismissed', "
                                          "resolved_on = CURRENT_DATE WHERE id = %s", f)   # no reason / by
    assert "dismiss_reason" in _err(c, "UPDATE ri_findings SET status = 'dismissed', resolution = 'dismissed', "
                                       "resolved_on = CURRENT_DATE, dismissed_by = %s, dismiss_reason = 'no' WHERE id = %s",
                                    str(uuid.uuid4()), f)
    assert "ri_findings_recovered" in _err(c, "UPDATE ri_findings SET status = 'resolved', resolution = 'auto_closed', "
                                              "resolved_on = CURRENT_DATE, recovered_paise = 10 WHERE id = %s", f)
    _q(c, "UPDATE ri_findings SET status = 'resolved', resolution = 'billed', resolved_on = CURRENT_DATE, "
          "recovered_paise = 10 WHERE id = %s", f)
    assert _q(c, ins, clinic, src)                                   # a closed finding frees the slot
    assert "rule_code" in _err(c, ins.replace("order_done_not_billed", "made_up"), clinic, src)
    _q(c, "INSERT INTO ri_runs (clinic_id, run_date) VALUES (%s, CURRENT_DATE)", clinic)
    assert "ri_runs_day_key" in _err(c, "INSERT INTO ri_runs (clinic_id, run_date) VALUES (%s, CURRENT_DATE)", clinic)


def test_idempotent_and_rollback_reapply(real_pg_conn):
    c = real_pg_conn
    cur = c.cursor()
    cur.execute("BEGIN")
    try:
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute(DOWN.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.ri_findings'), to_regclass('public.ri_runs'), "
                    "to_regprocedure('ri_scan(uuid,date,timestamptz,jsonb,text[])')")
        assert cur.fetchone() == (None, None, None)
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'clinics' "
                    "AND column_name = 'ri_settings'")
        assert cur.fetchone()[0] == 0
        for _ in range(2):
            cur.execute(UP.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('public.ri_findings') IS NOT NULL")
        assert cur.fetchone()[0]
    finally:
        cur.execute("ROLLBACK")
