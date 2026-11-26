-- ============================================================================
-- Rollback 147: Continuous Care measure (CC-M3). Run 149 → 148 first. Refused while observations or prompts exist
-- (cc_purge_clinic first). The normal rollback is a code revert.
-- ============================================================================
CREATE OR REPLACE FUNCTION pg_temp.kriya_has_rows(t TEXT, w TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $f$
DECLARE r BOOLEAN;
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN RETURN false; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I %s)', t, COALESCE(w, '')) INTO r;
    RETURN r;
END $f$;
DO $$ BEGIN
    IF (pg_temp.kriya_has_rows('cc_observations', NULL))
       OR (pg_temp.kriya_has_rows('cc_prompts', NULL)) THEN
        RAISE EXCEPTION '147_down: cc observations / prompts exist; run cc_purge_clinic first';
    END IF;
END $$;
DROP TABLE IF EXISTS cc_evaluations;
DROP TABLE IF EXISTS cc_measure_sources;
DROP TABLE IF EXISTS cc_observations;
DROP FUNCTION IF EXISTS cc_guard_observation();
DROP TABLE IF EXISTS cc_prompts;
DROP TABLE IF EXISTS cc_schedule_items;
-- then (manual): DELETE FROM schema_migrations WHERE name = '147_cc_measure.sql';
