-- ============================================================================
-- Migration 123: Radiology Information System (G13), PACS link, PCPNDT Form F
-- ============================================================================
-- A G1 imaging order (opd_orders, a service / custom line: X-ray, USG, CT, MRI)
-- gets a radiology report; an obstetric ultrasound also gets the statutory
-- PCPNDT Form F (Pre-Conception and Pre-Natal Diagnostic Techniques Act, 1994).
--
--   radiology_reports        RAD-YYYY-NNNNN, one chain per order: draft ->
--                            finalized (signed). A correction is a NEW row
--                            (version + 1, supersedes_id, reason, same number);
--                            finalizing it marks the old one 'amended'.
--   pcpndt_form_f_records    PNDT-YYYY-NNNNN, one per obstetric procedure (order),
--                            recorded and locked in one step; never changes again.
--
--   radiology_save_report     create / edit the draft, or open an amendment
--   radiology_record_form_f   record + lock Form F on an obstetric draft
--   radiology_finalize_report sign; order -> completed in the same transaction;
--                             an obstetric report is refused without Form F
--
-- Invariants held here, not only in the API:
--   * every write goes through the functions above (guards refuse direct
--     writes); the only direct write left is recording, once, where the
--     delivered PDF was stored;
--   * a finalized report names its radiologist and registration number, and
--     its findings / impression / snapshots never change;
--   * an ultrasound whose procedure or history reads obstetric IS obstetric
--     (radiology_is_obstetric_text) and cannot be finalized without Form F;
--   * no report and no Form F carries text that determines or discloses the
--     sex of the foetus (radiology_fetal_sex_text, a CHECK as well as a
--     trigger, mirrored by app/services/clinical_firewall.py). Obstetric
--     records may not mention sex / gender / genitalia at all;
--   * reports and Form F are append-only (only the owner's clinic purge deletes).
--
-- Deviations from the spec, on purpose:
--   * numbering uses opd_next_counter (row lock), not MAX(seq)+1, which hands
--     two concurrent reports the same number (see 121 / 122);
--   * foreign keys are tenant-composite (clinic_id, id), so a row can never
--     point at another clinic's order, patient or doctor;
--   * the report references its order WITH the order's patient (ON UPDATE
--     CASCADE), and Form F its report the same way, so a G7 emergency identity
--     merge (which moves opd_orders.patient_id) carries both along; the guards
--     admit exactly that change (patient_merge_allows, 121);
--   * the radiologist is NULL on a draft (a draft can be prepared by staff);
--     finalizing requires a doctor login with a registration number;
--   * Form F is keyed by order (one form per procedure) so an amended report
--     reuses the Form F of the procedure it reports;
--   * PACS URL must be http(s) (no javascript: links in the panel) and the
--     study UID is a DICOM UID (digits and dots, at most 64 characters).
--
-- Additive and idempotent. Rollback: migrations/rollback/123_down.sql.
-- ============================================================================

-- ── 1. Pure text rules (IMMUTABLE: used by CHECK constraints) ──────────────
-- Mirrored by app.services.radiology.is_obstetric_text; parity is tested.
CREATE OR REPLACE FUNCTION radiology_is_obstetric_text(p_text TEXT) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(p_text, '') ~* ('\m(obstetric\w*|antenatal|ante-natal|pregnan\w*|gravid\w*|fetal|foetal|fetus\w*|'
        || 'foetus\w*|anomaly scan|nt scan|nt-nb scan|nuchal|growth scan|biophysical|gestation\w*|dating scan|'
        || 'viability scan|tiffa|level ii scan|trimester|placenta\w*|umbilical artery|amniotic|liquor|'
        || 'ob usg|ob scan|obs usg|intrauterine pregnancy|iup)\M')
$$;

-- PCPNDT: true when the text determines or discloses the sex of the foetus.
-- Every report: the explicit phrases. Obstetric records: any sex / gender /
-- genital word at all (zero tolerance; the radiologist rephrases).
-- Mirrored by app.services.clinical_firewall.fetal_sex_disclosure; parity is tested.
CREATE OR REPLACE FUNCTION radiology_fetal_sex_text(p_text TEXT, p_obstetric BOOLEAN) RETURNS BOOLEAN
LANGUAGE sql IMMUTABLE AS $$
    SELECT COALESCE(p_text, '') ~* ('\m(sex[- ]?determination|gender[- ]?(determination|test|scan|prediction|reveal)|'
            || '(gender|sex) of (the |a |an )?(baby|babies|foetus\w*|fetus\w*|unborn|child)|'
            || '(baby|babies|foetal|fetal|foetus|fetus|unborn)(''s|s)? (gender|sex)|'
            || '(male|female) (foetus\w*|fetus\w*|baby|babies|child in (the )?womb))\M')
        OR COALESCE(p_text, '') ~ '(लड़का|लड़की|लिंग\s*(जांच|जाँच|परीक्षण)|అబ్బాయి|అమ్మాయి|లింగ\s*నిర్ధారణ|♂|♀)'
        OR (COALESCE(p_obstetric, false) AND COALESCE(p_text, '') ~* ('\m(sex|sexes|gender|male|female|boy|boys|girl|girls|'
            || 'genital\w*|scrot\w*|penis|penile|phallus|labia|labial|clitor\w*|testis|testes|testic\w*|'
            || 'turtle sign|hamburger sign|xx|xy)\M'))
$$;

-- composite FK target: the order AND its patient (merge cascade, see header)
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_orders_clinic_id_patient ON opd_orders (clinic_id, id, patient_id);

-- ── 2. Radiology reports ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radiology_reports (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id            UUID NULL,
    order_id             UUID NOT NULL,
    patient_id           UUID NOT NULL,
    family_member_id     UUID NULL,
    admission_id         UUID NULL,          -- copied from the order (reference only)
    report_year          SMALLINT NOT NULL,
    report_seq           INTEGER NOT NULL CHECK (report_seq > 0),
    report_number        TEXT GENERATED ALWAYS AS (opd_format_number('RAD-', report_year, report_seq, 5)) STORED,
    version              SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id        UUID NULL,
    amendment_reason     TEXT NULL CHECK (char_length(btrim(amendment_reason)) BETWEEN 5 AND 300),
    modality             TEXT NOT NULL CHECK (modality IN ('x_ray', 'ultrasound', 'ct', 'mri', 'mammography', 'dexa',
                                                           'fluoroscopy', 'other')),
    procedure_name       TEXT NOT NULL CHECK (char_length(btrim(procedure_name)) BETWEEN 1 AND 200),
    is_obstetric         BOOLEAN NOT NULL DEFAULT false,
    clinical_history     TEXT NULL CHECK (char_length(clinical_history) <= 1000),
    findings             TEXT NOT NULL DEFAULT '' CHECK (char_length(findings) <= 8000),
    impression           TEXT NOT NULL DEFAULT '' CHECK (char_length(impression) <= 2000),
    recommendations      TEXT NULL CHECK (char_length(recommendations) <= 1000),
    pacs_study_uid       TEXT NULL CHECK (pacs_study_uid ~ '^[0-9]+(\.[0-9]+)*$' AND char_length(pacs_study_uid) <= 64),
    pacs_viewer_url      TEXT NULL CHECK (pacs_viewer_url ~* '^https?://[^\s<>"'']+$' AND char_length(pacs_viewer_url) <= 500),
    status               TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'finalized', 'amended')),
    drafted_by_admin_id  UUID NULL,          -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    drafted_by_name      TEXT NOT NULL CHECK (char_length(btrim(drafted_by_name)) BETWEEN 1 AND 100),
    radiologist_id       UUID NULL,          -- doctors.id of the signer
    radiologist_admin_id UUID NULL,
    radiologist_snapshot JSONB NULL,         -- {doctor_id, name, qualifications, registration_number, registration_council}
    letterhead_snapshot  JSONB NULL,         -- frozen at signing so a reprint is identical
    patient_snapshot     JSONB NULL,
    finalized_at         TIMESTAMPTZ NULL,
    pdf_storage_path     TEXT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT radiology_reports_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT radiology_reports_clinic_id_patient_key UNIQUE (clinic_id, id, patient_id),
    CONSTRAINT radiology_reports_seq_key UNIQUE (clinic_id, report_year, report_seq, version),
    CONSTRAINT radiology_reports_order_version_key UNIQUE (clinic_id, order_id, version),
    CONSTRAINT radiology_reports_order_fk FOREIGN KEY (clinic_id, order_id, patient_id)
        REFERENCES opd_orders (clinic_id, id, patient_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT radiology_reports_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT radiology_reports_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT radiology_reports_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT radiology_reports_doctor_fk FOREIGN KEY (clinic_id, radiologist_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT radiology_reports_supersedes_fk FOREIGN KEY (clinic_id, supersedes_id)
        REFERENCES radiology_reports (clinic_id, id),
    CONSTRAINT radiology_reports_amendment_shape CHECK (
        (version = 1 AND supersedes_id IS NULL AND amendment_reason IS NULL)
     OR (version > 1 AND supersedes_id IS NOT NULL AND amendment_reason IS NOT NULL)),
    CONSTRAINT radiology_reports_finalized_consistent CHECK (
        (status = 'draft' AND finalized_at IS NULL)
     OR (status IN ('finalized', 'amended') AND finalized_at IS NOT NULL AND radiologist_id IS NOT NULL
         AND radiologist_snapshot ? 'registration_number'
         AND btrim(COALESCE(radiologist_snapshot ->> 'registration_number', '')) <> ''
         AND btrim(findings) <> '' AND btrim(impression) <> '')),
    CONSTRAINT radiology_reports_obstetric_shape CHECK (
        (NOT is_obstetric OR modality = 'ultrasound')
        AND (modality <> 'ultrasound' OR is_obstetric
             OR NOT radiology_is_obstetric_text(procedure_name || ' ' || COALESCE(clinical_history, '')))),
    CONSTRAINT radiology_reports_no_fetal_sex CHECK (NOT radiology_fetal_sex_text(
        procedure_name || ' ' || COALESCE(clinical_history, '') || ' ' || findings || ' ' || impression || ' '
        || COALESCE(recommendations, '') || ' ' || COALESCE(amendment_reason, ''), is_obstetric))
);
-- one unsigned working copy per order
CREATE UNIQUE INDEX IF NOT EXISTS uq_radiology_reports_open ON radiology_reports (clinic_id, order_id) WHERE status = 'draft';
CREATE INDEX IF NOT EXISTS idx_radiology_reports_order   ON radiology_reports (clinic_id, order_id);
CREATE INDEX IF NOT EXISTS idx_radiology_reports_patient ON radiology_reports (clinic_id, patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_radiology_reports_final   ON radiology_reports (clinic_id, status, finalized_at DESC);

-- ── 3. Statutory PCPNDT Form F ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pcpndt_form_f_records (
    id                                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id                         UUID NULL,
    order_id                          UUID NOT NULL,   -- the procedure (one form per procedure)
    radiology_report_id               UUID NOT NULL,   -- the report it was recorded on
    patient_id                        UUID NOT NULL,
    family_member_id                  UUID NULL,
    form_f_year                       SMALLINT NOT NULL,
    form_f_seq                        INTEGER NOT NULL CHECK (form_f_seq > 0),
    form_f_number                     TEXT GENERATED ALWAYS AS (opd_format_number('PNDT-', form_f_year, form_f_seq, 5)) STORED,
    doctor_id                         UUID NOT NULL,
    doctor_snapshot                   JSONB NOT NULL,
    patient_snapshot                  JSONB NULL,       -- {name, mrn, gender, age_years}
    letterhead_snapshot               JSONB NULL,       -- the centre's name / address: Form F names the facility
    -- maternal history
    patient_age                       SMALLINT NOT NULL CHECK (patient_age BETWEEN 10 AND 60),
    relative_name                     TEXT NOT NULL CHECK (char_length(btrim(relative_name)) BETWEEN 2 AND 100),
    referred_by                       TEXT NULL CHECK (char_length(referred_by) <= 150),
    num_living_children               SMALLINT NOT NULL DEFAULT 0 CHECK (num_living_children BETWEEN 0 AND 20),
    num_female_children               SMALLINT NOT NULL DEFAULT 0 CHECK (num_female_children >= 0),
    num_male_children                 SMALLINT NOT NULL DEFAULT 0 CHECK (num_male_children >= 0),
    last_menstrual_period             DATE NOT NULL,
    gestational_age_weeks             NUMERIC(3,1) NOT NULL CHECK (gestational_age_weeks BETWEEN 1.0 AND 45.0),
    -- statutory indication (PCPNDT Rules, Form F section B)
    clinical_indication               TEXT NOT NULL CHECK (clinical_indication IN (
                                          'dating_scan_early_pregnancy', 'first_trimester_screening',
                                          'anomaly_scan_target', 'fetal_growth_biometry',
                                          'liquor_placental_evaluation', 'suspected_ectopic_pregnancy',
                                          'suspected_molar_pregnancy', 'fetal_viability_bleeding',
                                          'multiple_gestation_chorionicity', 'other_medical_indication')),
    indication_notes                  TEXT NULL CHECK (char_length(indication_notes) <= 300),
    invasive_procedure_done           BOOLEAN NOT NULL DEFAULT false,   -- amniocentesis / CVS
    -- mandatory declarations: the form does not exist without both
    pregnant_woman_declaration_signed BOOLEAN NOT NULL CHECK (pregnant_woman_declaration_signed),
    doctor_declaration_signed         BOOLEAN NOT NULL CHECK (doctor_declaration_signed),
    is_locked                         BOOLEAN NOT NULL DEFAULT true CHECK (is_locked),
    locked_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by_admin_id              UUID NULL,
    pdf_storage_path                  TEXT NULL,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pcpndt_form_f_records_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT pcpndt_form_f_records_seq_key UNIQUE (clinic_id, form_f_year, form_f_seq),
    CONSTRAINT pcpndt_form_f_records_order_key UNIQUE (clinic_id, order_id),
    CONSTRAINT pcpndt_form_f_records_report_key UNIQUE (clinic_id, radiology_report_id),
    CONSTRAINT pcpndt_form_f_records_report_fk FOREIGN KEY (clinic_id, radiology_report_id, patient_id)
        REFERENCES radiology_reports (clinic_id, id, patient_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT pcpndt_form_f_records_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT pcpndt_form_f_records_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT pcpndt_form_f_records_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT pcpndt_form_f_records_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT pcpndt_form_f_records_children CHECK (num_female_children + num_male_children = num_living_children),
    CONSTRAINT pcpndt_form_f_records_signer CHECK (
        doctor_snapshot ? 'registration_number'
        AND btrim(COALESCE(doctor_snapshot ->> 'registration_number', '')) <> ''),
    CONSTRAINT pcpndt_form_f_records_other_needs_notes CHECK (
        clinical_indication <> 'other_medical_indication' OR char_length(btrim(COALESCE(indication_notes, ''))) >= 5),
    CONSTRAINT pcpndt_form_f_records_no_fetal_sex CHECK (NOT radiology_fetal_sex_text(
        COALESCE(indication_notes, '') || ' ' || COALESCE(referred_by, ''), true))
);
CREATE INDEX IF NOT EXISTS idx_pcpndt_form_f_register ON pcpndt_form_f_records (clinic_id, created_at DESC);

-- ── 4. Guards: function-only writes, signed records immutable ──────────────
CREATE OR REPLACE FUNCTION radiology_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.radiology_tx', true), '') = p_clinic_id::TEXT
$$;

CREATE OR REPLACE FUNCTION radiology_guard_report() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    -- report_number is generated: NULL in a BEFORE trigger's NEW, so never compared
    v_mutable TEXT[] := ARRAY['updated_at', 'pdf_storage_path', 'report_number'];
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'radiology_append_only:radiology_reports' USING ERRCODE = 'P0001';
    END IF;
    -- PCPNDT first: the most specific refusal, before any CHECK constraint
    IF radiology_fetal_sex_text(NEW.procedure_name || ' ' || COALESCE(NEW.clinical_history, '') || ' ' || NEW.findings
                                || ' ' || NEW.impression || ' ' || COALESCE(NEW.recommendations, '') || ' '
                                || COALESCE(NEW.amendment_reason, ''), NEW.is_obstetric) THEN
        RAISE EXCEPTION 'radiology_pcpndt_violation' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT radiology_in_tx(NEW.clinic_id) THEN
            RAISE EXCEPTION 'radiology_via_function_only' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status IN ('finalized', 'amended') THEN
        IF NEW.status <> OLD.status THEN
            -- only finalizing its amendment supersedes a finalized report
            IF NOT (OLD.status = 'finalized' AND NEW.status = 'amended'
                    AND COALESCE(current_setting('kriya.radiology_amending', true), '') = OLD.id::TEXT) THEN
                RAISE EXCEPTION 'radiology_report_locked:%', OLD.id USING ERRCODE = 'P0001';
            END IF;
            v_mutable := v_mutable || ARRAY['status'];
        END IF;
        IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable)
           OR (OLD.pdf_storage_path IS NOT NULL AND NEW.pdf_storage_path IS DISTINCT FROM OLD.pdf_storage_path) THEN
            RAISE EXCEPTION 'radiology_report_locked:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    -- a draft: only the functions edit it, never its identity
    IF NOT radiology_in_tx(OLD.clinic_id) THEN
        RAISE EXCEPTION 'radiology_via_function_only' USING ERRCODE = 'P0001';
    END IF;
    IF (NEW.clinic_id, NEW.order_id, NEW.patient_id, NEW.version, NEW.report_year, NEW.report_seq)
       IS DISTINCT FROM (OLD.clinic_id, OLD.order_id, OLD.patient_id, OLD.version, OLD.report_year, OLD.report_seq)
       OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        RAISE EXCEPTION 'radiology_report_locked:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'amended' THEN
        RAISE EXCEPTION 'radiology_bad_transition:draft->amended' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION radiology_guard_form_f() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'radiology_form_f_locked:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF radiology_fetal_sex_text(COALESCE(NEW.indication_notes, '') || ' ' || COALESCE(NEW.referred_by, ''), true) THEN
        RAISE EXCEPTION 'radiology_pcpndt_violation' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT radiology_in_tx(NEW.clinic_id) THEN
            RAISE EXCEPTION 'radiology_via_function_only' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    -- signed and locked at birth: the only later write is the stored PDF's path, once
    -- form_f_number is generated: NULL in a BEFORE trigger's NEW, so never compared
    IF OLD.pdf_storage_path IS NULL
       AND (to_jsonb(NEW) - ARRAY['pdf_storage_path', 'form_f_number']) = (to_jsonb(OLD) - ARRAY['pdf_storage_path', 'form_f_number']) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'radiology_form_f_locked:%', OLD.id USING ERRCODE = 'P0001';
END $$;

DROP TRIGGER IF EXISTS trg_radiology_reports_guard ON radiology_reports;
CREATE TRIGGER trg_radiology_reports_guard BEFORE INSERT OR UPDATE OR DELETE ON radiology_reports
    FOR EACH ROW EXECUTE FUNCTION radiology_guard_report();
DROP TRIGGER IF EXISTS trg_radiology_reports_touch ON radiology_reports;
CREATE TRIGGER trg_radiology_reports_touch BEFORE UPDATE ON radiology_reports
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_pcpndt_form_f_guard ON pcpndt_form_f_records;
CREATE TRIGGER trg_pcpndt_form_f_guard BEFORE INSERT OR UPDATE OR DELETE ON pcpndt_form_f_records
    FOR EACH ROW EXECUTE FUNCTION radiology_guard_form_f();

-- ── 5. Save a draft / open an amendment ────────────────────────────────────
-- p_row: {modality, procedure_name, is_obstetric, clinical_history, findings, impression,
--         recommendations, pacs_study_uid, pacs_viewer_url}. is_obstetric is forced on
-- for an ultrasound whose procedure or history reads obstetric.
-- p_expected_updated_at: the draft version the editor loaded (NULL = do not check).
CREATE OR REPLACE FUNCTION radiology_save_report(
    p_clinic_id UUID, p_order_id UUID, p_row JSONB, p_amendment_reason TEXT, p_expected_updated_at TIMESTAMPTZ,
    p_actor_id UUID, p_actor_name TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    o opd_orders;
    r radiology_reports;
    v_new BOOLEAN := false;
    v_year SMALLINT;
    v_modality TEXT := p_row ->> 'modality';
    v_proc TEXT := btrim(COALESCE(p_row ->> 'procedure_name', ''));
    v_hist TEXT := NULLIF(btrim(COALESCE(p_row ->> 'clinical_history', '')), '');
    v_obst BOOLEAN;
BEGIN
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'radiology_order_not_found' USING ERRCODE = 'P0001'; END IF;
    IF o.catalog_type = 'lab_test' THEN RAISE EXCEPTION 'radiology_order_not_imaging' USING ERRCODE = 'P0001'; END IF;
    IF o.status = 'cancelled' THEN RAISE EXCEPTION 'radiology_order_cancelled' USING ERRCODE = 'P0001'; END IF;
    IF v_proc = '' THEN v_proc := o.item_name; END IF;
    v_obst := COALESCE((p_row ->> 'is_obstetric')::BOOLEAN, false)
              OR (v_modality = 'ultrasound' AND radiology_is_obstetric_text(v_proc || ' ' || COALESCE(v_hist, '')));
    IF v_obst AND v_modality IS DISTINCT FROM 'ultrasound' THEN
        RAISE EXCEPTION 'radiology_obstetric_needs_ultrasound' USING ERRCODE = 'P0001';
    END IF;

    PERFORM set_config('kriya.radiology_tx', p_clinic_id::TEXT, true);
    SELECT * INTO r FROM radiology_reports WHERE clinic_id = p_clinic_id AND order_id = p_order_id
     ORDER BY version DESC LIMIT 1 FOR UPDATE;

    IF r.id IS NULL THEN
        -- an order closed with a text note (G1) has no report to write
        IF o.status = 'completed' THEN RAISE EXCEPTION 'radiology_order_closed' USING ERRCODE = 'P0001'; END IF;
        v_year := extract(year FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
        INSERT INTO radiology_reports (clinic_id, branch_id, order_id, patient_id, family_member_id, admission_id,
                                       report_year, report_seq, modality, procedure_name, is_obstetric,
                                       clinical_history, findings, impression, recommendations, pacs_study_uid,
                                       pacs_viewer_url, drafted_by_admin_id, drafted_by_name)
        VALUES (p_clinic_id, o.branch_id, o.id, o.patient_id, o.family_member_id, o.admission_id,
                v_year, opd_next_counter(p_clinic_id, 'rad:' || v_year), v_modality, left(v_proc, 200), v_obst,
                v_hist, COALESCE(p_row ->> 'findings', ''), COALESCE(p_row ->> 'impression', ''),
                NULLIF(btrim(COALESCE(p_row ->> 'recommendations', '')), ''), NULLIF(btrim(p_row ->> 'pacs_study_uid'), ''),
                NULLIF(btrim(p_row ->> 'pacs_viewer_url'), ''), p_actor_id, left(p_actor_name, 100))
        RETURNING * INTO r;
        v_new := true;
    ELSIF r.status = 'draft' THEN
        IF p_expected_updated_at IS NOT NULL
           AND date_trunc('milliseconds', r.updated_at) <> date_trunc('milliseconds', p_expected_updated_at) THEN
            RAISE EXCEPTION 'radiology_report_changed' USING ERRCODE = 'P0001';
        END IF;
        -- a recorded Form F fixes the study as obstetric
        IF EXISTS (SELECT 1 FROM pcpndt_form_f_records WHERE clinic_id = p_clinic_id AND order_id = o.id)
           AND NOT v_obst THEN
            RAISE EXCEPTION 'radiology_form_f_fixes_obstetric' USING ERRCODE = 'P0001';
        END IF;
        UPDATE radiology_reports SET modality = v_modality, procedure_name = left(v_proc, 200), is_obstetric = v_obst,
               clinical_history = v_hist, findings = COALESCE(p_row ->> 'findings', ''),
               impression = COALESCE(p_row ->> 'impression', ''),
               recommendations = NULLIF(btrim(COALESCE(p_row ->> 'recommendations', '')), ''),
               pacs_study_uid = NULLIF(btrim(p_row ->> 'pacs_study_uid'), ''),
               pacs_viewer_url = NULLIF(btrim(p_row ->> 'pacs_viewer_url'), ''),
               drafted_by_admin_id = p_actor_id, drafted_by_name = left(p_actor_name, 100)
         WHERE clinic_id = p_clinic_id AND id = r.id
        RETURNING * INTO r;
    ELSE   -- latest version is signed: a correction is a new, versioned amendment
        IF p_amendment_reason IS NULL OR char_length(btrim(p_amendment_reason)) < 5 THEN
            RAISE EXCEPTION 'radiology_amendment_reason_required' USING ERRCODE = 'P0001';
        END IF;
        IF r.is_obstetric AND NOT v_obst THEN
            RAISE EXCEPTION 'radiology_form_f_fixes_obstetric' USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO radiology_reports (clinic_id, branch_id, order_id, patient_id, family_member_id, admission_id,
                                       report_year, report_seq, version, supersedes_id, amendment_reason, modality,
                                       procedure_name, is_obstetric, clinical_history, findings, impression,
                                       recommendations, pacs_study_uid, pacs_viewer_url, drafted_by_admin_id,
                                       drafted_by_name)
        VALUES (p_clinic_id, r.branch_id, r.order_id, r.patient_id, r.family_member_id, r.admission_id,
                r.report_year, r.report_seq, r.version + 1, r.id, left(btrim(p_amendment_reason), 300), v_modality,
                left(v_proc, 200), v_obst, v_hist, COALESCE(p_row ->> 'findings', ''),
                COALESCE(p_row ->> 'impression', ''), NULLIF(btrim(COALESCE(p_row ->> 'recommendations', '')), ''),
                NULLIF(btrim(p_row ->> 'pacs_study_uid'), ''), NULLIF(btrim(p_row ->> 'pacs_viewer_url'), ''),
                p_actor_id, left(p_actor_name, 100))
        RETURNING * INTO r;
        v_new := true;
    END IF;
    PERFORM set_config('kriya.radiology_tx', '', true);
    RETURN to_jsonb(r) || jsonb_build_object('created', v_new);
END $$;

-- ── 6. Record and lock Form F ──────────────────────────────────────────────
-- p_row: {patient_age, relative_name, referred_by, num_living_children, num_female_children,
--         num_male_children, last_menstrual_period, gestational_age_weeks, clinical_indication,
--         indication_notes, invasive_procedure_done, pregnant_woman_declaration_signed,
--         doctor_declaration_signed, patient_snapshot, letterhead_snapshot}
-- A second call for the same procedure returns the recorded form (`replayed`).
CREATE OR REPLACE FUNCTION radiology_record_form_f(
    p_clinic_id UUID, p_report_id UUID, p_row JSONB, p_doctor_id UUID, p_doctor_snapshot JSONB, p_actor_id UUID
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    r radiology_reports;
    f pcpndt_form_f_records;
    v_order UUID;
    v_year SMALLINT;
    v_lmp DATE := (p_row ->> 'last_menstrual_period')::DATE;
    v_today DATE := (now() AT TIME ZONE 'Asia/Kolkata')::DATE;
BEGIN
    SELECT order_id INTO v_order FROM radiology_reports WHERE clinic_id = p_clinic_id AND id = p_report_id;
    IF v_order IS NULL THEN RAISE EXCEPTION 'radiology_report_not_found' USING ERRCODE = 'P0001'; END IF;
    -- lock order then report: the same order as save / finalize, so they never deadlock
    PERFORM 1 FROM opd_orders WHERE clinic_id = p_clinic_id AND id = v_order FOR UPDATE;
    SELECT * INTO r FROM radiology_reports WHERE clinic_id = p_clinic_id AND id = p_report_id FOR UPDATE;

    SELECT * INTO f FROM pcpndt_form_f_records WHERE clinic_id = p_clinic_id AND order_id = r.order_id;
    IF f.id IS NOT NULL THEN
        RETURN to_jsonb(f) || jsonb_build_object('replayed', true);
    END IF;
    IF NOT r.is_obstetric THEN RAISE EXCEPTION 'radiology_form_f_not_obstetric' USING ERRCODE = 'P0001'; END IF;
    IF r.status <> 'draft' THEN RAISE EXCEPTION 'radiology_not_draft' USING ERRCODE = 'P0001'; END IF;
    IF btrim(COALESCE(p_doctor_snapshot ->> 'registration_number', '')) = '' THEN
        RAISE EXCEPTION 'radiology_signer_registration_missing' USING ERRCODE = 'P0001';
    END IF;
    IF v_lmp IS NULL OR v_lmp > v_today OR v_lmp < v_today - 315 THEN
        RAISE EXCEPTION 'radiology_form_f_bad_lmp' USING ERRCODE = 'P0001';
    END IF;
    IF NOT COALESCE((p_row ->> 'pregnant_woman_declaration_signed')::BOOLEAN, false)
       OR NOT COALESCE((p_row ->> 'doctor_declaration_signed')::BOOLEAN, false) THEN
        RAISE EXCEPTION 'radiology_form_f_declarations' USING ERRCODE = 'P0001';
    END IF;

    PERFORM set_config('kriya.radiology_tx', p_clinic_id::TEXT, true);
    v_year := extract(year FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
    INSERT INTO pcpndt_form_f_records (clinic_id, branch_id, order_id, radiology_report_id, patient_id, family_member_id,
                                       form_f_year, form_f_seq, doctor_id, doctor_snapshot, patient_snapshot,
                                       letterhead_snapshot, patient_age, relative_name, referred_by, num_living_children,
                                       num_female_children, num_male_children, last_menstrual_period,
                                       gestational_age_weeks, clinical_indication, indication_notes,
                                       invasive_procedure_done, pregnant_woman_declaration_signed,
                                       doctor_declaration_signed, recorded_by_admin_id)
    SELECT p_clinic_id, r.branch_id, r.order_id, r.id, r.patient_id, r.family_member_id,
           v_year, opd_next_counter(p_clinic_id, 'pndt:' || v_year), p_doctor_id, p_doctor_snapshot,
           p_row -> 'patient_snapshot', p_row -> 'letterhead_snapshot', x.patient_age, btrim(x.relative_name), NULLIF(btrim(x.referred_by), ''),
           COALESCE(x.num_living_children, 0), COALESCE(x.num_female_children, 0), COALESCE(x.num_male_children, 0),
           v_lmp, x.gestational_age_weeks, x.clinical_indication, NULLIF(btrim(x.indication_notes), ''),
           COALESCE(x.invasive_procedure_done, false), true, true, p_actor_id
      FROM jsonb_to_record(p_row) AS x(patient_age SMALLINT, relative_name TEXT, referred_by TEXT,
                                       num_living_children SMALLINT, num_female_children SMALLINT,
                                       num_male_children SMALLINT, gestational_age_weeks NUMERIC,
                                       clinical_indication TEXT, indication_notes TEXT, invasive_procedure_done BOOLEAN)
    RETURNING * INTO f;
    PERFORM set_config('kriya.radiology_tx', '', true);
    RETURN to_jsonb(f) || jsonb_build_object('replayed', false);
END $$;

-- ── 7. Finalize & sign ─────────────────────────────────────────────────────
-- p_expected_updated_at: the draft the radiologist reviewed; an edit in
-- between refuses the signature instead of covering text nobody reviewed.
CREATE OR REPLACE FUNCTION radiology_finalize_report(
    p_clinic_id UUID, p_report_id UUID, p_expected_updated_at TIMESTAMPTZ, p_actor_id UUID, p_doctor_id UUID,
    p_signer_snapshot JSONB, p_letterhead_snapshot JSONB, p_patient_snapshot JSONB
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    r radiology_reports;
    o opd_orders;
    v_order UUID;
    v_name TEXT := left(COALESCE(p_signer_snapshot ->> 'name', 'radiologist'), 100);
BEGIN
    SELECT order_id INTO v_order FROM radiology_reports WHERE clinic_id = p_clinic_id AND id = p_report_id;
    IF v_order IS NULL THEN RAISE EXCEPTION 'radiology_report_not_found' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = v_order FOR UPDATE;
    SELECT * INTO r FROM radiology_reports WHERE clinic_id = p_clinic_id AND id = p_report_id FOR UPDATE;

    IF r.status = 'finalized' AND r.radiologist_admin_id IS NOT DISTINCT FROM p_actor_id THEN
        RETURN to_jsonb(r) || jsonb_build_object('replayed', true);   -- double click
    END IF;
    IF r.status <> 'draft' THEN RAISE EXCEPTION 'radiology_not_draft:%', r.status USING ERRCODE = 'P0001'; END IF;
    IF p_expected_updated_at IS NULL OR date_trunc('milliseconds', r.updated_at)
                                         <> date_trunc('milliseconds', p_expected_updated_at) THEN
        RAISE EXCEPTION 'radiology_report_changed' USING ERRCODE = 'P0001';
    END IF;
    IF o.status = 'cancelled' THEN RAISE EXCEPTION 'radiology_order_cancelled' USING ERRCODE = 'P0001'; END IF;
    IF btrim(r.findings) = '' OR btrim(r.impression) = '' THEN
        RAISE EXCEPTION 'radiology_report_incomplete' USING ERRCODE = 'P0001';
    END IF;
    IF btrim(COALESCE(p_signer_snapshot ->> 'registration_number', '')) = '' THEN
        RAISE EXCEPTION 'radiology_signer_registration_missing' USING ERRCODE = 'P0001';
    END IF;
    IF r.is_obstetric AND NOT EXISTS (SELECT 1 FROM pcpndt_form_f_records
                                       WHERE clinic_id = p_clinic_id AND order_id = r.order_id AND is_locked) THEN
        RAISE EXCEPTION 'radiology_form_f_required' USING ERRCODE = 'P0001';
    END IF;

    PERFORM set_config('kriya.radiology_tx', p_clinic_id::TEXT, true);
    UPDATE radiology_reports SET status = 'finalized', finalized_at = now(), radiologist_id = p_doctor_id,
           radiologist_admin_id = p_actor_id, radiologist_snapshot = p_signer_snapshot,
           letterhead_snapshot = p_letterhead_snapshot, patient_snapshot = p_patient_snapshot
     WHERE clinic_id = p_clinic_id AND id = r.id
    RETURNING * INTO r;
    IF r.supersedes_id IS NOT NULL THEN
        PERFORM set_config('kriya.radiology_amending', r.supersedes_id::TEXT, true);
        UPDATE radiology_reports SET status = 'amended'
         WHERE clinic_id = p_clinic_id AND id = r.supersedes_id AND status = 'finalized';
        PERFORM set_config('kriya.radiology_amending', '', true);
    END IF;
    PERFORM set_config('kriya.radiology_tx', '', true);

    -- the first signed report fulfils the order; an amendment leaves it completed
    IF o.status IN ('ordered', 'billed', 'sample_collected', 'in_progress', 'resulted') THEN
        UPDATE opd_orders SET status = 'completed', resulted_at = COALESCE(resulted_at, now()),
               resulted_by = COALESCE(resulted_by, p_actor_id), resulted_by_name = COALESCE(resulted_by_name, v_name),
               result_summary = COALESCE(result_summary,
                   left(r.report_number || ' finalized. Impression: ' || r.impression, 4000))
         WHERE clinic_id = p_clinic_id AND id = o.id;
    END IF;
    RETURN to_jsonb(r) || jsonb_build_object('replayed', false);
END $$;

-- ── 8. Function hygiene + API-role lockout (103/115/118 pattern) ───────────
ALTER FUNCTION radiology_is_obstetric_text(text) SET search_path = public, pg_temp;
ALTER FUNCTION radiology_fetal_sex_text(text, boolean) SET search_path = public, pg_temp;
ALTER FUNCTION radiology_in_tx(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION radiology_guard_report() SET search_path = public, pg_temp;
ALTER FUNCTION radiology_guard_form_f() SET search_path = public, pg_temp;
ALTER FUNCTION radiology_save_report(uuid, uuid, jsonb, text, timestamptz, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION radiology_record_form_f(uuid, uuid, jsonb, uuid, jsonb, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION radiology_finalize_report(uuid, uuid, timestamptz, uuid, uuid, jsonb, jsonb, jsonb)
    SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'radiology_save_report(uuid,uuid,jsonb,text,timestamptz,uuid,text)',
        'radiology_record_form_f(uuid,uuid,jsonb,uuid,jsonb,uuid)',
        'radiology_finalize_report(uuid,uuid,timestamptz,uuid,uuid,jsonb,jsonb,jsonb)']
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
    FOREACH t IN ARRAY ARRAY['radiology_reports', 'pcpndt_form_f_records'] LOOP
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

-- ── 9. Verify (fails the transaction if anything is missing) ───────────────
DO $$
BEGIN
    IF to_regclass('public.radiology_reports') IS NULL OR to_regclass('public.pcpndt_form_f_records') IS NULL THEN
        RAISE EXCEPTION '123 verify: radiology tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN (
          'radiology_reports_order_fk', 'radiology_reports_finalized_consistent', 'radiology_reports_amendment_shape',
          'radiology_reports_obstetric_shape', 'radiology_reports_no_fetal_sex', 'pcpndt_form_f_records_report_fk',
          'pcpndt_form_f_records_children', 'pcpndt_form_f_records_signer', 'pcpndt_form_f_records_no_fetal_sex',
          'pcpndt_form_f_records_order_key')) <> 10 THEN
        RAISE EXCEPTION '123 verify: radiology constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_radiology_reports_guard', 'trg_radiology_reports_touch', 'trg_pcpndt_form_f_guard')) <> 3 THEN
        RAISE EXCEPTION '123 verify: radiology triggers missing';
    END IF;
    IF to_regprocedure('radiology_save_report(uuid,uuid,jsonb,text,timestamptz,uuid,text)') IS NULL
       OR to_regprocedure('radiology_record_form_f(uuid,uuid,jsonb,uuid,jsonb,uuid)') IS NULL
       OR to_regprocedure('radiology_finalize_report(uuid,uuid,timestamptz,uuid,uuid,jsonb,jsonb,jsonb)') IS NULL THEN
        RAISE EXCEPTION '123 verify: radiology functions missing';
    END IF;
    IF NOT radiology_fetal_sex_text('Single live male fetus', false) OR radiology_fetal_sex_text('Normal study', true)
       OR NOT radiology_is_obstetric_text('USG Obstetric (Level II)') THEN
        RAISE EXCEPTION '123 verify: text rules misbehave';
    END IF;
END $$;
