-- ============================================================
-- Migration 004: Seed First Clinic & Backfill
-- Run AFTER 003_multi_tenant.sql
-- Replace placeholder values with your actual clinic data
-- ============================================================

-- 1. Insert your existing hospital as the first clinic
-- IMPORTANT: Update these values before running!
INSERT INTO clinics (name, whatsapp_number, plan, config)
VALUES (
  'TestHospital',
  '+917981945956',
  'pro',
  '{
    "meta_phone_number_id": "",
    "meta_access_token":    "",
    "clinic_name":          "TestHospital",
    "doctor_name":          "Dr. Admin",
    "language":             "en",
    "timezone":             "Asia/Kolkata"
  }'::jsonb
)
ON CONFLICT (whatsapp_number) DO NOTHING;

-- 2. Backfill clinic_id on all existing rows
UPDATE patients          SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE appointments      SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE conversations     SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE lab_reports       SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE prescriptions     SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE analytics_events  SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE doctors           SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE doctor_leaves     SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;
UPDATE hospital_holidays SET clinic_id = (SELECT id FROM clinics LIMIT 1) WHERE clinic_id IS NULL;

-- 3. Make clinic_id NOT NULL after backfill
-- NOTE: Only run these after confirming all rows are backfilled
-- ALTER TABLE patients          ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE appointments      ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE conversations     ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE lab_reports       ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE prescriptions     ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE analytics_events  ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE doctors           ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE doctor_leaves     ALTER COLUMN clinic_id SET NOT NULL;
-- ALTER TABLE hospital_holidays ALTER COLUMN clinic_id SET NOT NULL;
