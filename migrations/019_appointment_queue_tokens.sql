-- Migration 019: Live OPD queue/token status
-- Run in Supabase SQL Editor

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS token_number INT NULL;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS queue_status VARCHAR(20) NULL
    CHECK (queue_status IN ('waiting', 'in_consultation', 'done'));

-- Index to keep "next token for doctor+date" and "currently serving" lookups fast
CREATE INDEX IF NOT EXISTS idx_appointments_queue
    ON appointments (clinic_id, doctor_name, appointment_date, token_number);

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name IN ('token_number', 'queue_status');
