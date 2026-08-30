-- ============================================================================
-- Migration 065: Support the inbound_messages purge and erasure sweeps
-- ============================================================================
-- KA-P2-10.
--
-- inbound_messages (migration 047) had no purge job at all. It grows one row
-- per inbound patient message, forever, and each row holds a raw `phone` plus
-- the sanitised webhook body. At 10k messages/day that is ~3.6M rows a year on
-- the hot deduplication path, and a "delete my data" request left every one of
-- them in place.
--
-- Two new access patterns need indexes:
--
--   1. data_retention.purge_inbound_messages()
--        WHERE status = 'completed' AND completed_at < cutoff
--      Migration 047's idx_inbound_messages_status_retry is keyed on
--      (status, retry_at) and partial to received/failed_retryable, so it
--      cannot serve this. Without a matching index the nightly purge is a
--      full scan of the largest table in the schema.
--
--   2. data_retention.anonymize_clinical_records()
--        WHERE clinic_id = ... AND phone = ...
--      Migration 047 indexes clinic_id alone, which on a busy tenant still
--      scans every message that tenant ever received.
--
-- Both are plain CREATE INDEX IF NOT EXISTS — additive, no data change, and
-- safe to re-run. No rollback script: dropping an index is never required to
-- restore correctness, and `DROP INDEX IF EXISTS` on these two names is the
-- whole reversal if it is ever wanted.
-- ============================================================================

-- 1. Nightly purge of completed rows.
CREATE INDEX IF NOT EXISTS idx_inbound_messages_completed_purge
    ON inbound_messages (completed_at)
    WHERE status = 'completed';

-- 2. Per-patient erasure lookup.
CREATE INDEX IF NOT EXISTS idx_inbound_messages_clinic_phone
    ON inbound_messages (clinic_id, phone);

SELECT 'migration_065_complete' AS status;
