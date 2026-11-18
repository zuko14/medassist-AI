-- ============================================================================
-- Rollback 141: Pharmacy OS statutory registers (RX-M8)
-- Refused while any register entry exists: statutory rows go only with the owner purge
-- (SELECT rx_purge_clinic('<clinic_id>') for each pharmacy clinic first). Removes the deferred register
-- trigger from rx_stock_movements and restores rx_purge_clinic v7 (140). The normal rollback is a code revert.
-- ============================================================================

SET CONSTRAINTS ALL IMMEDIATE;   -- fire pending deferred checks / register appends before the guard looks

DO $$ BEGIN
    IF to_regclass('public.rx_register_entries') IS NOT NULL AND EXISTS (SELECT 1 FROM rx_register_entries) THEN
        RAISE EXCEPTION '141_down: register entries exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_rx_sm_register ON rx_stock_movements;
DROP FUNCTION IF EXISTS rx_sm_register();
DROP FUNCTION IF EXISTS rx_patient_snap(uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_batch_trace(uuid, uuid);
DROP FUNCTION IF EXISTS rx_record_empties(uuid, uuid, integer, uuid, uuid);
DROP FUNCTION IF EXISTS rx_register_verify(uuid, text);
DROP FUNCTION IF EXISTS rx_register_append(uuid, text, text, uuid, uuid, uuid, integer, text, uuid, jsonb, jsonb, jsonb,
                                           text, text, text, text, timestamptz, integer);
DROP FUNCTION IF EXISTS rx_register_hash(rx_register_entries);
DROP TABLE IF EXISTS rx_register_entries;
DROP FUNCTION IF EXISTS rx_register_guard_insert();

-- rx_purge_clinic v7 (140 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_par_levels WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_audits WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('audits', n);
    DELETE FROM rx_transfer_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_transfers WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('transfers', n);
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
