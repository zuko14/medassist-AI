-- ============================================================================
-- Rollback 154: Specialty OS IVF depth (lane SP). Refused while any IVF couple exists (ART records are retained by law:
-- export them, then run sp_purge_clinic); roll back 155 first. The normal rollback is a code revert.
-- ============================================================================
DO $$ BEGIN
    IF to_regclass('public.obs_pregnancies') IS NOT NULL THEN
        RAISE EXCEPTION '154_down: roll back 155 first';
    END IF;
    IF to_regclass('public.ivf_couples') IS NOT NULL AND EXISTS (SELECT 1 FROM ivf_couples) THEN
        RAISE EXCEPTION '154_down: IVF couples exist; export the ART records and run sp_purge_clinic first';
    END IF;
END $$;
DROP FUNCTION IF EXISTS ivf_witness_verify(uuid);
DO $$ BEGIN
    IF to_regclass('public.ivf_witness_events') IS NOT NULL THEN
        DROP FUNCTION IF EXISTS ivf_witness_hash(ivf_witness_events);
    END IF;
END $$;
DROP TABLE IF EXISTS ivf_witness_events;
DROP TABLE IF EXISTS ivf_embryos;
DROP TABLE IF EXISTS ivf_cycle_days;
DROP TABLE IF EXISTS ivf_cycles;
DROP TABLE IF EXISTS ivf_couples;
DROP FUNCTION IF EXISTS ivf_witness_append();
DROP FUNCTION IF EXISTS ivf_witness_append_only();
-- then (manual): DELETE FROM schema_migrations WHERE name = '154_sp_derma_ivf.sql';
