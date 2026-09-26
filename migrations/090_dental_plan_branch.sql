-- ============================================================================
-- Migration 090: Branch on dental treatment plans
-- ============================================================================
-- A dental chain pins front-desk staff to one branch. Without a branch on the
-- plan, a pinned receptionist saw and could act on every branch's plans.
-- Same convention as appointments (app.database.restrict_to_branch): a pinned
-- staff account sees its own branch's plans plus branch-less (clinic-wide)
-- plans; clinic admins see everything. NULLable, no default: every existing
-- plan stays clinic-wide and single-location clinics are unaffected.
--
-- PURELY ADDITIVE. Re-runnable. Safe with the previous build still running.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

ALTER TABLE dental_treatment_plans
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_dental_plans_clinic_branch
    ON dental_treatment_plans (clinic_id, branch_id);
