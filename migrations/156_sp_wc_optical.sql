-- ============================================================================
-- Migration 156: Specialty OS — Women & Child completion + optical stock + IOL → IPD (lane SP; WOMEN-CHILD.md, EYE.md)
--   obs_deliveries / obs_newborns  SP-WC-02: delivery record → each baby becomes a family member under the mother's
--                                  account through the existing registration path (family_members row; never new
--                                  identity storage); birth-registration fields (SP-WC-06: registrar number and date;
--                                  due within 21 days under the RBD Act — listed, never filed by Kriya).
--   child_growth_measurements      SP-WC-04: weight / length or height / head per visit; z-scores computed in code from
--                                  WHO LMS tables (sp_growth.py) and stored with the table version.
--   wc_gov_ids                     SP-WC-07: RCH id, MCP card, U-WIN beneficiary id, ABHA (for exports; no portal bot).
--   nicu_flowsheet_entries         SP-WC-08: NICU charting on an IPD admission (weight, vitals, feeds, phototherapy,
--                                  bilirubin, respiratory support).
--   sp_optical_items / sp_optical_stock_moves  SP-EYE-02: frames, lenses, contact lenses with an append-only stock
--                                  ledger; on-hand never negative (sp_optical_move); a job issues its frame and lens.
--   sp_estimates.converted_ipd_package_id  SP-EYE-05: an accepted IOL estimate → an IPD package (migration 116).
--   sp_purge_clinic / sp_purge_patient v2: the new tables (children first).
-- Existing objects touched: none of migrations < 151 (SP tables / functions only). Old-table references use
-- kriya_guard_refs (144). Rollback: rollback/156_rollback_sp_wc_optical.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS obs_deliveries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    pregnancy_id      UUID NOT NULL,
    admission_id      UUID NULL,
    delivered_at      TIMESTAMPTZ NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('vaginal', 'assisted_vaginal', 'caesarean')),
    gestation_weeks   SMALLINT NULL CHECK (gestation_weeks BETWEEN 20 AND 44),
    place             TEXT NOT NULL DEFAULT 'this_facility' CHECK (place IN ('this_facility', 'other_facility', 'home')),
    notes             TEXT NULL CHECK (char_length(notes) <= 500),
    entered_by_name   TEXT NOT NULL CHECK (char_length(btrim(entered_by_name)) BETWEEN 2 AND 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT obs_deliveries_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT obs_deliveries_pregnancy_key UNIQUE (clinic_id, pregnancy_id),
    CONSTRAINT obs_deliveries_pregnancy_fk FOREIGN KEY (clinic_id, pregnancy_id) REFERENCES obs_pregnancies (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS obs_newborns (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    delivery_id           UUID NOT NULL,
    birth_order           SMALLINT NOT NULL DEFAULT 1 CHECK (birth_order BETWEEN 1 AND 6),
    outcome               TEXT NOT NULL CHECK (outcome IN ('live_birth', 'stillbirth')),
    sex                   TEXT NULL CHECK (sex IN ('male', 'female', 'indeterminate')),   -- recorded after birth only
    birth_weight_g        SMALLINT NULL CHECK (birth_weight_g BETWEEN 300 AND 7000),
    length_cm             NUMERIC(4, 1) NULL CHECK (length_cm BETWEEN 25 AND 65),
    head_cm               NUMERIC(4, 1) NULL CHECK (head_cm BETWEEN 18 AND 45),
    apgar_1               SMALLINT NULL CHECK (apgar_1 BETWEEN 0 AND 10),
    apgar_5               SMALLINT NULL CHECK (apgar_5 BETWEEN 0 AND 10),
    nicu_admitted         BOOLEAN NOT NULL DEFAULT false,
    family_member_id      UUID NULL,                         -- the child under the mother's account (live births)
    birth_reg_no          TEXT NULL CHECK (char_length(btrim(birth_reg_no)) BETWEEN 3 AND 40),
    birth_registered_on   DATE NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT obs_newborns_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT obs_newborns_order_key UNIQUE (clinic_id, delivery_id, birth_order),
    CONSTRAINT obs_newborns_delivery_fk FOREIGN KEY (clinic_id, delivery_id) REFERENCES obs_deliveries (clinic_id, id),
    CONSTRAINT obs_newborns_registered CHECK ((birth_reg_no IS NULL) = (birth_registered_on IS NULL)),
    CONSTRAINT obs_newborns_child CHECK (outcome = 'live_birth' OR family_member_id IS NULL)
);

CREATE TABLE IF NOT EXISTS child_growth_measurements (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id       UUID NOT NULL,
    family_member_id UUID NULL,
    measured_on      DATE NOT NULL,
    weight_kg        NUMERIC(5, 2) NULL CHECK (weight_kg BETWEEN 0.3 AND 200),
    length_cm        NUMERIC(5, 1) NULL CHECK (length_cm BETWEEN 25 AND 220),
    position         TEXT NULL CHECK (position IN ('recumbent', 'standing')),
    head_cm          NUMERIC(4, 1) NULL CHECK (head_cm BETWEEN 18 AND 65),
    z                JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(z) = 'object'),
    table_version    TEXT NOT NULL DEFAULT 'who-2006-2007',
    entered_by_name  TEXT NOT NULL CHECK (char_length(btrim(entered_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT child_growth_measurements_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT child_growth_measurements_has_value CHECK (weight_kg IS NOT NULL OR length_cm IS NOT NULL OR head_cm IS NOT NULL),
    CONSTRAINT child_growth_measurements_position CHECK ((length_cm IS NULL) = (position IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_child_growth_child ON child_growth_measurements (clinic_id, patient_id, family_member_id, measured_on);

CREATE TABLE IF NOT EXISTS wc_gov_ids (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id       UUID NOT NULL,
    family_member_id UUID NULL,
    rch_id           TEXT NULL CHECK (rch_id ~ '^[0-9]{12}$'),
    mcp_card_no      TEXT NULL CHECK (char_length(btrim(mcp_card_no)) BETWEEN 3 AND 30),
    uwin_id          TEXT NULL CHECK (uwin_id ~ '^[A-Za-z0-9-]{6,30}$'),
    abha_number      TEXT NULL CHECK (abha_number ~ '^[0-9]{2}-[0-9]{4}-[0-9]{4}-[0-9]{4}$'),
    updated_by_name  TEXT NOT NULL CHECK (char_length(btrim(updated_by_name)) BETWEEN 2 AND 100),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT wc_gov_ids_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wc_gov_ids_person ON wc_gov_ids
    (clinic_id, patient_id, COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid));

CREATE TABLE IF NOT EXISTS nicu_flowsheet_entries (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id     UUID NOT NULL,
    recorded_at      TIMESTAMPTZ NOT NULL,
    weight_g         SMALLINT NULL CHECK (weight_g BETWEEN 300 AND 7000),
    temp_c           NUMERIC(3, 1) NULL CHECK (temp_c BETWEEN 30 AND 42),
    heart_rate       SMALLINT NULL CHECK (heart_rate BETWEEN 40 AND 260),
    resp_rate        SMALLINT NULL CHECK (resp_rate BETWEEN 5 AND 120),
    spo2             SMALLINT NULL CHECK (spo2 BETWEEN 40 AND 100),
    feed_type        TEXT NULL CHECK (feed_type IN ('breast', 'expressed_breast_milk', 'formula', 'iv_fluids', 'nil')),
    feed_ml          SMALLINT NULL CHECK (feed_ml BETWEEN 0 AND 500),
    phototherapy     BOOLEAN NOT NULL DEFAULT false,
    bilirubin_mg_dl  NUMERIC(4, 1) NULL CHECK (bilirubin_mg_dl BETWEEN 0 AND 40),
    resp_support     TEXT NOT NULL DEFAULT 'none' CHECK (resp_support IN ('none', 'oxygen', 'cpap', 'ventilator')),
    notes            TEXT NULL CHECK (char_length(notes) <= 300),
    entered_by_name  TEXT NOT NULL CHECK (char_length(btrim(entered_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT nicu_flowsheet_entries_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_nicu_flowsheet_admission ON nicu_flowsheet_entries (clinic_id, admission_id, recorded_at);

CREATE TABLE IF NOT EXISTS sp_optical_items (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sku                TEXT NOT NULL CHECK (sku ~ '^[A-Z0-9_-]{2,30}$'),
    kind               TEXT NOT NULL CHECK (kind IN ('frame', 'lens', 'contact_lens', 'solution', 'accessory')),
    name               TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    brand              TEXT NULL CHECK (char_length(brand) <= 80),
    power_desc         TEXT NULL CHECK (char_length(power_desc) <= 80),
    barcode            TEXT NULL CHECK (barcode ~ '^[0-9A-Za-z-]{4,40}$'),
    hsn_code           TEXT NULL CHECK (hsn_code ~ '^[0-9]{4,8}$'),
    mrp_paise          INTEGER NULL CHECK (mrp_paise >= 0),
    qty_on_hand        INTEGER NOT NULL DEFAULT 0 CHECK (qty_on_hand >= 0),
    reorder_level      INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active          BOOLEAN NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_optical_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_optical_items_sku_key UNIQUE (clinic_id, sku)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sp_optical_items_barcode ON sp_optical_items (clinic_id, barcode) WHERE barcode IS NOT NULL;

CREATE TABLE IF NOT EXISTS sp_optical_stock_moves (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_id         UUID NOT NULL,
    delta           INTEGER NOT NULL CHECK (delta <> 0 AND delta BETWEEN -1000 AND 1000),
    reason          TEXT NOT NULL CHECK (reason IN ('receive', 'issue_job', 'return_job', 'adjust', 'write_off')),
    job_id          UUID NULL,
    balance_after   INTEGER NOT NULL CHECK (balance_after >= 0),
    note            TEXT NULL CHECK (char_length(note) <= 200),
    by_name         TEXT NOT NULL CHECK (char_length(btrim(by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_optical_stock_moves_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_optical_stock_moves_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES sp_optical_items (clinic_id, id),
    CONSTRAINT sp_optical_stock_moves_job_fk FOREIGN KEY (clinic_id, job_id) REFERENCES sp_optical_jobs (clinic_id, id),
    CONSTRAINT sp_optical_stock_moves_job_reason CHECK ((reason IN ('issue_job', 'return_job')) = (job_id IS NOT NULL)),
    CONSTRAINT sp_optical_stock_moves_reason_sign CHECK (
        (reason IN ('receive', 'return_job') AND delta > 0) OR (reason IN ('issue_job', 'write_off') AND delta < 0)
        OR reason = 'adjust'),
    CONSTRAINT sp_optical_stock_moves_note CHECK (reason NOT IN ('adjust', 'write_off')
                                                  OR (note IS NOT NULL AND char_length(btrim(note)) >= 3))
);
CREATE INDEX IF NOT EXISTS idx_sp_optical_moves_item ON sp_optical_stock_moves (clinic_id, item_id, created_at DESC);

-- one place where stock changes: row lock, balance never negative, ledger row with the balance after
CREATE OR REPLACE FUNCTION sp_optical_move(p_clinic_id UUID, p_item_id UUID, p_delta INTEGER, p_reason TEXT,
                                           p_job_id UUID, p_note TEXT, p_by TEXT) RETURNS JSONB
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_qty INTEGER; v_id UUID;
BEGIN
    SELECT qty_on_hand INTO v_qty FROM sp_optical_items WHERE clinic_id = p_clinic_id AND id = p_item_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'sp_optical_item_missing' USING ERRCODE = 'P0002'; END IF;
    IF v_qty + p_delta < 0 THEN RAISE EXCEPTION 'sp_optical_out_of_stock' USING ERRCODE = '23514'; END IF;
    UPDATE sp_optical_items SET qty_on_hand = v_qty + p_delta, updated_at = now()
     WHERE clinic_id = p_clinic_id AND id = p_item_id;
    INSERT INTO sp_optical_stock_moves (clinic_id, item_id, delta, reason, job_id, balance_after, note, by_name)
    VALUES (p_clinic_id, p_item_id, p_delta, p_reason, p_job_id, v_qty + p_delta, p_note, p_by) RETURNING id INTO v_id;
    RETURN jsonb_build_object('move_id', v_id, 'qty_on_hand', v_qty + p_delta);
END $$;

-- the ledger is append-only (only the SP purge removes it)
CREATE OR REPLACE FUNCTION sp_optical_moves_append_only() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF current_setting('kriya.sp_purge', true) = OLD.clinic_id::text THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'sp_optical_moves_append_only' USING ERRCODE = '23514';
END $$;

ALTER TABLE sp_optical_jobs ADD COLUMN IF NOT EXISTS frame_item_id UUID NULL;
ALTER TABLE sp_optical_jobs ADD COLUMN IF NOT EXISTS lens_item_id UUID NULL;
ALTER TABLE sp_optical_jobs ADD COLUMN IF NOT EXISTS stock_issued_at TIMESTAMPTZ NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sp_optical_jobs_frame_item_fk') THEN
        ALTER TABLE sp_optical_jobs ADD CONSTRAINT sp_optical_jobs_frame_item_fk FOREIGN KEY (clinic_id, frame_item_id)
            REFERENCES sp_optical_items (clinic_id, id);
        ALTER TABLE sp_optical_jobs ADD CONSTRAINT sp_optical_jobs_lens_item_fk FOREIGN KEY (clinic_id, lens_item_id)
            REFERENCES sp_optical_items (clinic_id, id);
    END IF;
END $$;

-- SP-EYE-05: an estimate converts to a package sale (S3) OR an IPD package (116)
ALTER TABLE sp_estimates ADD COLUMN IF NOT EXISTS converted_ipd_package_id UUID NULL;
ALTER TABLE sp_estimates DROP CONSTRAINT IF EXISTS sp_estimates_converted;
ALTER TABLE sp_estimates ADD CONSTRAINT sp_estimates_converted CHECK (
    (status = 'converted') = ((converted_sale_id IS NOT NULL) OR (converted_ipd_package_id IS NOT NULL))
    AND NOT (converted_sale_id IS NOT NULL AND converted_ipd_package_id IS NOT NULL));

DROP TRIGGER IF EXISTS trg_obs_deliveries_refs ON obs_deliveries;
CREATE TRIGGER trg_obs_deliveries_refs BEFORE INSERT OR UPDATE ON obs_deliveries FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('admission_id', 'ipd_admissions');
DROP TRIGGER IF EXISTS trg_obs_newborns_refs ON obs_newborns;
CREATE TRIGGER trg_obs_newborns_refs BEFORE INSERT OR UPDATE ON obs_newborns FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('family_member_id', 'family_members');
DROP TRIGGER IF EXISTS trg_child_growth_refs ON child_growth_measurements;
CREATE TRIGGER trg_child_growth_refs BEFORE INSERT OR UPDATE ON child_growth_measurements FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');
DROP TRIGGER IF EXISTS trg_wc_gov_ids_refs ON wc_gov_ids;
CREATE TRIGGER trg_wc_gov_ids_refs BEFORE INSERT OR UPDATE ON wc_gov_ids FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');
DROP TRIGGER IF EXISTS trg_nicu_flowsheet_refs ON nicu_flowsheet_entries;
CREATE TRIGGER trg_nicu_flowsheet_refs BEFORE INSERT OR UPDATE ON nicu_flowsheet_entries FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('admission_id', 'ipd_admissions');
DROP TRIGGER IF EXISTS trg_sp_estimates_ipd_refs ON sp_estimates;
CREATE TRIGGER trg_sp_estimates_ipd_refs BEFORE INSERT OR UPDATE ON sp_estimates FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('converted_ipd_package_id', 'ipd_packages');
DROP TRIGGER IF EXISTS trg_sp_optical_moves_append_only ON sp_optical_stock_moves;
CREATE TRIGGER trg_sp_optical_moves_append_only BEFORE UPDATE OR DELETE ON sp_optical_stock_moves FOR EACH ROW
    EXECUTE FUNCTION sp_optical_moves_append_only();

-- ── purge chain v2 (children first) ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION sp_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t TEXT; n INTEGER; v JSONB := '{}'::jsonb;
BEGIN
    IF p_clinic_id IS NULL THEN RAISE EXCEPTION 'sp_bad_args' USING ERRCODE = 'P0001'; END IF;
    PERFORM set_config('kriya.sp_purge', p_clinic_id::TEXT, true);
    FOREACH t IN ARRAY ARRAY['nicu_flowsheet_entries', 'wc_gov_ids', 'child_growth_measurements', 'obs_newborns',
                             'obs_deliveries', 'imm_doses', 'obs_anc_visits', 'obs_pregnancies',
                             'ivf_witness_events', 'ivf_embryos', 'ivf_cycle_days', 'ivf_cycles', 'ivf_couples',
                             'sp_compliance_items', 'sp_dental_lab_orders', 'sp_optical_stock_moves', 'sp_optical_jobs',
                             'sp_optical_items', 'sp_camp_registrations', 'sp_camps', 'sp_imaging_links', 'sp_estimates',
                             'sp_instalments', 'sp_media', 'sp_media_consents', 'sp_package_consumptions',
                             'sp_package_sales', 'sp_packages', 'opd_encounter_forms']
    LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('DELETE FROM %I WHERE clinic_id = $1', t) USING p_clinic_id;
            GET DIAGNOSTICS n = ROW_COUNT;
            v := v || jsonb_build_object(t, n);
        END IF;
    END LOOP;
    PERFORM set_config('kriya.sp_purge', '', true);
    RETURN v;
END $$;

CREATE OR REPLACE FUNCTION sp_purge_patient(p_clinic_id UUID, p_patient_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_paths JSONB; n_media INTEGER; n_cons INTEGER; n_est INTEGER; n_camp INTEGER; n_ids INTEGER;
BEGIN
    IF p_clinic_id IS NULL OR p_patient_id IS NULL THEN RAISE EXCEPTION 'sp_bad_args' USING ERRCODE = 'P0001'; END IF;
    SELECT COALESCE(jsonb_agg(storage_path), '[]'::jsonb) INTO v_paths FROM sp_media
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id;
    DELETE FROM sp_media WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id;
    GET DIAGNOSTICS n_media = ROW_COUNT;
    DELETE FROM sp_media_consents WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id;
    GET DIAGNOSTICS n_cons = ROW_COUNT;
    DELETE FROM sp_estimates WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id AND status <> 'converted';
    GET DIAGNOSTICS n_est = ROW_COUNT;
    UPDATE sp_camp_registrations SET name = 'Erased', phone = NULL, screening = '{}'::jsonb
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id;
    GET DIAGNOSTICS n_camp = ROW_COUNT;
    DELETE FROM wc_gov_ids WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id;   -- government identifiers
    GET DIAGNOSTICS n_ids = ROW_COUNT;
    RETURN jsonb_build_object('media', n_media, 'media_consents', n_cons, 'estimates', n_est, 'camp_registrations', n_camp,
                              'gov_ids', n_ids, 'paths', v_paths);
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['obs_deliveries', 'obs_newborns', 'child_growth_measurements', 'wc_gov_ids',
                             'nicu_flowsheet_entries', 'sp_optical_items', 'sp_optical_stock_moves']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;

-- ── 156 verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['obs_deliveries', 'obs_newborns', 'child_growth_measurements', 'wc_gov_ids',
                             'nicu_flowsheet_entries', 'sp_optical_items', 'sp_optical_stock_moves']
    LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '156 verify: % missing', t; END IF;
    END LOOP;
    IF to_regprocedure('sp_optical_move(uuid,uuid,integer,text,uuid,text,text)') IS NULL THEN
        RAISE EXCEPTION '156 verify: sp_optical_move missing';
    END IF;
END $$;
