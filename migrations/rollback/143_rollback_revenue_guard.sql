-- ============================================================================
-- Rollback 143: Kriya Revenue Guard (RI-01)
-- Findings are derived data (the nightly scan rebuilds them from the billing tables), so the rollback drops them.
-- Clear features.revenue_guard for every clinic first (owner action); the normal rollback is a code revert:
-- pre-143 code runs unchanged on 143.
-- ============================================================================

DROP FUNCTION IF EXISTS ri_scan(uuid, date, timestamptz, jsonb, text[]);
DROP TABLE IF EXISTS ri_findings;
DROP TABLE IF EXISTS ri_runs;
ALTER TABLE clinics DROP COLUMN IF EXISTS ri_settings;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '143_revenue_guard.sql';
