-- Migration 012: Add connector_failed_reports table for per-report failure tracking

CREATE TABLE IF NOT EXISTS connector_failed_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE NOT NULL,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    external_report_id TEXT NOT NULL,
    vam_id TEXT,
    patient_name TEXT,
    failure_count INT NOT NULL DEFAULT 1,
    last_error TEXT,
    first_failed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    CONSTRAINT unique_clinic_connector_report UNIQUE (clinic_id, connector_type, external_report_id)
);

CREATE INDEX IF NOT EXISTS idx_connector_failed_reports_clinic ON connector_failed_reports(clinic_id, connector_type);
CREATE INDEX IF NOT EXISTS idx_connector_failed_reports_unresolved ON connector_failed_reports(clinic_id, resolved_at) WHERE resolved_at IS NULL;

-- RLS Policy: Locked to service_role only
ALTER TABLE connector_failed_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role access for connector_failed_reports" ON connector_failed_reports;
CREATE POLICY "Service role access for connector_failed_reports" ON connector_failed_reports
    FOR ALL TO service_role USING (true) WITH CHECK (true);
