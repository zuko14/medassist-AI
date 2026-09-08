-- ============================================================================
-- Migration 072: "Diagnostic Test Booking" plan (slug: diagbooking)
-- ============================================================================
-- A diagnostic centre that ONLY takes lab-test bookings and payments over
-- WhatsApp — no report connector, no report delivery, no reminders. The
-- feature set lives in PLAN_FEATURES (app/services/tenant.py); this migration
-- only widens the two CHECK constraints that would otherwise reject the new
-- slug, and seeds its plan_tiers row.
--
-- PURELY ADDITIVE. No existing row is read, updated or reclassified, so this
-- cannot disturb a live tenant. Re-runnable.
-- ============================================================================

DO $$
DECLARE
    con RECORD;
    bad_count INT;
BEGIN
    -- ── clinics.plan ────────────────────────────────────────────────────────
    -- Refuse to touch the constraint if any row already holds a value outside
    -- the known set: widening a CHECK over unrecognised data hides a problem
    -- rather than fixing it.
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('soloclinic', 'diagstream', 'diagbooking',
                       'essential', 'polyclinic', 'enterprise');
    IF bad_count > 0 THEN
        RAISE EXCEPTION 'Found % clinics with an unexpected plan value — resolve before migrating', bad_count;
    END IF;

    -- Looked up dynamically (same approach as migration 016) so this works
    -- regardless of the auto-generated constraint name in any environment.
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
                        'essential', 'polyclinic', 'enterprise'));

    -- ── plan_tiers.plan_name ────────────────────────────────────────────────
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
                                 'essential', 'polyclinic', 'enterprise'));
    END IF;
END $$;

-- Seed the tier. Quota mirrors diagstream; the owner re-prices from the
-- platform dashboard (PUT /platform/plan-tiers/{plan_name}).
INSERT INTO plan_tiers (
    plan_name, display_name, monthly_price_paise,
    included_messages_month, overage_price_paise
)
VALUES ('diagbooking', 'Diagnostic Test Booking', 0, 1000, 0)
ON CONFLICT (plan_name) DO NOTHING;
