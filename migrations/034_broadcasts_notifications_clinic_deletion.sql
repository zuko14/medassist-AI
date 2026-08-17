-- ============================================================
-- Migration 034: Broadcasts, Admin Notifications & Safe Clinic Deletion
-- Multi-Tenant Broadcast Messaging & Lifecycle Management
-- ============================================================

-- 1. Broadcasts Table (Platform Owner Dispatches)
CREATE TABLE IF NOT EXISTS broadcasts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_id TEXT NOT NULL DEFAULT 'platform_owner',
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    target_type VARCHAR(50) NOT NULL CHECK (target_type IN ('ALL', 'SELECTIVE', 'SINGLE')),
    target_clinic_ids JSONB DEFAULT '[]'::jsonb,
    recipient_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Admin Notifications Table (In-App Tenant Alerts)
CREATE TABLE IF NOT EXISTS admin_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    broadcast_id UUID REFERENCES broadcasts(id) ON DELETE SET NULL,
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    admin_id UUID REFERENCES clinic_admins(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for performance and isolation
CREATE INDEX IF NOT EXISTS idx_admin_notif_unread ON admin_notifications(admin_id, is_read);
CREATE INDEX IF NOT EXISTS idx_admin_notif_clinic ON admin_notifications(clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_admin_notif_broadcast ON admin_notifications(broadcast_id);

-- 3. Soft-Delete and Status Columns for Clinics Table
ALTER TABLE clinics 
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS status VARCHAR(50) NOT NULL DEFAULT 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_clinics_status ON clinics(status, deleted_at);

-- 4. Enable RLS on newly created tables
ALTER TABLE broadcasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Full access for service_role on broadcasts" 
    ON broadcasts FOR ALL TO service_role USING (true);

CREATE POLICY "Full access for service_role on admin_notifications" 
    ON admin_notifications FOR ALL TO service_role USING (true);
