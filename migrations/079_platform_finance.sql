-- ============================================================================
-- Migration 079: platform owner financial tracking (the Kriya AI books)
-- ============================================================================
-- The owner dashboard could already read what the platform COSTS (Meta
-- per-message spend, from outbound_message_ledger) but had nowhere to record
-- what it EARNS. `plan_tiers.monthly_price_paise` existed but was display-only
-- and sat at 0 for every tier, and the "Platform Revenue" tile was in fact
-- reading appointments.amount_paise — patients paying clinics, not clinics
-- paying Kriya. This migration adds the missing half of the ledger.
--
-- THREE TABLES, ALL NEW:
--   platform_billing_rates  what each clinic is actually charged (overrides
--                           the plan default, for early/negotiated deals)
--   platform_expenses       Render, Supabase, AI APIs, domains — recurring
--                           and one-off, versioned so history stays honest
--   platform_invoices       per clinic per month: billed vs collected
--
-- WHY A SEPARATE RATES TABLE INSTEAD OF A COLUMN ON `clinics`
-- `clinics` is read on the hot path of every inbound WhatsApp message and is
-- cached per tenant. Widening it for an owner-only billing concern would put
-- financial data into a row the bot loads thousands of times a day and would
-- force a cache-shape change. A side table joined only by owner endpoints
-- costs the message path nothing.
--
-- HOW HISTORY STAYS CORRECT WHEN PRICES CHANGE (the owner will raise prices)
--   Rates    — current state only. History lives in the invoice SNAPSHOT:
--              platform_invoices stores rate_paise/locations_billed/plan as
--              they were the moment the invoice was generated, so raising the
--              polyclinic price in November cannot rewrite September's bill.
--   Expenses — versioned in place. Editing a recurring amount CLOSES the old
--              row (effective_to) and INSERTS a new one, so a month's P&L is
--              always computed from the rows that were live in that month.
--
-- PURELY ADDITIVE: no existing table is altered, no existing row is updated,
-- no CHECK constraint is touched. Nothing outside the owner dashboard reads
-- these tables. Re-runnable.
-- ============================================================================

-- Fail fast rather than queueing behind a long transaction while live
-- bookings wait on the clinics lock (same guard as 077/078).
SET LOCAL lock_timeout = '5s';

-- ── 1. Per-clinic billing rate override ─────────────────────────────────────
-- ABSENCE IS MEANINGFUL: no row for a clinic means "charge the plan default"
-- (plan_tiers.monthly_price_paise). A row means the owner negotiated
-- something specific with that clinic, and it must survive plan price rises.
CREATE TABLE IF NOT EXISTS platform_billing_rates (
    clinic_id       UUID        PRIMARY KEY REFERENCES clinics(id) ON DELETE CASCADE,

    -- Integer paise, matching plan_tiers and meta_pricing_config. ₹8,000 = 800000.
    rate_paise      INTEGER     NOT NULL CHECK (rate_paise >= 0),

    -- 'per_location' multiplies by the clinic's billable (active) branch count.
    -- 'flat' charges rate_paise regardless of how many branches it opens —
    -- some deals are struck as one all-in number.
    billing_mode    TEXT        NOT NULL DEFAULT 'per_location'
                                CHECK (billing_mode IN ('per_location', 'flat')),

    notes           TEXT,
    effective_from  DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT
);

COMMENT ON TABLE platform_billing_rates IS
    'Owner-only. Per-clinic subscription rate override; no row = plan_tiers default.';
COMMENT ON COLUMN platform_billing_rates.rate_paise IS
    'Integer paise. Interpreted per-location or flat per billing_mode.';

ALTER TABLE platform_billing_rates ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY "Full access for service_role"
        ON platform_billing_rates FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ── 2. Platform operating expenses ──────────────────────────────────────────
-- Recurring rows (is_recurring = true) apply to every month from
-- effective_from until effective_to (NULL = still running). One-off rows
-- (is_recurring = false) pin to a single 'YYYY-MM' in `month`.
--
-- Meta WhatsApp spend is deliberately NOT stored here: it is computed live
-- from outbound_message_ledger x meta_pricing_config, and duplicating it as a
-- manual row would double-count it in the P&L.
CREATE TABLE IF NOT EXISTS platform_expenses (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL CHECK (length(trim(name)) > 0),
    category        TEXT        NOT NULL DEFAULT 'other'
                                CHECK (category IN (
                                    'hosting', 'database', 'ai', 'messaging',
                                    'domain', 'payment', 'people', 'other'
                                )),
    amount_paise    INTEGER     NOT NULL CHECK (amount_paise >= 0),

    is_recurring    BOOLEAN     NOT NULL DEFAULT true,

    -- One-off only: the 'YYYY-MM' the cost lands in. NULL for recurring rows.
    month           TEXT        CHECK (month IS NULL OR month ~ '^\d{4}-\d{2}$'),

    -- Recurring window. A superseded row keeps its history and is closed off
    -- rather than overwritten, so past months keep costing what they cost.
    effective_from  DATE        NOT NULL DEFAULT CURRENT_DATE,
    effective_to    DATE,

    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT,

    -- A recurring row is dated by its window; a one-off row is dated by month.
    CONSTRAINT platform_expenses_shape CHECK (
        (is_recurring = true  AND month IS NULL) OR
        (is_recurring = false AND month IS NOT NULL)
    ),
    CONSTRAINT platform_expenses_window CHECK (
        effective_to IS NULL OR effective_to >= effective_from
    )
);

CREATE INDEX IF NOT EXISTS idx_platform_expenses_recurring
    ON platform_expenses (effective_from, effective_to) WHERE is_recurring = true;
CREATE INDEX IF NOT EXISTS idx_platform_expenses_month
    ON platform_expenses (month) WHERE is_recurring = false;

COMMENT ON TABLE platform_expenses IS
    'Owner-only. Platform liabilities. Meta message spend is computed live, never stored here.';

ALTER TABLE platform_expenses ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY "Full access for service_role"
        ON platform_expenses FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ── 3. Monthly invoices: billed vs collected ────────────────────────────────
-- Generated on demand by the owner, idempotent per (clinic, month). The
-- snapshot columns are what make this table the historical record: they are
-- written once at generation and never recomputed.
CREATE TABLE IF NOT EXISTS platform_invoices (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    period_month        TEXT        NOT NULL CHECK (period_month ~ '^\d{4}-\d{2}$'),

    -- Snapshot of how the amount was arrived at, frozen at generation time.
    plan                TEXT,
    rate_paise          INTEGER     NOT NULL CHECK (rate_paise >= 0),
    billing_mode        TEXT        NOT NULL DEFAULT 'per_location',
    locations_billed    INTEGER     NOT NULL DEFAULT 1 CHECK (locations_billed >= 0),
    amount_paise        INTEGER     NOT NULL CHECK (amount_paise >= 0),

    -- Collections.
    status              TEXT        NOT NULL DEFAULT 'unpaid'
                                    CHECK (status IN ('unpaid', 'partial', 'paid', 'waived')),
    amount_paid_paise   INTEGER     NOT NULL DEFAULT 0 CHECK (amount_paid_paise >= 0),
    paid_at             DATE,
    payment_note        TEXT,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          TEXT,

    -- One invoice per clinic per month. This is what makes generation
    -- idempotent: a second "Generate" click conflicts instead of duplicating.
    CONSTRAINT platform_invoices_clinic_month_key UNIQUE (clinic_id, period_month)
);

CREATE INDEX IF NOT EXISTS idx_platform_invoices_month
    ON platform_invoices (period_month);
CREATE INDEX IF NOT EXISTS idx_platform_invoices_status
    ON platform_invoices (status) WHERE status <> 'paid';

COMMENT ON TABLE platform_invoices IS
    'Owner-only. What each clinic was billed per month and what was actually collected.';
COMMENT ON COLUMN platform_invoices.rate_paise IS
    'Snapshot at generation. Never recomputed — a later price rise must not rewrite an old invoice.';

ALTER TABLE platform_invoices ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
    CREATE POLICY "Full access for service_role"
        ON platform_invoices FOR ALL TO service_role USING (true);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- ── 4. Seed the expense rows the owner already pays ─────────────────────────
-- Amounts are 0: the dashboard shows them as rows waiting for a real number
-- rather than inventing a figure. The owner edits each one in place.
-- Guarded by NOT EXISTS (rather than ON CONFLICT) because `name` is
-- deliberately not unique — a superseded row and its replacement share it.
INSERT INTO platform_expenses (name, category, amount_paise, is_recurring, notes)
SELECT v.name, v.category, 0, true, v.notes
FROM (VALUES
    ('Render — web service',       'hosting',  'Application hosting'),
    ('Supabase',                   'database', 'Postgres, storage and auth'),
    ('AI API (Groq / OpenRouter)', 'ai',       'Conversation and summarisation calls'),
    ('Domain & DNS',               'domain',   'Annual cost entered as a monthly share')
) AS v(name, category, notes)
WHERE NOT EXISTS (
    SELECT 1 FROM platform_expenses e WHERE e.name = v.name AND e.is_recurring = true
);


-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'migration_079_complete' AS status;
