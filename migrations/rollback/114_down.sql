-- Rollback Migration 114. Pre-114 code never calls these; dropping them changes no data.
DROP TRIGGER IF EXISTS trg_ipd_wards_guard ON ipd_wards;
DROP FUNCTION IF EXISTS ipd_guard_ward();
DROP FUNCTION IF EXISTS opd_save_invoice_draft(uuid, uuid, timestamptz, jsonb, integer, text, text);
