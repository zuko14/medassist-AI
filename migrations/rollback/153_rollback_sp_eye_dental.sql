-- ============================================================================
-- Rollback 153: Specialty OS eye + dental depth (lane SP). Refused while optical jobs or dental lab orders exist (run
-- sp_purge_clinic first); roll back 154–155 first. The normal rollback is a code revert.
-- ============================================================================
DO $$ BEGIN
    IF to_regclass('public.ivf_couples') IS NOT NULL THEN
        RAISE EXCEPTION '153_down: roll back 154 first';
    END IF;
    IF (to_regclass('public.sp_optical_jobs') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_optical_jobs))
       OR (to_regclass('public.sp_dental_lab_orders') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_dental_lab_orders)) THEN
        RAISE EXCEPTION '153_down: optical jobs or dental lab orders exist; run sp_purge_clinic first';
    END IF;
END $$;
DROP TABLE IF EXISTS sp_compliance_items;
DROP TABLE IF EXISTS sp_dental_lab_orders;
DROP TABLE IF EXISTS sp_optical_jobs;
-- then (manual): DELETE FROM schema_migrations WHERE name = '153_sp_eye_dental.sql';
