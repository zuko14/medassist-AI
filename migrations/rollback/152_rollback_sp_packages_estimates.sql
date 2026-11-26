-- ============================================================================
-- Rollback 152: Specialty OS packages, instalments, estimates, imaging links, camps (lane SP). Refused while a package
-- sale, estimate or camp registration exists (run sp_purge_clinic first); roll back 153–155 first. OPD invoices of
-- package sales are ordinary invoices and stay. The normal rollback is a code revert.
-- ============================================================================
DO $$ BEGIN
    IF to_regclass('public.sp_optical_jobs') IS NOT NULL THEN
        RAISE EXCEPTION '152_down: roll back 153 first';
    END IF;
    IF (to_regclass('public.sp_package_sales') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_package_sales))
       OR (to_regclass('public.sp_estimates') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_estimates))
       OR (to_regclass('public.sp_camp_registrations') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_camp_registrations)) THEN
        RAISE EXCEPTION '152_down: package sales, estimates or camp registrations exist; run sp_purge_clinic first';
    END IF;
END $$;
ALTER TABLE IF EXISTS sp_media DROP CONSTRAINT IF EXISTS sp_media_package_sale_fk;
DROP TABLE IF EXISTS sp_camp_registrations;
DROP TABLE IF EXISTS sp_camps;
DROP TABLE IF EXISTS sp_imaging_links;
DROP TABLE IF EXISTS sp_estimates;
DROP TABLE IF EXISTS sp_instalments;
DROP TABLE IF EXISTS sp_package_consumptions;
DROP TABLE IF EXISTS sp_package_sales;
DROP TABLE IF EXISTS sp_packages;
DROP FUNCTION IF EXISTS sp_package_consume_guard();
-- then (manual): DELETE FROM schema_migrations WHERE name = '152_sp_packages_estimates.sql';
