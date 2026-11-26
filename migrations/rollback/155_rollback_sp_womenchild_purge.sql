-- ============================================================================
-- Rollback 155: Specialty OS women & child depth + SP purge chain (lane SP). Refused while a pregnancy record or a
-- vaccine dose exists (medical record: run sp_purge_clinic first). The normal rollback is a code revert.
-- ============================================================================
DO $$ BEGIN
    IF to_regclass('public.obs_deliveries') IS NOT NULL THEN
        RAISE EXCEPTION '155_down: roll back 156 first';
    END IF;
    IF (to_regclass('public.obs_pregnancies') IS NOT NULL AND EXISTS (SELECT 1 FROM obs_pregnancies))
       OR (to_regclass('public.imm_doses') IS NOT NULL AND EXISTS (SELECT 1 FROM imm_doses)) THEN
        RAISE EXCEPTION '155_down: pregnancy records or vaccine doses exist; run sp_purge_clinic first';
    END IF;
END $$;
DROP FUNCTION IF EXISTS sp_purge_patient(uuid, uuid);
DROP FUNCTION IF EXISTS sp_purge_clinic(uuid);
DROP TABLE IF EXISTS imm_doses;
DROP TABLE IF EXISTS obs_anc_visits;
DROP TABLE IF EXISTS obs_pregnancies;
-- then (manual): DELETE FROM schema_migrations WHERE name = '155_sp_womenchild_purge.sql';
