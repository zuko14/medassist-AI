-- ============================================================================
-- Migration 083: details on a diagnostic catalogue row
-- ============================================================================
-- A health package is sold on what it contains ("CBC, LFT, KFT, Lipid,
-- Thyroid, HbA1c -- 62 parameters"); a scan on what it covers ("MRI Brain with
-- contrast, report in 24h"). lab_tests had a name, price, sample type and
-- prep, but nowhere to say that, so a patient choosing between a ₹999 and a
-- ₹2,499 package on WhatsApp saw two names and two prices.
--
-- One nullable TEXT column, capped at 500 characters (it is printed on the
-- WhatsApp test card, whose body Meta caps at 1024). NULL means "no details",
-- which every existing row is: the card prints nothing extra for it.
--
-- No index is added for the new lab-interest events: a plain CREATE INDEX on
-- the live analytics_events table would block the inserts every patient tap
-- makes while it builds, and the existing clinic_id index already serves the
-- Insights read (one clinic, one window).
--
-- PURELY ADDITIVE. Re-runnable. Safe with the previous build running --
-- nothing reads the column until the new build is deployed.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

ALTER TABLE lab_tests ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_description_len_check;
ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_description_len_check
    CHECK (description IS NULL OR char_length(description) <= 500);

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'lab_tests' AND column_name = 'description';
