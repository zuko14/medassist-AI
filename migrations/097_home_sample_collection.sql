-- ============================================================================
-- Migration 097: Home sample collection (diagnostic centres)
-- ============================================================================
-- A lab-test booking can now be collected at the patient's home by the
-- centre's phlebotomist instead of at the centre. It is the SAME appointments
-- row (booking_type='lab_test'), so payment, hold expiry, refunds, admin
-- confirm/cancel and reconciliation all apply unchanged; these columns only
-- describe where and by whom the sample is collected.
--
-- Additive and backward compatible:
--   * collection_mode defaults to 'centre', so every existing row and every
--     booking written by code that predates this migration is a centre visit.
--   * every other column is nullable and only ever set on home bookings.
--   * clinic_admins gains full_name / phone (nullable) so a phlebotomist's
--     name and number can be given to the patient.
-- Nothing here changes behaviour until a centre enables home collection in
-- its settings (clinics.config / branches.config -> home_collection).
-- ============================================================================

ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS collection_mode TEXT NOT NULL DEFAULT 'centre',
    ADD COLUMN IF NOT EXISTS collection_slot TEXT,
    ADD COLUMN IF NOT EXISTS collection_address TEXT,
    ADD COLUMN IF NOT EXISTS collection_landmark TEXT,
    ADD COLUMN IF NOT EXISTS collection_lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS collection_lng DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS collection_contact_phone TEXT,
    ADD COLUMN IF NOT EXISTS home_collection_fee_paise INTEGER,
    ADD COLUMN IF NOT EXISTS phlebotomist_id UUID REFERENCES clinic_admins(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS collection_status TEXT,
    ADD COLUMN IF NOT EXISTS collection_status_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS collection_notes TEXT;

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_mode_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_collection_mode_check
    CHECK (collection_mode IN ('centre', 'home'));

-- Only a lab test can be collected at home, and a home booking must carry
-- everything the phlebotomist needs to reach the patient. The one exception is
-- a DPDP erasure (data_retention.anonymize_clinical_records), which replaces
-- the address with '[REDACTED]' and drops the coordinates.
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_home_collection_complete;
ALTER TABLE appointments ADD CONSTRAINT appointments_home_collection_complete
    CHECK (
        collection_mode = 'centre'
        OR (
            booking_type = 'lab_test'
            AND collection_slot IS NOT NULL
            AND collection_status IS NOT NULL
            AND collection_address IS NOT NULL
            AND collection_contact_phone IS NOT NULL
            AND (
                collection_address = '[REDACTED]'
                OR (collection_lat IS NOT NULL AND collection_lng IS NOT NULL)
            )
        )
    );

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_status_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_collection_status_check
    CHECK (collection_status IS NULL OR collection_status IN
        ('unassigned', 'assigned', 'en_route', 'collected', 'delivered', 'failed'));

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_coords_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_collection_coords_check
    CHECK (
        (collection_lat IS NULL OR collection_lat BETWEEN -90 AND 90)
        AND (collection_lng IS NULL OR collection_lng BETWEEN -180 AND 180)
    );

-- The phlebotomist's day view and the assignment sweep.
CREATE INDEX IF NOT EXISTS idx_appointments_home_collection
    ON appointments (clinic_id, appointment_date, collection_status)
    WHERE collection_mode = 'home';
CREATE INDEX IF NOT EXISTS idx_appointments_phlebotomist_day
    ON appointments (phlebotomist_id, appointment_date)
    WHERE phlebotomist_id IS NOT NULL;

ALTER TABLE clinic_admins
    ADD COLUMN IF NOT EXISTS full_name TEXT,
    ADD COLUMN IF NOT EXISTS phone TEXT;
