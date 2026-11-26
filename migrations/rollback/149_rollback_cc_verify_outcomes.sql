-- ============================================================================
-- Rollback 149: Continuous Care verification and outcomes (CC-M5). Verifications and snapshots are derived from the
-- clinical record and rebuilt by the jobs, so they are dropped. The normal rollback is a code revert.
-- ============================================================================
DROP FUNCTION IF EXISTS cc_purge_patient(uuid, uuid, uuid);
DROP TABLE IF EXISTS cc_outcome_snapshots;
DROP TABLE IF EXISTS cc_verifications;
-- then (manual): DELETE FROM schema_migrations WHERE name = '149_cc_verify_outcomes.sql';
