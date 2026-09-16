-- ============================================================================
-- Migration 077: Specialty hospital plans + treatments catalogue
-- ============================================================================
-- Adds four plans (derma, eye, dental, ivf), a per-clinic treatments
-- catalogue, the doctors who perform each treatment, and two nullable tag
-- columns on appointments.
--
-- WHY appointments.booking_type IS NOT WIDENED
-- A treatment booking is a consultation slot with a doctor. The slot
-- uniqueness indexes (migration 064), the time-required CHECK (039), the
-- doctor_id guards and the reminder jobs all key on
-- booking_type = 'consultation'. A new booking_type would silently opt those
-- rows out of double-booking protection. The treatment is a TAG on a
-- consultation, never a new booking type.
--
-- PURELY ADDITIVE: no existing row is updated; existing constraints other than
-- the two plan CHECKs are untouched. Re-runnable.
-- ============================================================================

-- Fail fast instead of queueing behind a long transaction and stalling live
-- bookings while waiting for the appointments lock.
SET LOCAL lock_timeout = '5s';

-- ── 1. Widen the plan CHECK constraints (same approach as migration 072) ────
DO $$
DECLARE
    con RECORD;
    bad_count INT;
BEGIN
    SELECT COUNT(*) INTO bad_count FROM clinics
    WHERE plan NOT IN ('soloclinic', 'diagstream', 'diagbooking',
                       'essential', 'polyclinic', 'enterprise',
                       'derma', 'eye', 'dental', 'ivf');
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
                        'derma', 'eye', 'dental', 'ivf'));

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
                                 'derma', 'eye', 'dental', 'ivf'));
    END IF;
END $$;

-- Quotas mirror 'essential' (2,500 messages). Price 0 until the owner sets it
-- from the platform dashboard (PUT /platform/plan-tiers/{plan_name}).
INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES
    ('derma',  'Dermatology & Hair', 0, 2500, 0),
    ('eye',    'Eye Hospital',       0, 2500, 0),
    ('dental', 'Dental Clinic',      0, 2500, 0),
    ('ivf',    'IVF & Fertility',    0, 2500, 0)
ON CONFLICT (plan_name) DO NOTHING;

-- ── 2. Treatments catalogue ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS specialty_treatments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    category          TEXT NOT NULL CHECK (char_length(btrim(category)) BETWEEN 1 AND 60),
    name              TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    short_name        TEXT CHECK (short_name IS NULL OR char_length(short_name) <= 24),
    description       TEXT CHECK (description IS NULL OR char_length(description) <= 400),
    description_hi    TEXT CHECK (description_hi IS NULL OR char_length(description_hi) <= 600),
    description_te    TEXT CHECK (description_te IS NULL OR char_length(description_te) <= 600),
    concerns          TEXT CHECK (concerns IS NULL OR char_length(concerns) <= 500),
    duration_minutes  INTEGER CHECK (duration_minutes IS NULL OR duration_minutes BETWEEN 5 AND 1440),
    price_from_paise  INTEGER NOT NULL DEFAULT 0 CHECK (price_from_paise >= 0),
    prep_instructions TEXT CHECK (prep_instructions IS NULL OR char_length(prep_instructions) <= 600),
    is_active         BOOLEAN NOT NULL DEFAULT true,
    display_order     INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'custom' CHECK (source IN ('custom', 'starter')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One treatment name per clinic, compared the way the app compares
-- (strip + lowercase), matching the lab_tests rule from migration 076.
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_treatment_name_per_clinic
    ON specialty_treatments (clinic_id, lower(btrim(name)));

CREATE INDEX IF NOT EXISTS idx_specialty_treatments_clinic_active
    ON specialty_treatments (clinic_id, is_active, category);

ALTER TABLE specialty_treatments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_specialty_treatments" ON specialty_treatments;
CREATE POLICY "service_role_all_specialty_treatments" ON specialty_treatments
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ── 3. Doctors who perform a treatment ───────────────────────────────────────
-- Carries clinic_id (unlike doctor_branches) so every query can be tenant
-- scoped directly and the table can sit in TENANT_OWNED_TABLES. That the
-- doctor belongs to the same clinic is verified by the admin API before insert.
CREATE TABLE IF NOT EXISTS treatment_doctors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    treatment_id UUID NOT NULL REFERENCES specialty_treatments(id) ON DELETE CASCADE,
    doctor_id    UUID NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (treatment_id, doctor_id)
);

CREATE INDEX IF NOT EXISTS idx_treatment_doctors_clinic_treatment
    ON treatment_doctors (clinic_id, treatment_id);
CREATE INDEX IF NOT EXISTS idx_treatment_doctors_doctor
    ON treatment_doctors (doctor_id);

ALTER TABLE treatment_doctors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_treatment_doctors" ON treatment_doctors;
CREATE POLICY "service_role_all_treatment_doctors" ON treatment_doctors
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ── 4. Tag columns on appointments ───────────────────────────────────────────
-- Nullable, no default: a metadata-only change, no table rewrite. ON DELETE
-- SET NULL keeps booking history when an admin deletes a treatment;
-- treatment_name is stored at booking time exactly like lab_test_name.
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS treatment_id UUID REFERENCES specialty_treatments(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS treatment_name TEXT;

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'migration_077_complete' AS status;
