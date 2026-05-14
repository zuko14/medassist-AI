-- ============================================================
-- Migration 005: Processed Messages for Idempotency
-- ============================================================

CREATE TABLE IF NOT EXISTS processed_messages (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id  TEXT        UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enable RLS
ALTER TABLE processed_messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access for service_role" ON processed_messages FOR ALL TO service_role USING (true);
