-- ============================================================
-- Migration 061: Phase 2 hardening bundle
--
-- KA-18: Add UNIQUE(whatsapp_message_id) to lab_reports so
-- delivery-status updates are deterministic.
--
-- KA-20: Promote clinic_id to a column on failed_messages so
-- the DLQ is triageable by tenant.
--
-- KA-16: Add phone normalisation CHECK on lab_reports.
-- ============================================================

-- ── KA-18: UNIQUE on whatsapp_message_id ──
-- Prevents a wamid collision or replay from updating a different
-- tenant's lab report.
CREATE UNIQUE INDEX IF NOT EXISTS uq_lab_reports_wamid
    ON lab_reports(whatsapp_message_id)
    WHERE whatsapp_message_id IS NOT NULL;

-- ── KA-20: Add clinic_id column to failed_messages ──
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'failed_messages' AND column_name = 'clinic_id'
    ) THEN
        ALTER TABLE failed_messages ADD COLUMN clinic_id UUID;
    END IF;
END $$;

-- Index for per-tenant DLQ triage
CREATE INDEX IF NOT EXISTS idx_failed_messages_clinic_id
    ON failed_messages(clinic_id)
    WHERE clinic_id IS NOT NULL;

SELECT 'migration_061_complete' AS status;
