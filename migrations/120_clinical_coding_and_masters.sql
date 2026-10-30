-- ============================================================================
-- Migration 120: Central masters, relational service catalogue, ICD-10 coding (G12)
-- ============================================================================
-- 1. billable_services: the clinic's tariff master, one row per service code.
--    Until now the catalogue lived in clinics.opd_settings.service_catalog
--    (JSON), read by Python billing, order placement (opd_place_orders) and the
--    GST line trigger (opd_invoice_item_tax).
--
--    Backward compatible, fail-open: every reader now goes through
--    opd_catalog_item(clinic, code) / opd_service_catalog(clinic): a master row
--    wins, and a code that exists only in the legacy JSON still resolves. The
--    JSON is backfilled here (opd_import_service_catalog, idempotent) and left
--    in place, so pre-120 code and a rollback keep working.
--
-- 2. icd10_codes: shared WHO ICD-10 reference (no clinic_id, read-only to the
--    API), seeded with ~400 common outpatient / inpatient codes. icd10_search()
--    ranks code-prefix, then description-prefix, then substring, then fuzzy
--    (pg_trgm word similarity) matches. pg_trgm and its GIN index are created
--    when the server has the extension (Supabase does); without it search
--    falls back to indexed prefix / ILIKE matching.
--
-- Additive and idempotent. Rollback: migrations/rollback/120_down.sql.
-- ============================================================================

-- ── 1. Service master ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS billable_services (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id            UUID NULL,          -- NULL = offered at every branch
    service_code         TEXT NOT NULL CHECK (service_code ~ '^[A-Z0-9_-]{2,40}$'),
    name                 TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
    -- 'diagnostic' kept from the legacy catalogue (ECG, X-ray ordered as investigations)
    category             TEXT NOT NULL CHECK (category IN ('consultation', 'procedure', 'nursing', 'radiology',
                                                           'diagnostic', 'physiotherapy', 'ambulance',
                                                           'room_service', 'other')),
    department           TEXT NULL CHECK (char_length(department) <= 100),
    standard_price_paise INTEGER NOT NULL DEFAULT 0 CHECK (standard_price_paise BETWEEN 0 AND 100000000),
    hsn_sac              TEXT NOT NULL DEFAULT '999312' CHECK (hsn_sac ~ '^[0-9]{4,8}$'),
    -- the GST slabs opd_invoice_items accepts (117), 40% included
    tax_rate_bps         SMALLINT NOT NULL DEFAULT 0 CHECK (tax_rate_bps IN (0, 500, 1200, 1800, 2800, 4000)),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    notes                TEXT NULL CHECK (char_length(notes) <= 300),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT billable_services_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT billable_services_clinic_code_key UNIQUE (clinic_id, service_code),
    CONSTRAINT billable_services_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_billable_services_clinic ON billable_services (clinic_id, category, is_active);

DROP TRIGGER IF EXISTS trg_billable_services_touch ON billable_services;
CREATE TRIGGER trg_billable_services_touch BEFORE UPDATE ON billable_services
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- A code is history: bills and orders carry it, so it never changes.
CREATE OR REPLACE FUNCTION opd_guard_billable_service() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.clinic_id, NEW.service_code) IS DISTINCT FROM (OLD.clinic_id, OLD.service_code) THEN
        RAISE EXCEPTION 'opd_service_code_immutable' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_billable_services_guard ON billable_services;
CREATE TRIGGER trg_billable_services_guard BEFORE UPDATE ON billable_services
    FOR EACH ROW EXECUTE FUNCTION opd_guard_billable_service();

-- legacy item_type <-> master category
CREATE OR REPLACE FUNCTION opd_service_item_type(p_category TEXT) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_category
        WHEN 'consultation' THEN 'consultation' WHEN 'nursing' THEN 'nursing'
        WHEN 'diagnostic' THEN 'diagnostic' WHEN 'radiology' THEN 'diagnostic'
        WHEN 'procedure' THEN 'procedure' WHEN 'physiotherapy' THEN 'procedure'
        ELSE 'other' END
$$;

-- A master row in the legacy catalogue item shape every reader already understands.
CREATE OR REPLACE FUNCTION opd_service_json(s billable_services) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'id', s.id, 'code', s.service_code, 'name', s.name, 'item_type', opd_service_item_type(s.category),
        'category', s.category, 'department', s.department, 'price_paise', s.standard_price_paise,
        'hsn_sac', s.hsn_sac, 'tax_rate_bps', s.tax_rate_bps, 'active', s.is_active, 'branch_id', s.branch_id,
        'notes', s.notes, 'updated_at', s.updated_at, 'source', 'master')
$$;

-- One service by code: master row first, legacy JSON entry as fallback; NULL if neither.
CREATE OR REPLACE FUNCTION opd_catalog_item(p_clinic_id UUID, p_code TEXT) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        (SELECT opd_service_json(s) FROM billable_services s WHERE s.clinic_id = p_clinic_id AND s.service_code = p_code),
        (SELECT c || jsonb_build_object('source', 'legacy')
           FROM clinics k,
                jsonb_array_elements(CASE WHEN jsonb_typeof(k.opd_settings -> 'service_catalog') = 'array'
                                          THEN k.opd_settings -> 'service_catalog' ELSE '[]'::jsonb END) c
          WHERE k.id = p_clinic_id AND c ->> 'code' = p_code
          LIMIT 1))
$$;

-- The whole effective catalogue: master rows plus legacy entries whose code has no master row.
CREATE OR REPLACE FUNCTION opd_service_catalog(p_clinic_id UUID) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT COALESCE(jsonb_agg(x.item ORDER BY lower(x.item ->> 'name'), x.item ->> 'code'), '[]'::jsonb)
      FROM (SELECT opd_service_json(s) AS item FROM billable_services s WHERE s.clinic_id = p_clinic_id
            UNION ALL
            SELECT c || jsonb_build_object('source', 'legacy')
              FROM clinics k,
                   jsonb_array_elements(CASE WHEN jsonb_typeof(k.opd_settings -> 'service_catalog') = 'array'
                                             THEN k.opd_settings -> 'service_catalog' ELSE '[]'::jsonb END) c
             WHERE k.id = p_clinic_id AND jsonb_typeof(c) = 'object' AND c ->> 'code' IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM billable_services s
                                WHERE s.clinic_id = p_clinic_id AND s.service_code = c ->> 'code')) x
$$;

-- Legacy JSON items -> master rows. p_overwrite = false: backfill (an existing
-- master row wins); true: the legacy settings API writing through. Malformed
-- values fall back to safe defaults instead of failing the batch. Returns the
-- number of rows inserted or updated.
CREATE OR REPLACE FUNCTION opd_import_service_catalog(p_clinic_id UUID, p_items JSONB, p_overwrite BOOLEAN)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    item JSONB; v_n INTEGER := 0; v_rows INTEGER;
    v_name TEXT; v_code TEXT; v_price INTEGER; v_hsn TEXT; v_tax SMALLINT; v_cat TEXT; v_active BOOLEAN;
BEGIN
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' THEN RETURN 0; END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        CONTINUE WHEN jsonb_typeof(item) <> 'object';
        v_name := left(btrim(COALESCE(item ->> 'name', item ->> 'title', '')), 200);
        CONTINUE WHEN v_name = '';
        v_code := upper(btrim(COALESCE(item ->> 'code', '')));
        IF v_code !~ '^[A-Z0-9_-]{2,40}$' THEN
            v_code := 'SVC_' || upper(substr(md5(lower(v_name)), 1, 8));
        END IF;
        v_price := CASE WHEN item ->> 'price_paise' ~ '^[0-9]{1,9}$' THEN (item ->> 'price_paise')::INTEGER
                        WHEN item ->> 'price' ~ '^[0-9]{1,7}(\.[0-9]{1,2})?$' THEN round((item ->> 'price')::NUMERIC * 100)::INTEGER
                        ELSE 0 END;
        v_price := LEAST(v_price, 100000000);
        v_hsn := CASE WHEN item ->> 'hsn_sac' ~ '^[0-9]{4,8}$' THEN item ->> 'hsn_sac' ELSE '999312' END;
        v_tax := CASE WHEN item ->> 'tax_rate_bps' IN ('0', '500', '1200', '1800', '2800', '4000')
                      THEN (item ->> 'tax_rate_bps')::SMALLINT ELSE 0 END;
        v_cat := CASE WHEN item ->> 'item_type' IN ('consultation', 'nursing', 'diagnostic', 'procedure', 'other')
                      THEN item ->> 'item_type' ELSE 'procedure' END;
        v_active := COALESCE(item ->> 'active', 'true') NOT IN ('false', 'f', '0');
        IF p_overwrite THEN
            INSERT INTO billable_services (clinic_id, service_code, name, category, standard_price_paise, hsn_sac,
                                           tax_rate_bps, is_active)
            VALUES (p_clinic_id, v_code, v_name, v_cat, v_price, v_hsn, v_tax, v_active)
            ON CONFLICT (clinic_id, service_code) DO UPDATE
               SET name = EXCLUDED.name, category = EXCLUDED.category,
                   standard_price_paise = EXCLUDED.standard_price_paise, hsn_sac = EXCLUDED.hsn_sac,
                   tax_rate_bps = EXCLUDED.tax_rate_bps, is_active = EXCLUDED.is_active;
        ELSE
            INSERT INTO billable_services (clinic_id, service_code, name, category, standard_price_paise, hsn_sac,
                                           tax_rate_bps, is_active)
            VALUES (p_clinic_id, v_code, v_name, v_cat, v_price, v_hsn, v_tax, v_active)
            ON CONFLICT (clinic_id, service_code) DO NOTHING;
        END IF;
        GET DIAGNOSTICS v_rows = ROW_COUNT;
        v_n := v_n + v_rows;
    END LOOP;
    RETURN v_n;
END $$;

-- Backfill every clinic's legacy catalogue (idempotent: re-running adds nothing).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT id, opd_settings -> 'service_catalog' AS items FROM clinics
              WHERE jsonb_typeof(opd_settings -> 'service_catalog') = 'array' LOOP
        PERFORM opd_import_service_catalog(r.id, r.items, false);
    END LOOP;
END $$;

-- ── 2. Readers move to the master (115 / 117 definitions, catalogue lookup replaced) ─
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
            -- 120: the service master (billable_services), legacy JSON as fallback
            v_catalog := opd_catalog_item(p_clinic_id, v_code);
            IF v_catalog IS NULL OR NOT COALESCE((v_catalog ->> 'active')::BOOLEAN, true) THEN
                RAISE EXCEPTION 'opd_order_catalog_not_found:%', n USING ERRCODE = 'P0001';
            END IF;
            v_name := v_catalog ->> 'name';
            v_price := (v_catalog ->> 'price_paise')::INTEGER;
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
    IF NEW.catalog_code IS NOT NULL THEN
        v_cat := opd_catalog_item(NEW.clinic_id, NEW.catalog_code);   -- 120: master first, legacy JSON fallback
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

-- ── 3. ICD-10 reference ────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS icd10_codes (
    code           TEXT PRIMARY KEY CHECK (code ~ '^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$'),
    description    TEXT NOT NULL CHECK (char_length(btrim(description)) BETWEEN 2 AND 300),
    chapter_number SMALLINT NULL CHECK (chapter_number BETWEEN 1 AND 22),
    chapter_title  TEXT NULL,
    is_common      BOOLEAN NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- prefix search without pg_trgm, and a stable order
CREATE INDEX IF NOT EXISTS idx_icd10_code_prefix ON icd10_codes (code text_pattern_ops);
CREATE INDEX IF NOT EXISTS idx_icd10_desc_prefix ON icd10_codes (lower(description) text_pattern_ops);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_icd10_trgm ON icd10_codes
                 USING gin (code gin_trgm_ops, description gin_trgm_ops)';
    END IF;
END $$;

-- WHO ICD-10 chapter of a code (U is chapter XXII, between XIX and XX).
CREATE OR REPLACE FUNCTION icd10_chapter(p_code TEXT) RETURNS TABLE (num SMALLINT, title TEXT)
LANGUAGE sql IMMUTABLE AS $$
    SELECT c.num::SMALLINT, c.title FROM (VALUES
        ('A00', 'C00', 1, 'Certain infectious and parasitic diseases'),
        ('C00', 'D50', 2, 'Neoplasms'),
        ('D50', 'E00', 3, 'Diseases of the blood and blood-forming organs and certain disorders involving the immune mechanism'),
        ('E00', 'F00', 4, 'Endocrine, nutritional and metabolic diseases'),
        ('F00', 'G00', 5, 'Mental and behavioural disorders'),
        ('G00', 'H00', 6, 'Diseases of the nervous system'),
        ('H00', 'H60', 7, 'Diseases of the eye and adnexa'),
        ('H60', 'I00', 8, 'Diseases of the ear and mastoid process'),
        ('I00', 'J00', 9, 'Diseases of the circulatory system'),
        ('J00', 'K00', 10, 'Diseases of the respiratory system'),
        ('K00', 'L00', 11, 'Diseases of the digestive system'),
        ('L00', 'M00', 12, 'Diseases of the skin and subcutaneous tissue'),
        ('M00', 'N00', 13, 'Diseases of the musculoskeletal system and connective tissue'),
        ('N00', 'O00', 14, 'Diseases of the genitourinary system'),
        ('O00', 'P00', 15, 'Pregnancy, childbirth and the puerperium'),
        ('P00', 'Q00', 16, 'Certain conditions originating in the perinatal period'),
        ('Q00', 'R00', 17, 'Congenital malformations, deformations and chromosomal abnormalities'),
        ('R00', 'S00', 18, 'Symptoms, signs and abnormal clinical and laboratory findings, not elsewhere classified'),
        ('S00', 'U00', 19, 'Injury, poisoning and certain other consequences of external causes'),
        ('U00', 'V00', 22, 'Codes for special purposes'),
        ('V00', 'Z00', 20, 'External causes of morbidity and mortality'),
        ('Z00', 'ZZZ', 21, 'Factors influencing health status and contact with health services')
    ) AS c (lo, hi, num, title)
    WHERE p_code >= c.lo AND p_code < c.hi
$$;

-- Autocomplete. Code-looking queries match by code prefix; words match
-- description prefix, substring, then (with pg_trgm) fuzzy word similarity
-- for typos ("diabetis"). Short queries prefer is_common codes. Zero-LLM.
CREATE OR REPLACE FUNCTION icd10_search(p_q TEXT, p_limit INTEGER DEFAULT 20)
RETURNS SETOF icd10_codes LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_q TEXT := lower(btrim(regexp_replace(COALESCE(p_q, ''), '\s+', ' ', 'g')));
    v_like TEXT;
    v_limit INTEGER := LEAST(GREATEST(COALESCE(p_limit, 20), 1), 50);
BEGIN
    IF char_length(v_q) = 0 THEN RETURN; END IF;
    v_like := replace(replace(replace(v_q, '\', '\\'), '%', '\%'), '_', '\_');
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        RETURN QUERY EXECUTE
            'SELECT c.* FROM icd10_codes c
              WHERE lower(c.code) LIKE $1 || ''%'' OR lower(c.description) LIKE ''%'' || $1 || ''%''
                 OR (char_length($2) >= 4 AND $2 <% lower(c.description))
              ORDER BY (lower(c.code) LIKE $1 || ''%'') DESC, (lower(c.description) LIKE $1 || ''%'') DESC,
                       (char_length($2) <= 3 AND c.is_common) DESC, (lower(c.description) LIKE ''%'' || $1 || ''%'') DESC,
                       word_similarity($2, lower(c.description)) DESC, c.is_common DESC, c.code
              LIMIT $3' USING v_like, v_q, v_limit;
    ELSE
        RETURN QUERY
            SELECT c.* FROM icd10_codes c
             WHERE lower(c.code) LIKE v_like || '%' OR lower(c.description) LIKE '%' || v_like || '%'
             ORDER BY (lower(c.code) LIKE v_like || '%') DESC, (lower(c.description) LIKE v_like || '%') DESC,
                      (char_length(v_q) <= 3 AND c.is_common) DESC, c.is_common DESC, c.code
             LIMIT v_limit;
    END IF;
END $$;

-- Seed: common outpatient / inpatient WHO ICD-10 codes (titles as WHO publishes
-- them; a few carry the everyday term in brackets so it is searchable).
INSERT INTO icd10_codes (code, description, chapter_number, chapter_title, is_common)
SELECT v.code, v.description, ch.num, ch.title, true
  FROM (VALUES
    ('A00.9', 'Cholera, unspecified'),
    ('A01.0', 'Typhoid fever'),
    ('A01.4', 'Paratyphoid fever, unspecified'),
    ('A02.0', 'Salmonella enteritis'),
    ('A03.9', 'Shigellosis, unspecified'),
    ('A04.9', 'Bacterial intestinal infection, unspecified'),
    ('A06.0', 'Acute amoebic dysentery'),
    ('A07.1', 'Giardiasis [lambliasis]'),
    ('A08.4', 'Viral intestinal infection, unspecified'),
    ('A09.0', 'Other and unspecified gastroenteritis and colitis of infectious origin'),
    ('A09.9', 'Gastroenteritis and colitis of unspecified origin'),
    ('A15.0', 'Tuberculosis of lung, confirmed by sputum microscopy with or without culture'),
    ('A16.2', 'Tuberculosis of lung, without mention of bacteriological or histological confirmation'),
    ('A18.0', 'Tuberculosis of bones and joints'),
    ('A19.9', 'Miliary tuberculosis, unspecified'),
    ('A27.9', 'Leptospirosis, unspecified'),
    ('A30.9', 'Leprosy, unspecified'),
    ('A41.9', 'Sepsis, unspecified'),
    ('A46', 'Erysipelas'),
    ('A49.9', 'Bacterial infection, unspecified'),
    ('A63.0', 'Anogenital (venereal) warts'),
    ('A75.3', 'Typhus fever due to Rickettsia tsutsugamushi [scrub typhus]'),
    ('A82.9', 'Rabies, unspecified'),
    ('A90', 'Dengue fever [classical dengue]'),
    ('A91', 'Dengue haemorrhagic fever'),
    ('A92.0', 'Chikungunya virus disease'),
    ('B00.9', 'Herpesviral infection, unspecified'),
    ('B01.9', 'Varicella without complication [chickenpox]'),
    ('B02.9', 'Zoster without complication [shingles]'),
    ('B05.9', 'Measles without complication'),
    ('B08.4', 'Enteroviral vesicular stomatitis with exanthem [hand, foot and mouth disease]'),
    ('B15.9', 'Hepatitis A without hepatic coma'),
    ('B16.9', 'Acute hepatitis B without delta-agent and without hepatic coma'),
    ('B17.1', 'Acute hepatitis C'),
    ('B18.1', 'Chronic viral hepatitis B without delta-agent'),
    ('B18.2', 'Chronic viral hepatitis C'),
    ('B19.9', 'Unspecified viral hepatitis without hepatic coma'),
    ('B20', 'Human immunodeficiency virus [HIV] disease resulting in infectious and parasitic diseases'),
    ('B24', 'Unspecified human immunodeficiency virus [HIV] disease'),
    ('B26.9', 'Mumps without complication'),
    ('B34.9', 'Viral infection, unspecified'),
    ('B35.4', 'Tinea corporis'),
    ('B35.6', 'Tinea cruris'),
    ('B37.0', 'Candidal stomatitis [oral thrush]'),
    ('B37.3', 'Candidiasis of vulva and vagina'),
    ('B50.9', 'Plasmodium falciparum malaria, unspecified'),
    ('B51.9', 'Plasmodium vivax malaria without complication'),
    ('B54', 'Unspecified malaria'),
    ('B76.9', 'Hookworm disease, unspecified'),
    ('B77.9', 'Ascariasis, unspecified'),
    ('B82.9', 'Intestinal parasitism, unspecified'),
    ('B86', 'Scabies'),
    ('C06.9', 'Malignant neoplasm of mouth, unspecified'),
    ('C16.9', 'Malignant neoplasm of stomach, unspecified'),
    ('C18.9', 'Malignant neoplasm of colon, unspecified'),
    ('C22.0', 'Liver cell carcinoma'),
    ('C34.9', 'Malignant neoplasm of bronchus or lung, unspecified'),
    ('C50.9', 'Malignant neoplasm of breast, unspecified'),
    ('C53.9', 'Malignant neoplasm of cervix uteri, unspecified'),
    ('C61', 'Malignant neoplasm of prostate'),
    ('D17.9', 'Benign lipomatous neoplasm, unspecified [lipoma]'),
    ('D25.9', 'Leiomyoma of uterus, unspecified [fibroid]'),
    ('D50.9', 'Iron deficiency anaemia, unspecified'),
    ('D51.9', 'Vitamin B12 deficiency anaemia, unspecified'),
    ('D52.9', 'Folate deficiency anaemia, unspecified'),
    ('D56.1', 'Beta thalassaemia'),
    ('D57.1', 'Sickle-cell disease without crisis'),
    ('D64.9', 'Anaemia, unspecified'),
    ('D69.0', 'Allergic purpura'),
    ('D69.6', 'Thrombocytopenia, unspecified'),
    ('E03.9', 'Hypothyroidism, unspecified'),
    ('E04.9', 'Nontoxic goitre, unspecified'),
    ('E05.9', 'Thyrotoxicosis, unspecified [hyperthyroidism]'),
    ('E10.1', 'Insulin-dependent diabetes mellitus [type 1] with ketoacidosis'),
    ('E10.9', 'Insulin-dependent diabetes mellitus [type 1] without complications'),
    ('E11.1', 'Non-insulin-dependent diabetes mellitus [type 2] with ketoacidosis'),
    ('E11.2', 'Non-insulin-dependent diabetes mellitus [type 2] with renal complications'),
    ('E11.4', 'Non-insulin-dependent diabetes mellitus [type 2] with neurological complications'),
    ('E11.5', 'Non-insulin-dependent diabetes mellitus [type 2] with peripheral circulatory complications'),
    ('E11.6', 'Non-insulin-dependent diabetes mellitus [type 2] with other specified complications'),
    ('E11.7', 'Non-insulin-dependent diabetes mellitus [type 2] with multiple complications'),
    ('E11.9', 'Non-insulin-dependent diabetes mellitus [type 2] without complications'),
    ('E14.9', 'Unspecified diabetes mellitus without complications'),
    ('E16.2', 'Hypoglycaemia, unspecified'),
    ('E28.2', 'Polycystic ovarian syndrome'),
    ('E44.0', 'Moderate protein-energy malnutrition'),
    ('E46', 'Unspecified protein-energy malnutrition'),
    ('E55.9', 'Vitamin D deficiency, unspecified'),
    ('E66.9', 'Obesity, unspecified'),
    ('E78.0', 'Pure hypercholesterolaemia'),
    ('E78.5', 'Hyperlipidaemia, unspecified'),
    ('E79.0', 'Hyperuricaemia without signs of inflammatory arthritis and tophaceous disease'),
    ('E86', 'Volume depletion [dehydration]'),
    ('E87.1', 'Hypo-osmolality and hyponatraemia'),
    ('E87.6', 'Hypokalaemia'),
    ('F03', 'Unspecified dementia'),
    ('F10.2', 'Mental and behavioural disorders due to use of alcohol: dependence syndrome'),
    ('F17.2', 'Mental and behavioural disorders due to use of tobacco: dependence syndrome'),
    ('F20.9', 'Schizophrenia, unspecified'),
    ('F31.9', 'Bipolar affective disorder, unspecified'),
    ('F32.9', 'Depressive episode, unspecified'),
    ('F41.1', 'Generalized anxiety disorder'),
    ('F41.9', 'Anxiety disorder, unspecified'),
    ('F43.2', 'Adjustment disorders'),
    ('F51.0', 'Nonorganic insomnia'),
    ('F90.0', 'Disturbance of activity and attention [ADHD]'),
    ('G03.9', 'Meningitis, unspecified'),
    ('G20', 'Parkinson''s disease'),
    ('G30.9', 'Alzheimer''s disease, unspecified'),
    ('G40.9', 'Epilepsy, unspecified'),
    ('G41.9', 'Status epilepticus, unspecified'),
    ('G43.9', 'Migraine, unspecified'),
    ('G44.2', 'Tension-type headache'),
    ('G45.9', 'Transient cerebral ischaemic attack, unspecified'),
    ('G47.3', 'Sleep apnoea'),
    ('G51.0', 'Bell''s palsy'),
    ('G56.0', 'Carpal tunnel syndrome'),
    ('G61.0', 'Guillain-Barré syndrome'),
    ('G62.9', 'Polyneuropathy, unspecified'),
    ('G80.9', 'Cerebral palsy, unspecified'),
    ('H00.0', 'Hordeolum and other deep inflammation of eyelid [stye]'),
    ('H04.1', 'Other disorders of lacrimal gland [dry eye]'),
    ('H10.3', 'Acute conjunctivitis, unspecified'),
    ('H10.9', 'Conjunctivitis, unspecified'),
    ('H25.9', 'Senile cataract, unspecified'),
    ('H26.9', 'Cataract, unspecified'),
    ('H36.0', 'Diabetic retinopathy'),
    ('H40.9', 'Glaucoma, unspecified'),
    ('H52.1', 'Myopia'),
    ('H52.4', 'Presbyopia'),
    ('H60.9', 'Otitis externa, unspecified'),
    ('H61.2', 'Impacted cerumen [ear wax]'),
    ('H65.9', 'Nonsuppurative otitis media, unspecified'),
    ('H66.9', 'Otitis media, unspecified'),
    ('H81.0', 'Ménière''s disease'),
    ('H81.1', 'Benign paroxysmal vertigo'),
    ('H91.9', 'Hearing loss, unspecified'),
    ('H92.0', 'Otalgia [earache]'),
    ('I01.9', 'Acute rheumatic heart disease, unspecified'),
    ('I05.9', 'Mitral valve disease, unspecified'),
    ('I10', 'Essential (primary) hypertension'),
    ('I11.9', 'Hypertensive heart disease without (congestive) heart failure'),
    ('I12.9', 'Hypertensive renal disease without renal failure'),
    ('I20.0', 'Unstable angina'),
    ('I20.9', 'Angina pectoris, unspecified'),
    ('I21.0', 'Acute transmural myocardial infarction of anterior wall'),
    ('I21.4', 'Acute subendocardial myocardial infarction'),
    ('I21.9', 'Acute myocardial infarction, unspecified'),
    ('I25.1', 'Atherosclerotic heart disease [coronary artery disease]'),
    ('I25.9', 'Chronic ischaemic heart disease, unspecified'),
    ('I26.9', 'Pulmonary embolism without mention of acute cor pulmonale'),
    ('I27.0', 'Primary pulmonary hypertension'),
    ('I42.0', 'Dilated cardiomyopathy'),
    ('I46.9', 'Cardiac arrest, unspecified'),
    ('I47.1', 'Supraventricular tachycardia'),
    ('I48.9', 'Atrial fibrillation and atrial flutter, unspecified'),
    ('I49.9', 'Cardiac arrhythmia, unspecified'),
    ('I50.0', 'Congestive heart failure'),
    ('I50.9', 'Heart failure, unspecified'),
    ('I61.9', 'Intracerebral haemorrhage, unspecified'),
    ('I63.9', 'Cerebral infarction, unspecified'),
    ('I64', 'Stroke, not specified as haemorrhage or infarction'),
    ('I67.4', 'Hypertensive encephalopathy'),
    ('I70.2', 'Atherosclerosis of arteries of extremities'),
    ('I80.2', 'Phlebitis and thrombophlebitis of other deep vessels of lower extremities [deep vein thrombosis]'),
    ('I83.9', 'Varicose veins of lower extremities without ulcer or inflammation'),
    ('I95.9', 'Hypotension, unspecified'),
    ('J00', 'Acute nasopharyngitis [common cold]'),
    ('J01.9', 'Acute sinusitis, unspecified'),
    ('J02.0', 'Streptococcal pharyngitis'),
    ('J02.9', 'Acute pharyngitis, unspecified'),
    ('J03.9', 'Acute tonsillitis, unspecified'),
    ('J04.0', 'Acute laryngitis'),
    ('J05.0', 'Acute obstructive laryngitis [croup]'),
    ('J06.9', 'Acute upper respiratory infection, unspecified'),
    ('J09', 'Influenza due to identified zoonotic or pandemic influenza virus'),
    ('J10.1', 'Influenza with other respiratory manifestations, seasonal influenza virus identified'),
    ('J11.1', 'Influenza with other respiratory manifestations, virus not identified'),
    ('J12.9', 'Viral pneumonia, unspecified'),
    ('J13', 'Pneumonia due to Streptococcus pneumoniae'),
    ('J15.9', 'Bacterial pneumonia, unspecified'),
    ('J18.0', 'Bronchopneumonia, unspecified'),
    ('J18.1', 'Lobar pneumonia, unspecified'),
    ('J18.9', 'Pneumonia, unspecified'),
    ('J20.9', 'Acute bronchitis, unspecified'),
    ('J21.9', 'Acute bronchiolitis, unspecified'),
    ('J30.4', 'Allergic rhinitis, unspecified'),
    ('J31.0', 'Chronic rhinitis'),
    ('J32.9', 'Chronic sinusitis, unspecified'),
    ('J34.2', 'Deviated nasal septum'),
    ('J35.0', 'Chronic tonsillitis'),
    ('J40', 'Bronchitis, not specified as acute or chronic'),
    ('J42', 'Unspecified chronic bronchitis'),
    ('J44.0', 'Chronic obstructive pulmonary disease with acute lower respiratory infection'),
    ('J44.1', 'Chronic obstructive pulmonary disease with acute exacerbation, unspecified'),
    ('J44.9', 'Chronic obstructive pulmonary disease, unspecified [COPD]'),
    ('J45.9', 'Asthma, unspecified'),
    ('J46', 'Status asthmaticus'),
    ('J47', 'Bronchiectasis'),
    ('J69.0', 'Pneumonitis due to food and vomit [aspiration pneumonia]'),
    ('J80', 'Adult respiratory distress syndrome'),
    ('J81', 'Pulmonary oedema'),
    ('J84.1', 'Other interstitial pulmonary diseases with fibrosis'),
    ('J90', 'Pleural effusion, not elsewhere classified'),
    ('J93.9', 'Pneumothorax, unspecified'),
    ('J96.0', 'Acute respiratory failure'),
    ('K02.9', 'Dental caries, unspecified'),
    ('K04.7', 'Periapical abscess without sinus'),
    ('K05.1', 'Chronic gingivitis'),
    ('K12.0', 'Recurrent oral aphthae [mouth ulcers]'),
    ('K21.0', 'Gastro-oesophageal reflux disease with oesophagitis'),
    ('K21.9', 'Gastro-oesophageal reflux disease without oesophagitis [GERD]'),
    ('K25.9', 'Gastric ulcer, unspecified as acute or chronic, without haemorrhage or perforation'),
    ('K26.9', 'Duodenal ulcer, unspecified as acute or chronic, without haemorrhage or perforation'),
    ('K27.9', 'Peptic ulcer, site unspecified, unspecified as acute or chronic, without haemorrhage or perforation'),
    ('K29.0', 'Acute haemorrhagic gastritis'),
    ('K29.7', 'Gastritis, unspecified'),
    ('K30', 'Functional dyspepsia'),
    ('K35.8', 'Acute appendicitis, other and unspecified'),
    ('K37', 'Unspecified appendicitis'),
    ('K40.9', 'Unilateral or unspecified inguinal hernia, without obstruction or gangrene'),
    ('K42.9', 'Umbilical hernia without obstruction or gangrene'),
    ('K43.9', 'Ventral hernia without obstruction or gangrene'),
    ('K50.9', 'Crohn''s disease, unspecified'),
    ('K51.9', 'Ulcerative colitis, unspecified'),
    ('K52.9', 'Noninfective gastroenteritis and colitis, unspecified'),
    ('K56.7', 'Ileus, unspecified'),
    ('K57.3', 'Diverticular disease of large intestine without perforation or abscess'),
    ('K58.9', 'Irritable bowel syndrome without diarrhoea'),
    ('K59.0', 'Constipation'),
    ('K60.2', 'Anal fissure, unspecified'),
    ('K60.3', 'Anal fistula'),
    ('K61.0', 'Anal abscess'),
    ('K64.9', 'Haemorrhoids, unspecified [piles]'),
    ('K70.3', 'Alcoholic cirrhosis of liver'),
    ('K74.6', 'Other and unspecified cirrhosis of liver'),
    ('K75.9', 'Inflammatory liver disease, unspecified'),
    ('K76.0', 'Fatty (change of) liver, not elsewhere classified'),
    ('K80.0', 'Calculus of gallbladder with acute cholecystitis'),
    ('K80.2', 'Calculus of gallbladder without cholecystitis [gallstones]'),
    ('K81.0', 'Acute cholecystitis'),
    ('K85.9', 'Acute pancreatitis, unspecified'),
    ('K86.1', 'Other chronic pancreatitis'),
    ('K90.0', 'Coeliac disease'),
    ('K92.0', 'Haematemesis'),
    ('K92.2', 'Gastrointestinal haemorrhage, unspecified'),
    ('L01.0', 'Impetigo [any organism] [any site]'),
    ('L02.9', 'Cutaneous abscess, furuncle and carbuncle, unspecified'),
    ('L03.9', 'Cellulitis, unspecified'),
    ('L08.9', 'Local infection of skin and subcutaneous tissue, unspecified'),
    ('L20.9', 'Atopic dermatitis, unspecified [eczema]'),
    ('L23.9', 'Allergic contact dermatitis, unspecified cause'),
    ('L29.9', 'Pruritus, unspecified'),
    ('L30.9', 'Dermatitis, unspecified'),
    ('L40.0', 'Psoriasis vulgaris'),
    ('L50.9', 'Urticaria, unspecified'),
    ('L60.0', 'Ingrowing nail'),
    ('L65.9', 'Nonscarring hair loss, unspecified'),
    ('L70.0', 'Acne vulgaris'),
    ('L72.1', 'Trichodermal cyst'),
    ('L80', 'Vitiligo'),
    ('L89.9', 'Decubitus ulcer and pressure area, unspecified'),
    ('L97', 'Ulcer of lower limb, not elsewhere classified'),
    ('M06.9', 'Rheumatoid arthritis, unspecified'),
    ('M10.9', 'Gout, unspecified'),
    ('M13.9', 'Arthritis, unspecified'),
    ('M15.9', 'Polyarthrosis, unspecified'),
    ('M16.9', 'Coxarthrosis, unspecified [osteoarthritis of hip]'),
    ('M17.9', 'Gonarthrosis, unspecified [osteoarthritis of knee]'),
    ('M19.9', 'Arthrosis, unspecified'),
    ('M25.5', 'Pain in joint'),
    ('M32.9', 'Systemic lupus erythematosus, unspecified'),
    ('M45', 'Ankylosing spondylitis'),
    ('M47.9', 'Spondylosis, unspecified'),
    ('M48.0', 'Spinal stenosis'),
    ('M50.1', 'Cervical disc disorder with radiculopathy'),
    ('M51.1', 'Lumbar and other intervertebral disc disorders with radiculopathy'),
    ('M54.2', 'Cervicalgia [neck pain]'),
    ('M54.3', 'Sciatica'),
    ('M54.4', 'Lumbago with sciatica'),
    ('M54.5', 'Low back pain'),
    ('M54.9', 'Dorsalgia, unspecified'),
    ('M62.6', 'Muscle strain'),
    ('M65.4', 'Radial styloid tenosynovitis [de Quervain]'),
    ('M72.2', 'Plantar fascial fibromatosis [plantar fasciitis]'),
    ('M75.0', 'Adhesive capsulitis of shoulder [frozen shoulder]'),
    ('M75.1', 'Rotator cuff syndrome'),
    ('M77.1', 'Lateral epicondylitis [tennis elbow]'),
    ('M79.1', 'Myalgia'),
    ('M79.7', 'Fibromyalgia'),
    ('M81.9', 'Osteoporosis, unspecified'),
    ('M86.9', 'Osteomyelitis, unspecified'),
    ('N04.9', 'Nephrotic syndrome, unspecified'),
    ('N10', 'Acute tubulo-interstitial nephritis [acute pyelonephritis]'),
    ('N17.9', 'Acute renal failure, unspecified [acute kidney injury]'),
    ('N18.5', 'Chronic kidney disease, stage 5'),
    ('N18.9', 'Chronic kidney disease, unspecified'),
    ('N19', 'Unspecified kidney failure'),
    ('N20.0', 'Calculus of kidney'),
    ('N20.1', 'Calculus of ureter'),
    ('N23', 'Unspecified renal colic'),
    ('N30.0', 'Acute cystitis'),
    ('N39.0', 'Urinary tract infection, site not specified'),
    ('N40', 'Hyperplasia of prostate'),
    ('N41.0', 'Acute prostatitis'),
    ('N43.3', 'Hydrocele, unspecified'),
    ('N45.9', 'Orchitis, epididymitis and epididymo-orchitis without abscess'),
    ('N46', 'Male infertility'),
    ('N47', 'Redundant prepuce, phimosis and paraphimosis'),
    ('N61', 'Inflammatory disorders of breast'),
    ('N63', 'Unspecified lump in breast'),
    ('N70.9', 'Salpingitis and oophoritis, unspecified'),
    ('N73.9', 'Female pelvic inflammatory disease, unspecified'),
    ('N76.0', 'Acute vaginitis'),
    ('N80.9', 'Endometriosis, unspecified'),
    ('N83.2', 'Other and unspecified ovarian cysts'),
    ('N91.2', 'Amenorrhoea, unspecified'),
    ('N92.0', 'Excessive and frequent menstruation with regular cycle'),
    ('N92.6', 'Irregular menstruation, unspecified'),
    ('N94.6', 'Dysmenorrhoea, unspecified'),
    ('N95.1', 'Menopausal and female climacteric states'),
    ('N97.9', 'Female infertility, unspecified'),
    ('O00.9', 'Ectopic pregnancy, unspecified'),
    ('O02.1', 'Missed abortion'),
    ('O03.9', 'Spontaneous abortion, complete or unspecified, without complication'),
    ('O06.9', 'Unspecified abortion, complete or unspecified, without complication'),
    ('O10.0', 'Pre-existing essential hypertension complicating pregnancy, childbirth and the puerperium'),
    ('O13', 'Gestational [pregnancy-induced] hypertension'),
    ('O14.9', 'Pre-eclampsia, unspecified'),
    ('O15.9', 'Eclampsia, unspecified as to time period'),
    ('O20.0', 'Threatened abortion'),
    ('O21.0', 'Mild hyperemesis gravidarum'),
    ('O23.4', 'Unspecified infection of urinary tract in pregnancy'),
    ('O24.4', 'Diabetes mellitus arising in pregnancy [gestational diabetes]'),
    ('O26.9', 'Pregnancy-related condition, unspecified'),
    ('O36.4', 'Maternal care for intrauterine death'),
    ('O42.9', 'Premature rupture of membranes, unspecified'),
    ('O44.1', 'Placenta praevia with haemorrhage'),
    ('O45.9', 'Premature separation of placenta, unspecified'),
    ('O47.9', 'False labour, unspecified'),
    ('O48', 'Prolonged pregnancy'),
    ('O60.1', 'Preterm spontaneous labour with preterm delivery'),
    ('O62.2', 'Other uterine inertia'),
    ('O68.9', 'Labour and delivery complicated by fetal stress, unspecified'),
    ('O72.1', 'Other immediate postpartum haemorrhage'),
    ('O80.0', 'Spontaneous vertex delivery'),
    ('O82.0', 'Delivery by elective caesarean section'),
    ('O82.1', 'Delivery by emergency caesarean section'),
    ('O86.0', 'Infection of obstetric surgical wound'),
    ('O91.2', 'Nonpurulent mastitis associated with childbirth'),
    ('O99.0', 'Anaemia complicating pregnancy, childbirth and the puerperium'),
    ('P07.1', 'Other low birth weight'),
    ('P07.3', 'Other preterm infants'),
    ('P21.9', 'Birth asphyxia, unspecified'),
    ('P22.9', 'Respiratory distress of newborn, unspecified'),
    ('P36.9', 'Bacterial sepsis of newborn, unspecified'),
    ('P59.9', 'Neonatal jaundice, unspecified'),
    ('P92.9', 'Feeding problem of newborn, unspecified'),
    ('Q21.0', 'Ventricular septal defect'),
    ('Q21.1', 'Atrial septal defect'),
    ('Q35.9', 'Cleft palate, unspecified'),
    ('Q53.9', 'Undescended testicle, unspecified'),
    ('Q66.0', 'Talipes equinovarus [club foot]'),
    ('Q90.9', 'Down''s syndrome, unspecified'),
    ('R00.0', 'Tachycardia, unspecified'),
    ('R04.0', 'Epistaxis [nosebleed]'),
    ('R05', 'Cough'),
    ('R06.0', 'Dyspnoea [breathlessness]'),
    ('R07.3', 'Other chest pain'),
    ('R07.4', 'Chest pain, unspecified'),
    ('R09.2', 'Respiratory arrest'),
    ('R10.1', 'Pain localized to upper abdomen'),
    ('R10.4', 'Other and unspecified abdominal pain'),
    ('R11', 'Nausea and vomiting'),
    ('R17', 'Unspecified jaundice'),
    ('R19.7', 'Diarrhoea, unspecified'),
    ('R21', 'Rash and other nonspecific skin eruption'),
    ('R25.2', 'Cramp and spasm'),
    ('R31', 'Unspecified haematuria'),
    ('R32', 'Unspecified urinary incontinence'),
    ('R33', 'Retention of urine'),
    ('R40.2', 'Coma, unspecified'),
    ('R42', 'Dizziness and giddiness'),
    ('R50.9', 'Fever, unspecified'),
    ('R51', 'Headache'),
    ('R52.9', 'Pain, unspecified'),
    ('R53', 'Malaise and fatigue'),
    ('R55', 'Syncope and collapse'),
    ('R56.0', 'Febrile convulsions'),
    ('R56.8', 'Other and unspecified convulsions'),
    ('R57.0', 'Cardiogenic shock'),
    ('R57.1', 'Hypovolaemic shock'),
    ('R59.0', 'Localized enlarged lymph nodes'),
    ('R60.0', 'Localized oedema'),
    ('R63.4', 'Abnormal weight loss'),
    ('R73.9', 'Hyperglycaemia, unspecified'),
    ('R74.0', 'Elevation of levels of transaminase and lactic acid dehydrogenase [LDH]'),
    ('S00.9', 'Superficial injury of head, part unspecified'),
    ('S01.9', 'Open wound of head, part unspecified'),
    ('S06.0', 'Concussion'),
    ('S06.9', 'Intracranial injury, unspecified'),
    ('S09.9', 'Unspecified injury of head'),
    ('S22.3', 'Fracture of rib'),
    ('S30.0', 'Contusion of lower back and pelvis'),
    ('S32.0', 'Fracture of lumbar vertebra'),
    ('S42.0', 'Fracture of clavicle'),
    ('S42.2', 'Fracture of upper end of humerus'),
    ('S52.5', 'Fracture of lower end of radius'),
    ('S52.9', 'Fracture of forearm, part unspecified'),
    ('S61.9', 'Open wound of wrist and hand, part unspecified'),
    ('S62.6', 'Fracture of other finger'),
    ('S63.5', 'Sprain and strain of wrist'),
    ('S72.0', 'Fracture of neck of femur'),
    ('S72.3', 'Fracture of shaft of femur'),
    ('S80.0', 'Contusion of knee'),
    ('S82.2', 'Fracture of shaft of tibia'),
    ('S82.6', 'Fracture of lateral malleolus'),
    ('S83.2', 'Tear of meniscus, current'),
    ('S93.4', 'Sprain and strain of ankle'),
    ('T07', 'Unspecified multiple injuries'),
    ('T14.0', 'Superficial injury of unspecified body region'),
    ('T14.1', 'Open wound of unspecified body region'),
    ('T14.9', 'Injury, unspecified'),
    ('T15.9', 'Foreign body on external eye, part unspecified'),
    ('T30.0', 'Burn of unspecified body region, unspecified degree'),
    ('T30.2', 'Burn of unspecified body region, second degree'),
    ('T39.1', 'Poisoning: 4-Aminophenol derivatives [paracetamol]'),
    ('T42.4', 'Poisoning: Benzodiazepines'),
    ('T50.9', 'Poisoning: Other and unspecified drugs, medicaments and biological substances'),
    ('T51.0', 'Toxic effect: Ethanol'),
    ('T60.0', 'Toxic effect: Organophosphate and carbamate insecticides'),
    ('T63.0', 'Toxic effect: Snake venom [snake bite]'),
    ('T63.2', 'Toxic effect: Venom of scorpion [scorpion sting]'),
    ('T67.0', 'Heatstroke and sunstroke'),
    ('T75.1', 'Drowning and nonfatal submersion'),
    ('T78.2', 'Anaphylactic shock, unspecified'),
    ('T78.3', 'Angioneurotic oedema'),
    ('T78.4', 'Allergy, unspecified'),
    ('T81.4', 'Infection following a procedure, not elsewhere classified'),
    ('T88.7', 'Unspecified adverse effect of drug or medicament'),
    ('U07.1', 'COVID-19, virus identified'),
    ('U07.2', 'COVID-19, virus not identified'),
    ('V89.2', 'Person injured in unspecified motor-vehicle accident, traffic [road traffic accident]'),
    ('W19', 'Unspecified fall'),
    ('W54', 'Bitten or struck by dog [dog bite]'),
    ('X20', 'Contact with venomous snakes and lizards'),
    ('X84', 'Intentional self-harm by unspecified means'),
    ('Z00.0', 'General medical examination'),
    ('Z00.1', 'Routine child health examination'),
    ('Z01.4', 'Gynaecological examination (general)(routine)'),
    ('Z02.7', 'Issue of medical certificate'),
    ('Z23.5', 'Need for immunization against tetanus alone'),
    ('Z24.2', 'Need for immunization against rabies'),
    ('Z30.0', 'General counselling and advice on contraception'),
    ('Z32.1', 'Pregnancy confirmed'),
    ('Z34.9', 'Supervision of normal pregnancy, unspecified [antenatal check-up]'),
    ('Z35.9', 'Supervision of high-risk pregnancy, unspecified'),
    ('Z38.0', 'Singleton, born in hospital'),
    ('Z39.2', 'Routine postpartum follow-up'),
    ('Z48.0', 'Attention to surgical dressings and sutures'),
    ('Z49.1', 'Extracorporeal dialysis'),
    ('Z51.1', 'Chemotherapy session for neoplasm'),
    ('Z71.9', 'Counselling, unspecified'),
    ('Z72.0', 'Tobacco use'),
    ('Z76.0', 'Issue of repeat prescription'),
    ('Z95.5', 'Presence of coronary angioplasty implant and graft'),
    ('Z96.6', 'Presence of orthopaedic joint implants')
  ) AS v (code, description)
  CROSS JOIN LATERAL icd10_chapter(v.code) ch
ON CONFLICT (code) DO UPDATE
   SET description = EXCLUDED.description, chapter_number = EXCLUDED.chapter_number,
       chapter_title = EXCLUDED.chapter_title, is_common = true;

-- ── 4. Hygiene, lockout, RLS ───────────────────────────────────────────────
ALTER FUNCTION opd_guard_billable_service() SET search_path = public, pg_temp;
ALTER FUNCTION opd_service_item_type(text) SET search_path = public, pg_temp;
ALTER FUNCTION opd_service_json(billable_services) SET search_path = public, pg_temp;
ALTER FUNCTION opd_catalog_item(uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION opd_service_catalog(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION opd_import_service_catalog(uuid, jsonb, boolean) SET search_path = public, pg_temp;
ALTER FUNCTION opd_place_orders(uuid, uuid, jsonb, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION opd_invoice_item_tax() SET search_path = public, pg_temp;
ALTER FUNCTION icd10_chapter(text) SET search_path = public, pg_temp;
-- pg_trgm lives in the "extensions" schema on Supabase
ALTER FUNCTION icd10_search(text, integer) SET search_path = public, extensions, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['opd_service_catalog(uuid)', 'opd_import_service_catalog(uuid,jsonb,boolean)',
                             'icd10_search(text,integer)', 'opd_catalog_item(uuid,text)']
    LOOP
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
    END LOOP;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['billable_services', 'icd10_codes'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_' || t, t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
    DROP POLICY IF EXISTS tenant_isolation_billable_services ON billable_services;
    CREATE POLICY tenant_isolation_billable_services ON billable_services FOR ALL
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
END $$;

-- ── 5. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.billable_services') IS NULL OR to_regclass('public.icd10_codes') IS NULL THEN
        RAISE EXCEPTION '120 verify: tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN ('billable_services_clinic_code_key',
          'billable_services_clinic_id_id_key', 'billable_services_branch_fk')) <> 3 THEN
        RAISE EXCEPTION '120 verify: billable_services constraints missing';
    END IF;
    IF to_regprocedure('opd_catalog_item(uuid,text)') IS NULL OR to_regprocedure('opd_service_catalog(uuid)') IS NULL
       OR to_regprocedure('icd10_search(text,integer)') IS NULL
       OR to_regprocedure('opd_import_service_catalog(uuid,jsonb,boolean)') IS NULL THEN
        RAISE EXCEPTION '120 verify: functions missing';
    END IF;
    IF (SELECT count(*) FROM icd10_codes WHERE is_common) < 300 THEN
        RAISE EXCEPTION '120 verify: ICD-10 seed incomplete';
    END IF;
    IF EXISTS (SELECT 1 FROM icd10_codes WHERE chapter_number IS NULL) THEN
        RAISE EXCEPTION '120 verify: ICD-10 code without a chapter';
    END IF;
END $$;
