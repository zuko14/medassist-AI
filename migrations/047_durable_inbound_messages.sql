-- ============================================================
-- Migration 047: Durable Inbound Message Processing Queue & DLQ
-- ============================================================

CREATE TABLE IF NOT EXISTS inbound_messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      TEXT        UNIQUE NOT NULL, -- WhatsApp wamid
    clinic_id       UUID        REFERENCES clinics(id) ON DELETE SET NULL,
    phone           TEXT        NOT NULL,
    display_phone   TEXT,
    phone_number_id TEXT,
    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT        NOT NULL DEFAULT 'received'
                                CHECK (status IN ('received', 'processing', 'completed', 'failed_retryable', 'dead_letter')),
    attempt_count   INT         NOT NULL DEFAULT 0,
    locked_at       TIMESTAMPTZ,
    retry_at        TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inbound_messages_status_retry
    ON inbound_messages(status, retry_at)
    WHERE status IN ('received', 'failed_retryable');

CREATE INDEX IF NOT EXISTS idx_inbound_messages_locked_at
    ON inbound_messages(status, locked_at)
    WHERE status = 'processing';

CREATE INDEX IF NOT EXISTS idx_inbound_messages_clinic_id
    ON inbound_messages(clinic_id);

-- Enable RLS
ALTER TABLE inbound_messages ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies 
        WHERE tablename = 'inbound_messages' AND policyname = 'Full access for service_role'
    ) THEN
        CREATE POLICY "Full access for service_role" ON inbound_messages FOR ALL TO service_role USING (true);
    END IF;
END
$$;
