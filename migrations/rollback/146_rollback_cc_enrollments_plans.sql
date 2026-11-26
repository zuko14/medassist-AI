-- ============================================================================
-- Rollback 146: Continuous Care enrollments and plans (CC-M2). Run 149 → 147 first. Refused while enrollments exist
-- (purge each CC clinic with cc_purge_clinic first). The normal rollback is a code revert.
-- ============================================================================
CREATE OR REPLACE FUNCTION pg_temp.kriya_has_rows(t TEXT, w TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $f$
DECLARE r BOOLEAN;
BEGIN
    IF to_regclass('public.' || t) IS NULL THEN RETURN false; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I %s)', t, COALESCE(w, '')) INTO r;
    RETURN r;
END $f$;
DO $$ BEGIN
    IF pg_temp.kriya_has_rows('cc_enrollments', NULL) THEN
        RAISE EXCEPTION '146_down: cc_enrollments rows exist; run cc_purge_clinic first';
    END IF;
END $$;
DROP FUNCTION IF EXISTS cc_install_plan(uuid, uuid, uuid, jsonb, text, date);
DROP TABLE IF EXISTS cc_discharge_followups;
DROP TABLE IF EXISTS cc_plan_items;
DROP TABLE IF EXISTS cc_care_plans;
DROP TABLE IF EXISTS cc_enrollments;
-- then (manual): DELETE FROM schema_migrations WHERE name = '146_cc_enrollments_plans.sql';
