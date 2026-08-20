-- ============================================================
-- Migration 035: Fix Multi-Tenant Unique Constraints
--
-- Replaces single-column UNIQUE(phone) constraints on:
--   1. conversations (conversations_phone_key)
--   2. patients (patients_phone_key)
-- with composite UNIQUE(clinic_id, phone) constraints.
--
-- Root cause: In multi-tenant architecture, patients and conversations
-- must be scoped to a clinic. Single-column UNIQUE(phone) caused
-- constraint violations (e.g. conversations_phone_key) when patients
-- interact across multiple clinics or during concurrent webhook delivery.
-- ============================================================

DO $$
DECLARE
    default_clinic_id UUID;
BEGIN
    -- Get default clinic ID for backfilling any orphaned records
    SELECT id INTO default_clinic_id FROM clinics ORDER BY created_at LIMIT 1;

    -- ────────────────────────────────────────────────────────────
    -- 1. CONVERSATIONS TABLE
    -- ────────────────────────────────────────────────────────────

    -- 1a. Drop legacy single-column UNIQUE constraint if exists
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'conversations_phone_key'
          AND conrelid = 'conversations'::regclass
    ) THEN
        ALTER TABLE conversations DROP CONSTRAINT conversations_phone_key;
    END IF;

    -- Drop any standalone unique index on conversations(phone)
    DROP INDEX IF EXISTS idx_conversations_phone_unique;

    -- 1b. Backfill NULL clinic_id if any exist
    IF default_clinic_id IS NOT NULL THEN
        UPDATE conversations SET clinic_id = default_clinic_id WHERE clinic_id IS NULL;
    END IF;

    -- 1c. Clean up any historical duplicate (clinic_id, phone) rows, keeping the most recent one
    DELETE FROM conversations c1
    USING conversations c2
    WHERE c1.clinic_id = c2.clinic_id
      AND c1.phone = c2.phone
      AND c1.id <> c2.id
      AND (
          c1.last_message_at < c2.last_message_at
          OR (c1.last_message_at = c2.last_message_at AND c1.created_at < c2.created_at)
          OR (c1.last_message_at IS NULL AND c2.last_message_at IS NOT NULL)
          OR (c1.id < c2.id AND c1.created_at = c2.created_at)
      );

    -- 1d. Add composite UNIQUE constraint (clinic_id, phone)
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'conversations_clinic_phone_key'
          AND conrelid = 'conversations'::regclass
    ) THEN
        ALTER TABLE conversations
            ADD CONSTRAINT conversations_clinic_phone_key UNIQUE (clinic_id, phone);
    END IF;

    -- Create lookup index for fast queries
    CREATE INDEX IF NOT EXISTS idx_conversations_clinic_phone
        ON conversations (clinic_id, phone);


    -- ────────────────────────────────────────────────────────────
    -- 2. PATIENTS TABLE
    -- ────────────────────────────────────────────────────────────

    -- 2a. Drop legacy single-column UNIQUE constraint if exists
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'patients_phone_key'
          AND conrelid = 'patients'::regclass
    ) THEN
        ALTER TABLE patients DROP CONSTRAINT patients_phone_key;
    END IF;

    -- Drop any standalone unique index on patients(phone)
    DROP INDEX IF EXISTS idx_patients_phone_unique;

    -- 2b. Backfill NULL clinic_id if any exist
    IF default_clinic_id IS NOT NULL THEN
        UPDATE patients SET clinic_id = default_clinic_id WHERE clinic_id IS NULL;
    END IF;

    -- 2c. Clean up any historical duplicate (clinic_id, phone) rows in patients, keeping the most recently seen
    DELETE FROM patients p1
    USING patients p2
    WHERE p1.clinic_id = p2.clinic_id
      AND p1.phone = p2.phone
      AND p1.id <> p2.id
      AND (
          p1.last_seen_at < p2.last_seen_at
          OR (p1.last_seen_at = p2.last_seen_at AND p1.created_at < p2.created_at)
          OR (p1.last_seen_at IS NULL AND p2.last_seen_at IS NOT NULL)
          OR (p1.id < p2.id AND p1.created_at = p2.created_at)
      );

    -- 2d. Add composite UNIQUE constraint (clinic_id, phone)
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'patients_clinic_phone_key'
          AND conrelid = 'patients'::regclass
    ) THEN
        ALTER TABLE patients
            ADD CONSTRAINT patients_clinic_phone_key UNIQUE (clinic_id, phone);
    END IF;

    -- Create lookup index for fast queries
    CREATE INDEX IF NOT EXISTS idx_patients_clinic_phone
        ON patients (clinic_id, phone);

END $$;
