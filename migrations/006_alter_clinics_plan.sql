-- Migration 006: Add plan and features columns to clinics table
-- Run in Supabase SQL Editor

ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'basic'
        CHECK (plan IN ('basic', 'pro', 'enterprise')),
    ADD COLUMN IF NOT EXISTS features JSONB NOT NULL DEFAULT '{}';

-- Index for any future plan-level queries (e.g. billing reports)
CREATE INDEX IF NOT EXISTS idx_clinics_plan ON clinics (plan);

-- Backfill: every existing clinic defaults to 'basic' (already handled by DEFAULT)
-- To manually upgrade a clinic:
--   UPDATE clinics SET plan = 'pro' WHERE id = '<clinic_uuid>';
--   UPDATE clinics SET plan = 'enterprise' WHERE id = '<clinic_uuid>';

-- To set a per-clinic feature override (e.g. sell lab_reports to a basic client):
--   UPDATE clinics SET features = '{"lab_reports": true}' WHERE id = '<clinic_uuid>';

-- Verify
SELECT id, name, plan, features FROM clinics LIMIT 10;
