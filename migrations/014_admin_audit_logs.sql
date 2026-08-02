-- Migration 014: Add admin_audit_logs table for individual staff identity audit tracking (NABH / DPDP Compliance)

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE,
    user_id UUID REFERENCES clinic_admins(id) ON DELETE SET NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_clinic_id ON admin_audit_logs(clinic_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_username ON admin_audit_logs(username);
CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at ON admin_audit_logs(created_at DESC);

-- Enable RLS (Service role access only)
ALTER TABLE admin_audit_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role access for admin_audit_logs" ON admin_audit_logs;
CREATE POLICY "Service role access for admin_audit_logs" ON admin_audit_logs
    FOR ALL TO service_role USING (true) WITH CHECK (true);
