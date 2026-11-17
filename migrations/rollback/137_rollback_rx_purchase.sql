-- ============================================================================
-- Rollback 137: Pharmacy OS purchase (RX-M4)
-- Refused while any supplier / PO / GRN / return row exists: purge each pharmacy
-- clinic first with SELECT rx_purge_clinic('<clinic_id>') (owner action).
-- Restores rx_purge_clinic v3 (136). The normal rollback is a code revert.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.rx_suppliers') IS NOT NULL AND (EXISTS (SELECT 1 FROM rx_suppliers)
         OR EXISTS (SELECT 1 FROM rx_grns) OR EXISTS (SELECT 1 FROM rx_purchase_orders)) THEN
        RAISE EXCEPTION '137_down: purchase rows exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP VIEW IF EXISTS rx_v_item_last_supplier;
DROP FUNCTION IF EXISTS rx_supplier_return(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_void_grn(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS rx_post_grn(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_po_refresh_status(uuid, uuid);
DROP FUNCTION IF EXISTS rx_send_po(uuid, uuid);
DROP TABLE IF EXISTS rx_supplier_return_lines;
DROP TABLE IF EXISTS rx_supplier_returns;
DROP TABLE IF EXISTS rx_grn_lines;
DROP TABLE IF EXISTS rx_grns;
DROP TABLE IF EXISTS rx_po_lines;
DROP TABLE IF EXISTS rx_purchase_orders;
DROP TABLE IF EXISTS rx_suppliers;
DROP FUNCTION IF EXISTS rx_grn_refresh(uuid, uuid);
DROP FUNCTION IF EXISTS rx_grn_header_after();
DROP FUNCTION IF EXISTS rx_grn_lines_after();
DROP FUNCTION IF EXISTS rx_grn_line_calc();
DROP FUNCTION IF EXISTS rx_srn_line_fill();
DROP FUNCTION IF EXISTS rx_guard_purchase_line();
DROP FUNCTION IF EXISTS rx_guard_purchase_doc();

-- rx_purge_clinic v3 (136 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_stock_balances WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_movements WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('movements', n);
    DELETE FROM rx_batches WHERE clinic_id = p_clinic_id;
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
-- DELETE FROM schema_migrations WHERE name = '137_rx_purchase.sql';
