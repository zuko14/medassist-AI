-- ============================================================================
-- Migration 075: clinic_id becomes mandatory on the core tenant tables
-- ============================================================================
-- Migration 004 backfilled clinic_id and then left the NOT NULL statements
-- commented out "for later". Later never came, so the nine oldest and most
-- sensitive tables have carried a nullable tenant column ever since. Tables
-- added afterwards (009, 010, 012) all declare NOT NULL, so this is drift, not
-- a design decision.
--
-- Why it matters now: a row with a NULL clinic_id belongs to no one, and
-- get_clinic_by_id() used to resolve "no one" to the OLDEST active clinic.
-- A reminder for such a row went out carrying another hospital's name,
-- address and emergency number. The application fix removes the guess; this
-- migration removes the state that made the guess reachable.
--
-- Deliberately EXCLUDED, where NULL is a correct and meaningful value:
--   processed_messages, inbound_messages, failed_messages
--       written at the ingestion boundary, BEFORE the tenant is resolved;
--       a NOT NULL here would break the webhook dedup guard.
--   admin_audit_logs, admin_sessions, clinic_admins
--       platform-level actors (super_admin, platform_owner) legitimately
--       have no clinic. Migration 051 already constrains that shape.
--   payment_events — done in migration 074.
--
-- Idempotent and re-runnable. Zero downtime: ADD CONSTRAINT ... NOT VALID is
-- instant and takes no table scan, and it blocks every future NULL from the
-- moment it lands. VALIDATE is attempted per table and only when that table is
-- already clean, so a table with legacy NULLs neither aborts this migration
-- nor blocks writes — it keeps the constraint unvalidated and reports the
-- count for an operator to clean up.
-- ============================================================================

DO $$
DECLARE
    tbl              TEXT;
    con              TEXT;
    null_rows        BIGINT;
    total_unresolved BIGINT := 0;
    targets          TEXT[] := ARRAY[
        'appointments',
        'patients',
        'conversations',
        'doctors',
        'doctor_leaves',
        'hospital_holidays',
        'lab_reports',
        'prescriptions',
        'analytics_events',
        'prescription_reminder_sends'
    ];
BEGIN
    FOREACH tbl IN ARRAY targets LOOP
        -- Skip anything this deployment does not have.
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = tbl
              AND column_name = 'clinic_id'
        ) THEN
            RAISE NOTICE 'skip %: no clinic_id column here', tbl;
            CONTINUE;
        END IF;

        con := 'chk_' || tbl || '_clinic_id_present';

        -- Declare the invariant. Instant, and stops new NULLs immediately.
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = con AND conrelid = tbl::regclass
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I CHECK (clinic_id IS NOT NULL) NOT VALID',
                tbl, con
            );
        END IF;

        EXECUTE format('SELECT count(*) FROM %I WHERE clinic_id IS NULL', tbl)
            INTO null_rows;

        IF null_rows = 0 THEN
            -- SHARE UPDATE EXCLUSIVE: concurrent reads and writes keep working.
            EXECUTE format('ALTER TABLE %I VALIDATE CONSTRAINT %I', tbl, con);
            RAISE NOTICE '% : clean, constraint validated', tbl;
        ELSE
            total_unresolved := total_unresolved + null_rows;
            RAISE WARNING
                '% : % existing row(s) have a NULL clinic_id. New NULLs are '
                'already blocked; assign these rows an owner, then run: '
                'ALTER TABLE % VALIDATE CONSTRAINT %;',
                tbl, null_rows, tbl, con;
        END IF;
    END LOOP;

    IF total_unresolved = 0 THEN
        RAISE NOTICE 'All core tenant tables now enforce clinic_id.';
    ELSE
        RAISE WARNING
            '% tenant row(s) across all tables still have no owner. They are '
            'invisible to per-clinic queries until assigned.', total_unresolved;
    END IF;
END $$;
