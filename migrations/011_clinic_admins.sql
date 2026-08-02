-- Migration 011: Add clinic_admins table for RBAC and tenant isolation

CREATE TABLE IF NOT EXISTS clinic_admins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE, -- NULL for platform-level super admins
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'clinic_admin', -- 'super_admin', 'clinic_admin', 'staff'
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clinic_admins_username ON clinic_admins(username);
CREATE INDEX IF NOT EXISTS idx_clinic_admins_clinic_id ON clinic_admins(clinic_id);

-- RLS Policy: Locked to service_role only
ALTER TABLE clinic_admins ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "Service role access for clinic_admins" ON clinic_admins;
CREATE POLICY "Service role access for clinic_admins" ON clinic_admins
    FOR ALL TO service_role USING (true) WITH CHECK (true);
