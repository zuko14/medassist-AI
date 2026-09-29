-- ============================================================================
-- Migration 096: Corporate partner accounts (dashboard-only labs)
-- ============================================================================
-- A partner lab (e.g. Taiyo Labs) that wants ONLY the Corporate Health
-- dashboards — no WhatsApp number, no Meta registration, no plan. The platform
-- owner creates it from Owner panel -> Corporate Health, uploads its companies'
-- reports and hands each company a login.
--
-- Every corporate table, and every login, is scoped by clinics.id (095), so a
-- partner is still a clinics row — but marked account_type = 'corporate_partner'
-- so the owner's tenant listings (leaderboard, overview, subscriptions,
-- finance / invoices, usage, data storage, broadcasts) skip it and it is never
-- billed, counted or messaged as a hospital.
--
-- Additive: one column with a constant default (no table rewrite) — every
-- existing row becomes 'tenant', i.e. unchanged. The second CHECK makes it
-- impossible for a partner row to carry a Meta phone_number_id, so it can never
-- be chosen by webhook tenant routing.
-- ============================================================================

ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'tenant';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinics_account_type_check') THEN
        ALTER TABLE clinics ADD CONSTRAINT clinics_account_type_check
            CHECK (account_type IN ('tenant', 'corporate_partner'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinics_partner_not_routable') THEN
        ALTER TABLE clinics ADD CONSTRAINT clinics_partner_not_routable
            CHECK (account_type = 'tenant' OR phone_number_id IS NULL);
    END IF;
END $$;

COMMENT ON COLUMN clinics.account_type IS
    'tenant = a WhatsApp client on a plan (default). corporate_partner = a dashboard-only lab '
    'for Corporate Health (096): no WhatsApp, excluded from owner tenant listings and billing.';
