-- ============================================================================
-- Migration 155: Specialty OS — Women & Child depth + the SP purge chain (lane SP; docs/specialty-os/WOMEN-CHILD.md)
--   obs_pregnancies  pregnancy episode (LMP, doctor-confirmed EDD and its source, G/P/A/L, blood group, risk factors);
--                    one active pregnancy per patient; no field can carry fetal sex (PCPNDT).
--   obs_anc_visits   each antenatal contact (BP, weight, fundal height, FHR, oedema, urine protein, Hb).
--   imm_doses        a vaccine dose given (schedule code-defined in app/services/sp_wc.py: IAP 2023 or UIP), with
--                    brand, batch, expiry, site and who gave it; one row per (child, vaccine, dose).
--   sp_purge_clinic  owner deletion / module purge: every SP row of one clinic, children first (bypasses the frozen-form
--                    and append-only witness guards for that clinic only).
--   sp_purge_patient DPDP erasure: the person's clinical photos (+ their consents), estimates and camp registrations.
--                    Forms, IVF and ANC records are part of the medical record and stay (anonymised with the patient).
-- Existing objects touched: none. Old-table references use kriya_guard_refs (144). Rollback: rollback/155_*.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS obs_pregnancies (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id       UUID NOT NULL,
    lmp              DATE NULL,
    edd              DATE NOT NULL,
    edd_source       TEXT NOT NULL CHECK (edd_source IN ('lmp', 'dating_scan', 'ivf_transfer')),
    gravida          SMALLINT NOT NULL CHECK (gravida BETWEEN 1 AND 20),
    para             SMALLINT NOT NULL DEFAULT 0 CHECK (para BETWEEN 0 AND 20),
    abortions        SMALLINT NOT NULL DEFAULT 0 CHECK (abortions BETWEEN 0 AND 20),
    living           SMALLINT NOT NULL DEFAULT 0 CHECK (living BETWEEN 0 AND 20),
    blood_group      TEXT NOT NULL DEFAULT 'unknown' CHECK (blood_group IN ('A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-',
                                                                         'unknown')),
    risk_factors     JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(risk_factors) = 'array'
                                                             AND jsonb_array_length(risk_factors) <= 20),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'delivered', 'ended')),
    ended_reason     TEXT NULL CHECK (char_length(btrim(ended_reason)) BETWEEN 3 AND 300),
    created_by_name  TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT obs_pregnancies_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT obs_pregnancies_gpal CHECK (para + abortions < gravida),
    CONSTRAINT obs_pregnancies_edd CHECK (lmp IS NULL OR edd BETWEEN lmp + 180 AND lmp + 310),
    CONSTRAINT obs_pregnancies_lmp_source CHECK (edd_source <> 'lmp' OR lmp IS NOT NULL),
    CONSTRAINT obs_pregnancies_ended CHECK ((status = 'ended') = (ended_reason IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_obs_pregnancies_active ON obs_pregnancies (clinic_id, patient_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS obs_anc_visits (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    pregnancy_id     UUID NOT NULL,
    encounter_id     UUID NULL,
    visit_date       DATE NOT NULL,
    ga_weeks         SMALLINT NOT NULL CHECK (ga_weeks BETWEEN 0 AND 44),
    bp_sys           SMALLINT NULL CHECK (bp_sys BETWEEN 60 AND 260),
    bp_dia           SMALLINT NULL CHECK (bp_dia BETWEEN 30 AND 160),
    weight_kg        NUMERIC(5, 1) NULL CHECK (weight_kg BETWEEN 25 AND 250),
    fundal_height_cm SMALLINT NULL CHECK (fundal_height_cm BETWEEN 5 AND 50),
    fhr_bpm          SMALLINT NULL CHECK (fhr_bpm BETWEEN 60 AND 220),
    oedema           BOOLEAN NULL,
    urine_protein    TEXT NULL CHECK (urine_protein IN ('nil', 'trace', '1+', '2+', '3+', '4+')),
    hb_g_dl          NUMERIC(4, 1) NULL CHECK (hb_g_dl BETWEEN 2 AND 22),
    notes            TEXT NULL CHECK (char_length(notes) <= 500),
    entered_by_name  TEXT NOT NULL CHECK (char_length(btrim(entered_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT obs_anc_visits_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT obs_anc_visits_pregnancy_fk FOREIGN KEY (clinic_id, pregnancy_id) REFERENCES obs_pregnancies (clinic_id, id),
    CONSTRAINT obs_anc_visits_bp CHECK ((bp_sys IS NULL) = (bp_dia IS NULL) AND (bp_sys IS NULL OR bp_sys > bp_dia))
);
CREATE INDEX IF NOT EXISTS idx_obs_anc_visits_pregnancy ON obs_anc_visits (clinic_id, pregnancy_id, visit_date);

CREATE TABLE IF NOT EXISTS imm_doses (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id       UUID NOT NULL,                       -- the guardian's account
    family_member_id UUID NULL,                           -- the child (NULL when the patient row is the child)
    schedule_code    TEXT NOT NULL CHECK (schedule_code IN ('iap_2023', 'uip')),
    vaccine_code     TEXT NOT NULL CHECK (vaccine_code ~ '^[a-z0-9_]{2,30}$'),
    dose_no          SMALLINT NOT NULL CHECK (dose_no BETWEEN 1 AND 10),
    given_on         DATE NOT NULL,
    brand            TEXT NULL CHECK (char_length(brand) <= 80),
    batch_no         TEXT NOT NULL CHECK (char_length(btrim(batch_no)) BETWEEN 2 AND 40),
    expiry           DATE NOT NULL,
    site             TEXT NOT NULL CHECK (site IN ('left_thigh', 'right_thigh', 'left_arm', 'right_arm', 'oral', 'other')),
    given_by_name    TEXT NOT NULL CHECK (char_length(btrim(given_by_name)) BETWEEN 2 AND 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT imm_doses_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT imm_doses_not_expired CHECK (expiry >= given_on)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_imm_doses_dose ON imm_doses
    (clinic_id, patient_id, COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid), vaccine_code, dose_no);

DROP TRIGGER IF EXISTS trg_obs_pregnancies_refs ON obs_pregnancies;
CREATE TRIGGER trg_obs_pregnancies_refs BEFORE INSERT OR UPDATE ON obs_pregnancies FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_obs_anc_visits_refs ON obs_anc_visits;
CREATE TRIGGER trg_obs_anc_visits_refs BEFORE INSERT OR UPDATE ON obs_anc_visits FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('encounter_id', 'opd_encounters');
DROP TRIGGER IF EXISTS trg_imm_doses_refs ON imm_doses;
CREATE TRIGGER trg_imm_doses_refs BEFORE INSERT OR UPDATE ON imm_doses FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');

-- ── purge chain (children first; each table only when its migration is applied) ─
CREATE OR REPLACE FUNCTION sp_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t TEXT; n INTEGER; v JSONB := '{}'::jsonb;
BEGIN
    IF p_clinic_id IS NULL THEN RAISE EXCEPTION 'sp_bad_args' USING ERRCODE = 'P0001'; END IF;
    PERFORM set_config('kriya.sp_purge', p_clinic_id::TEXT, true);
    FOREACH t IN ARRAY ARRAY['imm_doses', 'obs_anc_visits', 'obs_pregnancies',
                             'ivf_witness_events', 'ivf_embryos', 'ivf_cycle_days', 'ivf_cycles', 'ivf_couples',
                             'sp_compliance_items', 'sp_dental_lab_orders', 'sp_optical_jobs',
                             'sp_camp_registrations', 'sp_camps', 'sp_imaging_links', 'sp_estimates', 'sp_instalments',
                             'sp_media', 'sp_media_consents', 'sp_package_consumptions', 'sp_package_sales', 'sp_packages',
                             'opd_encounter_forms']
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

-- returns the deleted photos' storage paths so the caller removes the files from the private bucket
CREATE OR REPLACE FUNCTION sp_purge_patient(p_clinic_id UUID, p_patient_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_paths JSONB; n_media INTEGER; n_cons INTEGER; n_est INTEGER; n_camp INTEGER;
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
    RETURN jsonb_build_object('media', n_media, 'media_consents', n_cons, 'estimates', n_est, 'camp_registrations', n_camp,
                              'paths', v_paths);
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['obs_pregnancies', 'obs_anc_visits', 'imm_doses']
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

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['sp_purge_clinic(uuid)', 'sp_purge_patient(uuid, uuid)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f); END IF;
    END LOOP;
END $$;

-- ── 155 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.obs_pregnancies') IS NULL OR to_regclass('public.obs_anc_visits') IS NULL
       OR to_regclass('public.imm_doses') IS NULL THEN
        RAISE EXCEPTION '155 verify: women & child tables missing';
    END IF;
    IF to_regprocedure('sp_purge_clinic(uuid)') IS NULL OR to_regprocedure('sp_purge_patient(uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION '155 verify: sp purge functions missing';
    END IF;
END $$;
