-- ============================================================================
-- Rollback 115: investigation & procedure orders
-- DESTROYS order history (who ordered what, specimen barcodes, result notes).
-- Take pg_dump -t opd_orders FIRST. Bills are kept: their lines lose only the
-- order link. Refused while any order is still open in a worklist.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.opd_orders') IS NOT NULL AND EXISTS (
        SELECT 1 FROM opd_orders WHERE status IN ('ordered', 'billed', 'sample_collected', 'in_progress', 'resulted')) THEN
        RAISE EXCEPTION '115_down: orders are still open - complete or cancel every order first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_orders_worklist ON ipd_orders;
DROP TRIGGER IF EXISTS trg_ipd_orders_worklist_end ON ipd_orders;
DROP TRIGGER IF EXISTS trg_opd_invoices_orders ON opd_invoices;
DROP TRIGGER IF EXISTS trg_opd_inv_items_order ON opd_invoice_items;

DROP FUNCTION IF EXISTS opd_place_orders(uuid, uuid, jsonb, text, uuid, text);
DROP FUNCTION IF EXISTS opd_bill_orders(uuid, uuid, uuid[], boolean, uuid);
DROP FUNCTION IF EXISTS opd_cancel_order(uuid, uuid, text, uuid);
DROP FUNCTION IF EXISTS ipd_order_to_worklist();
DROP FUNCTION IF EXISTS ipd_order_end_to_worklist();
DROP FUNCTION IF EXISTS opd_invoice_orders_billing();
DROP FUNCTION IF EXISTS opd_guard_invoice_item_order();

-- the 114 draft save, without the order link
CREATE OR REPLACE FUNCTION opd_save_invoice_draft(
    p_clinic_id UUID, p_invoice_id UUID, p_expected_updated_at TIMESTAMPTZ, p_items JSONB,
    p_discount_paise INTEGER, p_discount_reason TEXT, p_notes TEXT)
RETURNS SETOF opd_invoices LANGUAGE plpgsql AS $$
DECLARE i opd_invoices; r JSONB; n INTEGER := 0;
BEGIN
    SELECT * INTO i FROM opd_invoices WHERE id = p_invoice_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF i.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft:%', i.status USING ERRCODE = 'P0001'; END IF;
    IF i.updated_at IS DISTINCT FROM p_expected_updated_at THEN
        RAISE EXCEPTION 'opd_stale_draft' USING ERRCODE = 'P0001';
    END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001';
    END IF;
    UPDATE opd_invoices SET discount_paise = 0 WHERE id = p_invoice_id AND clinic_id = p_clinic_id;
    DELETE FROM opd_invoice_items WHERE clinic_id = p_clinic_id AND invoice_id = p_invoice_id;
    FOR r IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        n := n + 1;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, doctor_id,
                                       lab_test_id, description, quantity, unit_price_paise, discount_paise)
        VALUES (p_clinic_id, p_invoice_id, n, r ->> 'item_type', NULLIF(r ->> 'catalog_code', ''),
                NULLIF(r ->> 'doctor_id', '')::UUID, NULLIF(r ->> 'lab_test_id', '')::UUID, r ->> 'description',
                (r ->> 'quantity')::SMALLINT, (r ->> 'unit_price_paise')::INTEGER,
                COALESCE((r ->> 'discount_paise')::INTEGER, 0));
    END LOOP;
    RETURN QUERY
    UPDATE opd_invoices
       SET discount_paise = p_discount_paise,
           discount_reason = CASE WHEN p_discount_paise > 0 THEN p_discount_reason END,
           notes = p_notes
     WHERE id = p_invoice_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;
ALTER FUNCTION opd_save_invoice_draft(uuid, uuid, timestamptz, jsonb, integer, text, text) SET search_path = public, pg_temp;

DROP INDEX IF EXISTS idx_opd_invoice_items_order;
ALTER TABLE opd_invoice_items DROP CONSTRAINT IF EXISTS opd_invoice_items_order_fk;
ALTER TABLE opd_invoice_items DROP COLUMN IF EXISTS order_id;

DROP TABLE IF EXISTS opd_orders CASCADE;
DROP FUNCTION IF EXISTS opd_order_completed_to_ipd();
DROP FUNCTION IF EXISTS opd_guard_order();
