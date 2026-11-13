-- ============================================================================
-- Rollback 129: Diagnostic OS logistics and outsourcing (DX-M5)
-- Refused while any batch, routing rule, outsource partner or outsourced
-- tube / order exists: purge each DX clinic first
-- (SELECT dx_purge_clinic('<id>')). Restores dx_insert_lines (127),
-- dx_reject_sample and dx_purge_clinic v4 (128).
-- The normal rollback is a code revert: pre-129 code runs unchanged on 129.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_batches') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_batches))
       OR (to_regclass('public.dx_routing_rules') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_routing_rules))
       OR (to_regclass('public.dx_outsource_partners') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_outsource_partners)) THEN
        RAISE EXCEPTION '129_down: batches, routing rules or outsource partners exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS dx_receive_batch(uuid, uuid, text[], numeric, text, uuid, text);
DROP FUNCTION IF EXISTS dx_dispatch_batch(uuid, uuid, numeric, text, uuid, text);
DROP FUNCTION IF EXISTS dx_batch_add(uuid, uuid, text[], uuid, text);
DROP FUNCTION IF EXISTS dx_create_batch(uuid, uuid, uuid, uuid, text, numeric, uuid, text);
DROP FUNCTION IF EXISTS dx_batch_json(uuid, uuid);

-- back to the 127 / 128 bodies before the columns they would reference go
CREATE OR REPLACE FUNCTION dx_insert_lines(p_clinic_id UUID, p_accession_id UUID, p_lines JSONB, p_actor_id UUID,
                                           p_actor_name TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    a dx_accessions; l JSONB; tst JSONB; v_item UUID; v_sample UUID; v_seq INTEGER; v_collect UUID; v_process UUID;
    v_n INTEGER := 0; v_cont_code TEXT;
BEGIN
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id;
    SELECT id INTO v_collect FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = a.branch_id AND is_active;
    IF v_collect IS NULL THEN PERFORM dx_raise('dx_no_collection_site'); END IF;
    -- processing site: the registration site if it processes, else the clinic's only processing site
    SELECT id INTO v_process FROM dx_sites WHERE clinic_id = p_clinic_id AND id = v_collect AND kind <> 'collection';
    IF v_process IS NULL THEN
        SELECT CASE WHEN count(*) = 1 THEN min(id::TEXT)::UUID END INTO v_process
          FROM dx_sites WHERE clinic_id = p_clinic_id AND is_active AND kind <> 'collection';
    END IF;
    FOR l IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
        INSERT INTO dx_accession_items (clinic_id, accession_id, lab_test_id, kind, fee_code, source_appointment_id,
                                        item_name, test_code, mrp_paise, price_paise, discount_paise,
                                        patient_share_paise, payer_share_paise, price_source)
        VALUES (p_clinic_id, p_accession_id, NULLIF(l ->> 'lab_test_id', '')::UUID, l ->> 'kind', l ->> 'fee_code',
                NULLIF(l ->> 'source_appointment_id', '')::UUID, left(l ->> 'item_name', 200), l ->> 'test_code',
                (l ->> 'mrp_paise')::INTEGER, (l ->> 'price_paise')::INTEGER, (l ->> 'discount_paise')::INTEGER,
                (l ->> 'patient_share_paise')::INTEGER, (l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source')
        RETURNING id INTO v_item;
        v_n := v_n + 1;
        FOR tst IN SELECT * FROM jsonb_array_elements(l -> 'tests') LOOP
            v_sample := NULL;
            IF tst ->> 'container_id' IS NOT NULL THEN
                IF v_process IS NULL THEN PERFORM dx_raise('dx_no_processing_site'); END IF;
                SELECT id INTO v_sample FROM dx_samples
                 WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id AND status = 'pending_collection'
                   AND container_id = (tst ->> 'container_id')::UUID AND processing_site_id = v_process
                 ORDER BY created_at LIMIT 1;
                IF v_sample IS NULL THEN
                    IF NOT EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id
                                                              AND container_id = (tst ->> 'container_id')::UUID)
                       AND a.sample_seq IS NOT NULL THEN
                        v_seq := a.sample_seq;
                    ELSE
                        v_seq := dx_next_sample_seq(p_clinic_id, a.accession_year);
                    END IF;
                    v_cont_code := tst ->> 'container_code';
                    INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source,
                                            tube_seq, collection_site_id, processing_site_id)
                    VALUES (p_clinic_id, p_accession_id, (tst ->> 'container_id')::UUID, tst ->> 'sample_type',
                            lpad((a.accession_year % 100)::TEXT, 2, '0') || v_cont_code || lpad(v_seq::TEXT, 8, '0'),
                            'generated', v_seq, v_collect, v_process)
                    RETURNING id INTO v_sample;
                END IF;
            END IF;
            INSERT INTO opd_orders (clinic_id, branch_id, source, accession_id, accession_item_id, dx_sample_id,
                                    patient_id, family_member_id, order_type, catalog_type, catalog_reference_id,
                                    item_name, sample_type, urgency, clinical_notes, unit_price_paise, ordered_by,
                                    ordered_by_name)
            VALUES (p_clinic_id, a.branch_id, 'dx', p_accession_id, v_item, v_sample, a.patient_id, a.family_member_id,
                    'investigation', 'lab_test', (tst ->> 'lab_test_id')::UUID, left(tst ->> 'name', 200),
                    CASE WHEN v_sample IS NULL THEN NULL ELSE left(tst ->> 'sample_type', 60) END,
                    a.priority, a.clinical_notes, 0, p_actor_id, left(p_actor_name, 100))
            ON CONFLICT DO NOTHING;     -- a package listing a test twice explodes it once
        END LOOP;
    END LOOP;
    RETURN v_n;
END $$;

CREATE OR REPLACE FUNCTION dx_reject_sample(p_clinic_id UUID, p_sample_id UUID, p_reason_code TEXT, p_notes TEXT,
                                            p_recollect BOOLEAN, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; s dx_samples; rr dx_rejection_reasons; o opd_orders; v_new UUID; v_seq INTEGER;
        v_code TEXT; v_n INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_sample_not_found'); END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = s.accession_id FOR UPDATE;  -- visit -> tube
    SELECT * INTO s FROM dx_samples WHERE clinic_id = p_clinic_id AND id = p_sample_id FOR UPDATE;
    IF s.status = 'rejected' THEN                                                   -- double click: same result
        RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', true);
    END IF;
    IF s.status NOT IN ('collected', 'in_transit', 'received', 'stored') THEN PERFORM dx_raise('dx_sample_not_rejectable'); END IF;
    SELECT * INTO rr FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id AND code = upper(btrim(p_reason_code))
                                                 AND is_active;
    IF NOT FOUND THEN PERFORM dx_raise('dx_rejection_reason_unknown'); END IF;
    IF rr.code = 'OTHER' AND char_length(btrim(COALESCE(p_notes, ''))) < 3 THEN PERFORM dx_raise('dx_rejection_notes_required'); END IF;
    -- a signed result is amended, never rejected away
    IF EXISTS (SELECT 1 FROM opd_orders o2 WHERE o2.clinic_id = p_clinic_id AND o2.dx_sample_id = s.id
                 AND (o2.status IN ('resulted', 'completed')
                      OR EXISTS (SELECT 1 FROM lab_results lr WHERE lr.clinic_id = o2.clinic_id AND lr.order_id = o2.id
                                                                AND lr.status IN ('validated', 'amended')))) THEN
        PERFORM dx_raise('dx_sample_results_signed');
    END IF;
    UPDATE dx_samples SET status = 'rejected', rejection_reason_code = rr.code,
                          rejection_notes = left(NULLIF(btrim(p_notes), ''), 500), rejected_at = now(), rejected_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND id = s.id;
    IF p_recollect THEN
        -- a recollection takes a fresh sequence (AUDIT A7); a pre-printed / external tube waits for its new label
        IF s.barcode_source = 'generated' THEN
            v_seq := dx_next_sample_seq(p_clinic_id, a.accession_year);
            SELECT code INTO v_code FROM dx_containers WHERE clinic_id = p_clinic_id AND id = s.container_id;
        END IF;
        INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source, tube_seq,
                                collection_site_id, processing_site_id, recollect_of)
        VALUES (p_clinic_id, a.id, s.container_id, s.sample_type,
                CASE WHEN v_seq IS NULL THEN NULL
                     ELSE lpad((a.accession_year % 100)::TEXT, 2, '0') || v_code || lpad(v_seq::TEXT, 8, '0') END,
                s.barcode_source, v_seq, s.collection_site_id, s.processing_site_id, s.id)
        RETURNING id INTO v_new;
    END IF;
    FOR o IN SELECT * FROM opd_orders WHERE clinic_id = p_clinic_id AND dx_sample_id = s.id
                                         AND status IN ('ordered', 'billed', 'sample_collected', 'in_progress')
              ORDER BY created_at, id LOOP
        UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                              cancellation_reason = 'sample_rejected:' || rr.code
         WHERE clinic_id = p_clinic_id AND id = o.id;
        IF v_new IS NOT NULL THEN
            INSERT INTO opd_orders (clinic_id, branch_id, source, accession_id, accession_item_id, dx_sample_id,
                                    replaces_order_id, patient_id, family_member_id, order_type, catalog_type,
                                    catalog_reference_id, item_name, sample_type, urgency, clinical_notes,
                                    unit_price_paise, ordered_by, ordered_by_name)
            VALUES (p_clinic_id, o.branch_id, 'dx', o.accession_id, o.accession_item_id, v_new, o.id, o.patient_id,
                    o.family_member_id, o.order_type, o.catalog_type, o.catalog_reference_id, o.item_name, o.sample_type,
                    o.urgency, o.clinical_notes, 0, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100));
        END IF;
        v_n := v_n + 1;
    END LOOP;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, a.id, s.id, 'rejected', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('reason_code', rr.code, 'reason', rr.label, 'notes', p_notes, 'orders_cancelled', v_n,
                               'replacement_sample_id', v_new));
    RETURN dx_visit_json(p_clinic_id, a.id)
        || jsonb_build_object('replayed', false, 'rejected_sample_id', s.id, 'replacement_sample_id', v_new,
                              'reason_label', rr.label);
END $$;

-- dx_purge_clinic back to v4 (migration 128)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
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

DROP FUNCTION IF EXISTS dx_resolve_routing(uuid, uuid, uuid, uuid);
DROP TABLE IF EXISTS dx_batch_samples;
DROP TABLE IF EXISTS dx_batches;
DROP TABLE IF EXISTS dx_routing_rules;
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_partner_fk;
ALTER TABLE opd_orders DROP COLUMN IF EXISTS outsource_partner_id;
ALTER TABLE dx_samples DROP CONSTRAINT IF EXISTS dx_samples_destination_shape;
ALTER TABLE dx_samples DROP CONSTRAINT IF EXISTS dx_samples_partner_fk;
ALTER TABLE dx_samples DROP COLUMN IF EXISTS outsource_partner_id;
DROP TABLE IF EXISTS dx_outsource_partners;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '129_dx_logistics.sql';
