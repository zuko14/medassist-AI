-- Migration 024: Persist orphan webhook security events (no booking_id)
-- Run in Supabase SQL Editor
--
-- _log_payment_event_raw() previously skipped the payment_events insert
-- entirely for orphan events (booking_id=None) — the exact case for
-- signature-verification failures, i.e. the most security-relevant event
-- type — falling back to app logs only, which are weaker for forensic
-- replay after a suspected attack (rotation, retention, no structured query).

CREATE TABLE IF NOT EXISTS webhook_security_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    event_type TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_security_events_created_at
    ON webhook_security_events (created_at DESC);

-- Verify
SELECT table_name FROM information_schema.tables
WHERE table_name = 'webhook_security_events';
