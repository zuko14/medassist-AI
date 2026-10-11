-- ============================================================================
-- Migration 106: AI receptionist learning loop (harvest -> suggest -> approve)
-- ============================================================================
-- What patients said that the receptionist (voice or WhatsApp) did not
-- understand or could not answer is kept briefly, grouped, and turned into
-- SUGGESTIONS. Nothing learned is ever live until clinic staff approve it, and
-- an approval only writes a row into the existing voice_lexicon_entries /
-- voice_knowledge_entries tables: no prompt is ever rewritten.
--
--   ai_reception_harvest_events  one unhandled patient turn (PII-stripped text,
--                                masked phone). Purged after 30 days.
--   ai_learning_suggestions      one grouped phrase + proposed mapping, pending
--                                staff review. A dismissed phrase is never
--                                proposed again (unique per clinic/category/phrase).
--
-- Additive only.
-- ============================================================================

CREATE TABLE IF NOT EXISTS ai_reception_harvest_events (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    channel               TEXT        NOT NULL CHECK (channel IN ('voice', 'whatsapp')),
    call_ref              TEXT,
    conversation_id       UUID,
    patient_phone_masked  TEXT,
    raw_utterance         TEXT        NOT NULL CHECK (char_length(btrim(raw_utterance)) BETWEEN 1 AND 400),
    trigger_reason        TEXT        NOT NULL CHECK (trigger_reason IN ('low_confidence', 'reprompt', 'handoff',
                                                                         'unmapped_entity', 'unanswered_question')),
    detected_language     TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_harvest_clinic_created ON ai_reception_harvest_events (clinic_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_learning_suggestions (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    category              TEXT        NOT NULL CHECK (category IN ('specialty_synonym', 'doctor_alias', 'test_alias',
                                                                   'knowledge_faq')),
    patient_phrase        TEXT        NOT NULL CHECK (char_length(btrim(patient_phrase)) BETWEEN 1 AND 200),
    proposed_canonical    TEXT        NOT NULL DEFAULT '',
    evidence_count        INT         NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    sample_utterances     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    safety_screen_passed  BOOLEAN     NOT NULL DEFAULT true,
    status                TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'dismissed')),
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_suggestion_phrase
    ON ai_learning_suggestions (clinic_id, category, lower(btrim(patient_phrase)));
CREATE INDEX IF NOT EXISTS idx_learning_suggestions_pending
    ON ai_learning_suggestions (clinic_id, evidence_count DESC) WHERE status = 'pending';

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ai_reception_harvest_events', 'ai_learning_suggestions'] LOOP
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
    END LOOP;
END $$;
