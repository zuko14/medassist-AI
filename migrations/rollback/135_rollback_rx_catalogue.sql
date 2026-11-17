-- ============================================================================
-- Rollback 135: Pharmacy OS catalogue (RX-M2)
-- Refused while any catalogue row exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Roll back 136 first.
-- Restores rx_purge_clinic v1 (134). The normal rollback is a code revert:
-- pre-135 code runs unchanged on 135.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.rx_batches') IS NOT NULL THEN
        RAISE EXCEPTION '135_down: roll back 136 first';
    END IF;
    IF to_regclass('public.rx_items') IS NOT NULL AND (EXISTS (SELECT 1 FROM rx_items)
         OR EXISTS (SELECT 1 FROM rx_generics) OR EXISTS (SELECT 1 FROM rx_manufacturers)) THEN
        RAISE EXCEPTION '135_down: catalogue rows exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS rx_resolve_items(uuid, text, integer);
DROP FUNCTION IF EXISTS rx_import_items(uuid, jsonb, boolean, uuid, text);
DROP FUNCTION IF EXISTS rx_update_item(uuid, uuid, jsonb, uuid, text, text);
DROP TABLE IF EXISTS rx_item_changes;
DROP TABLE IF EXISTS rx_item_aliases;
DROP TABLE IF EXISTS rx_item_barcodes;
DROP TABLE IF EXISTS rx_items;
DROP TABLE IF EXISTS rx_generics;
DROP TABLE IF EXISTS rx_manufacturers;
DROP FUNCTION IF EXISTS rx_guard_append_only();
DROP FUNCTION IF EXISTS rx_guard_item();
DROP FUNCTION IF EXISTS rx_schedule_rank(text);
DROP FUNCTION IF EXISTS rx_normalize(text);

-- rx_purge_clinic v1 (134 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_stores WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('stores', n);
    UPDATE clinics SET rx_state = 'OFF', rx_settings = '{}'::jsonb WHERE id = p_clinic_id;
    PERFORM set_config('kriya.rx_purge_clinic', '', true);
    RETURN v;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '135_rx_catalogue.sql';
