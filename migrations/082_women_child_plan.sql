-- ============================================================================
-- Migration 082: Women & Child hospital plan + treatment service lines
-- ============================================================================
-- Hospitals such as Rainbow Children's / BirthRight run three service lines
-- under one roof -- Child Care, Women Care and Fertility Care -- each with its
-- own sub-specialties, its own doctors and its own treatments. Around forty
-- paediatric sub-specialties alone.
--
-- 1. ONE plan slug, 'womenchild'. Like 'multispecialty' (078) it is a hybrid:
--    OPD departments, lab and radiology stay, and the treatments catalogue is
--    added on top. The plan is a registry entry in app/services/tenant.py;
--    here it only needs the two CHECK constraints widened and a tier row.
--
-- 2. ONE nullable column, specialty_treatments.service_line. Categories alone
--    cannot carry three service lines: "Surgery" exists in Women Care and in
--    Fertility Care, and a single category list of fifteen headings is what a
--    patient scrolls past. The WhatsApp flow shows a service-line picker ONLY
--    when a clinic's published treatments span two or more lines, so:
--      * every existing row is NULL and stays NULL -- no backfill;
--      * a clinic that never sets a line sees exactly today's flow;
--      * a single-specialty clinic whose new starter rows carry one line
--        still has fewer than two lines, and still sees today's flow.
--
-- PURELY ADDITIVE: no existing row is updated. Re-runnable.
-- DEPLOY ORDER: apply this BEFORE the application. The new build writes
-- service_line when it seeds starter treatments.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

-- ── 1. Widen the plan CHECK constraints (same approach as 072/077/078) ──────
DO $$
DECLARE
    con RECORD;
    bad_count INT;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('soloclinic', 'diagstream', 'diagbooking',
                       'essential', 'polyclinic', 'enterprise',
                       'derma', 'eye', 'dental', 'ivf', 'multispecialty',
                       'womenchild');
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
                        'derma', 'eye', 'dental', 'ivf', 'multispecialty',
                        'womenchild'));

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
                                 'derma', 'eye', 'dental', 'ivf', 'multispecialty',
                                 'womenchild'));
    END IF;
END $$;

-- Hospital scale, like polyclinic and multispecialty. Price stays 0 until the
-- owner sets it (PUT /platform/plan-tiers/{plan_name}).
INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES ('womenchild', 'Women & Child Hospital', 0, 5000, 0)
ON CONFLICT (plan_name) DO NOTHING;

-- ── 2. Service line on the treatments catalogue ─────────────────────────────
ALTER TABLE specialty_treatments
    ADD COLUMN IF NOT EXISTS service_line TEXT;

-- DROP-then-ADD: idempotent, and survives the statement splitter in
-- run_migrations.py. NULL means "no service line" and is always allowed.
-- Kept in step with SERVICE_LINES in app/services/specialty_catalog.py --
-- tests/test_women_child_plan.py fails if the two drift apart.
ALTER TABLE specialty_treatments
    DROP CONSTRAINT IF EXISTS specialty_treatments_service_line_check;

ALTER TABLE specialty_treatments
    ADD CONSTRAINT specialty_treatments_service_line_check
    CHECK (service_line IS NULL OR service_line IN (
        'child_care', 'women_care', 'fertility_care',
        'skin_hair', 'eye_care', 'dental_care'));

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'migration_082_complete' AS status;
