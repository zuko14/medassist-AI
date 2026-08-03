-- =====================================================================
-- CallMedex Integration Subsystem: Supabase SQL Migration Script
-- Version: 1.0.0
-- Applies to: Phases 0 through 9 of CallMedex MediAssist Integration
-- =====================================================================

-- 1. Create Enums for Job Status & Checkpoints
DO $$ BEGIN
    CREATE TYPE callmedex_job_status AS ENUM ('pending', 'processing', 'completed', 'failed', 'retrying');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE callmedex_checkpoint_stage AS ENUM (
        'CHECKPOINT_1_CREATED',
        'CHECKPOINT_2_AUTHENTICATED',
        'CHECKPOINT_3_BARCODE_LOCATED',
        'CHECKPOINT_4_REPORT_LOCATED',
        'CHECKPOINT_5_PDF_DOWNLOADED',
        'CHECKPOINT_6_VALIDATED',
        'CHECKPOINT_7_CALLBACK_SENT'
    );
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;

-- 2. CallMedex Connector Configurations Table (Per-Clinic EMR Credentials)
CREATE TABLE IF NOT EXISTS callmedex_connector_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id TEXT UNIQUE NOT NULL,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    emr_base_url TEXT NOT NULL,
    encrypted_credentials TEXT NOT NULL,
    capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. CallMedex Report Jobs Table (Core Job Execution & Stage Recovery Storage)
CREATE TABLE IF NOT EXISTS callmedex_report_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id TEXT UNIQUE NOT NULL,
    clinic_id TEXT NOT NULL,
    connector_type TEXT NOT NULL DEFAULT 'mocdoc',
    external_report_id TEXT NOT NULL, -- Accession Barcode (e.g. 260700009225)
    patient_phone TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    patient_mrn TEXT,
    report_name TEXT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'Laboratory',
    
    -- Execution & Recovery Tracking
    status callmedex_job_status NOT NULL DEFAULT 'pending',
    current_checkpoint callmedex_checkpoint_stage NOT NULL DEFAULT 'CHECKPOINT_1_CREATED',
    retry_count INT NOT NULL DEFAULT 0,
    error_message TEXT,
    
    -- Phase 4.5 & Phase 5 Storage Outputs
    pdf_storage_path TEXT,
    pdf_sha256 TEXT,
    canonical_json JSONB, -- Canonical OCR Lab Report JSON (Phase 5)
    
    -- Phase 6 AI Summary Outputs
    clinical_reasoning JSONB, -- Layer 1 Output (Phase 6)
    multi_audience_summary JSONB, -- Layer 2 Output with Provenance (Phase 6)
    
    -- Phase 7 & 8 WhatsApp & Webhook Callback Outputs
    whatsapp_status TEXT DEFAULT 'pending', -- pending, sent, delivered, failed
    whatsapp_message_id TEXT,
    callback_delivered BOOLEAN NOT NULL DEFAULT false,
    
    -- Tracing & Timestamps
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. CallMedex Audit Logs Table (Immutable Execution Event Ledger)
CREATE TABLE IF NOT EXISTS callmedex_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID REFERENCES callmedex_report_jobs(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    checkpoint callmedex_checkpoint_stage NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Create Performance & Lookup Indexes
CREATE INDEX IF NOT EXISTS idx_callmedex_jobs_task_id ON callmedex_report_jobs(task_id);
CREATE INDEX IF NOT EXISTS idx_callmedex_jobs_clinic_status ON callmedex_report_jobs(clinic_id, status);
CREATE INDEX IF NOT EXISTS idx_callmedex_jobs_barcode ON callmedex_report_jobs(external_report_id);
CREATE INDEX IF NOT EXISTS idx_callmedex_audit_task_id ON callmedex_audit_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_callmedex_audit_correlation ON callmedex_audit_logs(correlation_id);

-- 6. Automatic Updated_At Trigger Function (with Explicit Search Path Security)
CREATE OR REPLACE FUNCTION update_callmedex_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
SET search_path = public, pg_temp;


DROP TRIGGER IF EXISTS trigger_update_callmedex_jobs_timestamp ON callmedex_report_jobs;
CREATE TRIGGER trigger_update_callmedex_jobs_timestamp
    BEFORE UPDATE ON callmedex_report_jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_callmedex_timestamp();

DROP TRIGGER IF EXISTS trigger_update_callmedex_configs_timestamp ON callmedex_connector_configs;
CREATE TRIGGER trigger_update_callmedex_configs_timestamp
    BEFORE UPDATE ON callmedex_connector_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_callmedex_timestamp();

-- 7. Row Level Security (RLS) Configuration
ALTER TABLE callmedex_connector_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE callmedex_report_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE callmedex_audit_logs ENABLE ROW LEVEL SECURITY;

-- Allow service_role (backend worker API) full unrestricted access
DROP POLICY IF EXISTS "Service role full access on callmedex_connector_configs" ON callmedex_connector_configs;
CREATE POLICY "Service role full access on callmedex_connector_configs"
    ON callmedex_connector_configs FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access on callmedex_report_jobs" ON callmedex_report_jobs;
CREATE POLICY "Service role full access on callmedex_report_jobs"
    ON callmedex_report_jobs FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Service role full access on callmedex_audit_logs" ON callmedex_audit_logs;
CREATE POLICY "Service role full access on callmedex_audit_logs"
    ON callmedex_audit_logs FOR ALL TO service_role USING (true) WITH CHECK (true);
