-- ============================================================================
-- Migration 150: Pharmacy OS for standalone pharmacies — the `pharmacy` plan (lane RXR; docs/pharmacy-os/RETAIL-PLAN.md)
-- Widens clinics.plan / plan_tiers.plan_name with 'pharmacy' (dynamic constraint inspection, the 077/082 pattern) and
-- seeds its plan tier; pharmacy_order_requests (WhatsApp / voice / counter reservations a pharmacist reviews — never a sale by
-- itself: Schedule H / H1 / X still need the POS prescription checks, X13); rx_purge_clinic v10 (+ order requests).
--
-- Existing objects touched: clinics.plan CHECK, plan_tiers.plan_name CHECK (old values all still valid), rx_purge_clinic
-- (same body as 142 plus one DELETE). No trigger on any existing table; references into earlier migrations' tables use
-- the same-clinic guard (kriya_guard_refs, migration 144), never a FOREIGN KEY.
-- Rollback: rollback/150_rollback_retail_pharmacy.sql (refuses while a pharmacy-plan clinic or an order request exists).
-- ============================================================================

DO $$
DECLARE con RECORD;
BEGIN
    FOR con IN SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relname = 'clinics' AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%plan%'
                 AND pg_get_constraintdef(c.oid) LIKE '%soloclinic%' LOOP
        EXECUTE format('ALTER TABLE clinics DROP CONSTRAINT %I', con.conname);
    END LOOP;
    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking', 'essential', 'polyclinic', 'enterprise', 'derma', 'eye', 'dental', 'ivf', 'multispecialty', 'womenchild', 'pharmacy'));
    FOR con IN SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
               WHERE t.relname = 'plan_tiers' AND c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%plan_name%' LOOP
        EXECUTE format('ALTER TABLE plan_tiers DROP CONSTRAINT %I', con.conname);
    END LOOP;
    ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking', 'essential', 'polyclinic', 'enterprise', 'derma', 'eye', 'dental', 'ivf', 'multispecialty', 'womenchild', 'pharmacy'));
END $$;

INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES ('pharmacy', 'Pharmacy / Medical Store', 0, 1000, 0)
ON CONFLICT (plan_name) DO NOTHING;

CREATE TABLE IF NOT EXISTS pharmacy_order_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id        UUID NULL,
    patient_id      UUID NULL,
    phone           TEXT NOT NULL CHECK (phone ~ '^\+?[0-9]{10,15}$'),
    phone_masked    TEXT NOT NULL CHECK (char_length(phone_masked) BETWEEN 4 AND 20),
    kind            TEXT NOT NULL CHECK (kind IN ('prescription', 'refill', 'stock_query', 'callback')),
    source          TEXT NOT NULL CHECK (source IN ('whatsapp_photo', 'whatsapp_text', 'voice', 'counter')),
    media_ref       TEXT NULL CHECK (char_length(media_ref) <= 200),      -- Meta media id; fetched only by staff
    note            TEXT NULL CHECK (char_length(note) <= 500),           -- the customer's own words, never parsed
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewing', 'ready', 'collected', 'rejected',
                                                                  'cancelled')),
    rejected_reason TEXT NULL CHECK (char_length(btrim(rejected_reason)) BETWEEN 3 AND 300),
    sale_id         UUID NULL,
    pickup_by       TIMESTAMPTZ NULL,
    handled_by_name TEXT NULL CHECK (char_length(handled_by_name) <= 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pharmacy_order_requests_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT pharmacy_order_requests_reject_reason CHECK ((status = 'rejected') = (rejected_reason IS NOT NULL)),
    CONSTRAINT pharmacy_order_requests_photo CHECK (source <> 'whatsapp_photo' OR media_ref IS NOT NULL),
    CONSTRAINT pharmacy_order_requests_collected_sale CHECK (status <> 'collected' OR sale_id IS NOT NULL)
);
-- one open request per (phone, kind): a second photo or tap never queues twice
CREATE UNIQUE INDEX IF NOT EXISTS uq_pharmacy_order_requests_open ON pharmacy_order_requests (clinic_id, phone, kind)
    WHERE status IN ('new', 'reviewing', 'ready');
CREATE INDEX IF NOT EXISTS idx_pharmacy_order_requests_queue ON pharmacy_order_requests (clinic_id, status, created_at DESC);
DROP TRIGGER IF EXISTS trg_pharmacy_order_requests_refs ON pharmacy_order_requests;
CREATE TRIGGER trg_pharmacy_order_requests_refs BEFORE INSERT OR UPDATE ON pharmacy_order_requests FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'store_id', 'rx_stores', 'sale_id', 'rx_sales');

-- rx_purge_clinic v10 (142 body + order requests first)
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    IF to_regclass('public.pharmacy_order_requests') IS NOT NULL THEN
        DELETE FROM pharmacy_order_requests WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
        v := v || jsonb_build_object('order_requests', n);
    END IF;
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

DO $$
BEGIN
    EXECUTE 'ALTER TABLE pharmacy_order_requests ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE pharmacy_order_requests FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS service_role_all_pharmacy_order_requests ON pharmacy_order_requests';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'CREATE POLICY service_role_all_pharmacy_order_requests ON pharmacy_order_requests FOR ALL TO service_role '
                'USING (true) WITH CHECK (true)';
    END IF;
    EXECUTE 'DROP POLICY IF EXISTS tenant_isolation_pharmacy_order_requests ON pharmacy_order_requests';
    EXECUTE $p$CREATE POLICY tenant_isolation_pharmacy_order_requests ON pharmacy_order_requests FOR ALL
              USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
              WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON TABLE pharmacy_order_requests FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON TABLE pharmacy_order_requests FROM authenticated';
    END IF;
END $$;

-- ── Verify ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinics_plan_check'
                     AND pg_get_constraintdef(oid) LIKE '%pharmacy%') THEN
        RAISE EXCEPTION '150 verify: clinics.plan not widened';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM plan_tiers WHERE plan_name = 'pharmacy') THEN
        RAISE EXCEPTION '150 verify: pharmacy plan tier missing';
    END IF;
    IF to_regclass('public.pharmacy_order_requests') IS NULL OR to_regprocedure('rx_purge_clinic(uuid)') IS NULL THEN
        RAISE EXCEPTION '150 verify: pharmacy_order_requests / rx_purge_clinic missing';
    END IF;
END $$;
