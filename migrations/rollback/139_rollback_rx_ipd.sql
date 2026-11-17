-- ============================================================================
-- Rollback 139: Pharmacy OS inpatient pharmacy (RX-M6)
-- Refused while any indent exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Restores rx_purge_clinic v5 (138).
-- ipd_charges.source goes back to the 116
-- CHECK only when no 'pharmacy' charge exists: those rows belong to patient bills and are
-- never deleted here (ipd_purge_clinic owns them). The normal rollback is a code revert.
-- ============================================================================

SET CONSTRAINTS ALL IMMEDIATE;   -- a purge earlier in this transaction leaves deferred FK checks pending

DO $$ BEGIN
    IF to_regclass('public.rx_ipd_indents') IS NOT NULL AND EXISTS (SELECT 1 FROM rx_ipd_indents) THEN
        RAISE EXCEPTION '139_down: indents exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS rx_ipd_return(uuid, uuid, jsonb, text, uuid, uuid, text, boolean);
DROP FUNCTION IF EXISTS rx_link_mar(uuid, uuid, uuid, uuid);
DROP FUNCTION IF EXISTS rx_receive_dispense(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_cancel_indent(uuid, uuid, text, uuid);
DROP FUNCTION IF EXISTS rx_dispense_indent(uuid, uuid, uuid, jsonb, uuid, text, text);
DROP FUNCTION IF EXISTS rx_raise_indent(uuid, uuid, uuid, text, jsonb, text, uuid, text);
DROP FUNCTION IF EXISTS rx_indent_refresh(uuid, uuid);
DROP FUNCTION IF EXISTS rx_ipd_post_charge(uuid, uuid, text, integer, integer, text, text, uuid, text);
DROP FUNCTION IF EXISTS rx_ipd_unit_price(uuid, uuid);
DROP FUNCTION IF EXISTS rx_lock_admission_active(uuid, uuid);
DROP TABLE IF EXISTS rx_empty_returns;
DROP TABLE IF EXISTS rx_mar_links;
DROP TABLE IF EXISTS rx_dispense_lines;
DROP TABLE IF EXISTS rx_dispenses;
DROP TABLE IF EXISTS rx_indent_lines;
DROP TABLE IF EXISTS rx_ipd_indents;
DROP FUNCTION IF EXISTS rx_check_indent_line();
DROP FUNCTION IF EXISTS rx_guard_ipd_doc();


DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM ipd_charges WHERE source = 'pharmacy') THEN
        RAISE NOTICE '139_down: pharmacy charges exist on patient bills; ipd_charges_source_check stays wide';
    ELSE
        ALTER TABLE ipd_charges DROP CONSTRAINT IF EXISTS ipd_charges_source_check;
        ALTER TABLE ipd_charges ADD CONSTRAINT ipd_charges_source_check
            CHECK (source IN ('manual', 'bed_day', 'order', 'package'));
    END IF;
END $$;

-- rx_purge_clinic v5 (138 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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
-- DELETE FROM schema_migrations WHERE name = '139_rx_ipd.sql';
