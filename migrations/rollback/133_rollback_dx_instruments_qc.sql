-- ============================================================================
-- Rollback 133: Diagnostic OS instruments and QC (DX-M9)
-- Refused while any instrument result or QC run exists (unless the purge flag
-- for that clinic is set): purge each DX clinic first (SELECT dx_purge_clinic('<id>')).
-- Reverses the 133 text patches (dx_purge_clinic back to v8, dx_record_results
-- back to 130/131) and drops the five tables. The normal rollback is a code
-- revert: pre-133 code runs unchanged on 133.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_instrument_results') IS NOT NULL
        AND EXISTS (SELECT 1 FROM dx_instrument_results WHERE NOT dx_purging(clinic_id)))
       OR (to_regclass('public.dx_qc_results') IS NOT NULL
           AND EXISTS (SELECT 1 FROM dx_qc_results WHERE NOT dx_purging(clinic_id))) THEN
        RAISE EXCEPTION '133_down: instrument results or QC runs exist; run dx_purge_clinic for each DX clinic first';
    END IF;
END $$;

-- purge v9 -> v8
DO $$
DECLARE v_def TEXT;
    v_v8 TEXT := $s$PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders$s$;
    v_v9 TEXT := $s$PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    -- v9 (133): instruments and QC
    DELETE FROM dx_qc_results WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('qc_results', n);
    DELETE FROM dx_qc_materials WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_instrument_results WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('instrument_results', n);
    DELETE FROM dx_instruments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_bridge_agents WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('bridge_agents', n);$s$;
BEGIN
    v_def := pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure);
    IF position(v_v9 IN v_def) > 0 THEN
        EXECUTE replace(v_def, v_v9, v_v8);
    END IF;
END $$;

-- value meta: instrument_result_id no longer written
DO $$
DECLARE p TEXT[]; v_def TEXT;
BEGIN
    FOREACH p SLICE 1 IN ARRAY ARRAY[
        [$s$previous_value, previous_at, delta_pct, delta_failed, source)$s$,
         $s$previous_value, previous_at, delta_pct, delta_failed, source, instrument_result_id)$s$],
        [$s$COALESCE(x.source, 'manual')$s$, $s$COALESCE(x.source, 'manual'), x.instrument_result_id$s$],
        [$s$source TEXT);$s$, $s$source TEXT, instrument_result_id UUID);$s$]
    ] LOOP
        v_def := pg_get_functiondef('dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)'::regprocedure);
        IF position(p[2] IN v_def) > 0 THEN
            EXECUTE replace(v_def, p[2], p[1]);
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS dx_qc_results;
DROP TABLE IF EXISTS dx_qc_materials;
DROP TABLE IF EXISTS dx_instrument_results;
DROP TABLE IF EXISTS dx_instruments;
DROP TABLE IF EXISTS dx_bridge_agents;
DROP FUNCTION IF EXISTS dx_guard_instrument_result();
DROP FUNCTION IF EXISTS dx_guard_qc_result();
