-- Migration 021: Enforce OPD token-number uniqueness at the database level
-- Run in Supabase SQL Editor
--
-- Migration 019 added token_number/queue_status but only a non-unique index.
-- Two concurrent "Check In" requests for the same doctor+date can both read
-- the same MAX(token_number) before either commits, assigning the SAME
-- token to two different patients. This mirrors the fix already applied to
-- appointment-slot double-booking (see migration 008's idx_unique_active_slot)
-- — a partial UNIQUE index, not application-level locking, is the fix.

CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_queue_token
    ON appointments (clinic_id, doctor_name, appointment_date, token_number)
    WHERE token_number IS NOT NULL;

-- Verify
SELECT indexname FROM pg_indexes
WHERE tablename = 'appointments' AND indexname = 'idx_unique_queue_token';
