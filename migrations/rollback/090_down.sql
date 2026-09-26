-- Rollback Migration 090: branch on dental treatment plans (plans become clinic-wide again).
DROP INDEX IF EXISTS idx_dental_plans_clinic_branch;
ALTER TABLE dental_treatment_plans DROP COLUMN IF EXISTS branch_id;
