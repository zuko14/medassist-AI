-- ============================================================================
-- Migration 114: atomic OPD draft-bill save; safe change of an IPD ward's branch
-- ============================================================================
-- 1. opd_save_invoice_draft: the draft-bill save was five separate writes
--    (version check in Python with 0.5 s tolerance, delete lines, insert each
--    line, update header). Two cashiers saving the same draft both passed the
--    check and the later one silently replaced the other's lines; a failing
--    line left the draft with lines missing; and deleting the lines of a
--    discounted draft broke opd_invoices_discount_le_subtotal, so a draft with
--    a discount could never be saved again. Now one transaction under the
--    invoice row lock, exact version match, discount cleared before the lines
--    are replaced. Issuing (an UPDATE of the same row) waits for it.
--
-- 2. ipd_guard_ward: a ward's branch may change only while none of its beds is
--    occupied or reserved (those stays belong to the old branch). The ward's
--    beds are locked first, so a concurrent admission either completed before
--    the check (and is seen) or waits and then reads the new branch.
--
-- Additive: new function + trigger; no table or data change. Pre-114 code never
-- calls the function, and no pre-114 path changes a ward's branch. Idempotent.
-- Rollback: migrations/rollback/114_down.sql.
-- ============================================================================

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
    -- the subtotal follows the lines (opd_recalc_invoice_subtotal); clear the discount first
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

CREATE OR REPLACE FUNCTION ipd_guard_ward() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.branch_id IS DISTINCT FROM OLD.branch_id THEN
        PERFORM 1 FROM ipd_beds WHERE clinic_id = OLD.clinic_id AND ward_id = OLD.id ORDER BY id FOR UPDATE;
        IF EXISTS (SELECT 1 FROM ipd_beds WHERE clinic_id = OLD.clinic_id AND ward_id = OLD.id
                     AND status IN ('occupied', 'reserved')) THEN
            PERFORM ipd_raise('ipd_ward_in_use');
        END IF;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_wards_guard ON ipd_wards;
CREATE TRIGGER trg_ipd_wards_guard BEFORE UPDATE ON ipd_wards FOR EACH ROW EXECUTE FUNCTION ipd_guard_ward();

ALTER FUNCTION opd_save_invoice_draft(uuid, uuid, timestamptz, jsonb, integer, text, text) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_guard_ward() SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT := 'opd_save_invoice_draft(uuid,uuid,timestamptz,jsonb,integer,text,text)';
BEGIN
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
    END IF;
END $$;
