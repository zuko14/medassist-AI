-- Rollback Migration 097. Drops home-collection details from every booking;
-- the bookings themselves (and their payments) are kept as centre visits.
DROP INDEX IF EXISTS idx_appointments_phlebotomist_day;
DROP INDEX IF EXISTS idx_appointments_home_collection;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_coords_check;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_status_check;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_home_collection_complete;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_collection_mode_check;
ALTER TABLE appointments
    DROP COLUMN IF EXISTS collection_notes,
    DROP COLUMN IF EXISTS collection_status_at,
    DROP COLUMN IF EXISTS collection_status,
    DROP COLUMN IF EXISTS phlebotomist_id,
    DROP COLUMN IF EXISTS home_collection_fee_paise,
    DROP COLUMN IF EXISTS collection_contact_phone,
    DROP COLUMN IF EXISTS collection_lng,
    DROP COLUMN IF EXISTS collection_lat,
    DROP COLUMN IF EXISTS collection_landmark,
    DROP COLUMN IF EXISTS collection_address,
    DROP COLUMN IF EXISTS collection_slot,
    DROP COLUMN IF EXISTS collection_mode;
ALTER TABLE clinic_admins DROP COLUMN IF EXISTS phone, DROP COLUMN IF EXISTS full_name;
