-- ============================================================================
-- Migration 119: Clinical Documents Engine (G9)
-- ============================================================================
-- Printable, signed clinical documents on the clinic's letterhead:
--
--   * IPD discharge summary PDF. 111 already versions summaries (version,
--     supersedes_id, amend_reason) and freezes the signer (doctor_snapshot).
--     119 adds what a reprint needs to be identical forever: the letterhead and
--     the clinical record shown on the PDF (patient, stay, vitals, signed lab
--     results, orders, procedure notes), frozen when the summary is signed
--     (docs_sign_discharge_summary), plus where the PDF is stored and whether it
--     reached the patient's WhatsApp.
--   * clinical_consents: an informed consent recorded as signed (paper or
--     tablet) with signer, relationship and witness. Immutable once recorded.
--   * clinical_certificates: medical-leave / fitness / ... certificates with a
--     gapless CERT-YYYY-NNNNN number per clinic (opd_next_counter, the same row
--     lock as INV- / RCT- / CRN-). Immutable once issued; may only be voided.
--
-- Invariants held here, not only in the API:
--   * a signed summary's content and signer never change; its snapshots are
--     written once, by the documents functions; delivery bookkeeping may change;
--   * a consent or certificate never changes after it is recorded / issued,
--     except a certificate's issued -> void (with a reason) and delivery
--     bookkeeping; neither is ever deleted outside the owner's clinic purge;
--   * every reference is a composite (clinic_id, id) FK: no cross-tenant link.
--
-- Additive and idempotent. Rollback: migrations/rollback/119_down.sql.
-- ============================================================================

-- ── 1. Discharge summary: snapshots + delivery bookkeeping ─────────────────
ALTER TABLE ipd_discharge_summaries
    ADD COLUMN IF NOT EXISTS letterhead_snapshot JSONB NULL,
    ADD COLUMN IF NOT EXISTS clinical_snapshot   JSONB NULL,
    ADD COLUMN IF NOT EXISTS pdf_storage_path    TEXT NULL,
    ADD COLUMN IF NOT EXISTS delivery_status     TEXT NULL CHECK (delivery_status IN ('sent', 'failed', 'not_sent')),
    ADD COLUMN IF NOT EXISTS whatsapp_message_id TEXT NULL,
    ADD COLUMN IF NOT EXISTS last_sent_at        TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS send_count          SMALLINT NOT NULL DEFAULT 0;

-- Set by the docs_* functions for one clinic, transaction-local.
CREATE OR REPLACE FUNCTION docs_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.docs_tx', true), '') = p_clinic_id::TEXT
$$;

-- 111's guard, plus: a signed / superseded summary may receive its snapshots
-- once (inside a docs_* function), its storage path once, and delivery
-- bookkeeping. Every other column stays frozen.
CREATE OR REPLACE FUNCTION ipd_guard_discharge_summary() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_docs TEXT[] := ARRAY['updated_at', 'letterhead_snapshot', 'clinical_snapshot', 'pdf_storage_path',
                           'delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'draft' OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.admission_id <> OLD.admission_id OR NEW.version <> OLD.version
       OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_discharge_summaries');
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'signed' AND NOT ipd_in_tx(OLD.clinic_id) THEN
            PERFORM ipd_raise('ipd_sign_via_function_only');
        END IF;
        IF NEW.status = 'superseded' THEN PERFORM ipd_raise('ipd_bad_transition:draft->superseded'); END IF;
        IF (NEW.letterhead_snapshot, NEW.clinical_snapshot) IS DISTINCT FROM (OLD.letterhead_snapshot, OLD.clinical_snapshot)
           AND NOT docs_in_tx(OLD.clinic_id) THEN
            PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' AND NEW.status = 'superseded' AND ipd_in_tx(OLD.clinic_id)
       AND (to_jsonb(NEW) - 'status' - 'updated_at') = (to_jsonb(OLD) - 'status' - 'updated_at') THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    IF NEW.status = OLD.status
       AND (to_jsonb(NEW) - v_docs) = (to_jsonb(OLD) - v_docs)
       AND (NEW.letterhead_snapshot IS NOT DISTINCT FROM OLD.letterhead_snapshot
            OR (OLD.letterhead_snapshot IS NULL AND docs_in_tx(OLD.clinic_id)))
       AND (NEW.clinical_snapshot IS NOT DISTINCT FROM OLD.clinical_snapshot
            OR (OLD.clinical_snapshot IS NULL AND docs_in_tx(OLD.clinic_id)))
       AND (NEW.pdf_storage_path IS NOT DISTINCT FROM OLD.pdf_storage_path OR OLD.pdf_storage_path IS NULL) THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    RETURN NULL;
END $$;

-- Sign (111's ipd_sign_discharge_summary: attending doctor, registration,
-- final diagnosis, supersede chain) and freeze the PDF's snapshots, in one
-- transaction.
CREATE OR REPLACE FUNCTION docs_sign_discharge_summary(
    p_clinic_id UUID, p_summary_id UUID, p_doctor_id UUID, p_actor_id UUID, p_actor_name TEXT,
    p_letterhead_snapshot JSONB, p_clinical_snapshot JSONB
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ipd_discharge_summaries;
BEGIN
    PERFORM ipd_sign_discharge_summary(p_clinic_id, p_summary_id, p_doctor_id, p_actor_id, p_actor_name);
    PERFORM set_config('kriya.docs_tx', p_clinic_id::TEXT, true);
    UPDATE ipd_discharge_summaries SET letterhead_snapshot = p_letterhead_snapshot, clinical_snapshot = p_clinical_snapshot
     WHERE clinic_id = p_clinic_id AND id = p_summary_id
    RETURNING * INTO s;
    PERFORM set_config('kriya.docs_tx', '', true);
    RETURN to_jsonb(s);
END $$;

-- A summary signed before 119 has no snapshots: its first print freezes them.
-- Never overwrites a snapshot that exists.
CREATE OR REPLACE FUNCTION docs_freeze_discharge_summary(
    p_clinic_id UUID, p_summary_id UUID, p_letterhead_snapshot JSONB, p_clinical_snapshot JSONB
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ipd_discharge_summaries;
BEGIN
    SELECT * INTO s FROM ipd_discharge_summaries WHERE clinic_id = p_clinic_id AND id = p_summary_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_summary_not_found'); END IF;
    IF s.status = 'draft' THEN PERFORM ipd_raise('docs_not_signed'); END IF;
    IF s.letterhead_snapshot IS NOT NULL AND s.clinical_snapshot IS NOT NULL THEN RETURN to_jsonb(s); END IF;
    PERFORM set_config('kriya.docs_tx', p_clinic_id::TEXT, true);
    UPDATE ipd_discharge_summaries
       SET letterhead_snapshot = COALESCE(letterhead_snapshot, p_letterhead_snapshot),
           clinical_snapshot   = COALESCE(clinical_snapshot, p_clinical_snapshot)
     WHERE clinic_id = p_clinic_id AND id = p_summary_id
    RETURNING * INTO s;
    PERFORM set_config('kriya.docs_tx', '', true);
    RETURN to_jsonb(s);
END $$;

-- ── 2. Informed consents ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical_consents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    admission_id        UUID NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    consent_type        TEXT NOT NULL CHECK (consent_type IN ('general_admission', 'surgical', 'anesthesia',
                                                              'high_risk_procedure', 'blood_transfusion', 'refusal_lama')),
    language            TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'hi', 'te')),
    title               TEXT NOT NULL CHECK (char_length(btrim(title)) BETWEEN 3 AND 200),
    procedure_name      TEXT NULL CHECK (char_length(procedure_name) <= 200),
    body_text           TEXT NOT NULL CHECK (char_length(btrim(body_text)) BETWEEN 20 AND 8000),
    risks_explained     TEXT NULL CHECK (char_length(risks_explained) <= 4000),
    doctor_id           UUID NULL,        -- the clinician who explained the procedure
    doctor_snapshot     JSONB NULL,
    witness_name        TEXT NULL CHECK (char_length(witness_name) <= 120),
    witness_phone       TEXT NULL CHECK (witness_phone ~ '^\+?[0-9]{10,15}$'),
    signer_relationship TEXT NOT NULL DEFAULT 'self'
                        CHECK (signer_relationship IN ('self', 'parent', 'spouse', 'guardian', 'relative', 'other')),
    signer_name         TEXT NOT NULL CHECK (char_length(btrim(signer_name)) BETWEEN 1 AND 120),
    is_signed           BOOLEAN NOT NULL DEFAULT false,
    signed_at           TIMESTAMPTZ NULL,
    signed_by_admin_id  UUID NULL,        -- staff login that recorded the signature; no FK: staff rows can be deleted
    signed_by_name      TEXT NULL CHECK (char_length(signed_by_name) <= 100),
    letterhead_snapshot JSONB NULL,
    patient_snapshot    JSONB NULL,
    pdf_storage_path    TEXT NULL,
    idempotency_key     TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT clinical_consents_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT clinical_consents_signed_consistent CHECK (
        (is_signed AND signed_at IS NOT NULL AND signed_by_name IS NOT NULL) OR (NOT is_signed AND signed_at IS NULL)),
    -- a relative signing on the patient's behalf is witnessed
    CONSTRAINT clinical_consents_proxy_witnessed CHECK (signer_relationship = 'self' OR witness_name IS NOT NULL),
    CONSTRAINT clinical_consents_admission_fk FOREIGN KEY (clinic_id, admission_id)
        REFERENCES ipd_admissions (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT clinical_consents_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT clinical_consents_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT clinical_consents_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT clinical_consents_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_clinical_consents_admission ON clinical_consents (clinic_id, admission_id);
CREATE INDEX IF NOT EXISTS idx_clinical_consents_patient   ON clinical_consents (clinic_id, patient_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinical_consents_idempotency
    ON clinical_consents (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ── 3. Medical & fitness certificates ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS clinical_certificates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    encounter_id        UUID NULL,
    admission_id        UUID NULL,
    doctor_id           UUID NOT NULL,
    certificate_type    TEXT NOT NULL CHECK (certificate_type IN ('medical_leave', 'fitness', 'travel_fitness',
                                                                  'disability_assessment', 'birth_intimation',
                                                                  'death_intimation')),
    certificate_year    SMALLINT NOT NULL,
    certificate_seq     INTEGER NOT NULL CHECK (certificate_seq > 0),
    certificate_number  TEXT GENERATED ALWAYS AS (opd_format_number('CERT-', certificate_year, certificate_seq, 5)) STORED,
    diagnosis_text      TEXT NOT NULL CHECK (char_length(btrim(diagnosis_text)) BETWEEN 3 AND 2000),
    rest_days_advised   SMALLINT NULL CHECK (rest_days_advised BETWEEN 0 AND 365),
    start_date          DATE NULL,
    end_date            DATE NULL,
    remarks             TEXT NULL CHECK (char_length(remarks) <= 500),
    doctor_snapshot     JSONB NOT NULL,   -- {doctor_id, name, qualifications, registration_number, registration_council}
    letterhead_snapshot JSONB NULL,
    patient_snapshot    JSONB NULL,
    issued_by           UUID NULL,
    issued_by_name      TEXT NULL CHECK (char_length(issued_by_name) <= 100),
    pdf_storage_path    TEXT NULL,
    status              TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'void')),
    void_reason         TEXT NULL CHECK (char_length(btrim(void_reason)) BETWEEN 5 AND 300),
    voided_at           TIMESTAMPTZ NULL,
    voided_by           UUID NULL,
    delivery_status     TEXT NULL CHECK (delivery_status IN ('sent', 'failed', 'not_sent')),
    whatsapp_message_id TEXT NULL,
    last_sent_at        TIMESTAMPTZ NULL,
    send_count          SMALLINT NOT NULL DEFAULT 0,
    idempotency_key     TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT clinical_certificates_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT clinical_certificates_seq_key UNIQUE (clinic_id, certificate_year, certificate_seq),
    CONSTRAINT clinical_certificates_signer CHECK (
        btrim(COALESCE(doctor_snapshot ->> 'registration_number', '')) <> ''),
    CONSTRAINT clinical_certificates_dates CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date),
    CONSTRAINT clinical_certificates_leave_dates CHECK (
        certificate_type <> 'medical_leave' OR (start_date IS NOT NULL AND end_date IS NOT NULL)),
    CONSTRAINT clinical_certificates_void_consistent CHECK (
        (status = 'void') = (void_reason IS NOT NULL AND voided_at IS NOT NULL)),
    CONSTRAINT clinical_certificates_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT clinical_certificates_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT clinical_certificates_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT clinical_certificates_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT clinical_certificates_encounter_fk FOREIGN KEY (clinic_id, encounter_id)
        REFERENCES opd_encounters (clinic_id, id) ON DELETE SET NULL (encounter_id),
    CONSTRAINT clinical_certificates_admission_fk FOREIGN KEY (clinic_id, admission_id)
        REFERENCES ipd_admissions (clinic_id, id) ON DELETE SET NULL (admission_id)
);
CREATE INDEX IF NOT EXISTS idx_clinical_certificates_patient ON clinical_certificates (clinic_id, patient_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinical_certificates_idempotency
    ON clinical_certificates (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ── 4. Guards: recorded consents and issued certificates are immutable ─────
CREATE OR REPLACE FUNCTION docs_purging(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT opd_purging(p_clinic_id) OR ipd_purging(p_clinic_id)
$$;

CREATE OR REPLACE FUNCTION docs_guard_consent() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF docs_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'docs_record_locked:clinical_consents' USING ERRCODE = 'P0001';
    END IF;
    -- unsigned rows stay editable; a recorded signature freezes the form (storage path may be set once)
    IF OLD.is_signed AND ((to_jsonb(NEW) - 'pdf_storage_path') IS DISTINCT FROM (to_jsonb(OLD) - 'pdf_storage_path')
                          OR (OLD.pdf_storage_path IS NOT NULL
                              AND NEW.pdf_storage_path IS DISTINCT FROM OLD.pdf_storage_path)) THEN
        RAISE EXCEPTION 'docs_record_locked:clinical_consents' USING ERRCODE = 'P0001';
    END IF;
    IF (NEW.clinic_id, NEW.patient_id) IS DISTINCT FROM (OLD.clinic_id, OLD.patient_id) THEN
        RAISE EXCEPTION 'docs_record_locked:clinical_consents' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION docs_guard_certificate() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    -- certificate_number: generated, so NULL in a BEFORE trigger's NEW (and never writable)
    v_open TEXT[] := ARRAY['pdf_storage_path', 'status', 'void_reason', 'voided_at', 'voided_by',
                           'delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count',
                           'certificate_number'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF docs_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'docs_record_locked:clinical_certificates' USING ERRCODE = 'P0001';
    END IF;
    -- an encounter / admission purge nulls its link (ON DELETE SET NULL); nothing else moves
    IF docs_purging(OLD.clinic_id) THEN RETURN NEW; END IF;
    IF (to_jsonb(NEW) - v_open) IS DISTINCT FROM (to_jsonb(OLD) - v_open)
       OR (OLD.pdf_storage_path IS NOT NULL AND NEW.pdf_storage_path IS DISTINCT FROM OLD.pdf_storage_path) THEN
        RAISE EXCEPTION 'docs_record_locked:clinical_certificates' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'void' AND (NEW.status, NEW.void_reason, NEW.voided_at, NEW.voided_by)
                               IS DISTINCT FROM (OLD.status, OLD.void_reason, OLD.voided_at, OLD.voided_by) THEN
        RAISE EXCEPTION 'docs_record_locked:clinical_certificates' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_clinical_consents_guard ON clinical_consents;
CREATE TRIGGER trg_clinical_consents_guard BEFORE UPDATE OR DELETE ON clinical_consents
    FOR EACH ROW EXECUTE FUNCTION docs_guard_consent();
DROP TRIGGER IF EXISTS trg_clinical_certificates_guard ON clinical_certificates;
CREATE TRIGGER trg_clinical_certificates_guard BEFORE UPDATE OR DELETE ON clinical_certificates
    FOR EACH ROW EXECUTE FUNCTION docs_guard_certificate();

-- ── 5. Issue a certificate: gapless number + insert in one transaction ─────
-- p_row: the certificate's fields (validated by the API). A retried request
-- with the same key returns the first certificate (`replayed`).
CREATE OR REPLACE FUNCTION docs_issue_certificate(p_clinic_id UUID, p_row JSONB, p_idempotency_key TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE c clinical_certificates; v_year SMALLINT := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
BEGIN
    IF p_idempotency_key IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('docs_cert:' || p_clinic_id || ':' || p_idempotency_key, 0));
        SELECT * INTO c FROM clinical_certificates WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key;
        IF FOUND THEN RETURN to_jsonb(c) || jsonb_build_object('replayed', true); END IF;
    END IF;
    INSERT INTO clinical_certificates (clinic_id, branch_id, patient_id, family_member_id, encounter_id, admission_id,
                                       doctor_id, certificate_type, certificate_year, certificate_seq, diagnosis_text,
                                       rest_days_advised, start_date, end_date, remarks, doctor_snapshot,
                                       letterhead_snapshot, patient_snapshot, issued_by, issued_by_name, idempotency_key)
    SELECT p_clinic_id, x.branch_id, x.patient_id, x.family_member_id, x.encounter_id, x.admission_id, x.doctor_id,
           x.certificate_type, v_year, opd_next_counter(p_clinic_id, 'cert:' || v_year), x.diagnosis_text,
           x.rest_days_advised, x.start_date, x.end_date, x.remarks, x.doctor_snapshot, x.letterhead_snapshot,
           x.patient_snapshot, x.issued_by, x.issued_by_name, p_idempotency_key
      FROM jsonb_to_record(p_row) AS x(branch_id UUID, patient_id UUID, family_member_id UUID, encounter_id UUID,
                                       admission_id UUID, doctor_id UUID, certificate_type TEXT, diagnosis_text TEXT,
                                       rest_days_advised SMALLINT, start_date DATE, end_date DATE, remarks TEXT,
                                       doctor_snapshot JSONB, letterhead_snapshot JSONB, patient_snapshot JSONB,
                                       issued_by UUID, issued_by_name TEXT)
    RETURNING * INTO c;
    RETURN to_jsonb(c) || jsonb_build_object('replayed', false);
END $$;

-- ── 6. Function hygiene + API-role lockout (103/115 pattern) ───────────────
ALTER FUNCTION docs_in_tx(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION docs_purging(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_guard_discharge_summary() SET search_path = public, pg_temp;
ALTER FUNCTION docs_sign_discharge_summary(uuid, uuid, uuid, uuid, text, jsonb, jsonb) SET search_path = public, pg_temp;
ALTER FUNCTION docs_freeze_discharge_summary(uuid, uuid, jsonb, jsonb) SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_consent() SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_certificate() SET search_path = public, pg_temp;
ALTER FUNCTION docs_issue_certificate(uuid, jsonb, text) SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'docs_sign_discharge_summary(uuid,uuid,uuid,uuid,text,jsonb,jsonb)',
        'docs_freeze_discharge_summary(uuid,uuid,jsonb,jsonb)',
        'docs_issue_certificate(uuid,jsonb,text)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
        END IF;
    END LOOP;
END $$;

-- RLS parity (defense in depth); the service_role connection bypasses it and
-- the application's clinic predicate is the boundary (app/tenancy.py).
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['clinical_consents', 'clinical_certificates'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_' || t, t);
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

-- ── 7. Verify (fails the transaction if anything is missing) ───────────────
DO $$
BEGIN
    IF to_regclass('public.clinical_consents') IS NULL OR to_regclass('public.clinical_certificates') IS NULL THEN
        RAISE EXCEPTION '119 verify: document tables missing';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns WHERE table_name = 'ipd_discharge_summaries'
          AND column_name IN ('letterhead_snapshot', 'clinical_snapshot', 'pdf_storage_path', 'delivery_status')) <> 4 THEN
        RAISE EXCEPTION '119 verify: discharge summary columns missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN (
          'clinical_consents_admission_fk', 'clinical_consents_patient_fk', 'clinical_consents_signed_consistent',
          'clinical_certificates_seq_key', 'clinical_certificates_signer', 'clinical_certificates_patient_fk',
          'clinical_certificates_doctor_fk')) <> 7 THEN
        RAISE EXCEPTION '119 verify: document constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_clinical_consents_guard', 'trg_clinical_certificates_guard', 'trg_ipd_ds_guard')) <> 3 THEN
        RAISE EXCEPTION '119 verify: document triggers missing';
    END IF;
    IF to_regprocedure('docs_sign_discharge_summary(uuid,uuid,uuid,uuid,text,jsonb,jsonb)') IS NULL
       OR to_regprocedure('docs_freeze_discharge_summary(uuid,uuid,jsonb,jsonb)') IS NULL
       OR to_regprocedure('docs_issue_certificate(uuid,jsonb,text)') IS NULL THEN
        RAISE EXCEPTION '119 verify: document functions missing';
    END IF;
END $$;
