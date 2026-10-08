-- ============================================================================
-- Migration 102: AI receptionist knowledge base (clinic-written Q&A)
-- ============================================================================
-- The receptionist answers callers' questions about the hospital from this
-- clinic's own records (treatments, doctors, fees, timings). This table holds
-- what is not in any record: parking, insurance, EMI, offers, policies...
-- written by the clinic. An answer here is spoken word for word when a caller
-- asks that question; it is also part of the facts the grounded answerer may
-- use. Nothing here changes behaviour until a clinic adds an entry.
--
-- Additive only.
-- ============================================================================

CREATE TABLE IF NOT EXISTS voice_knowledge_entries (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    question    TEXT        NOT NULL CHECK (char_length(btrim(question)) BETWEEN 3 AND 200),
    answer      TEXT        NOT NULL CHECK (char_length(btrim(answer)) BETWEEN 2 AND 600),
    -- te / hi / en, or NULL = the answer may be spoken in any call language.
    language    TEXT        CHECK (language IS NULL OR language IN ('te', 'hi', 'en')),
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_voice_knowledge_clinic ON voice_knowledge_entries (clinic_id) WHERE is_active;

DO $$
BEGIN
    EXECUTE 'ALTER TABLE voice_knowledge_entries ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE voice_knowledge_entries FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS service_role_all_voice_knowledge_entries ON voice_knowledge_entries';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'CREATE POLICY service_role_all_voice_knowledge_entries ON voice_knowledge_entries '
                'FOR ALL TO service_role USING (true) WITH CHECK (true)';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        EXECUTE 'DROP POLICY IF EXISTS tenant_isolation_voice_knowledge_entries ON voice_knowledge_entries';
        EXECUTE 'CREATE POLICY tenant_isolation_voice_knowledge_entries ON voice_knowledge_entries '
                'FOR ALL TO kriya_app, authenticated, anon '
                'USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid) '
                'WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid)';
    END IF;
END $$;
