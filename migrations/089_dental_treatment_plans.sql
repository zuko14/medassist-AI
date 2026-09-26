-- ============================================================================
-- Migration 089: Dental treatment plans (multi-sitting courses), doctor
-- WhatsApp reminders, post-sitting reviews, owner-set monthly message limits
-- ============================================================================
-- Dental care is course-based: one Root Canal is 2-4 sittings, an implant or
-- aligner case is many, and the NEXT sitting is fixed by the front desk after
-- the dentist sees the patient. A plain appointment models one visit.
--
--   dental_treatment_plans  one row per course (patient + treatment + planned
--                           sittings + quote + WhatsApp consent)
--   appointments            each sitting IS an appointment row, linked by
--                           treatment_plan_id + sitting_number. Reusing the
--                           table keeps uq_appointment_active_slot (no double
--                           booking a dentist), leave/holiday checks, check-in,
--                           queue and the Appointments page working unchanged.
--   dental_doctor_digests   exactly-once guard for the doctor's daily schedule
--   clinic_message_quota_usage + reserve_message_quota()
--                           atomic per-month counters for the owner-set limits
--
-- Dental-only in the application (plan = 'dental' AND feature
-- dental_treatment_plans). Every column added to a shared table is NULLable
-- with no default except the invoice add-on (DEFAULT 0), so every existing row
-- and every other plan is unaffected.
--
-- PURELY ADDITIVE. Re-runnable. Safe with the previous build still running.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

-- ── 1. Small additive columns on shared tables ─────────────────────────────
ALTER TABLE doctors
    ADD COLUMN IF NOT EXISTS whatsapp_phone TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'doctors_whatsapp_phone_format') THEN
        ALTER TABLE doctors ADD CONSTRAINT doctors_whatsapp_phone_format
            CHECK (whatsapp_phone IS NULL OR whatsapp_phone ~ '^\+?[0-9]{10,15}$');
    END IF;
END $$;

ALTER TABLE specialty_treatments
    ADD COLUMN IF NOT EXISTS default_sittings SMALLINT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'specialty_treatments_default_sittings_range') THEN
        ALTER TABLE specialty_treatments ADD CONSTRAINT specialty_treatments_default_sittings_range
            CHECK (default_sittings IS NULL OR default_sittings BETWEEN 1 AND 30);
    END IF;
END $$;

ALTER TABLE platform_invoices
    ADD COLUMN IF NOT EXISTS messaging_addon_paise INTEGER NOT NULL DEFAULT 0;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'platform_invoices_messaging_addon_nonneg') THEN
        ALTER TABLE platform_invoices ADD CONSTRAINT platform_invoices_messaging_addon_nonneg
            CHECK (messaging_addon_paise >= 0);
    END IF;
END $$;

-- ── 2. dental_treatment_plans ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dental_treatment_plans (
    id                     UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    patient_phone          TEXT        NOT NULL CHECK (patient_phone ~ '^\+?[0-9]{10,15}$'),
    patient_name           TEXT        NOT NULL CHECK (length(btrim(patient_name)) BETWEEN 1 AND 100),
    patient_record_id      UUID        REFERENCES patient_records(id) ON DELETE SET NULL,
    treatment_id           UUID        REFERENCES specialty_treatments(id) ON DELETE SET NULL,
    treatment_name         TEXT        NOT NULL CHECK (length(btrim(treatment_name)) BETWEEN 1 AND 120),
    tooth_numbers          TEXT        CHECK (tooth_numbers IS NULL OR length(tooth_numbers) <= 60),
    planned_sittings       SMALLINT    NOT NULL CHECK (planned_sittings BETWEEN 1 AND 30),
    status                 TEXT        NOT NULL DEFAULT 'active'
                                       CHECK (status IN ('active', 'completed', 'cancelled')),
    quoted_amount_paise    INTEGER     CHECK (quoted_amount_paise IS NULL OR quoted_amount_paise >= 0),
    notes                  TEXT        CHECK (notes IS NULL OR length(notes) <= 2000),
    -- A patient imported from old software, or a walk-in, never opted in on
    -- WhatsApp. The front desk records the patient's agreement here; without
    -- it the patient is never messaged (the doctor still is).
    whatsapp_consent       BOOLEAN     NOT NULL DEFAULT false,
    whatsapp_consent_at    TIMESTAMPTZ,
    consent_recorded_by    TEXT,
    notify_patient         BOOLEAN     NOT NULL DEFAULT true,
    notify_doctor          BOOLEAN     NOT NULL DEFAULT true,
    created_by             TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dental_plans_clinic_status_created
    ON dental_treatment_plans (clinic_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dental_plans_clinic_phone
    ON dental_treatment_plans (clinic_id, patient_phone);

-- ── 3. Sittings = appointments linked to a plan ────────────────────────────
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS treatment_plan_id UUID REFERENCES dental_treatment_plans(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS sitting_number SMALLINT,
    ADD COLUMN IF NOT EXISTS sitting_notes TEXT,
    ADD COLUMN IF NOT EXISTS amount_collected_paise INTEGER,
    ADD COLUMN IF NOT EXISTS review_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_rating SMALLINT,
    ADD COLUMN IF NOT EXISTS review_received_at TIMESTAMPTZ;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'appointments_sitting_fields_valid') THEN
        ALTER TABLE appointments ADD CONSTRAINT appointments_sitting_fields_valid CHECK (
            (sitting_number IS NULL OR sitting_number BETWEEN 1 AND 30)
            AND (sitting_notes IS NULL OR length(sitting_notes) <= 2000)
            AND (amount_collected_paise IS NULL OR amount_collected_paise >= 0)
            AND (review_rating IS NULL OR review_rating BETWEEN 1 AND 3)
        );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_appointments_treatment_plan
    ON appointments (treatment_plan_id, sitting_number)
    WHERE treatment_plan_id IS NOT NULL;

-- One live booking per sitting number. A rescheduled sitting is the old row
-- cancelled plus a new row with the same number, so cancelled rows are exempt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_plan_sitting_active
    ON appointments (treatment_plan_id, sitting_number)
    WHERE treatment_plan_id IS NOT NULL
      AND status IN ('confirmed', 'completed', 'pending_payment', 'pending_review');

-- ── 4. Doctor daily-schedule idempotency ───────────────────────────────────
CREATE TABLE IF NOT EXISTS dental_doctor_digests (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    doctor_id      UUID        NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    digest_date    DATE        NOT NULL,
    sittings_count INTEGER     NOT NULL DEFAULT 0 CHECK (sittings_count >= 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dental_doctor_digests_once UNIQUE (clinic_id, doctor_id, digest_date)
);

-- ── 5. Owner-set monthly message limits: atomic counters ───────────────────
CREATE TABLE IF NOT EXISTS clinic_message_quota_usage (
    clinic_id     UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    period_month  TEXT        NOT NULL CHECK (period_month ~ '^\d{4}-\d{2}$'),
    kind          TEXT        NOT NULL CHECK (kind IN ('patient', 'doctor', 'review')),
    used          INTEGER     NOT NULL DEFAULT 0 CHECK (used >= 0),
    warned_90     BOOLEAN     NOT NULL DEFAULT false,
    warned_100    BOOLEAN     NOT NULL DEFAULT false,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clinic_id, period_month, kind)
);

-- Reserve one message under the limit, atomically. Returns the new count, or
-- -1 when the limit is already reached (nothing is incremented). p_limit NULL
-- means unlimited. The conditional ON CONFLICT UPDATE is what stops two
-- workers racing for the last message from both getting it.
CREATE OR REPLACE FUNCTION reserve_message_quota(
    p_clinic_id UUID, p_month TEXT, p_kind TEXT, p_limit INTEGER
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    new_used INTEGER;
BEGIN
    IF p_limit IS NOT NULL AND p_limit <= 0 THEN
        RETURN -1;
    END IF;
    INSERT INTO clinic_message_quota_usage AS q (clinic_id, period_month, kind, used)
    VALUES (p_clinic_id, p_month, p_kind, 1)
    ON CONFLICT (clinic_id, period_month, kind) DO UPDATE
        SET used = q.used + 1, updated_at = now()
        WHERE p_limit IS NULL OR q.used < p_limit
    RETURNING used INTO new_used;
    RETURN COALESCE(new_used, -1);
END;
$$;

-- Give back a reservation whose send failed (Meta refused, network error).
CREATE OR REPLACE FUNCTION release_message_quota(
    p_clinic_id UUID, p_month TEXT, p_kind TEXT
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE clinic_message_quota_usage
       SET used = GREATEST(used - 1, 0), updated_at = now()
     WHERE clinic_id = p_clinic_id AND period_month = p_month AND kind = p_kind;
$$;

REVOKE ALL ON FUNCTION reserve_message_quota(UUID, TEXT, TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION release_message_quota(UUID, TEXT, TEXT) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION reserve_message_quota(UUID, TEXT, TEXT, INTEGER) TO service_role;
        GRANT EXECUTE ON FUNCTION release_message_quota(UUID, TEXT, TEXT) TO service_role;
    END IF;
END $$;

-- ── 6. RLS: same pattern as migration 088 ──────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dental_treatment_plans', 'dental_doctor_digests', 'clinic_message_quota_usage'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                       'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
            EXECUTE format(
                'CREATE POLICY %I ON %I FOR ALL TO kriya_app, authenticated, anon '
                'USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid) '
                'WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid)',
                'tenant_isolation_' || t, t);
        END IF;
    END LOOP;
END $$;
