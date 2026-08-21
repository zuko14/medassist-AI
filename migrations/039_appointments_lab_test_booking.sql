-- Migration 039: Lab-test booking support on appointments
--
-- Diagnostic centers book lab tests, not doctor consultations. Rather than
-- forking a second booking table + payment pipeline, appointments gains a
-- booking_type discriminator plus nullable lab-test columns. Every existing
-- payment code path (create_booking_with_payment, process_payment_webhook,
-- expire_stale_bookings, refunds, admin confirm/reject/cancel, daily
-- reconciliation) keeps operating on this one table unchanged for
-- booking_type='consultation' rows.
--
-- IMPORTANT — do not "fix" this: lab_test bookings always have
-- doctor_name = NULL. The partial unique index idx_unique_active_slot
-- (migration 008) is defined as
--   UNIQUE (clinic_id, doctor_name, appointment_date, appointment_time)
--   WHERE status IN ('pending_payment', 'confirmed')
-- Postgres treats every NULL as distinct from every other NULL in a unique
-- index, so this constraint silently does not restrict lab_test rows —
-- which is exactly the desired behavior (many patients can share a
-- collection date). Making doctor_name NOT NULL would break lab-test
-- bookings; it is not a bug to be fixed.

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS booking_type TEXT NOT NULL DEFAULT 'consultation',
    ADD COLUMN IF NOT EXISTS lab_test_id UUID REFERENCES lab_tests(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lab_test_name TEXT;

ALTER TABLE appointments
    DROP CONSTRAINT IF EXISTS appointments_booking_type_check;
ALTER TABLE appointments
    ADD CONSTRAINT appointments_booking_type_check
    CHECK (booking_type IN ('consultation', 'lab_test'));

-- appointment_time was NOT NULL — lab_test bookings only record a
-- collection date (appointment_date); the collection window itself is a
-- branch/clinic-level setting (branches.config / clinics.config), not
-- stored per-booking.
ALTER TABLE appointments
    ALTER COLUMN appointment_time DROP NOT NULL;

ALTER TABLE appointments
    DROP CONSTRAINT IF EXISTS appointments_time_required_for_consultation;
ALTER TABLE appointments
    ADD CONSTRAINT appointments_time_required_for_consultation
    CHECK (
        (booking_type = 'consultation' AND appointment_time IS NOT NULL)
        OR booking_type = 'lab_test'
    );

CREATE INDEX IF NOT EXISTS idx_appointments_booking_type ON appointments(clinic_id, booking_type);
