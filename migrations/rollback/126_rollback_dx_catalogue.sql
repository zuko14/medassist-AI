-- ============================================================================
-- Rollback 126: Diagnostic OS catalogue (DX-M2)
-- Refused while any catalogue row exists or any lab test is DX-approved: dropping
-- the columns would silently lose the lab director's work. Purge each DX clinic
-- first with SELECT dx_purge_clinic('<clinic_id>') and clear approvals
-- (UPDATE lab_tests SET approved_at = NULL, approved_by = NULL, approval_digest = NULL).
-- Puts dx_purge_clinic back to its 125 (v1) body.
-- The normal rollback is a code revert: pre-126 code runs unchanged on 126.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.dx_test_components') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_test_components))
       OR (to_regclass('public.dx_signatories') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_signatories))
       OR (to_regclass('public.dx_containers') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_containers))
       OR (to_regclass('public.dx_departments') IS NOT NULL AND EXISTS (SELECT 1 FROM dx_departments)) THEN
        RAISE EXCEPTION '126_down: DX catalogue rows exist; run dx_purge_clinic for each DX clinic first';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'lab_tests' AND column_name = 'approved_at')
       AND EXISTS (SELECT 1 FROM lab_tests WHERE approved_at IS NOT NULL) THEN
        RAISE EXCEPTION '126_down: DX-approved lab tests exist; clear approvals first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_dx_lab_tests_clear_approval ON lab_tests;
DROP TRIGGER IF EXISTS trg_dx_parameters_clear_approval ON lab_test_parameters;
DROP TRIGGER IF EXISTS trg_dx_parameters_kind ON lab_test_parameters;
DROP TABLE IF EXISTS dx_test_components;
DROP TABLE IF EXISTS dx_signatories;
DROP FUNCTION IF EXISTS dx_approve_test(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS dx_test_digest(uuid, uuid);
DROP FUNCTION IF EXISTS dx_clear_test_approval();
DROP FUNCTION IF EXISTS dx_lab_tests_guard();
DROP FUNCTION IF EXISTS dx_guard_parameter_kind();
DROP FUNCTION IF EXISTS dx_guard_components();

ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_department_fk, DROP CONSTRAINT IF EXISTS lab_tests_container_fk,
    DROP CONSTRAINT IF EXISTS lab_tests_dx_check;
DROP INDEX IF EXISTS uq_lab_tests_test_code;
ALTER TABLE lab_tests
    DROP COLUMN IF EXISTS test_code, DROP COLUMN IF EXISTS kind, DROP COLUMN IF EXISTS department_id,
    DROP COLUMN IF EXISTS container_id, DROP COLUMN IF EXISTS sample_volume_ml, DROP COLUMN IF EXISTS method,
    DROP COLUMN IF EXISTS tat_minutes, DROP COLUMN IF EXISTS stat_tat_minutes, DROP COLUMN IF EXISTS print_title,
    DROP COLUMN IF EXISTS interpretation, DROP COLUMN IF EXISTS report_notes, DROP COLUMN IF EXISTS gender_restriction,
    DROP COLUMN IF EXISTS loinc_code, DROP COLUMN IF EXISTS print_order, DROP COLUMN IF EXISTS dx_result_mode,
    DROP COLUMN IF EXISTS approved_at, DROP COLUMN IF EXISTS approved_by, DROP COLUMN IF EXISTS approval_digest;

ALTER TABLE lab_test_parameters DROP CONSTRAINT IF EXISTS lab_test_parameters_dx_check;
ALTER TABLE lab_test_parameters
    DROP COLUMN IF EXISTS parameter_code, DROP COLUMN IF EXISTS method, DROP COLUMN IF EXISTS decimal_places,
    DROP COLUMN IF EXISTS rounding_mode, DROP COLUMN IF EXISTS formula, DROP COLUMN IF EXISTS reference_text,
    DROP COLUMN IF EXISTS machine_code, DROP COLUMN IF EXISTS delta_direction, DROP COLUMN IF EXISTS delta_limit_percent,
    DROP COLUMN IF EXISTS delta_limit_abs, DROP COLUMN IF EXISTS delta_lookback_days, DROP COLUMN IF EXISTS low_exclusive,
    DROP COLUMN IF EXISTS high_exclusive;

DROP TABLE IF EXISTS dx_containers;
DROP TABLE IF EXISTS dx_departments;
DROP FUNCTION IF EXISTS dx_raise(text);

-- dx_purge_clinic back to v1 (migration 125)
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '126_dx_catalogue.sql';
