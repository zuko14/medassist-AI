-- ============================================================================
-- Rollback 138: Pharmacy OS counter sales (RX-M5)
-- Refused while any sale / override exists: purge each pharmacy clinic first with
-- SELECT rx_purge_clinic('<clinic_id>') (owner action). Restores opd_close_shift to the
-- migration 116 body verbatim and rx_purge_clinic v4 (137). The normal rollback is a code revert.
-- ============================================================================

SET CONSTRAINTS ALL IMMEDIATE;   -- a purge earlier in this transaction leaves deferred FK checks pending

DO $$ BEGIN
    IF to_regclass('public.rx_sales') IS NOT NULL
       AND (EXISTS (SELECT 1 FROM rx_sales) OR EXISTS (SELECT 1 FROM rx_overrides)) THEN
        RAISE EXCEPTION '138_down: sales exist; run rx_purge_clinic for each pharmacy clinic first';
    END IF;
END $$;

-- opd_close_shift: the 116 body (116_ipd_finance.sql §15), verbatim
CREATE OR REPLACE FUNCTION opd_close_shift(
    p_clinic_id UUID, p_shift_id UUID, p_admin_id UUID, p_declared_paise INTEGER, p_notes TEXT)
RETURNS SETOF opd_cashier_shifts LANGUAGE plpgsql AS $$
DECLARE s opd_cashier_shifts; v_expected INTEGER;
BEGIN
    SELECT * INTO s FROM opd_cashier_shifts WHERE id = p_shift_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF s.status <> 'open' THEN RAISE EXCEPTION 'opd_shift_not_open' USING ERRCODE = 'P0001'; END IF;
    IF s.cashier_admin_id <> p_admin_id THEN RAISE EXCEPTION 'opd_shift_not_yours' USING ERRCODE = 'P0001'; END IF;
    SELECT s.opening_float_paise
         + COALESCE((SELECT SUM(CASE WHEN kind = 'payment' THEN amount_paise ELSE -amount_paise END)
                       FROM opd_receipts WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
         + COALESCE((SELECT SUM(CASE WHEN kind = 'deposit' THEN amount_paise ELSE -amount_paise END)
                       FROM ipd_deposits WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
      INTO v_expected;
    RETURN QUERY
    UPDATE opd_cashier_shifts
       SET status = 'closed', closed_at = now(), expected_cash_paise = v_expected,
           declared_cash_paise = p_declared_paise, close_notes = p_notes
     WHERE id = p_shift_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;
ALTER FUNCTION opd_close_shift(uuid, uuid, uuid, integer, text) SET search_path = public, pg_temp;

DROP FUNCTION IF EXISTS rx_shift_expected_cash(uuid, uuid);
DROP FUNCTION IF EXISTS rx_void_sale(uuid, uuid, text, uuid, uuid, text);
DROP FUNCTION IF EXISTS rx_record_return(uuid, uuid, jsonb, text, text, uuid, text, uuid, text, boolean, uuid);
DROP FUNCTION IF EXISTS rx_complete_sale(uuid, uuid, jsonb, uuid, text, uuid, text, uuid, boolean, boolean);
DROP FUNCTION IF EXISTS rx_consume_override(uuid, uuid, text, uuid, bigint, uuid);
DROP TABLE IF EXISTS rx_overrides;
DROP TABLE IF EXISTS rx_return_lines;
DROP TABLE IF EXISTS rx_returns;
DROP TABLE IF EXISTS rx_credit_notes;
DROP TABLE IF EXISTS rx_rx_fulfilments;
DROP TABLE IF EXISTS rx_sale_payments;
DROP TABLE IF EXISTS rx_sale_items;
DROP TABLE IF EXISTS rx_sales;
DROP FUNCTION IF EXISTS rx_sale_refresh(uuid, uuid);
DROP FUNCTION IF EXISTS rx_sale_header_after();
DROP FUNCTION IF EXISTS rx_sale_lines_after();
DROP FUNCTION IF EXISTS rx_sale_line_calc();
DROP FUNCTION IF EXISTS rx_guard_sale();
DROP FUNCTION IF EXISTS rx_guard_override();
DROP FUNCTION IF EXISTS rx_require_tx();
DROP FUNCTION IF EXISTS rx_rx_units(text, text, integer, text);
DROP FUNCTION IF EXISTS rx_unit_price(integer, integer, integer);
DROP FUNCTION IF EXISTS rx_gst_split_inclusive(bigint, integer, boolean);

-- rx_purge_clinic v4 (137 body)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '138_rx_pos.sql';
