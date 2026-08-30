-- ============================================================
-- Migration 056: Fix appointments_status_check for cancelled_dedup
--
-- KA-02: Migration 043 sets rows to 'cancelled_dedup' but the CHECK
-- constraint from 008 does not include it. This makes 043 fail on
-- any database that enforces constraints before migration 056 runs.
--
-- This migration is idempotent: DROP IF EXISTS + ADD.
-- ============================================================

-- Drop the existing constraint (which may or may not include cancelled_dedup)
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_status_check;

-- Re-create with the complete set of valid statuses (including cancelled_dedup)
ALTER TABLE appointments ADD CONSTRAINT appointments_status_check
    CHECK (status IN (
        'confirmed', 'cancelled', 'rescheduled', 'completed', 'no_show',
        'pending_payment', 'expired', 'refunded', 'pending_review',
        'cancelled_dedup'
    ));

-- ── Verify ──────────────────────────────────────────────────────────────
-- Ensure the partial unique index from 043 still exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'uq_appointment_active_slot'
    ) THEN
        RAISE EXCEPTION 'uq_appointment_active_slot index missing — migration 043 may not have been applied';
    END IF;
END $$;

-- NOTE: schema_migrations is written by scripts/migrate.py:124 with the
-- file's SHA256. checksum is NOT NULL (scripts/migrate.py:60), so a
-- self-INSERT here omits it and aborts the migration on any fresh
-- database. Migrations must not record themselves.

SELECT 'migration_056_complete' AS status,
       (SELECT COUNT(*) FROM appointments WHERE status = 'cancelled_dedup') AS dedup_count;
