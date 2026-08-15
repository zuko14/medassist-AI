-- ============================================================
-- Migration 032: Outbound Message Ledger
--
-- Append-only, per-message accounting table for every WhatsApp
-- message sent by the platform. One row = one API call to Meta.
--
-- This is the SINGLE SOURCE OF TRUTH for all usage dashboards
-- and billing reconciliation. Never updated — only INSERTed.
--
-- Design decisions:
--   • clinic_id is NOT NULL — every outbound message must be
--     attributable to a tenant. If resolution fails, the send
--     itself would have failed upstream.
--   • meta_message_id is nullable — populated from Meta API
--     response on success, NULL on send failure.
--   • category defaults to 'utility' — the most common class
--     for healthcare appointment flows.
--   • source_service identifies which code path originated the
--     send for debugging and per-service usage breakdowns.
--   • mark_read is tracked as a message_type so the ledger is
--     a complete record of ALL Meta API calls, but it will be
--     excluded from billable counts by the accounting service.
-- ============================================================

CREATE TABLE IF NOT EXISTS outbound_message_ledger (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,

    -- Meta response fields
    meta_message_id TEXT,                       -- wamid from Meta response; NULL if send failed

    -- Recipient
    recipient_phone TEXT        NOT NULL,

    -- Message classification
    message_type    TEXT        NOT NULL CHECK (message_type IN (
        'text', 'template', 'interactive_buttons', 'interactive_list',
        'document', 'location', 'image', 'mark_read'
    )),
    template_name   TEXT,                       -- populated only for template messages
    category        TEXT        NOT NULL DEFAULT 'utility' CHECK (category IN (
        'utility', 'marketing', 'authentication', 'service'
    )),

    -- Delivery status
    direction       TEXT        NOT NULL DEFAULT 'outbound',
    send_success    BOOLEAN     NOT NULL DEFAULT true,

    -- Source attribution (which service originated the send)
    source_service  TEXT        NOT NULL,

    -- Timestamps
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Indexes ─────────────────────────────────────────────────────────────────
-- Primary dashboard query: "all messages for clinic X in date range"
CREATE INDEX IF NOT EXISTS idx_oml_clinic_sent
    ON outbound_message_ledger(clinic_id, sent_at);

-- Category breakdown query: "utility vs marketing for clinic X in month"
CREATE INDEX IF NOT EXISTS idx_oml_clinic_cat_sent
    ON outbound_message_ledger(clinic_id, category, sent_at);

-- Duplicate-detection / reconciliation lookup by Meta message ID
CREATE INDEX IF NOT EXISTS idx_oml_meta_msg_id
    ON outbound_message_ledger(meta_message_id)
    WHERE meta_message_id IS NOT NULL;

-- ── Row Level Security ──────────────────────────────────────────────────────
ALTER TABLE outbound_message_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access for service_role"
    ON outbound_message_ledger FOR ALL TO service_role USING (true);
