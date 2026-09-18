-- ============================================================================
-- Migration 085: AI Features Phase 2 — Price-List Import & Weekly Summaries
-- ============================================================================
-- 1. catalogue_import_previews: Stages uploaded price lists (CSV, XLSX, PDF, Images).
--    All rows are held for 24 hours until approved and applied by an authorized admin.
-- 2. weekly_insights_summaries: Caches clinic-wide weekly operational summaries
--    per clinic per ISO week with rate-limited daily regenerations.

CREATE TABLE IF NOT EXISTS catalogue_import_previews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    branch_id UUID REFERENCES branches(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'processing' CHECK (status IN ('processing', 'pending', 'applying', 'applied', 'failed', 'expired')),
    rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL,
    failure_reason TEXT,
    applied_counts JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '24 hours'),
    applied_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_catalogue_import_previews_clinic
    ON catalogue_import_previews(clinic_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_catalogue_import_previews_expiry
    ON catalogue_import_previews(expires_at)
    WHERE status IN ('processing', 'pending');

CREATE TABLE IF NOT EXISTS weekly_insights_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    iso_year INT NOT NULL,
    iso_week INT NOT NULL,
    fact_sheet JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ai' CHECK (source IN ('ai', 'template')),
    regenerate_date DATE,
    regenerate_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_weekly_insights_clinic_week UNIQUE(clinic_id, iso_year, iso_week)
);

CREATE INDEX IF NOT EXISTS idx_weekly_insights_summaries_clinic
    ON weekly_insights_summaries(clinic_id, iso_year DESC, iso_week DESC);
