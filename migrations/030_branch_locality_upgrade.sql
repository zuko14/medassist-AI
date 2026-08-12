-- ============================================================
-- Migration 030: Branch Locality Upgrade
-- Drops the unique constraint on (clinic_id, name) that blocks
-- Polyclinics from having same-named branches at different
-- locations. Backfills short_name for existing branches.
-- ============================================================

-- ── Step 1: Drop the unique index on (clinic_id, name) ──
-- This is the root cause of the "Branch already exists" error
-- when a Polyclinic tries to add a second branch with the same
-- parent organization name.
DROP INDEX IF EXISTS idx_branches_clinic_name;


-- ── Step 2: Backfill short_name for existing branches ──
-- Any branch that was created before this migration might have
-- short_name = NULL. Set it to the branch name so the WhatsApp
-- chatbot and Admin UI have a locality to display.
UPDATE branches
SET short_name = name
WHERE short_name IS NULL
   OR short_name = '';


-- ── Step 3: Add composite index for fast chatbot queries ──
-- The WhatsApp bot queries: WHERE clinic_id = :id AND is_active = true
-- ORDER BY display_order ASC
CREATE INDEX IF NOT EXISTS idx_branches_tenant_active_order
    ON branches(clinic_id, is_active, display_order);
