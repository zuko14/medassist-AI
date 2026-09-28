-- Rollback Migration 093: re-run migrations/092_whatsapp_leads.sql FIRST to restore
-- the 092 function (it does not reference the column), then drop the column.
ALTER TABLE patients DROP COLUMN IF EXISTS data_consent_declined_at;
