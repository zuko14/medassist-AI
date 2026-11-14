-- ============================================================================
-- Rollback 131: Diagnostic OS reports, templates, delivery (DX-M7) + P6b adoption
-- Refused while any template, release, delivery, override, print, share link or
-- adopted OPD / IPD order exists: purge each DX clinic first
-- (SELECT dx_purge_clinic('<id>')). Restores opd_guard_order (121), the bench
-- functions' source = 'dx' checks (130) and dx_purge_clinic v6 (130).
-- The normal rollback is a code revert: pre-131 code runs unchanged on 131.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_report_templates') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_report_templates))
       OR (to_regclass('public.dx_report_releases') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_report_releases))
       OR (to_regclass('public.dx_delivery_overrides') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_delivery_overrides))
       OR EXISTS (SELECT 1 FROM opd_orders WHERE source <> 'dx' AND accession_id IS NOT NULL) THEN
        RAISE EXCEPTION '131_down: report rows or adopted orders exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_dx_outbox_receipt ON opd_receipts;
DROP TRIGGER IF EXISTS trg_dx_outbox_invoice ON opd_invoices;
DROP TRIGGER IF EXISTS trg_dx_internal_items ON dx_accession_items;
ALTER TABLE dx_accessions DROP CONSTRAINT IF EXISTS dx_accessions_template_fk;
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_accounts_template_fk;
DROP FUNCTION IF EXISTS dx_adopt_orders(uuid, uuid[], uuid, text);
DROP FUNCTION IF EXISTS dx_outbox_claim(integer);
DROP FUNCTION IF EXISTS dx_share_take_attempt(text);
DROP FUNCTION IF EXISTS dx_create_share_link(uuid, uuid, text, integer, uuid);
DROP FUNCTION IF EXISTS dx_log_print(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_create_delivery_override(uuid, uuid, text, timestamptz, uuid, text);
DROP FUNCTION IF EXISTS dx_requeue_delivery(uuid, uuid);
DROP FUNCTION IF EXISTS dx_reevaluate_deliveries(uuid, uuid);
DROP FUNCTION IF EXISTS dx_mark_delivery_result(uuid, uuid, boolean, uuid, text);
DROP FUNCTION IF EXISTS dx_claim_delivery(uuid, uuid);
DROP FUNCTION IF EXISTS dx_set_release_render(uuid, uuid, boolean, text, text, integer);
DROP FUNCTION IF EXISTS dx_create_release(uuid, uuid, uuid[], text, uuid, text);
DROP FUNCTION IF EXISTS dx_decide_delivery(uuid, dx_release_deliveries);
DROP FUNCTION IF EXISTS dx_delivery_status_for(boolean, text);
DROP FUNCTION IF EXISTS dx_delivery_decision(uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_active_override(uuid, uuid);
DROP FUNCTION IF EXISTS dx_visit_has_dues(uuid, uuid);
DROP FUNCTION IF EXISTS dx_publish_template(uuid, uuid, uuid);
DROP TABLE IF EXISTS dx_share_links;
DROP TABLE IF EXISTS dx_print_log;
DROP TABLE IF EXISTS dx_outbox;
DROP TABLE IF EXISTS dx_release_deliveries;
DROP TABLE IF EXISTS dx_delivery_overrides;
DROP TABLE IF EXISTS dx_release_results;
DROP TABLE IF EXISTS dx_report_releases;
DROP TABLE IF EXISTS dx_report_templates;
DROP FUNCTION IF EXISTS dx_outbox_from_receipt();
DROP FUNCTION IF EXISTS dx_outbox_from_invoice();
DROP FUNCTION IF EXISTS dx_guard_internal_items();
DROP FUNCTION IF EXISTS dx_guard_release();
DROP FUNCTION IF EXISTS dx_guard_report_template();

-- bench functions: back to "is a source = 'dx' order" (130)
DO $$
DECLARE f TEXT; v_def TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
                             'dx_verify_result(uuid,uuid,timestamptz,text,text,uuid,text)',
                             'dx_approve_results(uuid,jsonb,uuid,text,boolean)'] LOOP
        v_def := pg_get_functiondef(f::regprocedure);
        EXECUTE replace(v_def, 'o.accession_id IS NULL', $s$o.source <> 'dx'$s$);
    END LOOP;
    v_def := pg_get_functiondef('dx_lab_results_gate()'::regprocedure);
    EXECUTE replace(v_def, '(SELECT accession_id FROM opd_orders WHERE clinic_id = OLD.clinic_id AND id = OLD.order_id) IS NULL',
                    $s$(SELECT source FROM opd_orders WHERE clinic_id = OLD.clinic_id AND id = OLD.order_id) IS DISTINCT FROM 'dx'$s$);
END $$;

-- opd_guard_order back to 121
CREATE OR REPLACE FUNCTION opd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['status', 'updated_at', 'cancellation_reason', 'cancelled_at', 'cancelled_by',
                              'specimen_barcode', 'sample_collected_at', 'sample_collected_by',
                              'sample_collected_by_name', 'resulted_at', 'resulted_by', 'resulted_by_name',
                              'result_summary'];
    v_ok BOOLEAN;
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        -- only the owner's clinic purge (cascading from encounters / IPD orders)
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'opd_append_only:opd_orders' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('completed', 'cancelled') THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.sample_collected_at IS NOT NULL
       AND (NEW.specimen_barcode, NEW.sample_collected_at, NEW.sample_collected_by, NEW.sample_collected_by_name)
           IS DISTINCT FROM
           (OLD.specimen_barcode, OLD.sample_collected_at, OLD.sample_collected_by, OLD.sample_collected_by_name) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.resulted_at IS NOT NULL
       AND (NEW.resulted_at, NEW.resulted_by, NEW.resulted_by_name, NEW.result_summary)
           IS DISTINCT FROM (OLD.resulted_at, OLD.resulted_by, OLD.resulted_by_name, OLD.result_summary) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    v_ok := CASE OLD.status
        WHEN 'ordered'          THEN NEW.status IN ('billed', 'sample_collected', 'completed', 'cancelled')
        WHEN 'billed'           THEN NEW.status IN ('ordered', 'sample_collected', 'completed', 'cancelled')
        WHEN 'sample_collected' THEN NEW.status IN ('in_progress', 'resulted', 'completed', 'cancelled')
        WHEN 'in_progress'      THEN NEW.status IN ('resulted', 'completed', 'cancelled')
        WHEN 'resulted'         THEN NEW.status = 'completed'
        ELSE false END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'opd_order_bad_transition:%->%', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    -- billing state follows the bill (opd_invoice_orders_billing), never a direct write
    IF NEW.status IN ('ordered', 'billed') AND OLD.status IN ('ordered', 'billed')
       AND current_setting('kriya.opd_order_billing', true) IS DISTINCT FROM OLD.clinic_id::TEXT THEN
        RAISE EXCEPTION 'opd_order_billing_is_derived' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'completed' AND OLD.status IN ('ordered', 'billed') AND OLD.sample_type IS NOT NULL THEN
        RAISE EXCEPTION 'opd_order_sample_required' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
ALTER FUNCTION opd_guard_order() SET search_path = public, pg_temp;

-- dx_purge_clinic back to v6 (migration 130)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_result_attachments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_critical_events WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('critical_events', n);
    DELETE FROM dx_result_verifications WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_result_value_meta WHERE clinic_id = p_clinic_id;
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
