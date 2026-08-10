-- ============================================================================
-- Migration 025: Branch-scoped connectors + tenant-isolation cleanup
--
-- Diagnostic centers (diagstream/polyclinic) can have multiple branches,
-- each with its own MocDoc/HMIS credentials (separate software login per
-- branch). branch_id is nullable — NULL means "clinic-level connector"
-- (today's single-branch behavior, unchanged).
-- ============================================================================

ALTER TABLE integration_connectors
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE CASCADE;

ALTER TABLE integration_processed_reports
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE SET NULL;

ALTER TABLE connector_audit_log
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE SET NULL;

ALTER TABLE connector_failed_reports
    ADD COLUMN IF NOT EXISTS branch_id UUID REFERENCES branches(id) ON DELETE SET NULL;

-- Replace the old clinic+type unique index (one connector per clinic, ever)
-- with two partial indexes: one row per clinic when there's no branch, one
-- row per (clinic, branch) when there is one.
DROP INDEX IF EXISTS idx_connectors_clinic_type;

CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_clinic_type_no_branch
    ON integration_connectors(clinic_id, connector_type) WHERE branch_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_clinic_type_branch
    ON integration_connectors(clinic_id, connector_type, branch_id) WHERE branch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_connectors_branch ON integration_connectors(branch_id);
CREATE INDEX IF NOT EXISTS idx_audit_branch ON connector_audit_log(branch_id);
CREATE INDEX IF NOT EXISTS idx_failed_reports_branch ON connector_failed_reports(branch_id);

-- connector_failed_reports' old "one row per report ever" uniqueness needs
-- to also key on branch, otherwise two branches processing the same
-- external_report_id (unlikely but not impossible with shared HMIS numbering)
-- would collide.
ALTER TABLE connector_failed_reports DROP CONSTRAINT IF EXISTS unique_clinic_connector_report;
CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_reports_unique_no_branch
    ON connector_failed_reports(clinic_id, connector_type, external_report_id) WHERE branch_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_failed_reports_unique_branch
    ON connector_failed_reports(clinic_id, connector_type, external_report_id, branch_id) WHERE branch_id IS NOT NULL;
