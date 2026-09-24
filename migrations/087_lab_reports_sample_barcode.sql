-- Cross-intake deduplication between CallMedex report jobs and a processing
-- centre's own EMR connector (Accumx: CallMedex processing centre AND direct
-- MocDoc client). The two intakes use unrelated report ids (CallMedex
-- report_job_id vs MocDoc "{VAMID}_{ReportNo}"); the specimen barcode / MocDoc
-- SampleID is the one identifier both can carry. Nullable, additive: every
-- existing row and intake path is unaffected.
ALTER TABLE lab_reports ADD COLUMN IF NOT EXISTS sample_barcode TEXT;

CREATE INDEX IF NOT EXISTS idx_lab_reports_clinic_sample_barcode
    ON lab_reports(clinic_id, sample_barcode)
    WHERE sample_barcode IS NOT NULL;
