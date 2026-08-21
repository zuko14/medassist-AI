-- Migration 040: Link lab reports to the booking they fulfill
--
-- Best-effort link from an incoming report to the open lab-test booking it
-- fulfills. This is additive to PatientMatchService's existing safety gate,
-- not a replacement — delivery safety still depends solely on the existing
-- phone+name match logic in patient_match.py, unchanged.

ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS matched_booking_id UUID REFERENCES appointments(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_lab_reports_matched_booking ON lab_reports(matched_booking_id) WHERE matched_booking_id IS NOT NULL;
