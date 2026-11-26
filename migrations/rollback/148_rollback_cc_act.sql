-- ============================================================================
-- Rollback 148: Continuous Care act (CC-M4). Run 149 first. Refused while cases exist (cc_purge_clinic first).
-- ============================================================================
CREATE OR REPLACE FUNCTION pg_temp.kriya_has_rows(t TEXT, w TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $f$
DECLARE r BOOLEAN;
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN RETURN false; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I %s)', t, COALESCE(w, '')) INTO r;
    RETURN r;
END $f$;
DO $$ BEGIN
    IF pg_temp.kriya_has_rows('cc_cases', NULL) THEN
        RAISE EXCEPTION '148_down: cc_cases rows exist; run cc_purge_clinic first';
    END IF;
END $$;
DROP TABLE IF EXISTS cc_actions;
DROP TABLE IF EXISTS cc_case_events;
DROP FUNCTION IF EXISTS cc_guard_case_event();
DROP TABLE IF EXISTS cc_cases;
DROP TABLE IF EXISTS cc_radar_rows;
-- then (manual): DELETE FROM schema_migrations WHERE name = '148_cc_act.sql';
