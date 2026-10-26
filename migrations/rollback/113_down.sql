-- Rollback Migration 113. Pre-113 code runs unchanged on a 113 schema; this only drops the key.
DROP INDEX IF EXISTS uq_opd_receipts_idempotency;
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_idempotency_key_len;
ALTER TABLE opd_receipts DROP COLUMN IF EXISTS idempotency_key;
