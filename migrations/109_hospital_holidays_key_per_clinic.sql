-- ============================================================================
-- Migration 109: one holiday per date, PER CLINIC
-- ============================================================================
-- hospital_holidays was created (migration 001) with holiday_date as its only
-- PRIMARY KEY, before the table had a clinic_id (migration 003). That key spans
-- every tenant: once one clinic (or the 001 seed rows) has a holiday on a date,
-- every other clinic's POST /admin/holidays for that date fails with a primary-key
-- violation (500). Kriya Ketu bug register D-27.
--
-- This replaces it with the tenant-correct key (clinic_id, holiday_date).
-- clinic_id is already NOT NULL (migration 075), and the old key already
-- guarantees one row per date, so no row can collide under the new key: no data
-- changes. Idempotent. Rollback: migrations/rollback/109_down.sql.
-- ============================================================================

DO $$
DECLARE
    pk_name text;
    pk_cols text[];
BEGIN
    SELECT c.conname, array_agg(a.attname::text ORDER BY k.ord)
      INTO pk_name, pk_cols
      FROM pg_constraint c
      CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.conrelid = 'hospital_holidays'::regclass AND c.contype = 'p'
     GROUP BY c.conname;

    IF pk_cols IS NOT DISTINCT FROM ARRAY['clinic_id', 'holiday_date'] THEN
        RETURN;  -- already applied
    END IF;
    IF pk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE hospital_holidays DROP CONSTRAINT %I', pk_name);
    END IF;
    ALTER TABLE hospital_holidays
        ADD CONSTRAINT hospital_holidays_pkey PRIMARY KEY (clinic_id, holiday_date);
END $$;
