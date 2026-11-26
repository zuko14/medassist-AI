-- ============================================================================
-- Migration 154: Specialty OS — Derma and IVF depth (lane SP; docs/specialty-os/DERMA.md, IVF.md)
--   Derma needs no table of its own: session packages are S3 (152), before / after photos S1 (151), procedure and exam
--   records S2 forms (derma_procedure_v1, derma_exam_v1).
--   IVF (ART Act 2021 / ART Rules 2022 — register FORMATS await legal confirmation, owner decision SP-O2):
--     ivf_couples       two linked people (female partner + male partner or an anonymised donor code), identity type +
--                       last 4 only, verified by a named staff member;
--     ivf_cycles        type, protocol (doctor's text), trigger date-time exactly as entered + acknowledgement state,
--                       outcome under a release hold (never auto-sent: IVF-7);
--     ivf_cycle_days    stimulation chart (drugs as signed, follicles L/R in mm, endometrium, E2 / LH / P4);
--     ivf_embryos       grading entered by the embryologist (no AI), disposition; freeze / discard need a consent row;
--     ivf_witness_events double witnessing (two distinct logins ≤ 15 min apart), append-only, sha256 hash-chained per
--                       clinic like the RX registers (141); ivf_witness_verify() re-walks the chain.
--   No field anywhere carries embryo or fetal sex (PCPNDT).
-- Existing objects touched: none. Old-table references use kriya_guard_refs (144). Rollback: rollback/154_*.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS ivf_couples (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    female_patient_id        UUID NOT NULL,
    male_patient_id          UUID NULL,
    donor_code               TEXT NULL CHECK (donor_code ~ '^[A-Z0-9-]{3,30}$'),
    female_id_type           TEXT NOT NULL CHECK (female_id_type IN ('aadhaar', 'passport', 'voter_id', 'driving_licence', 'pan')),
    female_id_last4          TEXT NOT NULL CHECK (female_id_last4 ~ '^[0-9A-Z]{4}$'),
    male_id_type             TEXT NULL CHECK (male_id_type IN ('aadhaar', 'passport', 'voter_id', 'driving_licence', 'pan')),
    male_id_last4            TEXT NULL CHECK (male_id_last4 ~ '^[0-9A-Z]{4}$'),
    marital_status_declared  TEXT NOT NULL CHECK (marital_status_declared IN ('married', 'single_woman')),
    identity_verified_by_name TEXT NOT NULL CHECK (char_length(btrim(identity_verified_by_name)) BETWEEN 2 AND 100),
    identity_verified_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                   TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ivf_couples_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ivf_couples_partner CHECK ((male_patient_id IS NOT NULL) <> (donor_code IS NOT NULL)),
    CONSTRAINT ivf_couples_male_id CHECK ((male_patient_id IS NULL) = (male_id_type IS NULL)
                                         AND (male_id_type IS NULL) = (male_id_last4 IS NULL)),
    CONSTRAINT ivf_couples_single_uses_donor CHECK (marital_status_declared <> 'single_woman' OR donor_code IS NOT NULL),
    CONSTRAINT ivf_couples_distinct CHECK (male_patient_id IS NULL OR male_patient_id <> female_patient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ivf_couples_active ON ivf_couples (clinic_id, female_patient_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS ivf_cycles (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    couple_id                UUID NOT NULL,
    cycle_type               TEXT NOT NULL CHECK (cycle_type IN ('iui', 'ivf', 'icsi', 'fet', 'donor_oocyte', 'donor_sperm')),
    protocol_text            TEXT NULL CHECK (char_length(protocol_text) <= 120),
    start_date               DATE NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'stimulation' CHECK (status IN ('stimulation', 'triggered', 'opu_done',
                                                                                  'transfer_done', 'frozen_all',
                                                                                  'cancelled', 'completed')),
    trigger_at               TIMESTAMPTZ NULL,
    trigger_entered_by_name  TEXT NULL CHECK (char_length(trigger_entered_by_name) <= 100),
    trigger_ack_at           TIMESTAMPTZ NULL,
    trigger_ack_via          TEXT NULL CHECK (trigger_ack_via IN ('whatsapp_button', 'phone_call', 'in_person')),
    opu_at                   TIMESTAMPTZ NULL,
    transfer_at              TIMESTAMPTZ NULL,
    outcome                  TEXT NOT NULL DEFAULT 'pending' CHECK (outcome IN ('pending', 'negative', 'biochemical',
                                                                               'clinical', 'not_applicable')),
    result_released_at       TIMESTAMPTZ NULL,
    result_released_by_name  TEXT NULL CHECK (char_length(result_released_by_name) <= 100),
    created_by_name          TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ivf_cycles_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ivf_cycles_couple_fk FOREIGN KEY (clinic_id, couple_id) REFERENCES ivf_couples (clinic_id, id),
    CONSTRAINT ivf_cycles_trigger CHECK ((trigger_at IS NULL) = (trigger_entered_by_name IS NULL)),
    CONSTRAINT ivf_cycles_ack CHECK ((trigger_ack_at IS NULL) = (trigger_ack_via IS NULL)
                                     AND (trigger_ack_at IS NULL OR trigger_at IS NOT NULL)),
    CONSTRAINT ivf_cycles_release CHECK ((result_released_at IS NULL) = (result_released_by_name IS NULL)
                                         AND (result_released_at IS NULL OR outcome <> 'pending'))
);
CREATE INDEX IF NOT EXISTS idx_ivf_cycles_couple ON ivf_cycles (clinic_id, couple_id, start_date DESC);

CREATE TABLE IF NOT EXISTS ivf_cycle_days (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    cycle_id          UUID NOT NULL,
    day_no            SMALLINT NOT NULL CHECK (day_no BETWEEN 1 AND 40),
    on_date           DATE NOT NULL,
    drugs             JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(drugs) = 'array' AND jsonb_array_length(drugs) <= 10),
    prescription_id   UUID NULL,                               -- the signed Rx the drug lines were copied from
    follicles_left    JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(follicles_left) = 'array'
                                                               AND jsonb_array_length(follicles_left) <= 40),
    follicles_right   JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(follicles_right) = 'array'
                                                               AND jsonb_array_length(follicles_right) <= 40),
    endometrium_mm    NUMERIC(4, 1) NULL CHECK (endometrium_mm BETWEEN 0 AND 40),
    e2_pg_ml          NUMERIC(8, 1) NULL CHECK (e2_pg_ml BETWEEN 0 AND 20000),
    lh_miu_ml         NUMERIC(6, 1) NULL CHECK (lh_miu_ml BETWEEN 0 AND 500),
    p4_ng_ml          NUMERIC(6, 2) NULL CHECK (p4_ng_ml BETWEEN 0 AND 200),
    entered_by_name   TEXT NOT NULL CHECK (char_length(btrim(entered_by_name)) BETWEEN 2 AND 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ivf_cycle_days_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ivf_cycle_days_day_key UNIQUE (clinic_id, cycle_id, day_no),
    CONSTRAINT ivf_cycle_days_cycle_fk FOREIGN KEY (clinic_id, cycle_id) REFERENCES ivf_cycles (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS ivf_embryos (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    cycle_id       UUID NOT NULL,
    label          TEXT NOT NULL CHECK (label ~ '^E[0-9]{1,2}$'),
    day            SMALLINT NOT NULL CHECK (day BETWEEN 1 AND 7),
    grade          TEXT NOT NULL CHECK (grade ~ '^([1-6][ABC][ABC]|[1-4]PN|[0-9]{1,2}c(G[1-4])?|arrested)$'),
    disposition    TEXT NOT NULL DEFAULT 'in_culture' CHECK (disposition IN ('in_culture', 'transferred', 'frozen',
                                                                            'discarded', 'thawed')),
    consent_id     UUID NULL,                                  -- clinical_consents (119) of the right template
    cryo_location  TEXT NULL CHECK (cryo_location ~ '^T[0-9]{1,2}-C[0-9]{1,2}-K[0-9]{1,2}-S[0-9]{1,2}$'),
    disposed_at    TIMESTAMPTZ NULL,
    graded_by_name TEXT NOT NULL CHECK (char_length(btrim(graded_by_name)) BETWEEN 2 AND 100),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ivf_embryos_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ivf_embryos_label_key UNIQUE (clinic_id, cycle_id, label),
    CONSTRAINT ivf_embryos_cycle_fk FOREIGN KEY (clinic_id, cycle_id) REFERENCES ivf_cycles (clinic_id, id),
    CONSTRAINT ivf_embryos_consent CHECK (disposition NOT IN ('frozen', 'discarded') OR consent_id IS NOT NULL),
    CONSTRAINT ivf_embryos_cryo CHECK ((disposition = 'frozen') = (cryo_location IS NOT NULL)),
    CONSTRAINT ivf_embryos_disposed CHECK ((disposition = 'in_culture') = (disposed_at IS NULL))
);
-- one straw position holds one embryo
CREATE UNIQUE INDEX IF NOT EXISTS uq_ivf_embryos_cryo ON ivf_embryos (clinic_id, cryo_location) WHERE disposition = 'frozen';

CREATE TABLE IF NOT EXISTS ivf_witness_events (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    seq                BIGINT NOT NULL,
    cycle_id           UUID NOT NULL,
    embryo_id          UUID NULL,
    step               TEXT NOT NULL CHECK (step IN ('oocyte_collection', 'sperm_prep', 'insemination', 'fertilisation_check',
                                                     'transfer', 'freeze', 'thaw', 'discard')),
    performed_by       UUID NOT NULL,
    performed_by_name  TEXT NOT NULL CHECK (char_length(btrim(performed_by_name)) BETWEEN 2 AND 100),
    witnessed_by       UUID NOT NULL,
    witnessed_by_name  TEXT NOT NULL CHECK (char_length(btrim(witnessed_by_name)) BETWEEN 2 AND 100),
    performed_at       TIMESTAMPTZ NOT NULL,
    witnessed_at       TIMESTAMPTZ NOT NULL,
    note               TEXT NULL CHECK (char_length(note) <= 300),
    prev_hash          BYTEA NULL,
    row_hash           BYTEA NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ivf_witness_events_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ivf_witness_events_seq_key UNIQUE (clinic_id, seq),
    CONSTRAINT ivf_witness_events_cycle_fk FOREIGN KEY (clinic_id, cycle_id) REFERENCES ivf_cycles (clinic_id, id),
    CONSTRAINT ivf_witness_events_embryo_fk FOREIGN KEY (clinic_id, embryo_id) REFERENCES ivf_embryos (clinic_id, id),
    CONSTRAINT ivf_witness_events_two_people CHECK (performed_by <> witnessed_by
                                                    AND lower(btrim(performed_by_name)) <> lower(btrim(witnessed_by_name))),
    CONSTRAINT ivf_witness_events_window CHECK (witnessed_at >= performed_at
                                                AND witnessed_at - performed_at <= interval '15 minutes'),
    CONSTRAINT ivf_witness_events_embryo_steps CHECK (step NOT IN ('transfer', 'freeze', 'thaw', 'discard') OR embryo_id IS NOT NULL)
);

CREATE OR REPLACE FUNCTION ivf_witness_hash(r ivf_witness_events) RETURNS BYTEA
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT sha256(COALESCE(r.prev_hash, ''::BYTEA) || convert_to(
        ((to_jsonb(r) - 'prev_hash' - 'row_hash' - 'created_at' - 'performed_at' - 'witnessed_at')
         || jsonb_build_object('performed_at', to_char(r.performed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                               'witnessed_at', to_char(r.witnessed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')))::TEXT,
        'UTF8'))
$$;

-- seq, prev_hash and row_hash are always computed here (a client value is overwritten), under a per-clinic lock.
CREATE OR REPLACE FUNCTION ivf_witness_append() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_prev ivf_witness_events;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ivf_witness:' || NEW.clinic_id::TEXT));
    SELECT * INTO v_prev FROM ivf_witness_events WHERE clinic_id = NEW.clinic_id ORDER BY seq DESC LIMIT 1;
    NEW.seq := COALESCE(v_prev.seq, 0) + 1;
    NEW.prev_hash := v_prev.row_hash;
    NEW.row_hash := ''::BYTEA;
    NEW.row_hash := ivf_witness_hash(NEW);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION ivf_witness_append_only() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF current_setting('kriya.sp_purge', true) = OLD.clinic_id::text THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION 'ivf_witness_append_only' USING ERRCODE = '23514';
END $$;

-- NULL when the clinic's chain is intact, else the first seq whose hash or link does not match
CREATE OR REPLACE FUNCTION ivf_witness_verify(p_clinic_id UUID) RETURNS BIGINT
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $$
DECLARE r ivf_witness_events; v_prev BYTEA := NULL; v_seq BIGINT := 0; v_row ivf_witness_events;
BEGIN
    FOR r IN SELECT * FROM ivf_witness_events WHERE clinic_id = p_clinic_id ORDER BY seq LOOP
        v_seq := v_seq + 1;
        v_row := r;
        v_row.row_hash := ''::BYTEA;
        IF r.seq <> v_seq OR r.prev_hash IS DISTINCT FROM v_prev OR ivf_witness_hash(v_row) <> r.row_hash THEN
            RETURN r.seq;
        END IF;
        v_prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS trg_ivf_couples_refs ON ivf_couples;
CREATE TRIGGER trg_ivf_couples_refs BEFORE INSERT OR UPDATE ON ivf_couples FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('female_patient_id', 'patients', 'male_patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_ivf_cycle_days_refs ON ivf_cycle_days;
CREATE TRIGGER trg_ivf_cycle_days_refs BEFORE INSERT OR UPDATE ON ivf_cycle_days FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('prescription_id', 'opd_prescriptions');
DROP TRIGGER IF EXISTS trg_ivf_embryos_refs ON ivf_embryos;
CREATE TRIGGER trg_ivf_embryos_refs BEFORE INSERT OR UPDATE ON ivf_embryos FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('consent_id', 'clinical_consents');
DROP TRIGGER IF EXISTS trg_ivf_witness_events_append ON ivf_witness_events;
CREATE TRIGGER trg_ivf_witness_events_append BEFORE INSERT ON ivf_witness_events FOR EACH ROW
    EXECUTE FUNCTION ivf_witness_append();
DROP TRIGGER IF EXISTS trg_ivf_witness_events_append_only ON ivf_witness_events;
CREATE TRIGGER trg_ivf_witness_events_append_only BEFORE UPDATE OR DELETE ON ivf_witness_events FOR EACH ROW
    EXECUTE FUNCTION ivf_witness_append_only();

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ivf_couples', 'ivf_cycles', 'ivf_cycle_days', 'ivf_embryos', 'ivf_witness_events']
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

-- ── 154 verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ivf_couples', 'ivf_cycles', 'ivf_cycle_days', 'ivf_embryos', 'ivf_witness_events']
    LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            RAISE EXCEPTION '154 verify: % missing', t;
        END IF;
    END LOOP;
    IF to_regprocedure('ivf_witness_verify(uuid)') IS NULL THEN
        RAISE EXCEPTION '154 verify: ivf_witness_verify missing';
    END IF;
END $$;
