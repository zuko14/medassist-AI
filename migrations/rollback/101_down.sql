-- Rollback Migration 101. Leave days are lost; phlebotomists become available
-- on every date again.
ALTER TABLE clinic_admins DROP COLUMN IF EXISTS off_dates;
