-- Meta delivery receipts, per report. The outbound_message_ledger is
-- append-only by design (migration 032), so receipts land here instead.
ALTER TABLE lab_reports
    ADD COLUMN IF NOT EXISTS whatsapp_message_id  TEXT,
    ADD COLUMN IF NOT EXISTS delivery_status      TEXT,   -- sent|delivered|read|failed
    ADD COLUMN IF NOT EXISTS delivery_error       TEXT,
    ADD COLUMN IF NOT EXISTS delivery_updated_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_lab_reports_wamid
    ON lab_reports(whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL;
