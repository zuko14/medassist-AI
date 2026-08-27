-- ============================================================================
-- Migration 054: Payment Events Clinic ID, Provider Event ID, and Duplicate Index Cleanup
-- ============================================================================
-- 1. Adds clinic_id and provider_event_id to payment_events (T4.1 / KRIYA audit)
-- 2. Creates unique index on provider_event_id for webhook idempotency
-- 3. Safely backfills clinic_id from appointments table
-- 4. Drops duplicate slot index idx_unique_active_slot (T4.2)
-- ============================================================================

-- Step 1: Add columns
ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id);
ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS provider_event_id TEXT;

-- Step 2: Unique index for webhook provider event deduplication
CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_event_provider_id
    ON payment_events (provider_event_id)
    WHERE provider_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_payment_events_clinic_id
    ON payment_events (clinic_id)
    WHERE clinic_id IS NOT NULL;

-- Step 3: Backfill clinic_id from appointments table
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = 'public' AND tablename = 'payment_events'
    ) THEN
        -- Temporarily disable append-only trigger for migration backfill
        IF EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname = 'trg_payment_events_no_update'
        ) THEN
            ALTER TABLE payment_events DISABLE TRIGGER trg_payment_events_no_update;
        END IF;

        UPDATE payment_events pe
        SET clinic_id = a.clinic_id
        FROM appointments a
        WHERE pe.booking_id = a.id
          AND pe.clinic_id IS NULL
          AND a.clinic_id IS NOT NULL;

        -- Re-enable append-only trigger
        IF EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname = 'trg_payment_events_no_update'
        ) THEN
            ALTER TABLE payment_events ENABLE TRIGGER trg_payment_events_no_update;
        END IF;
    END IF;
END $$;

-- Step 4: Drop duplicate active slot index (T4.2)
-- uq_appointment_active_slot (from 043) is retained as the single canonical index.
DROP INDEX IF EXISTS idx_unique_active_slot;
