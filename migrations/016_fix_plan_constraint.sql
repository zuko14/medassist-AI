-- Migration 016: Fix clinics.plan CHECK constraint drift
-- Run in Supabase SQL Editor
--
-- migrations/006_alter_clinics_plan.sql created the constraint with
-- ('basic','pro','enterprise'), but every application code path (Pydantic
-- models in app/routers/clinics.py, the PLAN_FEATURES registry in
-- app/services/tenant.py, admin/platform.html) has since moved to
-- ('soloclinic','diagstream','essential','polyclinic','enterprise').
-- This migration brings the DB constraint in line with the code.

DO $$
DECLARE
    bad_count INT;
    con RECORD;
BEGIN
    -- Abort loudly if any clinic holds a value outside the known old+new sets —
    -- never silently reclassify data we don't recognize.
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('basic', 'pro', 'enterprise',
                        'soloclinic', 'diagstream', 'essential', 'polyclinic');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with unexpected plan value — resolve before migrating', bad_count;
    END IF;

    -- Drop whichever CHECK constraint currently governs the plan column —
    -- looked up dynamically so this works regardless of the auto-generated
    -- constraint name in any given environment. This MUST run before the
    -- UPDATE below: the old constraint only allows 'basic'/'pro'/'enterprise',
    -- so writing 'essential' while it's still active would violate it.
    FOR con IN
        SELECT pg_constraint.conname
        FROM pg_constraint
        JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
        WHERE pg_class.relname = 'clinics'
          AND pg_constraint.contype = 'c'
          AND pg_get_constraintdef(pg_constraint.oid) LIKE '%plan%'
    LOOP
        EXECUTE format('ALTER TABLE clinics DROP CONSTRAINT %I', con.conname);
    END LOOP;

    -- Map legacy tier values to the closest clinic-type equivalent.
    -- 'enterprise' needs no mapping — it exists in both the old and new sets.
    UPDATE clinics SET plan = 'essential' WHERE plan IN ('basic', 'pro');

    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
        CHECK (plan IN ('soloclinic', 'diagstream', 'essential', 'polyclinic', 'enterprise'));
END $$;

-- Verify — every row should now show one of the 5 new plan values
SELECT id, name, plan FROM clinics ORDER BY created_at;
