-- ============================================================
-- Migration 033: Plan Tiers & Meta Pricing Configuration
--
-- Two tables that replace ALL hardcoded pricing constants:
--
--   plan_tiers         — Per-plan message quotas and subscription
--                        pricing. Owner-editable via dashboard.
--
--   meta_pricing_config — Meta WhatsApp Cloud API per-message
--                        cost rates (INR paise). Owner-only
--                        visibility. NEVER exposed to clinic APIs.
--
-- Design decisions:
--   • All monetary values are INTEGER paise (never floats).
--   • included_messages_month = 0 means unlimited (enterprise).
--   • meta_pricing_config is a singleton row (id='default')
--     with effective_from for audit trail.
--   • Overage behavior: SOFT-CAP — messages are never blocked.
--     The dashboard shows an overage badge + upgrade CTA.
-- ============================================================

-- ── Plan Tiers ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plan_tiers (
    id                      UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_name               TEXT    UNIQUE NOT NULL CHECK (plan_name IN (
        'soloclinic', 'diagstream', 'essential', 'polyclinic', 'enterprise'
    )),
    display_name            TEXT    NOT NULL,

    -- Subscription pricing (integer paise — ₹500 = 50000)
    monthly_price_paise     INTEGER NOT NULL DEFAULT 0,

    -- Message quotas (0 = unlimited for enterprise)
    included_messages_month INTEGER NOT NULL DEFAULT 500,

    -- Overage pricing (paise per message above quota; 0 = no charge)
    overage_price_paise     INTEGER NOT NULL DEFAULT 0,

    -- Lifecycle
    is_active               BOOLEAN     NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at on plan_tiers
CREATE OR REPLACE FUNCTION update_plan_tiers_updated_at()
RETURNS TRIGGER
SET search_path = ''
AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS plan_tiers_updated_at ON plan_tiers;
CREATE TRIGGER plan_tiers_updated_at
    BEFORE UPDATE ON plan_tiers
    FOR EACH ROW EXECUTE FUNCTION update_plan_tiers_updated_at();

-- Seed plan tiers with approved defaults
-- Q1 answers: 500 / 1000 / 2500 / 5000 / unlimited
INSERT INTO plan_tiers (plan_name, display_name, monthly_price_paise, included_messages_month, overage_price_paise)
VALUES
    ('soloclinic',  'Solo Clinic',   0, 500,  0),
    ('diagstream',  'DiagStream',    0, 1000, 0),
    ('essential',   'Essential',     0, 2500, 0),
    ('polyclinic',  'PolyClinic',    0, 5000, 0),
    ('enterprise',  'Enterprise',    0, 0,    0)   -- 0 = unlimited
ON CONFLICT (plan_name) DO NOTHING;

-- RLS
ALTER TABLE plan_tiers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access for service_role"
    ON plan_tiers FOR ALL TO service_role USING (true);


-- ── Meta Pricing Configuration ──────────────────────────────────────────────
-- Owner-only table. These rates are NEVER exposed to clinic-facing APIs.

CREATE TABLE IF NOT EXISTS meta_pricing_config (
    id                      TEXT        PRIMARY KEY DEFAULT 'default',

    -- Per-message cost in integer paise
    utility_paise           INTEGER     NOT NULL DEFAULT 12,    -- ₹0.12/message
    marketing_paise         INTEGER     NOT NULL DEFAULT 75,    -- ₹0.75/message
    authentication_paise    INTEGER     NOT NULL DEFAULT 10,    -- ₹0.10/message
    service_paise           INTEGER     NOT NULL DEFAULT 0,     -- free within 24h window

    currency                TEXT        NOT NULL DEFAULT 'INR',
    effective_from          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by              TEXT
);

-- Seed default Meta pricing
INSERT INTO meta_pricing_config (
    id, utility_paise, marketing_paise, authentication_paise, service_paise
) VALUES (
    'default', 12, 75, 10, 0
) ON CONFLICT (id) DO NOTHING;

-- RLS
ALTER TABLE meta_pricing_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Full access for service_role"
    ON meta_pricing_config FOR ALL TO service_role USING (true);
