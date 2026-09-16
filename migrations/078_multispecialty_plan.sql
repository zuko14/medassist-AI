-- ============================================================================
-- Migration 078: multispecialty plan (general hospital + treatments catalogue)
-- ============================================================================
-- Adds ONE plan slug for hospitals that run ordinary OPD departments, a lab
-- and radiology AND a treatments catalogue — skin, eye or dental procedures
-- alongside emergency, cardiac, maternity and dialysis care.
--
-- WHY THERE IS NO SCHEMA CHANGE HERE
-- specialty_treatments (migration 077) has no specialty_type column: a
-- treatment is identified by its clinic and its category. One clinic can
-- therefore already hold "Acne Scar Treatment" (Acne & Scars) next to
-- "Cataract Surgery" (Cataract). Mixing specialties needs no new column, no
-- new table and no backfill — only a plan slug the CHECK constraints accept.
--
-- WHY THE PLAN IS NOT IN SPECIALTY_BY_PLAN (app/services/tenant.py)
-- That map means "this facility IS one specialty" and drives both the starter
-- list and whether the patient's WhatsApp menu keeps its departments row. A
-- hospital with fifteen departments must keep that row, so the slug lives in
-- HYBRID_SPECIALTY_PLANS instead.
--
-- PURELY ADDITIVE: no existing row is updated; the only constraints touched
-- are the two plan CHECKs, widened by one value each. Re-runnable.
-- ============================================================================

-- Fail fast instead of queueing behind a long transaction while live bookings
-- wait on the clinics lock.
SET LOCAL lock_timeout = '5s';

-- ── Widen the plan CHECK constraints (same approach as migrations 072/077) ──
DO $$
DECLARE
    con RECORD;
    bad_count INT;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('soloclinic', 'diagstream', 'diagbooking',
                       'essential', 'polyclinic', 'enterprise',
                       'derma', 'eye', 'dental', 'ivf', 'multispecialty');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with an unexpected plan value — resolve before migrating', bad_count;
    END IF;

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

    ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
        CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                        'essential', 'polyclinic', 'enterprise',
                        'derma', 'eye', 'dental', 'ivf', 'multispecialty'));

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_name = 'plan_tiers') THEN
        FOR con IN
            SELECT pg_constraint.conname
            FROM pg_constraint
            JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
            WHERE pg_class.relname = 'plan_tiers'
              AND pg_constraint.contype = 'c'
              AND pg_get_constraintdef(pg_constraint.oid) LIKE '%plan_name%'
        LOOP
            EXECUTE format('ALTER TABLE plan_tiers DROP CONSTRAINT %I', con.conname);
        END LOOP;

        ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
            CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                                 'essential', 'polyclinic', 'enterprise',
                                 'derma', 'eye', 'dental', 'ivf', 'multispecialty'));
    END IF;
END $$;

-- Quota mirrors 'polyclinic' (5,000 messages): this is a hospital-scale plan,
-- not a single-chair clinic. Price stays 0 until the owner sets it from the
-- platform dashboard (PUT /platform/plan-tiers/{plan_name}).
INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES ('multispecialty', 'Multi-Specialty Hospital', 0, 5000, 0)
ON CONFLICT (plan_name) DO NOTHING;

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'migration_078_complete' AS status;
