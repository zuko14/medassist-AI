-- Migration 018: Post-discharge health check-in tracking
-- Run in Supabase SQL Editor
--
-- Separate from the existing `followup_sent` column, which drives a
-- same-day/next-day satisfaction survey. These two new flags drive a
-- distinct clinical safety check-in on day+3 and day+7 after the visit.

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS health_checkin_3d_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS health_checkin_7d_sent BOOLEAN NOT NULL DEFAULT false;

-- Verify
SELECT column_name FROM information_schema.columns
WHERE table_name = 'appointments' AND column_name LIKE 'health_checkin%';
