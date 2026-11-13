-- ============================================================================
-- Rollback 127: Diagnostic OS visits and billing (DX-M3)
-- Refused while any visit, referrer or B2B account exists: visits carry bills,
-- orders and tubes. Purge each DX clinic first: SELECT dx_purge_clinic('<id>').
-- Restores the 115 opd_orders constraints (source opd/ipd, doctor required) and
-- dx_purge_clinic as defined by migration 126.
-- The normal rollback is a code revert: pre-127 code runs unchanged on 127.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_accessions') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_accessions))
       OR (to_regclass('public.dx_referrers') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_referrers))
       OR (to_regclass('public.dx_b2b_accounts') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_b2b_accounts)) THEN
        RAISE EXCEPTION '127_down: DX visits / referrers / B2B accounts exist; run dx_purge_clinic for each DX clinic first';
    END IF;
    IF EXISTS (SELECT 1 FROM opd_orders WHERE source = 'dx')
       OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'opd_invoices' AND column_name = 'accession_id')
          AND EXISTS (SELECT 1 FROM opd_invoices WHERE accession_id IS NOT NULL) THEN
        RAISE EXCEPTION '127_down: DX orders or DX bills exist';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_dx_guard_invoice_item ON opd_invoice_items;
DROP TRIGGER IF EXISTS trg_dx_invoice_accession ON opd_invoices;
DROP TRIGGER IF EXISTS trg_dx_lab_test_delete ON lab_tests;

ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_accession_fk, DROP CONSTRAINT IF EXISTS opd_orders_dx_item_fk,
    DROP CONSTRAINT IF EXISTS opd_orders_dx_sample_fk, DROP CONSTRAINT IF EXISTS opd_orders_replaces_fk,
    DROP CONSTRAINT IF EXISTS opd_orders_doctor_required, DROP CONSTRAINT IF EXISTS opd_orders_source_shape,
    DROP CONSTRAINT IF EXISTS opd_orders_source_check;
DROP INDEX IF EXISTS uq_opd_orders_dx_item_test;
DROP INDEX IF EXISTS idx_opd_orders_dx_accession;
DROP INDEX IF EXISTS idx_opd_orders_dx_sample;
ALTER TABLE opd_orders DROP COLUMN IF EXISTS accession_id, DROP COLUMN IF EXISTS accession_item_id,
    DROP COLUMN IF EXISTS dx_sample_id, DROP COLUMN IF EXISTS replaces_order_id;
ALTER TABLE opd_orders ALTER COLUMN doctor_id SET NOT NULL;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_source_check CHECK (source IN ('opd', 'ipd'));
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_source_shape CHECK (
    (source = 'opd' AND encounter_id IS NOT NULL AND appointment_id IS NOT NULL
                    AND admission_id IS NULL AND ipd_order_id IS NULL)
 OR (source = 'ipd' AND admission_id IS NOT NULL AND ipd_order_id IS NOT NULL
                    AND encounter_id IS NULL AND appointment_id IS NULL));

DROP INDEX IF EXISTS idx_opd_invoices_accession;
ALTER TABLE opd_invoices DROP COLUMN IF EXISTS accession_id;
DROP INDEX IF EXISTS idx_opd_invoice_items_dx_item;
ALTER TABLE opd_invoice_items DROP COLUMN IF EXISTS dx_accession_item_id;
ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_dx_b2b_fk;
DROP INDEX IF EXISTS idx_family_members_dx_b2b;
ALTER TABLE family_members DROP COLUMN IF EXISTS dx_b2b_account_id;

DROP TABLE IF EXISTS dx_events;
DROP TABLE IF EXISTS dx_accession_bookings;
DROP TABLE IF EXISTS dx_samples;
DROP TABLE IF EXISTS dx_accession_items;
DROP TABLE IF EXISTS dx_accessions;
DROP TABLE IF EXISTS dx_referrers;
DROP TABLE IF EXISTS dx_b2b_accounts;

DROP FUNCTION IF EXISTS dx_redact_patient(uuid, uuid);
DROP FUNCTION IF EXISTS dx_bill_visit(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_cancel_visit(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_cancel_item(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_add_items(uuid, uuid, jsonb, text, numeric, uuid, text);
DROP FUNCTION IF EXISTS dx_quote(uuid, jsonb);
DROP FUNCTION IF EXISTS dx_register_visit(uuid, jsonb, uuid, text);
DROP FUNCTION IF EXISTS dx_visit_json(uuid, uuid);
DROP FUNCTION IF EXISTS dx_insert_lines(uuid, uuid, jsonb, uuid, text);
DROP FUNCTION IF EXISTS dx_log(uuid, uuid, text, uuid, text, jsonb);
DROP FUNCTION IF EXISTS dx_next_sample_seq(uuid, integer);
DROP FUNCTION IF EXISTS dx_price_items(jsonb, jsonb);
DROP FUNCTION IF EXISTS dx_test_ready(uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_explode(uuid, uuid);
DROP FUNCTION IF EXISTS dx_guard_lab_test_delete();
DROP FUNCTION IF EXISTS dx_guard_invoice_accession();
DROP FUNCTION IF EXISTS dx_guard_invoice_item();
DROP FUNCTION IF EXISTS dx_guard_tx();
DROP FUNCTION IF EXISTS dx_in_tx(uuid);

-- dx_purge_clinic back to v2 (migration 126)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM dx_test_components WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('components', n);
    DELETE FROM dx_signatories WHERE clinic_id = p_clinic_id;
    UPDATE lab_tests SET department_id = NULL, container_id = NULL
     WHERE clinic_id = p_clinic_id AND (department_id IS NOT NULL OR container_id IS NOT NULL);
    DELETE FROM dx_containers  WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_departments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '127_dx_visits_and_billing.sql';
