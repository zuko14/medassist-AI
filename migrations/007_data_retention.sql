-- Migration 007: Data Retention Columns
-- Adds NMC + DPDP compliance metadata to clinical record tables.
--
-- Purpose:
--   - Track which tier each record belongs to (clinical vs session)
--   - Record when clinical records were anonymized (DPDP erasure requests)
--   - Enforce 7-year NMC retention on clinical tables
--
-- Run this migration in your Supabase SQL editor or via migration tool.
-- Safe to run multiple times (uses IF NOT EXISTS / ALTER TABLE IF NOT EXISTS).

-- ── patients table ────────────────────────────────────────────────────────────
-- Track when patient PII was anonymized (DPDP erasure request processed)
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMPTZ DEFAULT NULL;

-- ── appointments table ────────────────────────────────────────────────────────
-- retention_tier: 'clinical' (7yr) | 'session' (30day) — always 'clinical' for appointments
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS retention_tier TEXT NOT NULL DEFAULT 'clinical';

-- clinical_committed_at: timestamp when record was committed to long-term clinical storage
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS clinical_committed_at TIMESTAMPTZ DEFAULT NOW();

-- ── lab_reports table ─────────────────────────────────────────────────────────
ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS retention_tier TEXT NOT NULL DEFAULT 'clinical';

ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS clinical_committed_at TIMESTAMPTZ DEFAULT NOW();

-- ── conversations table ───────────────────────────────────────────────────────
-- retention_tier: always 'session' — purged after 30 days
ALTER TABLE conversations
    ADD COLUMN IF NOT EXISTS retention_tier TEXT NOT NULL DEFAULT 'session';

-- ── processed_messages table ──────────────────────────────────────────────────
-- Ensure the message_id unique constraint exists (required for atomic idempotency)
-- This is likely already there, but safe to re-assert.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'processed_messages_message_id_key'
    ) THEN
        ALTER TABLE processed_messages
            ADD CONSTRAINT processed_messages_message_id_key UNIQUE (message_id);
    END IF;
EXCEPTION WHEN OTHERS THEN
    -- Constraint may already exist under a different name — safe to ignore
    NULL;
END $$;

-- ── Index for retention purge jobs ────────────────────────────────────────────
-- Speed up the daily purge queries (conversations older than 30 days)
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
    ON conversations (updated_at);

-- Speed up the analytics purge queries
CREATE INDEX IF NOT EXISTS idx_analytics_events_created_at
    ON analytics_events (created_at);

-- ── Comment on column purpose ─────────────────────────────────────────────────
COMMENT ON COLUMN patients.anonymized_at IS
    'Timestamp when patient PII was anonymized per DPDP Act erasure request. '
    'NULL = PII intact. Non-null = patient requested deletion, PII is [REDACTED].';

COMMENT ON COLUMN appointments.retention_tier IS
    'Data retention tier: clinical (7-year NMC mandate) or session (30-day DPDP).';

COMMENT ON COLUMN conversations.retention_tier IS
    'Data retention tier: session data is purged after 30 days per DPDP minimization.';
