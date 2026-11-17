-- ============================================================================
-- Rollback 140: Pharmacy OS transfers, stock audits, par levels (RX-M7)
-- Refused while any transfer, audit or par level exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Removes the two audit triggers from
-- rx_stock_movements and restores rx_purge_clinic v6 (139). The normal rollback is a code revert.
-- ============================================================================

SET CONSTRAINTS ALL IMMEDIATE;   -- a purge earlier in this transaction leaves deferred FK checks pending

DO $$ BEGIN
    IF to_regclass('public.rx_transfers') IS NOT NULL
       AND (EXISTS (SELECT 1 FROM rx_transfers) OR EXISTS (SELECT 1 FROM rx_stock_audits)
            OR EXISTS (SELECT 1 FROM rx_par_levels)) THEN
        RAISE EXCEPTION '140_down: transfers, audits or par levels exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_rx_sm_audit_delta ON rx_stock_movements;
DROP TRIGGER IF EXISTS trg_rx_sm_audit_freeze ON rx_stock_movements;
DROP FUNCTION IF EXISTS rx_sm_audit_delta();
DROP FUNCTION IF EXISTS rx_sm_audit_freeze();
DROP FUNCTION IF EXISTS rx_cancel_audit(uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_post_audit(uuid, uuid, jsonb, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS rx_review_audit(uuid, uuid);
DROP FUNCTION IF EXISTS rx_record_counts(uuid, uuid, jsonb);
DROP FUNCTION IF EXISTS rx_start_audit(uuid, uuid, text, text, uuid[], boolean, uuid, text);
DROP FUNCTION IF EXISTS rx_audit_in_scope(rx_stock_audits, rx_items);
DROP FUNCTION IF EXISTS rx_set_par_levels(uuid, uuid, jsonb, uuid);
DROP FUNCTION IF EXISTS rx_cancel_transfer(uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_receive_transfer(uuid, uuid, jsonb, uuid, text, boolean);
DROP FUNCTION IF EXISTS rx_dispatch_transfer(uuid, uuid, uuid, text, boolean);
DROP FUNCTION IF EXISTS rx_save_transfer(uuid, uuid, uuid, uuid, jsonb, text, text, uuid, text);
DROP TABLE IF EXISTS rx_par_levels;
DROP TABLE IF EXISTS rx_stock_audit_lines;
DROP TABLE IF EXISTS rx_stock_audits;
DROP TABLE IF EXISTS rx_transfer_lines;
DROP TABLE IF EXISTS rx_transfers;
DROP FUNCTION IF EXISTS rx_guard_fn_only();

-- rx_purge_clinic v6 (139 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_mar_links WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_empty_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_dispense_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_dispenses WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('dispenses', n);
    DELETE FROM rx_indent_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_ipd_indents WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('indents', n);
    DELETE FROM rx_overrides WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_return_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_credit_notes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_rx_fulfilments WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sale_payments WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sale_items WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sales WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sales', n);
    DELETE FROM rx_supplier_return_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_supplier_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_grn_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_grns WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('grns', n);
    DELETE FROM rx_po_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_purchase_orders WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_suppliers WHERE clinic_id = p_clinic_id;
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

DO $$ BEGIN
    REVOKE ALL ON FUNCTION rx_purge_clinic(uuid) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION rx_purge_clinic(uuid) TO service_role;
    END IF;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '140_rx_transfers_audits.sql';
