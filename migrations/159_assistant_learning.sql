-- ============================================================================
-- Migration 159: Kriya Ketu learning (phrases staff use that the rules did not know).
-- ============================================================================
-- A phrase the LLM fallback mapped to an action, that staff then confirmed and completed (assistant_runs
-- route_layer = 'llm', state = 'COMPLETED_VERIFIED'), at least MIN_EVIDENCE times, is suggested to the clinic
-- admin (app/assistant/learning.py; computed on demand, nothing stored until a decision). An approved phrase
-- becomes an extra trigger for that clinic, tried only after the built-in rules found nothing, through the same
-- fill / policy / preview / confirmation path. A dismissed phrase is never suggested again.
--
-- No PHI: the phrase is a 2–5 word n-gram shared by several instructions, with digits removed; the admin reads it
-- before approving. Additive only. Rollback: migrations/rollback/159_rollback_assistant_learning.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS assistant_learned_phrases (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    phrase      TEXT        NOT NULL CHECK (length(phrase) BETWEEN 3 AND 80 AND phrase !~ '[0-9]'),
    action_id   TEXT        NOT NULL CHECK (action_id ~ '^[a-z_]+\.[a-z_]+$'),
    status      TEXT        NOT NULL CHECK (status IN ('active', 'dismissed')),
    evidence    INT         NOT NULL DEFAULT 0 CHECK (evidence >= 0),
    decided_by  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, phrase, action_id)
);
CREATE INDEX IF NOT EXISTS idx_assistant_learned_active ON assistant_learned_phrases (clinic_id) WHERE status = 'active';

ALTER TABLE assistant_learned_phrases ENABLE ROW LEVEL SECURITY;
ALTER TABLE assistant_learned_phrases FORCE ROW LEVEL SECURITY;
DO $$
BEGIN
    DROP POLICY IF EXISTS service_role_all_assistant_learned_phrases ON assistant_learned_phrases;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_assistant_learned_phrases ON assistant_learned_phrases FOR ALL
            TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE assistant_learned_phrases FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE assistant_learned_phrases FROM authenticated;
    END IF;
END $$;

-- ── 159 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'assistant_learned_phrases') THEN
        RAISE EXCEPTION '159 verify: assistant_learned_phrases missing';
    END IF;
END $$;
