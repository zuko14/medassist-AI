-- ============================================================================
-- Rollback 125: Diagnostic OS foundation (DX-M1)
-- Refused while any dx_sites row exists: purge each DX clinic first with
-- SELECT dx_purge_clinic('<clinic_id>') (owner action). Any clinic with
-- features.dx_enabled = true should have it cleared by the owner beforehand.
-- The normal rollback is a code revert: pre-125 code runs unchanged on 125.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.dx_sites') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_sites) THEN
        RAISE EXCEPTION '125_down: dx_sites rows exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS dx_update_settings(uuid, jsonb, integer);
DROP FUNCTION IF EXISTS dx_purge_clinic(uuid);
DROP FUNCTION IF EXISTS dx_purging(uuid);
DROP TABLE IF EXISTS dx_sites;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_dx_state_check,
    DROP COLUMN IF EXISTS dx_settings, DROP COLUMN IF EXISTS dx_state;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '125_dx_os_foundation.sql';
