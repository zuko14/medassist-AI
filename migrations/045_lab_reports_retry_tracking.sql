-- Migration 045: Add retry tracking columns to lab_reports
-- ─────────────────────────────────────────────────────────────────
-- Purpose: Support automatic retry queue for failed WhatsApp deliveries.
--          Reports that fail due to transient Meta API errors (500s)
--          are marked as 'pending_retry' and re-attempted by the scheduler.
-- ─────────────────────────────────────────────────────────────────

-- retry_count: number of delivery attempts so far (initial send = 1)
ALTER TABLE lab_reports ADD COLUMN IF NOT EXISTS retry_count integer DEFAULT 0;

-- next_retry_at: when the scheduler should next attempt delivery (NULL = not queued)
ALTER TABLE lab_reports ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;

-- Index for the retry worker: efficiently find reports ready for retry
CREATE INDEX IF NOT EXISTS idx_lab_reports_pending_retry
    ON lab_reports (next_retry_at)
    WHERE status = 'pending_retry' AND next_retry_at IS NOT NULL;

-- ─────────────────────────────────────────────────────────────────
-- Rollback:
--   ALTER TABLE lab_reports DROP COLUMN IF EXISTS retry_count;
--   ALTER TABLE lab_reports DROP COLUMN IF EXISTS next_retry_at;
--   DROP INDEX IF EXISTS idx_lab_reports_pending_retry;
-- ─────────────────────────────────────────────────────────────────
