-- Migration 017: Doctor slot-timing configuration columns
-- Run in Supabase SQL Editor
--
-- Adds start/end/duration columns so admins can configure real per-doctor
-- shift timings. morning_slots/evening_slots (existing JSONB columns) remain
-- the materialized list the booking engine reads — these new columns are
-- config only, used by the admin write path to regenerate that list.

ALTER TABLE doctors ADD COLUMN IF NOT EXISTS morning_start TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS morning_end TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS evening_start TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS evening_end TIME NULL;
ALTER TABLE doctors ADD COLUMN IF NOT EXISTS slot_duration_minutes INT NOT NULL DEFAULT 30;

-- Backfill existing doctors' config columns from their current materialized
-- slot arrays so the admin edit form has sensible values to pre-fill,
-- assuming the existing default 30-min cadence (matches the seed data in
-- migrations/001_initial_schema.sql).
UPDATE doctors
SET morning_start = '09:00', morning_end = '12:00'
WHERE morning_start IS NULL AND morning_slots IS NOT NULL AND jsonb_array_length(morning_slots) > 0;

UPDATE doctors
SET evening_start = '17:00', evening_end = '19:00'
WHERE evening_start IS NULL AND evening_slots IS NOT NULL AND jsonb_array_length(evening_slots) > 0;

-- Verify
SELECT id, name, morning_start, morning_end, evening_start, evening_end, slot_duration_minutes
FROM doctors ORDER BY created_at;
