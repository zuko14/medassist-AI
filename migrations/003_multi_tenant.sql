-- ============================================================
-- Migration 003: Multi-Tenant SaaS Support
-- Adds clinics table and clinic_id to all existing tables
-- ============================================================

-- 1. Master clinics table
CREATE TABLE IF NOT EXISTS clinics (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name              TEXT        NOT NULL,
    whatsapp_number   TEXT        UNIQUE NOT NULL,
    -- whatsapp_number = the E.164 number patients message TO
    -- e.g. "+919876543210"
    plan              TEXT        NOT NULL DEFAULT 'basic'
                                  CHECK (plan IN ('basic','pro','enterprise')),
    config            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    /*
      config JSONB schema:
      {
        "meta_phone_number_id": "1234567890",
        "meta_access_token":    "EAAxxxx",
        "clinic_name":          "Apollo Hyderabad",
        "doctor_name":          "Dr. Ravi Kumar",
        "system_prompt":        "custom AI personality (optional)",
        "language":             "en | hi | te | ta | kn",
        "logo_url":             "https://...",
        "timezone":             "Asia/Kolkata"
      }
    */
    is_active         BOOLEAN     NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_clinics_updated_at()
RETURNS TRIGGER
SET search_path = ''
AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER clinics_updated_at
  BEFORE UPDATE ON clinics
  FOR EACH ROW EXECUTE FUNCTION update_clinics_updated_at();

-- 2. Add clinic_id to all existing tables
ALTER TABLE patients          ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE appointments      ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE conversations     ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE lab_reports       ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE prescriptions     ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE analytics_events  ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE doctors           ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE doctor_leaves     ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;
ALTER TABLE hospital_holidays ADD COLUMN IF NOT EXISTS clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE;

-- 3. Create indexes for tenant-scoped queries
CREATE INDEX IF NOT EXISTS idx_patients_clinic      ON patients(clinic_id);
CREATE INDEX IF NOT EXISTS idx_appointments_clinic   ON appointments(clinic_id);
CREATE INDEX IF NOT EXISTS idx_conversations_clinic  ON conversations(clinic_id);
CREATE INDEX IF NOT EXISTS idx_lab_reports_clinic     ON lab_reports(clinic_id);
CREATE INDEX IF NOT EXISTS idx_prescriptions_clinic   ON prescriptions(clinic_id);
CREATE INDEX IF NOT EXISTS idx_analytics_clinic       ON analytics_events(clinic_id);
CREATE INDEX IF NOT EXISTS idx_doctors_clinic          ON doctors(clinic_id);
CREATE INDEX IF NOT EXISTS idx_doctor_leaves_clinic    ON doctor_leaves(clinic_id);
CREATE INDEX IF NOT EXISTS idx_holidays_clinic         ON hospital_holidays(clinic_id);

-- 4. Enable RLS on clinics table
ALTER TABLE clinics ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access for service_role" ON clinics FOR ALL TO service_role USING (true);

-- NOTE: Run 004_seed_first_clinic.sql next to seed and backfill
