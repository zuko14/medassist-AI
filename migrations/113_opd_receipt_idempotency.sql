-- ============================================================================
-- Migration 113: OPD counter receipts are idempotent in the database
-- ============================================================================
-- A cashier's retried or double-clicked "Record payment" carries the same
-- client idempotency key. Until now the key lived only in one process's memory
-- (production runs several), so a retry landing on another worker recorded the
-- money twice. The key is now stored on the receipt and unique per clinic: the
-- second insert fails and the API returns the first receipt.
--
-- Additive + backward compatible: nullable column; pre-113 code never writes
-- it. Idempotent. Rollback: migrations/rollback/113_down.sql.
-- ============================================================================

ALTER TABLE opd_receipts ADD COLUMN IF NOT EXISTS idempotency_key TEXT NULL;
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_idempotency_key_len;
ALTER TABLE opd_receipts ADD CONSTRAINT opd_receipts_idempotency_key_len
    CHECK (idempotency_key IS NULL OR char_length(idempotency_key) BETWEEN 8 AND 64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_receipts_idempotency
    ON opd_receipts (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
