-- ============================================================
-- Migration 031: Flexible Doctor Shift Cleanup & Sanitization
-- Sanitizes legacy '00:00:00' time entries to NULL and ensures
-- slot arrays for inactive shifts are set to empty arrays.
-- ============================================================

-- ── Step 1: Convert legacy '00:00:00' shift times to NULL ──
UPDATE doctors
SET morning_start = NULL, morning_end = NULL
WHERE morning_start = '00:00:00' OR morning_end = '00:00:00';

UPDATE doctors
SET evening_start = NULL, evening_end = NULL
WHERE evening_start = '00:00:00' OR evening_end = '00:00:00';

-- ── Step 2: Clear phantom slot arrays for inactive shifts ──
-- Where shift times are NULL, ensure slot arrays are empty jsonb arrays []
UPDATE doctors
SET morning_slots = '[]'::jsonb
WHERE morning_start IS NULL AND morning_end IS NULL
  AND morning_slots IS NOT NULL AND morning_slots != '[]'::jsonb;

UPDATE doctors
SET evening_slots = '[]'::jsonb
WHERE evening_start IS NULL AND evening_end IS NULL
  AND evening_slots IS NOT NULL AND evening_slots != '[]'::jsonb;
