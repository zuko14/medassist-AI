-- Rollback 079: remove the platform owner financial tracking tables.
--
-- ORDER: roll the APPLICATION back first. The /platform/finance/* endpoints
-- are the only readers; with the old build deployed, nothing touches these
-- tables and this script is safe to run at any time.
--
-- DATA LOSS WARNING — unlike 077/078 this migration created real tables, and
-- the owner's books live in them: negotiated per-clinic rates, the expense
-- history, and every invoice with its paid/unpaid state. None of it is
-- reconstructible from anywhere else in the schema. The guard below refuses
-- to drop anything once real data has been entered; export first if you
-- genuinely intend to discard it:
--
--   \copy (SELECT * FROM platform_invoices)      TO 'invoices.csv'      CSV HEADER
--   \copy (SELECT * FROM platform_expenses)      TO 'expenses.csv'      CSV HEADER
--   \copy (SELECT * FROM platform_billing_rates) TO 'billing_rates.csv' CSV HEADER
--
-- ...then re-run with the force flag set:
--   psql -v ROLLBACK_079_FORCE=1 -f 079_down.sql
--
-- No existing table was altered by 079, so there is nothing to restore.

SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    n_invoices INT := 0;
    n_rates    INT := 0;
    n_expenses INT := 0;
    forced     BOOLEAN := coalesce(current_setting('ROLLBACK_079_FORCE', true), '') <> '';
BEGIN
    IF to_regclass('public.platform_invoices') IS NOT NULL THEN
        EXECUTE 'SELECT COUNT(*) FROM platform_invoices' INTO n_invoices;
    END IF;
    IF to_regclass('public.platform_billing_rates') IS NOT NULL THEN
        EXECUTE 'SELECT COUNT(*) FROM platform_billing_rates' INTO n_rates;
    END IF;
    -- Seeded rows sit at amount_paise = 0; only a real figure counts as data
    -- the owner would mind losing.
    IF to_regclass('public.platform_expenses') IS NOT NULL THEN
        EXECUTE 'SELECT COUNT(*) FROM platform_expenses WHERE amount_paise > 0' INTO n_expenses;
    END IF;

    IF NOT forced AND (n_invoices + n_rates + n_expenses) > 0 THEN
        RAISE EXCEPTION
            'Refusing to drop the platform books: % invoice(s), % rate override(s), % priced expense(s). Export them, then re-run with -v ROLLBACK_079_FORCE=1',
            n_invoices, n_rates, n_expenses;
    END IF;
END $$;

DROP TABLE IF EXISTS platform_invoices;
DROP TABLE IF EXISTS platform_expenses;
DROP TABLE IF EXISTS platform_billing_rates;

SELECT 'rollback_079_complete' AS status;
