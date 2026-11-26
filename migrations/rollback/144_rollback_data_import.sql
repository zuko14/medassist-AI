-- ============================================================================
-- Rollback 144: Kriya Data Import (MI-01)
-- Refused while any applied batch exists: undo each batch from the wizard first (that removes or restores what it
-- wrote into doctors / services / packages / payers / appointments). Links and legacy history are import data and
-- are dropped with their tables; patient_records (088) is untouched. The normal rollback is a code revert.
-- ============================================================================

CREATE OR REPLACE FUNCTION pg_temp.kriya_has_rows(t TEXT, w TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $f$
DECLARE r BOOLEAN;
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN RETURN false; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I %s)', t, COALESCE(w, '')) INTO r;
    RETURN r;
END $f$;
DO $$ BEGIN
    IF pg_temp.kriya_has_rows('mi_batches', $q$WHERE status = 'applied'$q$) THEN
        RAISE EXCEPTION '144_down: applied import batches exist; undo them first';
    END IF;
END $$;

DROP FUNCTION IF EXISTS mi_undo(uuid, uuid);
DROP FUNCTION IF EXISTS mi_refresh_matches(uuid);
DROP TABLE IF EXISTS mi_chronic_candidates;
DROP TABLE IF EXISTS mi_diagnosis_map;
DROP TABLE IF EXISTS mi_opening_balances;
DROP TABLE IF EXISTS mi_legacy_lab_values;
DROP TABLE IF EXISTS mi_legacy_encounters;
DROP TABLE IF EXISTS mi_match_queue;
DROP TABLE IF EXISTS mi_patient_links;
DROP TABLE IF EXISTS mi_batch_items;
DROP TABLE IF EXISTS mi_row_errors;
DROP TABLE IF EXISTS mi_batches;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid WHERE p.proname = 'kriya_guard_refs') THEN
        DROP FUNCTION IF EXISTS kriya_guard_refs();
    END IF;
END $$;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '144_data_import.sql';
