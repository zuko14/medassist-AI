-- ============================================================================
-- Migration 147: Continuous Care measure (CC-M3 / CC-28; DATA-MODEL.md CC-M3)
-- Materialised schedule items, prompts (every card / call attempt), append-only observations with correction
-- provenance (ADR-CC-12; guard trigger on this NEW table only), lab parameter → measure mappings, evaluations.
-- Existing objects touched: none. Rollback: rollback/147_rollback_cc_measure.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS cc_schedule_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    enrollment_id UUID NOT NULL,
    plan_item_id  UUID NOT NULL,
    due_at        TIMESTAMPTZ NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('medication', 'measure', 'lab', 'visit', 'education', 'procedure_session',
                                                'vaccine', 'anc_visit')),
    status        TEXT NOT NULL DEFAULT 'due' CHECK (status IN ('due', 'prompted', 'answered', 'missed', 'skipped')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_schedule_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_schedule_items_due_key UNIQUE (clinic_id, plan_item_id, due_at),
    CONSTRAINT cc_schedule_items_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_schedule_items_item_fk FOREIGN KEY (clinic_id, plan_item_id) REFERENCES cc_plan_items (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cc_schedule_due ON cc_schedule_items (clinic_id, status, due_at);

CREATE TABLE IF NOT EXISTS cc_prompts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    enrollment_id     UUID NOT NULL,
    schedule_item_ids UUID[] NOT NULL DEFAULT '{}',
    kind              TEXT NOT NULL CHECK (kind IN ('adherence', 'reading', 'phq9', 'gad7', 'lab_due', 'visit_due',
                                                    'consent_invite', 'visit_booked', 'service_ended', 'safety', 'symptoms')),
    measure_code      TEXT NULL CHECK (measure_code ~ '^[a-z0-9_]{2,20}$'),
    channel           TEXT NOT NULL CHECK (channel IN ('whatsapp_interactive', 'whatsapp_template', 'voice')),
    phone             TEXT NOT NULL CHECK (phone ~ '^\+?[0-9]{10,15}$'),
    template_name     TEXT NULL CHECK (char_length(template_name) <= 60),
    quota_reserved    BOOLEAN NOT NULL DEFAULT false,
    status            TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'delivered', 'read',
                                                                     'awaiting_value', 'answered', 'failed', 'expired',
                                                                     'unparsed')),
    failure_code      TEXT NULL CHECK (failure_code IN ('window_closed', 'template_unapproved', 'quota', 'budget',
                                                        'meta_error', 'opted_out', 'no_optin', 'no_answer')),
    state             JSONB NOT NULL DEFAULT '{}'::jsonb,            -- PHQ answers so far, re-ask count
    wa_message_id     TEXT NULL CHECK (char_length(wa_message_id) <= 200),
    voice_job_id      UUID NULL,
    sent_at           TIMESTAMPTZ NULL,
    answered_at       TIMESTAMPTZ NULL,
    expires_at        TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_prompts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_prompts_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_prompts_failed CHECK ((status = 'failed') = (failure_code IS NOT NULL))
);
-- the typed-reading claim's one indexed lookup (ADR-CC-09): open prompts for a phone
CREATE INDEX IF NOT EXISTS idx_cc_prompts_open ON cc_prompts (clinic_id, phone, status, sent_at DESC)
    WHERE status IN ('sent', 'delivered', 'read', 'awaiting_value');
CREATE INDEX IF NOT EXISTS idx_cc_prompts_enrollment ON cc_prompts (clinic_id, enrollment_id, created_at DESC);

CREATE TABLE IF NOT EXISTS cc_observations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    enrollment_id       UUID NOT NULL,
    prompt_id           UUID NULL,
    measure_code        TEXT NOT NULL CHECK (measure_code ~ '^[a-z0-9_]{2,20}$'),
    value_num           NUMERIC(8, 2) NULL,
    value_num2          NUMERIC(8, 2) NULL,
    value_code          TEXT NULL CHECK (char_length(value_code) <= 40),
    unit                TEXT NULL CHECK (unit IN ('mg/dL', 'mmol/L', 'mmHg', '%', 'kg', 'score', 'mIU/L', 'mmHg_iop')),
    observed_at         TIMESTAMPTZ NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('whatsapp_button', 'whatsapp_typed', 'voice_dtmf',
                                                        'voice_speech_confirmed', 'lab_result', 'coordinator_entered',
                                                        'clinic_device')),
    source_message_id   TEXT NULL CHECK (char_length(source_message_id) <= 200),
    lab_result_value_id UUID NULL,
    entered_by          UUID NULL,
    entered_by_name     TEXT NULL CHECK (char_length(entered_by_name) <= 100),
    corrects_id         UUID NULL,
    correction_reason   TEXT NULL CHECK (char_length(btrim(correction_reason)) BETWEEN 5 AND 300),
    plausible           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_observations_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_observations_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_observations_prompt_fk FOREIGN KEY (clinic_id, prompt_id) REFERENCES cc_prompts (clinic_id, id),
    CONSTRAINT cc_observations_corrects_fk FOREIGN KEY (clinic_id, corrects_id) REFERENCES cc_observations (clinic_id, id),
    CONSTRAINT cc_observations_correction CHECK ((corrects_id IS NULL) = (correction_reason IS NULL)),
    CONSTRAINT cc_observations_value CHECK (value_num IS NOT NULL OR value_code IS NOT NULL),
    CONSTRAINT cc_observations_staff CHECK (source <> 'coordinator_entered' OR entered_by_name IS NOT NULL)
);
-- the duplicate-webhook guard: one observation per WhatsApp message (two taps of "Taken" store once)
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_observations_message ON cc_observations (clinic_id, source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_observations_lab ON cc_observations (clinic_id, enrollment_id, lab_result_value_id)
    WHERE lab_result_value_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cc_observations_latest ON cc_observations (clinic_id, enrollment_id, measure_code, observed_at DESC);

DROP TRIGGER IF EXISTS trg_cc_observations_refs ON cc_observations;
CREATE TRIGGER trg_cc_observations_refs BEFORE INSERT ON cc_observations FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('lab_result_value_id', 'lab_result_values');

-- append-only (ADR-CC-12): the app never updates or deletes an observation; only the purge functions may
CREATE OR REPLACE FUNCTION cc_guard_observation() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF current_setting('kriya.cc_purge', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION 'cc_observation_append_only' USING ERRCODE = 'P0001';
END $$;
DROP TRIGGER IF EXISTS trg_cc_observations_guard ON cc_observations;
CREATE TRIGGER trg_cc_observations_guard BEFORE UPDATE OR DELETE ON cc_observations
    FOR EACH ROW EXECUTE FUNCTION cc_guard_observation();

CREATE TABLE IF NOT EXISTS cc_measure_sources (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    lab_test_id  UUID NOT NULL,
    parameter_id UUID NULL,
    parameter_name TEXT NULL CHECK (char_length(parameter_name) <= 120),
    measure_code TEXT NOT NULL CHECK (measure_code IN ('hba1c', 'fbg', 'ppbg', 'rbg', 'tsh', 'ldl', 'creatinine', 'alt',
                                                       'wbc', 'hb', 'beta_hcg', 'egfr', 'lipid_total')),
    unit_factor  NUMERIC(8, 4) NOT NULL DEFAULT 1 CHECK (unit_factor > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_measure_sources_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_measure_sources_key UNIQUE (clinic_id, lab_test_id, measure_code)
);

DROP TRIGGER IF EXISTS trg_cc_measure_sources_refs ON cc_measure_sources;
CREATE TRIGGER trg_cc_measure_sources_refs BEFORE INSERT OR UPDATE ON cc_measure_sources FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('lab_test_id', 'lab_tests', 'parameter_id', 'lab_test_parameters');

CREATE TABLE IF NOT EXISTS cc_evaluations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    enrollment_id    UUID NOT NULL,
    activation_id    UUID NOT NULL,
    protocol_version TEXT NOT NULL,
    tier             TEXT NOT NULL CHECK (tier IN ('stable', 'watch', 'urgent')),
    reasons          JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(reasons) = 'array'),
    proposed         JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(proposed) = 'array'),
    safety_card      TEXT NULL CHECK (safety_card ~ '^[a-z0-9_]{2,40}$'),
    observation_ids  UUID[] NOT NULL DEFAULT '{}',
    inputs_sha256    TEXT NOT NULL CHECK (inputs_sha256 ~ '^[0-9a-f]{64}$'),
    evaluated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_evaluations_clinic_id_id_key UNIQUE (clinic_id, id),
    -- a parallel run on the same inputs writes once (VERIFICATION §6)
    CONSTRAINT cc_evaluations_inputs_key UNIQUE (clinic_id, enrollment_id, inputs_sha256),
    CONSTRAINT cc_evaluations_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_evaluations_activation_fk FOREIGN KEY (clinic_id, activation_id) REFERENCES cc_protocol_activations (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_cc_evaluations_latest ON cc_evaluations (clinic_id, enrollment_id, evaluated_at DESC);

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cc_schedule_items', 'cc_prompts', 'cc_observations', 'cc_measure_sources', 'cc_evaluations']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;


DO $$
BEGIN
    IF to_regclass('public.cc_schedule_items') IS NULL OR to_regclass('public.cc_prompts') IS NULL
       OR to_regclass('public.cc_observations') IS NULL OR to_regclass('public.cc_measure_sources') IS NULL
       OR to_regclass('public.cc_evaluations') IS NULL THEN
        RAISE EXCEPTION '147 verify: cc measure tables missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cc_observations_guard') THEN
        RAISE EXCEPTION '147 verify: observation guard missing';
    END IF;
END $$;
