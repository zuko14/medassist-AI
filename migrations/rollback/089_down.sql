-- ============================================================================
-- Rollback Migration 089: Dental treatment plans
-- ============================================================================
-- DESTRUCTIVE for dental plan data (plans, sitting links/notes/reviews, quota
-- counters). Appointments themselves are kept; only the added columns go.

DROP FUNCTION IF EXISTS release_message_quota(UUID, TEXT, TEXT);
DROP FUNCTION IF EXISTS reserve_message_quota(UUID, TEXT, TEXT, INTEGER);
DROP TABLE IF EXISTS clinic_message_quota_usage CASCADE;
DROP TABLE IF EXISTS dental_doctor_digests CASCADE;
DROP INDEX IF EXISTS uq_appointments_plan_sitting_active;
DROP INDEX IF EXISTS idx_appointments_treatment_plan;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_sitting_fields_valid;
ALTER TABLE appointments
    DROP COLUMN IF EXISTS review_received_at,
    DROP COLUMN IF EXISTS review_rating,
    DROP COLUMN IF EXISTS review_requested_at,
    DROP COLUMN IF EXISTS amount_collected_paise,
    DROP COLUMN IF EXISTS sitting_notes,
    DROP COLUMN IF EXISTS sitting_number,
    DROP COLUMN IF EXISTS treatment_plan_id;
DROP TABLE IF EXISTS dental_treatment_plans CASCADE;
ALTER TABLE platform_invoices DROP CONSTRAINT IF EXISTS platform_invoices_messaging_addon_nonneg;
ALTER TABLE platform_invoices DROP COLUMN IF EXISTS messaging_addon_paise;
ALTER TABLE specialty_treatments DROP CONSTRAINT IF EXISTS specialty_treatments_default_sittings_range;
ALTER TABLE specialty_treatments DROP COLUMN IF EXISTS default_sittings;
ALTER TABLE doctors DROP CONSTRAINT IF EXISTS doctors_whatsapp_phone_format;
ALTER TABLE doctors DROP COLUMN IF EXISTS whatsapp_phone;
