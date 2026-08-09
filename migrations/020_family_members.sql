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

-- Verify
SELECT table_name FROM information_schema.tables WHERE table_name = 'family_members';
