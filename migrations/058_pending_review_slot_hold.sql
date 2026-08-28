-- ============================================================
-- Migration 058: Include pending_review in slot-hold index
--
-- KA-13: When a payment amount mismatch moves a booking to 
-- pending_review, the slot MUST remain held (another patient
-- cannot book it). The previous partial unique index only
-- covered (confirmed, pending_payment), so pending_review
-- released the slot prematurely.
-- ============================================================

-- Drop and recreate the partial unique index to include pending_review
DROP INDEX IF EXISTS uq_appointment_active_slot;

CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments(clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

-- Also update the slot lookup index
DROP INDEX IF EXISTS idx_appointments_slot_lookup;

CREATE INDEX idx_appointments_slot_lookup
    ON appointments(clinic_id, doctor_name, appointment_date, status)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

INSERT INTO schema_migrations (name) VALUES ('058_pending_review_slot_hold.sql') ON CONFLICT (name) DO NOTHING;

-- Verify
SELECT 'migration_058_complete' AS status;
