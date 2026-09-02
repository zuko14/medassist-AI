-- ============================================================
-- Migration 068: Subscription lifecycle + daily report limits
--
-- Adds three things:
--   1. Fixed-tier daily report limits on clinics.
--   2. A 30-day prepaid subscription window with a 5-day grace
--      period, expressed as dates. Status is DERIVED from those
--      dates in app/services/subscription.py; the stored
--      subscription_status column is the sticky floor (an owner
--      can suspend early, and a renewal is the only thing that
--      lifts it).
--   3. Per-calendar-day (Asia/Kolkata) outbound counters.
--
-- BACKWARD COMPATIBILITY
--   Existing clinics get subscription_start_date = the moment
--   this migration runs, NOT their created_at. Backdating to
--   created_at would suspend every live tenant the second this
--   deploys. Every existing clinic therefore begins with a full
--   30-day window and the owner re-dates them from the console.
-- ============================================================

ALTER TABLE clinics
ADD COLUMN IF NOT EXISTS daily_report_limit INTEGER NOT NULL DEFAULT 100
    CHECK (daily_report_limit IN (0, 50, 100, 200, 300, 500)),
ADD COLUMN IF NOT EXISTS subscription_start_date TIMESTAMPTZ NOT NULL DEFAULT now(),
ADD COLUMN IF NOT EXISTS subscription_end_date TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
ADD COLUMN IF NOT EXISTS grace_period_days INTEGER NOT NULL DEFAULT 5,
ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT 'active'
    CHECK (subscription_status IN ('active', 'grace_period', 'suspended', 'trial')),
ADD COLUMN IF NOT EXISTS last_renewed_at TIMESTAMPTZ;

COMMENT ON COLUMN clinics.daily_report_limit IS
    'Reports dispatchable per Asia/Kolkata calendar day. 0 = unlimited (enterprise).';
COMMENT ON COLUMN clinics.subscription_status IS
    'Sticky floor only. The effective status is computed from the dates by '
    'app/services/subscription.compute_subscription_state().';

-- ── Daily usage tracking ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinic_daily_usage (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    usage_date               DATE        NOT NULL DEFAULT CURRENT_DATE,
    reports_delivered_count  INTEGER     NOT NULL DEFAULT 0,
    prescriptions_sent_count INTEGER     NOT NULL DEFAULT 0,
    reminders_sent_count     INTEGER     NOT NULL DEFAULT 0,
    followups_sent_count     INTEGER     NOT NULL DEFAULT 0,
    total_outbound_count     INTEGER     NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(clinic_id, usage_date)
);

CREATE INDEX IF NOT EXISTS idx_cdu_clinic_date ON clinic_daily_usage(clinic_id, usage_date);

ALTER TABLE clinic_daily_usage ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'clinic_daily_usage'
          AND policyname = 'Full access for service_role'
    ) THEN
        CREATE POLICY "Full access for service_role"
            ON clinic_daily_usage FOR ALL TO service_role USING (true);
    END IF;
END $$;

-- ── Atomic counter increment ────────────────────────────────────────────────
-- One statement, one round-trip, no read-modify-write race between the four
-- production processes. Counters are passed as separate typed parameters
-- rather than a column name, so no caller can inject a column reference.
CREATE OR REPLACE FUNCTION increment_clinic_daily_usage(
    p_clinic_id      UUID,
    p_usage_date     DATE,
    p_reports        INTEGER DEFAULT 0,
    p_prescriptions  INTEGER DEFAULT 0,
    p_reminders      INTEGER DEFAULT 0,
    p_followups      INTEGER DEFAULT 0,
    p_total          INTEGER DEFAULT 0
)
RETURNS clinic_daily_usage
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    result clinic_daily_usage;
BEGIN
    INSERT INTO clinic_daily_usage (
        clinic_id, usage_date,
        reports_delivered_count, prescriptions_sent_count,
        reminders_sent_count, followups_sent_count, total_outbound_count
    )
    VALUES (
        p_clinic_id, p_usage_date,
        GREATEST(p_reports, 0), GREATEST(p_prescriptions, 0),
        GREATEST(p_reminders, 0), GREATEST(p_followups, 0), GREATEST(p_total, 0)
    )
    ON CONFLICT (clinic_id, usage_date) DO UPDATE SET
        reports_delivered_count  = clinic_daily_usage.reports_delivered_count  + GREATEST(p_reports, 0),
        prescriptions_sent_count = clinic_daily_usage.prescriptions_sent_count + GREATEST(p_prescriptions, 0),
        reminders_sent_count     = clinic_daily_usage.reminders_sent_count     + GREATEST(p_reminders, 0),
        followups_sent_count     = clinic_daily_usage.followups_sent_count     + GREATEST(p_followups, 0),
        total_outbound_count     = clinic_daily_usage.total_outbound_count     + GREATEST(p_total, 0),
        updated_at               = now()
    RETURNING * INTO result;

    RETURN result;
END;
$$;

-- Restrict execution to backend service_role only (blocks public PostgREST RPC access)
REVOKE EXECUTE ON FUNCTION increment_clinic_daily_usage(UUID, DATE, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION increment_clinic_daily_usage(UUID, DATE, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER) TO service_role;

-- ── Audit feed index ────────────────────────────────────────────────────────
-- The owner audit feed filters the ledger by clinic + source_service over a
-- window; without this it is a full scan of an append-only table.
CREATE INDEX IF NOT EXISTS idx_oml_clinic_source_sent
    ON outbound_message_ledger(clinic_id, source_service, sent_at DESC);

-- ── Record Migration ────────────────────────────────────────────────────────
-- Recorded by scripts/migrate.py

