-- ============================================================================
-- Rollback 130: Diagnostic OS result workflow (DX-M6)
-- Refused while any value meta, verification, critical event or attachment
-- exists: purge each DX clinic first (SELECT dx_purge_clinic('<id>')).
-- Restores dx_purge_clinic v5 (migration 129). lab_results is untouched except
-- for dropping the DX gate trigger.
-- The normal rollback is a code revert: pre-130 code runs unchanged on 130.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_result_value_meta') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_result_value_meta))
       OR (to_regclass('public.dx_result_verifications') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_result_verifications))
       OR (to_regclass('public.dx_critical_events') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_critical_events))
       OR (to_regclass('public.dx_result_attachments') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_result_attachments)) THEN
        RAISE EXCEPTION '130_down: result workflow rows exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_dx_lab_results_gate ON lab_results;
DROP FUNCTION IF EXISTS dx_approve_results(uuid, jsonb, uuid, text, boolean);
DROP FUNCTION IF EXISTS dx_critical_pending(uuid, uuid);
DROP FUNCTION IF EXISTS dx_log_critical_call(uuid, uuid, text, text, text, boolean, text, uuid, text);
DROP FUNCTION IF EXISTS dx_verify_result(uuid, uuid, timestamptz, text, text, uuid, text);
DROP FUNCTION IF EXISTS dx_record_results(uuid, uuid, jsonb, jsonb, boolean, text, uuid, text);
DROP TABLE IF EXISTS dx_result_attachments;
DROP TABLE IF EXISTS dx_critical_events;
DROP TABLE IF EXISTS dx_result_verifications;
DROP TABLE IF EXISTS dx_result_value_meta;
DROP FUNCTION IF EXISTS dx_guard_result_attachment();
DROP FUNCTION IF EXISTS dx_lab_results_gate();

-- dx_purge_clinic back to v5 (migration 129)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM dx_batch_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_batches WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('batches', n);
    DELETE FROM opd_orders WHERE clinic_id = p_clinic_id AND source = 'dx'; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('orders', n);
    DELETE FROM dx_events WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_items WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accessions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('visits', n);
    UPDATE family_members SET dx_b2b_account_id = NULL WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL;
    DELETE FROM dx_referrers WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    DELETE FROM dx_routing_rules WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('routing_rules', n);
    DELETE FROM dx_outsource_partners WHERE clinic_id = p_clinic_id;
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
-- DELETE FROM schema_migrations WHERE name = '130_dx_result_workflow.sql';
