"""Migration 114 against real PostgreSQL: rolls back cleanly, re-applies idempotently, and pins
search_path on its functions. Behaviour is covered end to end in tests/test_opd_billing_real_pg.py
(draft saves) and tests/test_branch_isolation_real_pg.py (ward branch moves)."""

from pathlib import Path

MIG = Path(__file__).resolve().parents[1] / "migrations"


def test_rollback_then_reapply(real_pg_conn):
    cur = real_pg_conn.cursor()
    cur.execute("BEGIN")   # everything below is rolled back
    try:
        cur.execute((MIG / "rollback" / "114_down.sql").read_text(encoding="utf-8"))
        cur.execute("SELECT to_regprocedure('opd_save_invoice_draft(uuid,uuid,timestamptz,jsonb,integer,text,text)')")
        assert cur.fetchone()[0] is None
        for _ in range(2):
            cur.execute((MIG / "114_draft_bill_save_and_ward_branch.sql").read_text(encoding="utf-8"))
        cur.execute("""SELECT proname FROM pg_proc WHERE proname IN ('opd_save_invoice_draft', 'ipd_guard_ward')
                       AND NOT EXISTS (SELECT 1 FROM unnest(coalesce(proconfig, '{}')) c WHERE c LIKE 'search_path=%')""")
        assert cur.fetchall() == []
    finally:
        cur.execute("ROLLBACK")
