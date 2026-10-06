-- Rollback Migration 099. Drops per-test delivery history; the connector falls
-- back to whole-order dedup (top-ups for partially approved orders stop).
ALTER TABLE integration_processed_reports DROP COLUMN IF EXISTS test_names;
