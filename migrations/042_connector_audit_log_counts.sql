ALTER TABLE connector_audit_log
    ADD COLUMN IF NOT EXISTS reports_matched       INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reports_needs_review  INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reports_delivered     INTEGER DEFAULT 0;
