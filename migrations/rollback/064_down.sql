-- ============================================================================
-- ROLLBACK for migration 064_fix_slot_uniqueness_key.sql
-- ============================================================================
-- NOT auto-applied. scripts/migrate.py globs `migrations/[0-9]*.sql`
-- non-recursively (scripts/migrate.py:95), so this subdirectory is invisible
-- to the runner by design. Run it deliberately:
--
--     psql "$DATABASE_URL" -f migrations/rollback/064_down.sql
--
-- Restores the migration-060 index verbatim and removes 064 from
-- schema_migrations so the forward migration can be re-applied afterwards.
--
-- WARNING: restoring the 060 index re-opens KA-P0-01 (two patients can hold
-- the same physician at the same minute). Only run this to unblock a failed
-- deploy, and re-apply 064 as soon as the blocking issue is resolved.
--
-- WARNING: rows quarantined by 064 step 2 are NOT resurrected. They carry
-- status='cancelled' and refund_reason='duplicate_slot_quarantine'. That is
-- deliberate: un-cancelling them would recreate the double-bookings. To find
-- them:
--     SELECT booking_ref, patient_phone, doctor_name, appointment_date,
--            appointment_time, payment_id
--     FROM appointments
--     WHERE refund_reason = 'duplicate_slot_quarantine';
-- ============================================================================

DROP INDEX IF EXISTS uq_appointment_active_slot;
DROP INDEX IF EXISTS uq_appointment_active_slot_unassigned;

-- Restore the migration-060 index exactly as it was.
CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments(
        clinic_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(doctor_id, '00000000-0000-0000-0000-000000000000'::uuid),
        appointment_date,
        appointment_time
    )
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

-- Restore the migration-060 fallback lookup index.
CREATE INDEX IF NOT EXISTS idx_appointments_doctor_name_fallback
    ON appointments(clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND doctor_id IS NULL;

-- Restore the migration-058 slot lookup index.
DROP INDEX IF EXISTS idx_appointments_slot_lookup;
CREATE INDEX idx_appointments_slot_lookup
    ON appointments(clinic_id, doctor_name, appointment_date, status)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

DELETE FROM schema_migrations WHERE name = '064_fix_slot_uniqueness_key.sql';

SELECT 'rollback_064_complete' AS status;
