-- ============================================================================
-- Rollback 134: Pharmacy OS foundation (RX-M1)
-- Refused while any rx_stores row exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Any clinic with
-- features.pharmacy_enabled = true should have it cleared by the owner beforehand.
-- The normal rollback is a code revert: pre-134 code runs unchanged on 134.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.rx_stores') IS NOT NULL AND EXISTS (SELECT 1 FROM rx_stores) THEN
        RAISE EXCEPTION '134_down: rx_stores rows exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS rx_update_settings(uuid, jsonb, integer);
DROP FUNCTION IF EXISTS rx_purge_clinic(uuid);
DROP TABLE IF EXISTS rx_stores;
DROP FUNCTION IF EXISTS rx_guard_store();
DROP FUNCTION IF EXISTS rx_raise(text);
DROP FUNCTION IF EXISTS rx_purging(uuid);
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_rx_state_check,
    DROP COLUMN IF EXISTS rx_settings, DROP COLUMN IF EXISTS rx_state;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '134_rx_foundation.sql';
