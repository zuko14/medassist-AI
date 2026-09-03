-- ============================================================
-- Migration 071: Production Readiness & Row Level Security Hardening
--
-- 1. Enforces FORCE ROW LEVEL SECURITY across remaining tenant-scoped tables:
--    - integration_processed_reports
--    - clinic_daily_usage
--    - admin_sessions
-- 2. Establishes tenant isolation policies for non-service_role callers.
-- 3. Idempotent index and constraint hardening.
-- ============================================================

-- 1. Force Row Level Security on integration_processed_reports
ALTER TABLE IF EXISTS integration_processed_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS integration_processed_reports FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'integration_processed_reports'
          AND policyname = 'tenant_isolation_processed_reports'
    ) THEN
        CREATE POLICY "tenant_isolation_processed_reports" ON integration_processed_reports
            FOR ALL TO kriya_app, authenticated, anon
            USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);
    END IF;
END $$;

-- 2. Force Row Level Security on clinic_daily_usage
ALTER TABLE IF EXISTS clinic_daily_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS clinic_daily_usage FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'clinic_daily_usage'
          AND policyname = 'tenant_isolation_clinic_daily_usage'
    ) THEN
        CREATE POLICY "tenant_isolation_clinic_daily_usage" ON clinic_daily_usage
            FOR ALL TO kriya_app, authenticated, anon
            USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);
    END IF;
END $$;

-- 3. Force Row Level Security on admin_sessions
ALTER TABLE IF EXISTS admin_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS admin_sessions FORCE ROW LEVEL SECURITY;

-- 4. Idempotent composite index on clinic_daily_usage
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_daily_usage_clinic_date
    ON clinic_daily_usage(clinic_id, usage_date);

-- 5. Additional index on integration_processed_reports
CREATE INDEX IF NOT EXISTS idx_processed_reports_clinic_processed
    ON integration_processed_reports(clinic_id, processed_at DESC);
