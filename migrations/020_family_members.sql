-- Migration 020: Family / dependent profiles for shared-phone booking
-- Run in Supabase SQL Editor

CREATE TABLE IF NOT EXISTS family_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id VARCHAR(64) NOT NULL,
    primary_phone VARCHAR(20) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    relationship VARCHAR(30) NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    UNIQUE (clinic_id, primary_phone, full_name)
);

CREATE INDEX IF NOT EXISTS idx_family_members_lookup
    ON family_members (clinic_id, primary_phone);

-- Enable RLS and grant service_role access (used by backend API/Bot)
ALTER TABLE family_members ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role access for family_members" ON family_members;
CREATE POLICY "Service role access for family_members" ON family_members
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Verify
SELECT table_name FROM information_schema.tables WHERE table_name = 'family_members';
