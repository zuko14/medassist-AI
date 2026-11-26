-- ============================================================================
-- Rollback 151: Specialty OS media vault + forms engine (lane SP). Refused while clinical rows exist (forms are part of
-- the medical record; photos are patient data): run sp_purge_clinic for each clinic first. Roll back 152–155 first.
-- kriya_guard_refs() belongs to migration 144 and is never dropped here. The normal rollback is a code revert.
-- ============================================================================
DO $$ BEGIN
    IF to_regclass('public.sp_packages') IS NOT NULL THEN
        RAISE EXCEPTION '151_down: roll back 152 first';
    END IF;
    IF (to_regclass('public.opd_encounter_forms') IS NOT NULL AND EXISTS (SELECT 1 FROM opd_encounter_forms))
       OR (to_regclass('public.sp_media') IS NOT NULL AND EXISTS (SELECT 1 FROM sp_media)) THEN
        RAISE EXCEPTION '151_down: specialty forms or clinical media exist; run sp_purge_clinic first';
    END IF;
END $$;
DROP TABLE IF EXISTS opd_encounter_forms;
DROP TABLE IF EXISTS sp_media;
DROP TABLE IF EXISTS sp_media_consents;
DROP FUNCTION IF EXISTS sp_form_freeze();
-- then (manual): DELETE FROM schema_migrations WHERE name = '151_sp_media_forms.sql';
