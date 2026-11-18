-- ============================================================================
-- Rollback 142: Pharmacy OS refills, alerts, integrity checks, item metrics (RX-M9)
-- Refused while any refill plan or request exists (patients' opt-ins): purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Alerts and metrics are derived and simply dropped.
-- Restores rx_purge_clinic v8 (141). The normal rollback is a code revert.
-- ============================================================================

SET CONSTRAINTS ALL IMMEDIATE;   -- fire pending deferred checks / register appends before the guard looks

DO $$ BEGIN
    IF to_regclass('public.rx_refill_plans') IS NOT NULL
       AND (EXISTS (SELECT 1 FROM rx_refill_plans) OR EXISTS (SELECT 1 FROM rx_refill_requests)) THEN
        RAISE EXCEPTION '142_down: refill plans or requests exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_rx_patient_erased ON patients;
DROP FUNCTION IF EXISTS rx_patient_erased();
DROP FUNCTION IF EXISTS rx_expiry_return_plan(uuid, uuid, date);
DROP FUNCTION IF EXISTS rx_alerts_scan(uuid, date, integer);
DROP FUNCTION IF EXISTS rx_ledger_check(uuid);
DROP TABLE IF EXISTS rx_item_metrics;
DROP TABLE IF EXISTS rx_alerts;
DROP TABLE IF EXISTS rx_refill_requests;
DROP TABLE IF EXISTS rx_refill_plans;

-- rx_purge_clinic v8 (141 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_register_entries WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('register_entries', n);
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
