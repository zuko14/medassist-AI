-- ============================================================================
-- Rollback 156: Women & Child completion + optical stock + IOL → IPD (lane SP). Refused while any of the new rows
-- exist (medical record / stock ledger: run sp_purge_clinic first). Restores the 155 purge functions and the 152
-- estimate conversion CHECK. The normal rollback is a code revert.
-- ============================================================================
DO $$
DECLARE t TEXT; v INT;
BEGIN
    FOREACH t IN ARRAY ARRAY['obs_deliveries', 'obs_newborns', 'child_growth_measurements', 'wc_gov_ids',
                             'nicu_flowsheet_entries', 'sp_optical_items', 'sp_optical_stock_moves']
    LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            v := NULL;
            EXECUTE format('SELECT 1 FROM %I LIMIT 1', t) INTO v;   -- EXECUTE never sets FOUND
            IF v IS NOT NULL THEN RAISE EXCEPTION '156_down: % has rows; run sp_purge_clinic first', t; END IF;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'sp_estimates'
                 AND column_name = 'converted_ipd_package_id')
       AND EXISTS (SELECT 1 FROM sp_estimates WHERE converted_ipd_package_id IS NOT NULL) THEN
        RAISE EXCEPTION '156_down: estimates converted to IPD packages exist';
    END IF;
END $$;
ALTER TABLE IF EXISTS sp_optical_jobs DROP CONSTRAINT IF EXISTS sp_optical_jobs_frame_item_fk;
ALTER TABLE IF EXISTS sp_optical_jobs DROP CONSTRAINT IF EXISTS sp_optical_jobs_lens_item_fk;
ALTER TABLE IF EXISTS sp_optical_jobs DROP COLUMN IF EXISTS frame_item_id;
ALTER TABLE IF EXISTS sp_optical_jobs DROP COLUMN IF EXISTS lens_item_id;
ALTER TABLE IF EXISTS sp_optical_jobs DROP COLUMN IF EXISTS stock_issued_at;
DROP TRIGGER IF EXISTS trg_sp_estimates_ipd_refs ON sp_estimates;
ALTER TABLE IF EXISTS sp_estimates DROP CONSTRAINT IF EXISTS sp_estimates_converted;
ALTER TABLE IF EXISTS sp_estimates DROP COLUMN IF EXISTS converted_ipd_package_id;
ALTER TABLE IF EXISTS sp_estimates ADD CONSTRAINT sp_estimates_converted CHECK ((status = 'converted') = (converted_sale_id IS NOT NULL));
DROP TABLE IF EXISTS sp_optical_stock_moves;
DROP TABLE IF EXISTS sp_optical_items;
DROP TABLE IF EXISTS nicu_flowsheet_entries;
DROP TABLE IF EXISTS wc_gov_ids;
DROP TABLE IF EXISTS child_growth_measurements;
DROP TABLE IF EXISTS obs_newborns;
DROP TABLE IF EXISTS obs_deliveries;
DROP FUNCTION IF EXISTS sp_optical_move(uuid, uuid, integer, text, uuid, text, text);
DROP FUNCTION IF EXISTS sp_optical_moves_append_only();

-- the 155 purge functions
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

-- then (manual): DELETE FROM schema_migrations WHERE name = '156_sp_wc_optical.sql';
