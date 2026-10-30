-- ============================================================================
-- Rollback 118: In-house Lab Information System (LIS)
-- DESTROYS every lab parameter definition and every lab result. Take
-- pg_dump -t lab_test_parameters -t lab_results -t lab_result_values FIRST.
-- Refused while any result exists: a signed lab report is a legal record that
-- pre-118 code cannot show. The normal rollback is a code revert: pre-118 code
-- runs unchanged on 118 (it never reads these tables).
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.lab_results') IS NOT NULL AND EXISTS (SELECT 1 FROM lab_results) THEN
        RAISE EXCEPTION '118_down: lab results exist';
    END IF;
END $$;

DROP TABLE IF EXISTS lab_result_values;
DROP TABLE IF EXISTS lab_results;
DROP TABLE IF EXISTS lab_test_parameters;
DROP FUNCTION IF EXISTS lis_record_results(uuid, uuid, jsonb, boolean, text, uuid, text);
DROP FUNCTION IF EXISTS lis_validate_result(uuid, uuid, timestamptz, uuid, jsonb, jsonb, jsonb);
DROP FUNCTION IF EXISTS lis_guard_result();
DROP FUNCTION IF EXISTS lis_guard_result_value();
ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_clinic_id_id_key;
