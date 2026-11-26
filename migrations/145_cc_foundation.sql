-- ============================================================================
-- Migration 145: Kriya Continuous Care foundation (lane CC, CC-M1 / CC-02; docs/continuous-care/DATA-MODEL.md)
-- clinics.cc_state / cc_settings; signed protocol activations; DPDP care consents (WhatsApp opt-in + purpose consent);
-- clinic-approved safety / education card texts (versioned); the two additive CHECK widens (voice purpose
-- 'care_checkin', quota kind 'care'); cc_purge_clinic (deletes every cc_* table that exists, in FK order — later CC
-- migrations add tables to its list by redefining nothing: the list below already names them).
--
-- Existing objects touched: clinics (+2 columns, defaults), voice_outbound_jobs.purpose CHECK (+care_checkin),
-- clinic_message_quota_usage.kind CHECK (+care). No trigger on any existing table (ADR-CC-13).
-- Rollback: rollback/145_rollback_cc_foundation.sql (refuses while a new CHECK value is in use).
-- ============================================================================

ALTER TABLE clinics ADD COLUMN IF NOT EXISTS cc_state TEXT NOT NULL DEFAULT 'OFF';
ALTER TABLE clinics ADD COLUMN IF NOT EXISTS cc_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinics_cc_state_check') THEN
        ALTER TABLE clinics ADD CONSTRAINT clinics_cc_state_check CHECK (cc_state IN ('OFF', 'ACTIVE', 'WIND_DOWN'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinics_cc_settings_object') THEN
        ALTER TABLE clinics ADD CONSTRAINT clinics_cc_settings_object CHECK (jsonb_typeof(cc_settings) = 'object');
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_clinics_cc_on ON clinics (id) WHERE cc_state <> 'OFF';

-- ── Widen voice purpose and quota kind (112 / 089 pattern: find inline checks by definition) ──
DO $$
DECLARE c TEXT;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'voice_outbound_jobs'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%purpose%' LOOP
        EXECUTE format('ALTER TABLE voice_outbound_jobs DROP CONSTRAINT %I', c);
    END LOOP;
    ALTER TABLE voice_outbound_jobs ADD CONSTRAINT voice_outbound_jobs_purpose_check
        CHECK (purpose IN ('lead_followup', 'callback_request', 'appointment_reminder', 'care_checkin'));
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'clinic_message_quota_usage'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%kind%' LOOP
        EXECUTE format('ALTER TABLE clinic_message_quota_usage DROP CONSTRAINT %I', c);
    END LOOP;
    ALTER TABLE clinic_message_quota_usage ADD CONSTRAINT clinic_message_quota_usage_kind_check
        CHECK (kind IN ('patient', 'doctor', 'review', 'care'));
END $$;
CREATE INDEX IF NOT EXISTS idx_voice_jobs_care ON voice_outbound_jobs (clinic_id, status, next_attempt_at)
    WHERE purpose = 'care_checkin';

-- ── Protocol activations: a clinic's doctor-signed use of a pack version (ADR-CC-06) ──
CREATE TABLE IF NOT EXISTS cc_protocol_activations (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    protocol_code      TEXT NOT NULL CHECK (protocol_code ~ '^[a-z0-9_]{2,40}$'),
    version            TEXT NOT NULL CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    params             JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(params) = 'object'),
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
    step_protocol_text TEXT NULL CHECK (char_length(step_protocol_text) <= 1000),   -- clinic's own step text (htn)
    created_by         UUID NULL,
    created_by_name    TEXT NULL CHECK (char_length(created_by_name) <= 100),
    signed_by_admin_id UUID NULL,
    signer_snapshot    JSONB NULL,
    signed_at          TIMESTAMPTZ NULL,
    retired_at         TIMESTAMPTZ NULL,
    retired_reason     TEXT NULL CHECK (char_length(btrim(retired_reason)) BETWEEN 3 AND 300),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_protocol_activations_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_protocol_activations_signed CHECK (
        status = 'draft' OR (signed_at IS NOT NULL AND signed_by_admin_id IS NOT NULL
                             AND signer_snapshot ? 'registration_number'
                             AND char_length(btrim(signer_snapshot ->> 'registration_number')) >= 3)),
    CONSTRAINT cc_protocol_activations_retired CHECK ((status = 'retired') = (retired_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_activation_active ON cc_protocol_activations (clinic_id, protocol_code)
    WHERE status = 'active';

-- ── Care consents (ADR-CC-11): WhatsApp opt-in first, then the DPDP 'continuous_care' purpose ──
CREATE TABLE IF NOT EXISTS cc_consents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id        UUID NOT NULL,
    family_member_id  UUID NULL,
    wa_optin_at       TIMESTAMPTZ NULL,
    wa_optin_method   TEXT NULL CHECK (wa_optin_method IN ('in_person', 'phone_call', 'whatsapp_reply')),
    wa_optin_by       UUID NULL,
    notice_version    TEXT NULL CHECK (notice_version ~ '^cc-notice-[0-9]+$'),
    notice_sha256     TEXT NULL CHECK (notice_sha256 ~ '^[0-9a-f]{64}$'),
    language          TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'hi', 'te')),
    method            TEXT NULL CHECK (method IN ('whatsapp_button', 'voice_dtmf', 'counter_signed')),
    captured_by       UUID NULL,
    captured_by_name  TEXT NULL CHECK (char_length(captured_by_name) <= 100),
    guardian_relation TEXT NULL CHECK (char_length(btrim(guardian_relation)) BETWEEN 2 AND 40),
    granted_at        TIMESTAMPTZ NULL,
    withdrawn_at      TIMESTAMPTZ NULL,
    withdrawn_via     TEXT NULL CHECK (withdrawn_via IN ('whatsapp_button', 'stop_keyword', 'voice', 'counter', 'erasure')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_consents_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_consents_optin_shape CHECK ((wa_optin_at IS NULL) = (wa_optin_method IS NULL)),
    -- a granted purpose consent records exactly which notice was shown and how it was given
    CONSTRAINT cc_consents_granted_shape CHECK (granted_at IS NULL OR (notice_version IS NOT NULL
                                               AND notice_sha256 IS NOT NULL AND method IS NOT NULL)),
    CONSTRAINT cc_consents_withdrawn_shape CHECK ((withdrawn_at IS NULL) = (withdrawn_via IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_consent_live ON cc_consents
    (clinic_id, patient_id, COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE withdrawn_at IS NULL;

-- ── Same-clinic guard for references into pre-existing tables ──────────────
-- New lanes do not put FOREIGN KEYs on tables owned by earlier migrations (103 onwards): an FK would make those
-- migrations' rollbacks fail (DROP TABLE … depends on it). Instead every reference column of a new table is checked
-- by this guard (a trigger on the NEW table only — ADR-CC-13): the row must exist AND belong to the same clinic, so a
-- cross-tenant reference still fails inside Postgres. TG_ARGV: column, table, column, table, …
-- (kriya_guard_refs is created by migration 144.)
DROP TRIGGER IF EXISTS trg_cc_consents_refs ON cc_consents;
CREATE TRIGGER trg_cc_consents_refs BEFORE INSERT OR UPDATE ON cc_consents FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');

-- ── Clinic-approved card texts (PROTOCOLS §4): one approved row per (key, language) is live ──
CREATE TABLE IF NOT EXISTS cc_cards (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    card_key         TEXT NOT NULL CHECK (card_key ~ '^[a-z0-9_]{2,40}$'),
    language         TEXT NOT NULL CHECK (language IN ('en', 'hi', 'te')),
    version          INTEGER NOT NULL CHECK (version >= 1),
    body             TEXT NOT NULL CHECK (char_length(btrim(body)) BETWEEN 10 AND 1000),
    status           TEXT NOT NULL DEFAULT 'approved' CHECK (status IN ('approved', 'superseded')),
    approved_by      UUID NULL,
    approved_by_name TEXT NULL CHECK (char_length(approved_by_name) <= 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_cards_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_cards_version_key UNIQUE (clinic_id, card_key, language, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_cards_live ON cc_cards (clinic_id, card_key, language) WHERE status = 'approved';

-- ── Purge: every cc_* table of a clinic, children first. Tables a later migration has not created yet are skipped,
-- so one definition serves every CC migration (and the rollbacks). Guarded tables read kriya.cc_purge. ──
CREATE OR REPLACE FUNCTION cc_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE t TEXT; n INTEGER; v JSONB := '{}'::jsonb;
BEGIN
    IF p_clinic_id IS NULL THEN RAISE EXCEPTION 'cc_bad_args' USING ERRCODE = 'P0001'; END IF;
    PERFORM set_config('kriya.cc_purge', 'on', true);
    FOREACH t IN ARRAY ARRAY['cc_outcome_snapshots', 'cc_verifications', 'cc_actions', 'cc_case_events', 'cc_cases',
                             'cc_radar_rows', 'cc_evaluations', 'cc_observations', 'cc_prompts', 'cc_schedule_items',
                             'cc_measure_sources', 'cc_discharge_followups', 'cc_plan_items', 'cc_care_plans',
                             'cc_enrollments', 'cc_cards', 'cc_consents', 'cc_protocol_activations']
    LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('DELETE FROM %I WHERE clinic_id = $1', t) USING p_clinic_id;
            GET DIAGNOSTICS n = ROW_COUNT;
            v := v || jsonb_build_object(t, n);
        END IF;
    END LOOP;
    UPDATE clinics SET cc_state = 'OFF', cc_settings = '{}'::jsonb WHERE id = p_clinic_id;
    PERFORM set_config('kriya.cc_purge', '', true);
    RETURN v;
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cc_protocol_activations', 'cc_consents', 'cc_cards']
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
    EXECUTE 'REVOKE ALL ON FUNCTION cc_purge_clinic(uuid) FROM PUBLIC';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION cc_purge_clinic(uuid) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION cc_purge_clinic(uuid) FROM authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION cc_purge_clinic(uuid) TO service_role';
    END IF;
END $$;

-- ── Verify ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'clinics'
          AND column_name IN ('cc_state', 'cc_settings')) <> 2 THEN
        RAISE EXCEPTION '145 verify: clinics.cc_state / cc_settings missing';
    END IF;
    IF to_regclass('public.cc_protocol_activations') IS NULL OR to_regclass('public.cc_consents') IS NULL
       OR to_regclass('public.cc_cards') IS NULL THEN
        RAISE EXCEPTION '145 verify: cc foundation tables missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'voice_outbound_jobs_purpose_check'
                     AND pg_get_constraintdef(oid) LIKE '%care_checkin%') THEN
        RAISE EXCEPTION '145 verify: voice purpose not widened';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'clinic_message_quota_usage_kind_check'
                     AND pg_get_constraintdef(oid) LIKE '%care%') THEN
        RAISE EXCEPTION '145 verify: quota kind not widened';
    END IF;
    IF to_regprocedure('cc_purge_clinic(uuid)') IS NULL OR to_regprocedure('kriya_guard_refs()') IS NULL THEN
        RAISE EXCEPTION '145 verify: cc_purge_clinic missing';
    END IF;
END $$;
