-- ============================================================
-- Migration 036: Staff Role, Delegated Permissions & Branch Scoping
-- Additive & Non-Destructive Multi-Tenant RBAC Evolution
-- ============================================================

-- Add staff_role column (text label / preset name)
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS staff_role TEXT;

-- Add permissions array (defaults to empty array - backward compatible)
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS permissions TEXT[] NOT NULL DEFAULT '{}';

-- Add branch_id foreign key for branch-scoped staff (optional)
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE SET NULL;

-- Index for fast branch-level staff lookup
CREATE INDEX IF NOT EXISTS idx_clinic_admins_branch ON clinic_admins(branch_id);

-- Verify structure
SELECT id, username, role, staff_role, permissions, branch_id
FROM clinic_admins
ORDER BY created_at DESC;
