-- ============================================================================
-- Rollback 120: service master, ICD-10 reference
-- Restores 115's opd_place_orders and 117's opd_invoice_item_tax (verbatim),
-- which read clinics.opd_settings.service_catalog again. Services created or
-- edited only in the master (not in the legacy JSON) disappear from billing:
-- refused while any master row has no matching legacy entry. Take
-- pg_dump -t billable_services FIRST. ICD-10 reference data is re-seedable.
-- The normal rollback is a code revert: pre-120 code runs unchanged on 120.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.billable_services') IS NOT NULL AND EXISTS (
        SELECT 1 FROM billable_services s JOIN clinics k ON k.id = s.clinic_id
         WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                               CASE WHEN jsonb_typeof(k.opd_settings -> 'service_catalog') = 'array'
                                    THEN k.opd_settings -> 'service_catalog' ELSE '[]'::jsonb END) c
                            WHERE c ->> 'code' = s.service_code)) THEN
        RAISE EXCEPTION '120_down: services exist only in the master; export them to opd_settings.service_catalog first';
    END IF;
END $$;

-- pre-120 definitions, copied verbatim from 115 / 117
CREATE OR REPLACE FUNCTION opd_place_orders(
    p_clinic_id UUID, p_encounter_id UUID, p_items JSONB, p_idempotency_key TEXT,
    p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    e opd_encounters; r JSONB; n INTEGER := 0; v_id UUID; v_ids UUID[] := '{}';
    v_name TEXT; v_price INTEGER; v_sample TEXT; v_code TEXT; v_ref UUID; v_cat TEXT;
    v_catalog JSONB; v_billing JSONB; v_prior UUID;
BEGIN
    IF p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9_-]{8,72}$' THEN
        RAISE EXCEPTION 'opd_order_bad_key' USING ERRCODE = 'P0001';
    END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) NOT BETWEEN 1 AND 30 THEN
        RAISE EXCEPTION 'opd_order_empty' USING ERRCODE = 'P0001';
    END IF;
    SELECT * INTO e FROM opd_encounters WHERE id = p_encounter_id AND clinic_id = p_clinic_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;

    SELECT encounter_id INTO v_prior FROM opd_orders
     WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key || ':1';
    IF FOUND THEN
        IF v_prior <> p_encounter_id THEN RAISE EXCEPTION 'opd_order_key_reused' USING ERRCODE = 'P0001'; END IF;
        RETURN jsonb_build_object('replayed', true, 'orders',
            (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
              WHERE x.clinic_id = p_clinic_id AND left(x.idempotency_key, length(p_idempotency_key) + 1)
                                                  = p_idempotency_key || ':'));
    END IF;
    IF e.status = 'superseded' THEN RAISE EXCEPTION 'opd_encounter_superseded' USING ERRCODE = 'P0001'; END IF;

    SELECT COALESCE(opd_settings -> 'service_catalog', '[]'::jsonb) INTO v_catalog
      FROM clinics WHERE id = p_clinic_id;
    IF jsonb_typeof(v_catalog) IS DISTINCT FROM 'array' THEN v_catalog := '[]'::jsonb; END IF;

    FOR r IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        n := n + 1;
        v_cat := r ->> 'catalog_type';
        v_ref := NULL; v_code := NULL; v_name := NULL; v_price := NULL; v_sample := NULL;
        IF v_cat = 'lab_test' THEN
            v_ref := NULLIF(r ->> 'catalog_reference_id', '')::UUID;
            SELECT t.name, t.price_paise, t.sample_type INTO v_name, v_price, v_sample
              FROM lab_tests t WHERE t.id = v_ref AND t.clinic_id = p_clinic_id AND t.is_active;
            IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_catalog_not_found:%', n USING ERRCODE = 'P0001'; END IF;
        ELSIF v_cat = 'service_catalog' THEN
            v_code := r ->> 'catalog_code';
            SELECT c ->> 'name', (c ->> 'price_paise')::INTEGER INTO v_name, v_price
              FROM jsonb_array_elements(v_catalog) c
             WHERE c ->> 'code' = v_code AND COALESCE((c ->> 'active')::BOOLEAN, true)
             LIMIT 1;
            IF NOT FOUND THEN RAISE EXCEPTION 'opd_order_catalog_not_found:%', n USING ERRCODE = 'P0001'; END IF;
            v_sample := NULLIF(btrim(r ->> 'sample_type'), '');
        ELSIF v_cat = 'custom' THEN
            v_name := btrim(r ->> 'item_name');
            v_price := COALESCE((r ->> 'unit_price_paise')::INTEGER, 0);
            v_sample := NULLIF(btrim(r ->> 'sample_type'), '');
        ELSE
            RAISE EXCEPTION 'opd_order_bad_catalog:%', n USING ERRCODE = 'P0001';
        END IF;

        v_id := NULL;
        INSERT INTO opd_orders (clinic_id, branch_id, source, encounter_id, appointment_id, patient_id,
                                family_member_id, doctor_id, order_type, catalog_type, catalog_reference_id,
                                catalog_code, item_name, sample_type, urgency, clinical_notes, unit_price_paise,
                                quantity, ordered_by, ordered_by_name, idempotency_key, created_at)
        VALUES (p_clinic_id, e.branch_id, 'opd', e.id, e.appointment_id, e.patient_id, e.family_member_id,
                e.doctor_id, r ->> 'order_type', v_cat, v_ref, v_code, left(v_name, 200), left(v_sample, 60),
                COALESCE(r ->> 'urgency', 'routine'), NULLIF(btrim(r ->> 'clinical_notes'), ''),
                COALESCE(v_price, 0), COALESCE((r ->> 'quantity')::SMALLINT, 1), p_actor_id,
                -- clock_timestamp: lines of one call keep the doctor's order on the bill and worklist
                left(p_actor_name, 100), p_idempotency_key || ':' || n, clock_timestamp())
        ON CONFLICT (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
        RETURNING id INTO v_id;
        IF v_id IS NULL THEN
            -- a concurrent call with this key committed first: its orders are the answer
            IF n = 1 THEN
                RETURN jsonb_build_object('replayed', true, 'orders',
                    (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
                      WHERE x.clinic_id = p_clinic_id AND left(x.idempotency_key, length(p_idempotency_key) + 1)
                                                          = p_idempotency_key || ':'));
            END IF;
            RAISE EXCEPTION 'opd_order_key_reused' USING ERRCODE = 'P0001';
        END IF;
        v_ids := v_ids || v_id;
    END LOOP;

    v_billing := opd_bill_orders(p_clinic_id, e.appointment_id, v_ids, false, p_actor_id);
    RETURN jsonb_build_object('replayed', false, 'billing', v_billing, 'orders',
        (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.created_at, x.id) FROM opd_orders x
          WHERE x.clinic_id = p_clinic_id AND x.id = ANY (v_ids)));
END $$;

CREATE OR REPLACE FUNCTION opd_invoice_item_tax() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_settings JSONB; v_cat JSONB; v_tax INTEGER;
BEGIN
    SELECT opd_settings INTO v_settings FROM clinics WHERE id = NEW.clinic_id;
    IF NEW.catalog_code IS NOT NULL AND jsonb_typeof(v_settings -> 'service_catalog') = 'array' THEN
        SELECT c INTO v_cat FROM jsonb_array_elements(v_settings -> 'service_catalog') c
         WHERE c ->> 'code' = NEW.catalog_code LIMIT 1;
        IF v_cat IS NOT NULL THEN
            NEW.hsn_sac := NULLIF(v_cat ->> 'hsn_sac', '');
            NEW.tax_rate_bps := COALESCE(NULLIF(v_cat ->> 'tax_rate_bps', '')::SMALLINT, 0);
        END IF;
    END IF;
    IF NULLIF(btrim(v_settings ->> 'gstin'), '') IS NULL THEN
        NEW.tax_rate_bps := 0;   -- an unregistered clinic cannot charge GST
    END IF;
    v_tax := opd_gst_amount(NEW.quantity::BIGINT * NEW.unit_price_paise - NEW.discount_paise, NEW.tax_rate_bps);
    NEW.igst_paise := 0;
    NEW.cgst_paise := v_tax / 2;
    NEW.sgst_paise := v_tax - v_tax / 2;
    RETURN NEW;
END $$;

ALTER FUNCTION opd_place_orders(uuid, uuid, jsonb, text, uuid, text) SET search_path = public, pg_temp;

DROP FUNCTION IF EXISTS icd10_search(text, integer);
DROP FUNCTION IF EXISTS icd10_chapter(text);
DROP TABLE IF EXISTS icd10_codes;
DROP FUNCTION IF EXISTS opd_import_service_catalog(uuid, jsonb, boolean);
DROP FUNCTION IF EXISTS opd_service_catalog(uuid);
DROP FUNCTION IF EXISTS opd_catalog_item(uuid, text);
DROP FUNCTION IF EXISTS opd_service_json(billable_services);   -- takes the table's row type
DROP TABLE IF EXISTS billable_services;
DROP FUNCTION IF EXISTS opd_service_item_type(text);
DROP FUNCTION IF EXISTS opd_guard_billable_service();
-- pg_trgm is left installed: other objects may use it.
