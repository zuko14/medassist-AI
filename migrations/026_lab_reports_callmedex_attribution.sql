-- CallMedex wiring fix: lab_reports.external_report_id is queried by
-- app/integrations/callmedex/api/router.py's idempotency check, and
-- app/integrations/callmedex/workers/runner.py now inserts a row per
-- processed CallMedex report — both assumed this column already existed.
ALTER TABLE lab_reports ADD COLUMN IF NOT EXISTS external_report_id TEXT;
ALTER TABLE lab_reports ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'admin';

-- Prevents duplicate rows for the same CallMedex job on retry/re-delivery.
CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_reports_clinic_external_report
    ON lab_reports(clinic_id, external_report_id)
    WHERE external_report_id IS NOT NULL;
