-- ============================================================================
-- Migration 062: Track whether the AI summary actually REACHED the patient
--
-- lab_reports.ai_summary records that a summary was GENERATED. Outside the
-- 24h customer-service window the report goes out as a document template
-- carrying only {patient_name, report_name}, so the summary is stored but
-- never delivered. The admin panel read the stored text as proof of delivery
-- and showed a ✅/⚠️ for reports the patient received with no summary at all.
--
-- ai_summary_sent is the honest signal: TRUE only when the summary text was
-- actually dispatched to the patient.
-- ============================================================================

ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS ai_summary_sent BOOLEAN NOT NULL DEFAULT FALSE;

-- Historical rows: freeform (in-window) sends did deliver the summary, but we
-- cannot distinguish them retroactively. Leaving the backfill at FALSE keeps
-- the column honest — it never claims a delivery we cannot evidence.

CREATE INDEX IF NOT EXISTS idx_lab_reports_summary_not_sent
    ON lab_reports(clinic_id, uploaded_at DESC)
    WHERE ai_summary_sent = FALSE;

-- ── Record migration ──
-- NOTE: schema_migrations is written by scripts/migrate.py:124 with the
-- file's SHA256. checksum is NOT NULL (scripts/migrate.py:60), so a
-- self-INSERT here omits it and aborts the migration on any fresh
-- database. Migrations must not record themselves.

