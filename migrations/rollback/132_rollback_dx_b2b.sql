-- ============================================================================
-- Rollback 132: Diagnostic OS B2B money (DX-M8)
-- Refused while any ledger row, statement, rate card, barcode stock or partner
-- login exists: purge each DX clinic first (SELECT dx_purge_clinic('<id>')).
-- Reverses the 132 text patches, restores dx_price_items (127) and
-- dx_purge_clinic v7 (131). The normal rollback is a code revert: pre-132 code
-- runs unchanged on 132.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_b2b_ledger') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_b2b_ledger))
       OR (to_regclass('public.dx_b2b_statements') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_b2b_statements))
       OR (to_regclass('public.dx_rate_cards') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_rate_cards))
       OR (to_regclass('public.dx_barcode_stock') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_barcode_stock))
       OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'clinic_admins'
                                                         AND column_name = 'dx_b2b_account_id')
          AND EXISTS (SELECT 1 FROM clinic_admins WHERE dx_b2b_account_id IS NOT NULL) THEN
        RAISE EXCEPTION '132_down: B2B rows or partner logins exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

-- reverse the guarded text patches (to -> from)
DO $$
DECLARE p TEXT[]; v_def TEXT;
BEGIN
    FOREACH p SLICE 1 IN ARRAY ARRAY[
        ['dx_register_visit(uuid,jsonb,uuid,text)',
         $s$'billing_mode', v_mode,$s$,
         $s$'billing_mode', v_mode, 'b2b_account_id', v_acc.id,$s$],
        ['dx_register_visit(uuid,jsonb,uuid,text)',
         $s$PERFORM dx_insert_lines(p_clinic_id, v_id, v_lines, p_actor_id, p_actor_name);$s$,
         $s$PERFORM dx_insert_lines(p_clinic_id, v_id, v_lines, p_actor_id, p_actor_name);
    PERFORM dx_b2b_post_visit_charges(p_clinic_id, v_id, p_actor_id, p_actor_name);$s$],
        ['dx_add_items(uuid,uuid,jsonb,text,numeric,uuid,text)',
         $s$'billing_mode', a.billing_mode,$s$,
         $s$'billing_mode', a.billing_mode, 'b2b_account_id', a.b2b_account_id, 'accession_id', a.id,$s$],
        ['dx_add_items(uuid,uuid,jsonb,text,numeric,uuid,text)',
         $s$PERFORM dx_insert_lines(p_clinic_id, a.id, v_lines, p_actor_id, p_actor_name);$s$,
         $s$PERFORM dx_insert_lines(p_clinic_id, a.id, v_lines, p_actor_id, p_actor_name);
    PERFORM dx_b2b_post_visit_charges(p_clinic_id, a.id, p_actor_id, p_actor_name);$s$],
        ['dx_quote(uuid,jsonb)',
         $s$'billing_mode', p_payload ->> 'billing_mode',$s$,
         $s$'billing_mode', p_payload ->> 'billing_mode', 'b2b_account_id', p_payload ->> 'b2b_account_id',$s$],
        ['dx_cancel_item(uuid,uuid,text,uuid,text)',
         $s$PERFORM dx_log(p_clinic_id, a.id, 'item_cancelled'$s$,
         $s$PERFORM dx_b2b_reverse_charge(p_clinic_id, it.id, p_reason, p_actor_id, p_actor_name);
    PERFORM dx_log(p_clinic_id, a.id, 'item_cancelled'$s$],
        ['dx_cancel_visit(uuid,uuid,text,uuid,text)',
         $s$PERFORM dx_log(p_clinic_id, a.id, 'cancelled'$s$,
         $s$PERFORM dx_b2b_reverse_visit_charges(p_clinic_id, a.id, p_reason, p_actor_id, p_actor_name);
    PERFORM dx_log(p_clinic_id, a.id, 'cancelled'$s$],
        ['dx_bill_visit(uuid,uuid,uuid,text)',
         $s$IF a.billing_mode <> 'patient' THEN$s$,
         $s$IF a.billing_mode NOT IN ('patient', 'b2b_split') THEN$s$],
        ['dx_insert_lines(uuid,uuid,jsonb,uuid,text)',
         $s$patient_share_paise, payer_share_paise, price_source)$s$,
         $s$patient_share_paise, payer_share_paise, price_source, rate_card_id)$s$],
        ['dx_insert_lines(uuid,uuid,jsonb,uuid,text)',
         $s$(l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source')$s$,
         $s$(l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source', NULLIF(l ->> 'rate_card_id', '')::UUID)$s$],
        ['dx_bind_barcode(uuid,uuid,text,uuid,text)',
         $s$-- shortcut: no dx_barcode_stock range check (dx_barcode_not_issued) until DX-M8 adds the stock table$s$,
         $s$PERFORM dx_check_barcode_stock(p_clinic_id, (SELECT b2b_account_id FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = s.accession_id), s.collection_site_id, v_code);$s$]
    ] LOOP
        v_def := pg_get_functiondef(p[1]::regprocedure);
        IF position(p[3] IN v_def) = 0 THEN CONTINUE; END IF;
        EXECUTE replace(v_def, p[3], p[2]);
    END LOOP;
END $$;

-- dx_price_items back to the 127 resolver (B2B modes refused)
CREATE OR REPLACE FUNCTION dx_price_items(p_ctx JSONB, p_items JSONB) RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_clinic UUID := (p_ctx ->> 'clinic_id')::UUID;
    v_branch UUID := NULLIF(p_ctx ->> 'branch_id', '')::UUID;
    v_gender TEXT := p_ctx ->> 'gender';
    v_mode   TEXT := COALESCE(p_ctx ->> 'billing_mode', 'patient');
    v_limit  NUMERIC := COALESCE((p_ctx ->> 'discount_limit_percent')::NUMERIC, 0);
    r JSONB; t lab_tests; m lab_tests; v_out JSONB := '[]'::jsonb; v_tests JSONB; v_price INTEGER; v_disc INTEGER;
    v_net INTEGER; v_member UUID; v_cont dx_containers;
BEGIN
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        PERFORM dx_raise('dx_no_items');
    END IF;
    IF v_mode LIKE 'b2b%' THEN PERFORM dx_raise('dx_b2b_not_enabled'); END IF;   -- B2B ledger arrives with DX-M8
    FOR r IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_disc := COALESCE((r ->> 'discount_paise')::INTEGER, 0);
        IF r ? 'fee_code' THEN
            v_price := (r ->> 'amount_paise')::INTEGER;
            IF v_price IS NULL OR v_price < 0 THEN PERFORM dx_raise('dx_fee_invalid'); END IF;
            v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price END;
            v_out := v_out || jsonb_build_object(
                'kind', 'fee', 'fee_code', r ->> 'fee_code', 'lab_test_id', NULL,
                'item_name', COALESCE(NULLIF(btrim(r ->> 'item_name'), ''), initcap(replace(r ->> 'fee_code', '_', ' '))),
                'test_code', NULL, 'mrp_paise', v_price, 'price_paise', v_price, 'discount_paise', 0,
                'patient_share_paise', v_net, 'payer_share_paise', 0, 'price_source', 'fee',
                'source_appointment_id', r -> 'source_appointment_id', 'tests', '[]'::jsonb);
            CONTINUE;
        END IF;
        t := dx_test_ready(v_clinic, (r ->> 'lab_test_id')::UUID, v_gender);
        -- branch row -> clinic row price (rate cards join in DX-M8)
        SELECT COALESCE((SELECT b.price_paise FROM lab_tests b
                          WHERE b.clinic_id = v_clinic AND b.branch_id = v_branch AND b.is_active
                            AND t.test_code IS NOT NULL AND upper(b.test_code) = upper(t.test_code) LIMIT 1),
                        t.price_paise) INTO v_price;
        IF v_disc < 0 OR v_disc > v_price THEN PERFORM dx_raise('dx_discount_invalid'); END IF;
        IF v_disc > floor(v_price * v_limit / 100) THEN PERFORM dx_raise('dx_discount_over_limit'); END IF;
        v_tests := '[]'::jsonb;
        FOR v_member IN SELECT dx_explode(v_clinic, t.id) LOOP
            m := dx_test_ready(v_clinic, v_member, v_gender);
            v_cont := NULL;
            IF m.dx_result_mode = 'parameters' AND m.container_id IS NOT NULL THEN
                SELECT * INTO v_cont FROM dx_containers WHERE clinic_id = v_clinic AND id = m.container_id;
            END IF;
            v_tests := v_tests || jsonb_build_object('lab_test_id', m.id, 'name', m.name, 'test_code', m.test_code,
                'container_id', v_cont.id, 'container_code', v_cont.code, 'sample_type', v_cont.sample_type,
                'result_mode', m.dx_result_mode);
        END LOOP;
        IF jsonb_array_length(v_tests) = 0 THEN PERFORM dx_raise('dx_group_empty:' || t.name); END IF;
        v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price - v_disc END;
        v_out := v_out || jsonb_build_object(
            'kind', t.kind, 'fee_code', NULL, 'lab_test_id', t.id, 'item_name', t.name, 'test_code', t.test_code,
            'mrp_paise', t.price_paise, 'price_paise', v_price, 'discount_paise', CASE WHEN v_mode = 'internal' THEN v_price ELSE v_disc END,
            'patient_share_paise', v_net, 'payer_share_paise', 0,
            'price_source', CASE WHEN r ? 'source_appointment_id' AND r ->> 'source_appointment_id' IS NOT NULL THEN 'booking'
                                 ELSE 'mrp' END,
            'source_appointment_id', r -> 'source_appointment_id', 'tests', v_tests);
    END LOOP;
    RETURN v_out;
END $$;

-- dx_purge_clinic back to v7 (migration 131)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_share_links WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_print_log WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_deliveries WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_delivery_overrides WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_results WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_report_releases WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('releases', n);
    DELETE FROM dx_outbox WHERE clinic_id = p_clinic_id;
    -- adopted OPD / IPD orders stay with their encounter or admission, unlinked from the bench
    UPDATE opd_orders SET accession_id = NULL, accession_item_id = NULL, dx_sample_id = NULL
     WHERE clinic_id = p_clinic_id AND source <> 'dx' AND accession_id IS NOT NULL; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('adopted_orders_unlinked', n);
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
    DELETE FROM dx_report_templates WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('templates', n);
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

DROP FUNCTION IF EXISTS dx_b2b_evaluate_holds(uuid);
DROP FUNCTION IF EXISTS dx_b2b_set_hold(uuid, uuid, text, text, text, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_void_statement(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_issue_statement(uuid, uuid, date, date, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_record_payment(uuid, uuid, bigint, text, text, text, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_reverse_visit_charges(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_reverse_charge(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_post_visit_charges(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_post_charge(uuid, uuid, uuid, uuid, bigint, uuid, text);
DROP FUNCTION IF EXISTS dx_b2b_ageing(uuid);
DROP FUNCTION IF EXISTS dx_b2b_balance(uuid, uuid);
DROP FUNCTION IF EXISTS dx_b2b_unpaid(uuid, uuid);
DROP FUNCTION IF EXISTS dx_check_barcode_stock(uuid, uuid, uuid, text);
DROP TABLE IF EXISTS dx_barcode_stock;
DROP TABLE IF EXISTS dx_b2b_ledger;
DROP TABLE IF EXISTS dx_b2b_statements;
DROP FUNCTION IF EXISTS dx_guard_barcode_stock();
DROP FUNCTION IF EXISTS dx_guard_b2b_statement();
DROP FUNCTION IF EXISTS dx_guard_b2b_ledger();
DROP INDEX IF EXISTS idx_clinic_admins_dx_b2b_account;
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_dx_b2b_account_fk;
ALTER TABLE clinic_admins DROP COLUMN IF EXISTS dx_b2b_account_id;
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_accounts_rate_card_fk;
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_hold_kind_shape;
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_copay_percent;
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_copay_shape;
ALTER TABLE dx_b2b_accounts
    DROP COLUMN IF EXISTS rate_card_id, DROP COLUMN IF EXISTS credit_limit_paise, DROP COLUMN IF EXISTS credit_days,
    DROP COLUMN IF EXISTS gstin, DROP COLUMN IF EXISTS billing_address, DROP COLUMN IF EXISTS statement_cycle,
    DROP COLUMN IF EXISTS auto_hold, DROP COLUMN IF EXISTS hold_kind, DROP COLUMN IF EXISTS copay_mode,
    DROP COLUMN IF EXISTS copay_value;
DROP TABLE IF EXISTS dx_rate_card_items;
DROP TABLE IF EXISTS dx_rate_cards;
ALTER FUNCTION dx_price_items(jsonb, jsonb) SET search_path = public, pg_temp;
ALTER FUNCTION dx_purge_clinic(uuid) SET search_path = public, pg_temp;
