-- ============================================================================
-- Migration 009: Integration Connectors (MocDoc, Practo, etc.)
-- ============================================================================

-- 1. Connector configuration per clinic
CREATE TABLE IF NOT EXISTS integration_connectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    is_enabled BOOLEAN DEFAULT FALSE,
    config JSONB NOT NULL DEFAULT '{}',
    -- config stores (password encrypted at app layer with Fernet):
    -- {
    --   "username": "...",
    --   "password_encrypted": "...",
    --   "clinic_slug": "visakha-multispeciality-clinics",
    --   "base_url": "https://mocdoc.com",
    --   "poll_interval_minutes": 10,
    --   "admin_alert_phone": "+91XXXXXXXXXX"
    -- }
    last_run_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_clinic_type
    ON integration_connectors(clinic_id, connector_type);

-- 2. Idempotency: track which reports have been processed
CREATE TABLE IF NOT EXISTS integration_processed_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    external_report_id TEXT NOT NULL,    -- e.g. "VAM-39927_29220" (VAMID_ReportNo)
    patient_phone TEXT,
    patient_name TEXT,
    report_name TEXT,
    lab_report_id UUID REFERENCES lab_reports(id),
    processed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_unique
    ON integration_processed_reports(clinic_id, connector_type, external_report_id);

-- 3. Audit log for connector runs (metadata-only, no PHI)
CREATE TABLE IF NOT EXISTS connector_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    run_status TEXT NOT NULL,  -- 'success', 'partial', 'failed'
    reports_found INTEGER DEFAULT 0,
    reports_new INTEGER DEFAULT 0,
    reports_uploaded INTEGER DEFAULT 0,
    reports_failed INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_clinic
    ON connector_audit_log(clinic_id, created_at DESC);

-- RLS Policies
ALTER TABLE integration_connectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_processed_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all_connectors" ON integration_connectors
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all_processed" ON integration_processed_reports
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "service_role_all_audit" ON connector_audit_log
    FOR ALL TO service_role USING (true) WITH CHECK (true);
