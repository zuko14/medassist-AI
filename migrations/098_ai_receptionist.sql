-- ============================================================================
-- Migration 098: AI voice receptionist (owner opt-in per clinic)
-- ============================================================================
-- Additive only. No existing table or column changes. Nothing here does
-- anything until the platform owner enables clinics.features.ai_receptionist
-- for a clinic AND maps at least one Exotel number to it in voice_numbers.
--
--   voice_numbers          Exotel virtual number (Exophone) -> clinic/branch.
--                          The hospital keeps its published number and forwards
--                          it to the Exophone; tenant is resolved ONLY from here.
--   voice_calls            one row per call (inbound or outbound), dialog state,
--                          outcome, versions, usage and cost.
--   voice_call_events      ordered timeline: caller turns, agent turns, NLU,
--                          tool calls with verification proof, safety, handoff.
--   voice_lexicon_entries  per-clinic synonyms, doctor aliases, test aliases,
--                          pronunciations.
--   voice_outbound_jobs    queued outbound calls (lead follow-up, callbacks).
-- ============================================================================

CREATE TABLE IF NOT EXISTS voice_numbers (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    branch_id         UUID        REFERENCES branches(id) ON DELETE CASCADE,
    exophone          TEXT        NOT NULL CHECK (exophone ~ '^\+[1-9][0-9]{7,14}$'),
    published_number  TEXT,
    reception_number  TEXT,
    label             TEXT,
    is_active         BOOLEAN     NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One Exophone can never route to two tenants.
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_numbers_exophone ON voice_numbers (exophone);
CREATE INDEX IF NOT EXISTS idx_voice_numbers_clinic ON voice_numbers (clinic_id);

CREATE TABLE IF NOT EXISTS voice_calls (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    branch_id           UUID        REFERENCES branches(id) ON DELETE SET NULL,
    call_ref            TEXT        NOT NULL,
    provider            TEXT        NOT NULL DEFAULT 'exotel',
    provider_call_sid   TEXT,
    stream_sid          TEXT,
    direction           TEXT        NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    mode                TEXT        NOT NULL DEFAULT 'live' CHECK (mode IN ('live', 'shadow', 'test')),
    caller_phone        TEXT        NOT NULL,
    exophone            TEXT,
    patient_id          UUID        REFERENCES patients(id) ON DELETE SET NULL,
    caller_relation     TEXT        NOT NULL DEFAULT 'UNKNOWN',
    language            TEXT        NOT NULL DEFAULT 'te-IN',
    status              TEXT        NOT NULL DEFAULT 'in_progress'
                        CHECK (status IN ('queued', 'ringing', 'in_progress', 'completed', 'handed_off',
                                          'failed', 'dropped', 'no_answer')),
    automation_paused   BOOLEAN     NOT NULL DEFAULT false,
    takeover_requested_at TIMESTAMPTZ,
    primary_intent      TEXT,
    intents             TEXT[]      NOT NULL DEFAULT '{}',
    outcome             TEXT,
    handoff_reason      TEXT,
    handoff_packet      JSONB,
    dialog              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    quality             JSONB,
    versions            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    stt_seconds         NUMERIC(10, 2) NOT NULL DEFAULT 0,
    tts_chars           INTEGER     NOT NULL DEFAULT 0,
    llm_tokens          INTEGER     NOT NULL DEFAULT 0,
    telephony_seconds   INTEGER     NOT NULL DEFAULT 0,
    cost_paise          INTEGER     NOT NULL DEFAULT 0,
    outbound_job_id     UUID,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- target of the composite FK on voice_call_events (tenant consistency in the DB)
    UNIQUE (id, clinic_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_calls_ref ON voice_calls (call_ref);
-- A provider retry of the same stream can never create a second call row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_calls_provider_sid
    ON voice_calls (provider, provider_call_sid) WHERE provider_call_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_voice_calls_clinic_started ON voice_calls (clinic_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_voice_calls_clinic_status ON voice_calls (clinic_id, status);

CREATE TABLE IF NOT EXISTS voice_call_events (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clinic_id       UUID        NOT NULL,
    call_id         UUID        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT        NOT NULL CHECK (kind IN ('turn_user', 'turn_agent', 'nlu', 'tool', 'system',
                                                         'error', 'handoff', 'safety', 'latency')),
    name            TEXT        NOT NULL,
    status          TEXT        CHECK (status IS NULL OR status IN ('ok', 'fail', 'skipped', 'pending')),
    text            TEXT,
    data            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    duration_ms     INTEGER,
    correlation_id  TEXT,
    FOREIGN KEY (call_id, clinic_id) REFERENCES voice_calls (id, clinic_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_voice_events_call ON voice_call_events (call_id, id);
CREATE INDEX IF NOT EXISTS idx_voice_events_clinic_ts ON voice_call_events (clinic_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_voice_events_clinic_kind ON voice_call_events (clinic_id, kind, name);

CREATE TABLE IF NOT EXISTS voice_lexicon_entries (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL CHECK (kind IN ('specialty_synonym', 'doctor_alias', 'test_alias',
                                                     'pronunciation')),
    phrase      TEXT        NOT NULL CHECK (char_length(btrim(phrase)) BETWEEN 1 AND 80),
    canonical   TEXT        NOT NULL CHECK (char_length(btrim(canonical)) BETWEEN 1 AND 120),
    language    TEXT,
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_lexicon_phrase
    ON voice_lexicon_entries (clinic_id, kind, lower(btrim(phrase)));

CREATE TABLE IF NOT EXISTS voice_outbound_jobs (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    patient_phone    TEXT        NOT NULL,
    purpose          TEXT        NOT NULL CHECK (purpose IN ('lead_followup', 'callback_request')),
    status           TEXT        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'dialing', 'completed', 'failed', 'cancelled', 'skipped')),
    attempts         SMALLINT    NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    context          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_error       TEXT,
    call_id          UUID        REFERENCES voice_calls(id) ON DELETE SET NULL,
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- At most one live job per person and purpose: double clicks and the hourly
-- auto-queue cannot call the same lead twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_outbound_active
    ON voice_outbound_jobs (clinic_id, patient_phone, purpose) WHERE status IN ('queued', 'dialing');
CREATE INDEX IF NOT EXISTS idx_voice_outbound_due ON voice_outbound_jobs (status, next_attempt_at);

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['voice_numbers', 'voice_calls', 'voice_call_events',
                             'voice_lexicon_entries', 'voice_outbound_jobs'] LOOP
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
