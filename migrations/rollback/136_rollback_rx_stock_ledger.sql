-- ============================================================================
-- Rollback 136: Pharmacy OS batches and stock ledger (RX-M3)
-- Refused while any batch or movement exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Restores rx_purge_clinic v2
-- (135). The normal rollback is a code revert: pre-136 code runs unchanged on 136.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.rx_batches') IS NOT NULL
       AND (EXISTS (SELECT 1 FROM rx_batches) OR EXISTS (SELECT 1 FROM rx_stock_movements)) THEN
        RAISE EXCEPTION '136_down: stock rows exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP VIEW IF EXISTS rx_v_item_stock;
DROP VIEW IF EXISTS rx_v_fefo;
DROP VIEW IF EXISTS rx_v_batch_stock;
DROP FUNCTION IF EXISTS rx_opening_stock(uuid, uuid, jsonb, boolean, uuid, text);
DROP FUNCTION IF EXISTS rx_post_writeoff(uuid, uuid, uuid, jsonb, uuid, text);
DROP TABLE IF EXISTS rx_stock_balances;
DROP TABLE IF EXISTS rx_stock_movements;
DROP TABLE IF EXISTS rx_batches;
DROP FUNCTION IF EXISTS rx_guard_balance();
DROP FUNCTION IF EXISTS rx_sm_apply();
DROP FUNCTION IF EXISTS rx_sm_expiry_lock();
DROP FUNCTION IF EXISTS rx_sm_guard_insert();
DROP FUNCTION IF EXISTS rx_guard_batch();
DROP FUNCTION IF EXISTS rx_ist_date(timestamptz);
DROP FUNCTION IF EXISTS rx_in_tx(uuid);

-- rx_purge_clinic v2 (135 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_item_changes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_aliases WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_barcodes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_items WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('items', n);
    DELETE FROM rx_generics WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_manufacturers WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stores WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('stores', n);
    UPDATE clinics SET rx_state = 'OFF', rx_settings = '{}'::jsonb WHERE id = p_clinic_id;
    PERFORM set_config('kriya.rx_purge_clinic', '', true);
    RETURN v;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '136_rx_stock_ledger.sql';
