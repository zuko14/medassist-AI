-- ============================================================================
-- Migration 084: AI Features Infrastructure — Usage Ledger
-- ============================================================================
-- Centralized accounting for platform AI spend across both live WhatsApp chat
-- and administrative operations (catalogue extraction, quality cleanup, test
-- detail drafts, and weekly operational summaries).
--
-- Money is tracked in integer paise (1 INR = 100 paise) converted from
-- OpenRouter real cost usage using the configured USD->INR exchange rate.
--
-- PURELY ADDITIVE. Re-runnable. Safe with old build running.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS ai_usage_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openrouter',
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_paise INTEGER NOT NULL DEFAULT 0,
    is_fallback BOOLEAN NOT NULL DEFAULT false,
    success BOOLEAN NOT NULL DEFAULT true,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_ledger_clinic_created
    ON ai_usage_ledger(clinic_id, created_at)
    WHERE clinic_id IS NOT NULL;

ALTER TABLE ai_usage_ledger ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'ai_usage_ledger' AND policyname = 'service_role_all_ai_usage_ledger'
    ) THEN
        CREATE POLICY "service_role_all_ai_usage_ledger" ON ai_usage_ledger
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;
