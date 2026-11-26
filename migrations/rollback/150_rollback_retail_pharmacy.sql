-- ============================================================================
-- Rollback 150: the `pharmacy` plan (lane RXR). Refused while a clinic is on plan 'pharmacy' (move it to another plan
-- or delete it first) or while any order request exists (purge the clinic's pharmacy first). Restores
-- rx_purge_clinic v9 (142 body) and the 082 plan CHECKs. The normal rollback is a code revert.
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM clinics WHERE plan = 'pharmacy') THEN
        RAISE EXCEPTION '150_down: clinics on plan pharmacy exist; change their plan first';
    END IF;
    IF to_regclass('public.pharmacy_order_requests') IS NOT NULL AND EXISTS (SELECT 1 FROM pharmacy_order_requests) THEN
        RAISE EXCEPTION '150_down: pharmacy_order_requests rows exist; run rx_purge_clinic first';
    END IF;
END $$;

DROP TABLE IF EXISTS pharmacy_order_requests;

-- rx_purge_clinic v9 (142 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_refill_requests WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_refill_plans WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('refill_plans', n);
    DELETE FROM rx_alerts WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_metrics WHERE clinic_id = p_clinic_id;
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

DELETE FROM plan_tiers WHERE plan_name = 'pharmacy';
DO $$
DECLARE con RECORD;
BEGIN
    FOR con IN SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relname = 'clinics' AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%plan%'
                 AND pg_get_constraintdef(c.oid) LIKE '%soloclinic%' LOOP
        EXECUTE format('ALTER TABLE clinics DROP CONSTRAINT %I', con.conname);
    END LOOP;
    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking', 'essential', 'polyclinic', 'enterprise', 'derma', 'eye', 'dental', 'ivf', 'multispecialty', 'womenchild'));
    FOR con IN SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relname = 'plan_tiers' AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%plan_name%' LOOP
        EXECUTE format('ALTER TABLE plan_tiers DROP CONSTRAINT %I', con.conname);
    END LOOP;
    ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking', 'essential', 'polyclinic', 'enterprise', 'derma', 'eye', 'dental', 'ivf', 'multispecialty', 'womenchild'));
END $$;
