-- Single global WhatsApp identity used to deliver CallMedex-booked reports
-- (app/integrations/callmedex/whatsapp/service.py), editable from the owner
-- platform instead of being fixed at deploy time via CALLMEDEX_WHATSAPP_*
-- env vars. Single row enforced by the fixed 'default' primary key.
CREATE TABLE IF NOT EXISTS callmedex_whatsapp_settings (
    id TEXT PRIMARY KEY DEFAULT 'default' CHECK (id = 'default'),
    phone_number_id TEXT,
    api_token_encrypted TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    updated_by TEXT
);

-- Backend-only table (owner platform + CallMedex send path use the service
-- role key) — same service_role-only RLS pattern as every other backend
-- table in this schema, e.g. integration_connectors in migration 009.
ALTER TABLE callmedex_whatsapp_settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all_callmedex_whatsapp_settings" ON callmedex_whatsapp_settings
    FOR ALL TO service_role USING (true) WITH CHECK (true);
