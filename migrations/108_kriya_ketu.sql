-- ============================================================================
-- Migration 108: Kriya Ketu staff assistant run log (KK-P1-01)
-- ============================================================================
-- One row per staff instruction to Kriya Ketu: what was asked, how it was
-- routed, the preview the user confirmed, per-step results and the outcome.
-- Domain writes still happen only through the existing /admin endpoints and
-- their own audit rows; this table holds assistant metadata only.
--
--   input_text / transcript_original may name patients (PHI): tenant-scoped,
--   never logged, purged after 90 days (DataRetentionService.purge_assistant_runs).
--   Speech audio is never stored.
--
-- Additive only. Rollback: migrations/rollback/108_down.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assistant_runs (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    user_id             UUID,                    -- clinic_admins.id; NULL for the env super_admin
    username            TEXT        NOT NULL,
    role                TEXT        NOT NULL,
    client_request_id   UUID        NOT NULL,
    input_mode          TEXT        NOT NULL CHECK (input_mode IN ('text', 'voice')),
    input_text          TEXT        NOT NULL CHECK (length(input_text) <= 1000),
    transcript_original TEXT        CHECK (transcript_original IS NULL OR length(transcript_original) <= 1000),
    language            TEXT,
    route_layer         TEXT        CHECK (route_layer IN ('nav', 'rules', 'form', 'llm', 'none')),
    action_id           TEXT,
    params              JSONB       NOT NULL DEFAULT '{}',
    preview             JSONB,
    plan_hash           TEXT,
    state               TEXT        NOT NULL,
    steps               JSONB       NOT NULL DEFAULT '[]',
    result              JSONB,
    error_code          TEXT,
    llm_tokens          INT         NOT NULL DEFAULT 0,
    llm_cost_paise      INT         NOT NULL DEFAULT 0,
    route_ms            INT,
    ip_address          TEXT,
    expires_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, username, client_request_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_clinic_created ON assistant_runs (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_active ON assistant_runs (clinic_id, username)
    WHERE state IN ('NEEDS_CLARIFICATION', 'NEEDS_CONFIRMATION', 'EXECUTING', 'VERIFYING', 'UNKNOWN_OUTCOME');

DO $$
DECLARE t TEXT := 'assistant_runs';
BEGIN
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                       'service_role_all_' || t, t);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR ALL TO kriya_app, authenticated, anon '
            'USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid) '
            'WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid)',
            'tenant_isolation_' || t, t);
    END IF;
END $$;
