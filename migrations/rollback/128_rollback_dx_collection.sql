-- ============================================================================
-- Rollback 128: Diagnostic OS collection and rejection (DX-M4)
-- Refused while any tube has left pending_collection, any doorstep money or
-- label print is recorded: purge each DX clinic first
-- (SELECT dx_purge_clinic('<id>')). Restores dx_purge_clinic v3 (migration 127).
-- The normal rollback is a code revert: pre-128 code runs unchanged on 128.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_field_collections') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_field_collections))
       OR (to_regclass('public.dx_label_prints') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_label_prints))
       OR EXISTS (SELECT 1 FROM dx_samples WHERE status <> 'pending_collection' OR recollect_of IS NOT NULL)
       OR EXISTS (SELECT 1 FROM opd_orders WHERE source = 'dx' AND replaces_order_id IS NOT NULL) THEN
        RAISE EXCEPTION '128_down: collected / rejected tubes, doorstep money or label prints exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_dx_guard_sample ON dx_samples;
DROP TABLE IF EXISTS dx_label_prints;
DROP TABLE IF EXISTS dx_field_collections;
DROP TABLE IF EXISTS dx_rejection_reasons;

DROP FUNCTION IF EXISTS dx_handover_field_collection(uuid, uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_record_field_collection(uuid, uuid, integer, text, text, uuid, text, text);
DROP FUNCTION IF EXISTS dx_mark_sample_stored(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_receive_sample(uuid, text, uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_reject_sample(uuid, uuid, text, text, boolean, uuid, text);
DROP FUNCTION IF EXISTS dx_collect_samples(uuid, uuid, jsonb, text, uuid, text, text);
DROP FUNCTION IF EXISTS dx_bind_barcode(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_visit_due(uuid, uuid);
DROP FUNCTION IF EXISTS dx_sample_json(uuid, uuid);
DROP FUNCTION IF EXISTS dx_seed_rejection_reasons(uuid);
DROP FUNCTION IF EXISTS dx_guard_sample();
DROP FUNCTION IF EXISTS dx_guard_append_only();

-- dx_purge_clinic back to v3 (migration 127)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
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
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
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
-- DELETE FROM schema_migrations WHERE name = '128_dx_collection.sql';
