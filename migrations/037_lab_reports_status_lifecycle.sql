-- ============================================================================
-- Migration 037: Diagnostic Center Report Lifecycle, Patient Matching & Runner Lock
-- ============================================================================

-- 1. Extend lab_reports with matching metadata and lifecycle tracking
ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS match_confidence NUMERIC,
    ADD COLUMN IF NOT EXISTS match_source TEXT,
    ADD COLUMN IF NOT EXISTS matched_patient_id UUID REFERENCES patients(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolved_by TEXT;

-- 2. Indexes for diagnostic center triage and queue filtering
CREATE INDEX IF NOT EXISTS idx_lab_reports_clinic_status
    ON lab_reports(clinic_id, status);

CREATE INDEX IF NOT EXISTS idx_lab_reports_status_created
    ON lab_reports(status, uploaded_at DESC);

-- 3. Runner distributed advisory lock columns on integration_connectors
ALTER TABLE integration_connectors
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS locked_by TEXT;

CREATE INDEX IF NOT EXISTS idx_connectors_lock
    ON integration_connectors(locked_at);
