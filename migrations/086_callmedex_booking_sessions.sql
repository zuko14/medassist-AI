-- Conversation state for the CallMedex WhatsApp number's home-sample-collection
-- booking flow (app/integrations/callmedex/whatsapp/booking.py).
--
-- Deliberately NOT the clinic-scoped `conversations` table: the CallMedex number
-- belongs to no Kriya clinic, and every clinic-scoped table carries a
-- clinic_id FK. Durable (not in-process) so a patient's consecutive taps work
-- across all web workers. Rows are deleted on booking/cancel and purged after
-- a day of inactivity (DPDP: phone + address are personal data).
CREATE TABLE IF NOT EXISTS callmedex_booking_sessions (
    phone      TEXT PRIMARY KEY,
    state      TEXT NOT NULL DEFAULT 'menu',
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_callmedex_booking_sessions_updated_at
    ON callmedex_booking_sessions(updated_at);

-- Backend-only (service role), same pattern as callmedex_whatsapp_settings (027).
ALTER TABLE callmedex_booking_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE callmedex_booking_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_callmedex_booking_sessions" ON callmedex_booking_sessions;
CREATE POLICY "service_role_all_callmedex_booking_sessions" ON callmedex_booking_sessions
    FOR ALL TO service_role USING (true) WITH CHECK (true);
