-- ============================================================================
-- Migration 101: phlebotomist leave days
-- ============================================================================
-- A phlebotomist can be marked off for given dates (leave, sick day). On those
-- dates they receive no home visits, their already-assigned visits are handed
-- to a colleague, and when EVERY phlebotomist who serves a branch is off, that
-- date offers no home collection slots on WhatsApp.
--
-- Stored on the account row (clinic_admins is already tenant-scoped by
-- clinic_id), so no new table, policy or tenancy entry. Past dates are pruned
-- by the app on every write; the array stays small.
--
-- Additive, NOT NULL with a constant default: metadata-only ALTER.
-- Nothing changes until a centre marks someone off.
-- ============================================================================

ALTER TABLE clinic_admins
    ADD COLUMN IF NOT EXISTS off_dates DATE[] NOT NULL DEFAULT '{}';
