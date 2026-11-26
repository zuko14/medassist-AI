-- ============================================================================
-- Migration 151: Specialty OS shared blocks S1 + S2 (lane SP; docs/specialty-os/GAP-ANALYSIS.md §2)
--   S1 clinical media vault: sp_media_consents (DPDP photo consent, separate from care consent) and sp_media (private
--      storage path, sha256, angle, session; never sent on WhatsApp without an explicit staff share; erasure deletes).
--   S2 specialty forms engine: opd_encounter_forms — one versioned, code-defined form per encounter and form code
--      (eye_exam_v1, dental_chart_v1, …); validated by app/services/sp_forms.py; FROZEN once the encounter is signed
--      (trigger on this NEW table reads opd_encounters.status; nothing is added to opd_encounters).
--
-- Existing objects touched: none. References into earlier migrations' tables use kriya_guard_refs (migration 144),
-- never a FOREIGN KEY, so rollbacks of 103+ keep working. Every table is clinic-scoped with RLS parity.
-- Rollback: rollback/151_rollback_sp_media_forms.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sp_media_consents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    allow_share         BOOLEAN NOT NULL DEFAULT false,        -- patient may receive their own photos on WhatsApp
    method              TEXT NOT NULL CHECK (method IN ('counter_signed', 'whatsapp_button')),
    signer_name         TEXT NOT NULL CHECK (char_length(btrim(signer_name)) BETWEEN 1 AND 120),
    signer_relationship TEXT NOT NULL DEFAULT 'self'
                        CHECK (signer_relationship IN ('self', 'parent', 'spouse', 'guardian', 'relative', 'other')),
    notice_version      TEXT NOT NULL CHECK (notice_version ~ '^sp-media-[0-9]+$'),
    captured_by_name    TEXT NOT NULL CHECK (char_length(btrim(captured_by_name)) BETWEEN 2 AND 100),
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at        TIMESTAMPTZ NULL,
    withdrawn_reason    TEXT NULL CHECK (char_length(btrim(withdrawn_reason)) BETWEEN 3 AND 300),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_media_consents_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_media_consents_withdrawn CHECK ((withdrawn_at IS NULL) = (withdrawn_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_sp_media_consents_patient ON sp_media_consents (clinic_id, patient_id, granted_at DESC);

CREATE TABLE IF NOT EXISTS sp_media (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    consent_id          UUID NOT NULL,
    encounter_id        UUID NULL,
    package_sale_id     UUID NULL,                              -- S3 sale this photo documents (migration 152)
    context             TEXT NOT NULL CHECK (context IN ('derma', 'dental', 'eye', 'wound', 'general')),
    body_site           TEXT NULL CHECK (char_length(btrim(body_site)) BETWEEN 1 AND 60),
    angle               TEXT NOT NULL DEFAULT 'front' CHECK (angle IN ('front', 'left', 'right', 'left_45', 'right_45',
                                                                       'top', 'back', 'closeup', 'intraoral_upper',
                                                                       'intraoral_lower', 'intraoral_front', 'other')),
    session_no          SMALLINT NULL CHECK (session_no BETWEEN 1 AND 100),
    storage_path        TEXT NOT NULL CHECK (storage_path ~ '^[0-9a-f-]{36}/clinical-media/[0-9a-f-]{36}/[0-9a-f]{32}\.(jpg|png|webp)$'),
    content_type        TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    size_bytes          INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 15728640),
    sha256              TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    captured_by_name    TEXT NOT NULL CHECK (char_length(btrim(captured_by_name)) BETWEEN 2 AND 100),
    shared_at           TIMESTAMPTZ NULL,
    shared_by_name      TEXT NULL CHECK (char_length(shared_by_name) <= 100),
    deleted_at          TIMESTAMPTZ NULL,
    deleted_reason      TEXT NULL CHECK (char_length(btrim(deleted_reason)) BETWEEN 3 AND 300),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_media_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_media_path_key UNIQUE (clinic_id, storage_path),
    CONSTRAINT sp_media_path_clinic CHECK (storage_path LIKE clinic_id::text || '/clinical-media/' || patient_id::text || '/%'),
    CONSTRAINT sp_media_consent_fk FOREIGN KEY (clinic_id, consent_id) REFERENCES sp_media_consents (clinic_id, id),
    CONSTRAINT sp_media_shared CHECK ((shared_at IS NULL) = (shared_by_name IS NULL)),
    CONSTRAINT sp_media_deleted CHECK ((deleted_at IS NULL) = (deleted_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_sp_media_patient ON sp_media (clinic_id, patient_id, captured_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS opd_encounter_forms (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    encounter_id    UUID NOT NULL,
    patient_id      UUID NOT NULL,
    form_code       TEXT NOT NULL CHECK (form_code ~ '^[a-z][a-z0-9_]{2,39}$'),
    form_version    SMALLINT NOT NULL CHECK (form_version BETWEEN 1 AND 99),
    data            JSONB NOT NULL CHECK (jsonb_typeof(data) = 'object'),
    computed        JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(computed) = 'object'),
    updated_by_name TEXT NOT NULL CHECK (char_length(btrim(updated_by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_encounter_forms_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_encounter_forms_one_per_code UNIQUE (clinic_id, encounter_id, form_code)
);
CREATE INDEX IF NOT EXISTS idx_opd_encounter_forms_patient ON opd_encounter_forms (clinic_id, patient_id, form_code, created_at DESC);

-- Freeze on sign: a form of a signed (or superseded) encounter can never change; only the SP purge bypasses it.
CREATE OR REPLACE FUNCTION sp_form_freeze() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_enc UUID := COALESCE(NEW.encounter_id, OLD.encounter_id);
        v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id); v_status TEXT;
BEGIN
    IF current_setting('kriya.sp_purge', true) = v_clinic::text THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.encounter_id <> OLD.encounter_id OR NEW.form_code <> OLD.form_code
                             OR NEW.patient_id <> OLD.patient_id) THEN
        RAISE EXCEPTION 'sp_form_immutable_keys' USING ERRCODE = '23514';
    END IF;
    SELECT status INTO v_status FROM opd_encounters WHERE id = v_enc AND clinic_id = v_clinic;
    IF v_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'sp_form_frozen:%', COALESCE(v_status, 'missing') USING ERRCODE = '23514';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_sp_media_consents_refs ON sp_media_consents;
CREATE TRIGGER trg_sp_media_consents_refs BEFORE INSERT OR UPDATE ON sp_media_consents FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');
DROP TRIGGER IF EXISTS trg_sp_media_refs ON sp_media;
CREATE TRIGGER trg_sp_media_refs BEFORE INSERT OR UPDATE ON sp_media FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members',
                                      'encounter_id', 'opd_encounters');
DROP TRIGGER IF EXISTS trg_opd_encounter_forms_refs ON opd_encounter_forms;
CREATE TRIGGER trg_opd_encounter_forms_refs BEFORE INSERT OR UPDATE ON opd_encounter_forms FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('encounter_id', 'opd_encounters', 'patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_opd_encounter_forms_zz_freeze ON opd_encounter_forms;
CREATE TRIGGER trg_opd_encounter_forms_zz_freeze BEFORE INSERT OR UPDATE OR DELETE ON opd_encounter_forms FOR EACH ROW
    EXECUTE FUNCTION sp_form_freeze();

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sp_media_consents', 'sp_media', 'opd_encounter_forms']
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

-- ── 151 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.sp_media_consents') IS NULL OR to_regclass('public.sp_media') IS NULL
       OR to_regclass('public.opd_encounter_forms') IS NULL THEN
        RAISE EXCEPTION '151 verify: SP media / forms tables missing';
    END IF;
    IF to_regprocedure('sp_form_freeze()') IS NULL OR to_regprocedure('kriya_guard_refs()') IS NULL THEN
        RAISE EXCEPTION '151 verify: sp_form_freeze / kriya_guard_refs missing';
    END IF;
END $$;
