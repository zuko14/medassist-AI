-- ============================================================================
-- Migration 121: Emergency / Casualty OS, rapid triage, MLC register (G7)
-- ============================================================================
-- emergency_visits     one row per casualty arrival, EMG-YYYY-NNNNN (gapless,
--                      opd_next_counter row lock), triage RED / YELLOW / GREEN / BLACK.
--                      An unconscious or unidentified patient gets a temporary
--                      patient record ("Unknown Male ~40y") in the same transaction.
-- emergency_mlc_records the statutory medico-legal case / police intimation,
--                      MLC-YYYY-NNNNN; frozen once the doctor locks it.
-- patient_merge_log    provenance of every identity merge.
--
-- emergency_merge_patient moves everything recorded against a temporary
-- patient (ER visits, MLC, appointments, consultations, prescriptions, bills,
-- admissions with their charts, orders, lab results, consents, certificates,
-- voice calls, lab reports) to the real patient in ONE transaction. The guards
-- that freeze signed / issued records (103, 111, 115, 117, 118, 119) are
-- redefined here, verbatim plus one line: inside that merge they accept an
-- update whose ONLY change is patient_id from the temporary record to the
-- target (patient_merge_allows). Every other column stays frozen; signed
-- documents keep the identity they were signed under in their snapshots.
--
-- Dispositions are one function each way (emergency_disposition): admit to
-- IPD calls ipd_admit in the same transaction; LAMA requires the G9 refusal
-- consent; death links the G9 death intimation.
--
-- The spec's MAX(seq)+1 numbering is replaced by opd_next_counter: MAX+1
-- hands two concurrent registrations the same EMG number.
-- Additive. Rollback: migrations/rollback/121_down.sql.
-- ============================================================================

-- ── 1. Temporary identities ────────────────────────────────────────────────
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS is_temporary BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS merged_into_patient_id UUID NULL,
    ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'patients_merged_into_fk') THEN
        ALTER TABLE patients ADD CONSTRAINT patients_merged_into_fk FOREIGN KEY (clinic_id, merged_into_patient_id)
            REFERENCES patients (clinic_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'patients_merge_consistent') THEN
        ALTER TABLE patients ADD CONSTRAINT patients_merge_consistent CHECK (
            (merged_into_patient_id IS NULL) = (merged_at IS NULL)
            AND (merged_into_patient_id IS NULL OR (is_temporary AND merged_into_patient_id <> id)));
    END IF;
END $$;

-- Inside emergency_merge_patient only: true when an UPDATE changes nothing but
-- patient_id, from the merge's temporary patient to its target. Generated
-- columns are NULL in a BEFORE trigger's NEW, so they are left out.
CREATE OR REPLACE FUNCTION patient_merge_allows(p_relid OID, p_old JSONB, p_new JSONB)
RETURNS BOOLEAN LANGUAGE plpgsql STABLE AS $$
DECLARE v TEXT := COALESCE(current_setting('kriya.patient_merge', true), ''); v_skip TEXT[];
BEGIN
    IF v = '' OR (p_old ->> 'clinic_id') || ':' || (p_old ->> 'patient_id') || ':' || (p_new ->> 'patient_id') <> v THEN
        RETURN false;
    END IF;
    SELECT COALESCE(array_agg(attname::TEXT), '{}') INTO v_skip FROM pg_attribute
     WHERE attrelid = p_relid AND attgenerated <> '' AND NOT attisdropped;
    v_skip := v_skip || ARRAY['patient_id', 'updated_at', 'version'];
    RETURN (p_new - v_skip) = (p_old - v_skip);
END $$;

-- opd_guard_signed_record (latest: 103_opd_os_core.sql) + merge exemption
CREATE OR REPLACE FUNCTION opd_guard_signed_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['updated_at', 'bmi'];
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.doctor_id <> OLD.doctor_id OR NEW.appointment_id <> OLD.appointment_id
       OR NEW.version <> OLD.version OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'superseded' THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->superseded' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' THEN
        IF TG_TABLE_NAME = 'opd_prescriptions' THEN
            v_mutable := v_mutable || ARRAY['delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count'];
        END IF;
        IF NEW.status = 'superseded' THEN
            v_mutable := v_mutable || ARRAY['status', 'superseded_at'];
        ELSIF NEW.status <> 'signed' THEN
            RAISE EXCEPTION 'opd_bad_transition:signed->%', NEW.status USING ERRCODE = 'P0001';
        END IF;
        IF (to_jsonb(NEW) - v_mutable) = (to_jsonb(OLD) - v_mutable) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
END $$;

-- opd_guard_invoice (latest: 117_gst_billing_and_credit_notes.sql) + merge exemption
CREATE OR REPLACE FUNCTION opd_guard_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_items   INTEGER;
    v_total   INTEGER;
    v_tax     INTEGER;
    v_mutable TEXT[] := ARRAY['updated_at', 'total_paise', 'invoice_number',
                              'payment_link_gateway', 'payment_link_id', 'payment_link_url',
                              'payment_link_amount_paise', 'payment_link_created_at'];
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.appointment_id IS DISTINCT FROM OLD.appointment_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    -- paid_paise moves only inside the receipt trigger, for this invoice
    IF NEW.paid_paise <> OLD.paid_paise
       AND current_setting('kriya.opd_receipt_invoice', true) IS DISTINCT FROM OLD.id::TEXT THEN
        RAISE EXCEPTION 'opd_paid_is_derived' USING ERRCODE = 'P0001';
    END IF;
    -- credited_paise moves only inside opd_issue_credit_note, for this invoice
    IF NEW.credited_paise <> OLD.credited_paise
       AND current_setting('kriya.opd_credit_invoice', true) IS DISTINCT FROM OLD.id::TEXT THEN
        RAISE EXCEPTION 'opd_credited_is_derived' USING ERRCODE = 'P0001';
    END IF;

    IF OLD.status = 'draft' THEN
        SELECT count(*), COALESCE(SUM(line_total_paise + total_tax_paise), 0), COALESCE(SUM(total_tax_paise), 0)
          INTO v_items, v_total, v_tax
          FROM opd_invoice_items WHERE invoice_id = OLD.id AND clinic_id = OLD.clinic_id;
        -- the header discount lowers taxable value pro rata
        NEW.total_tax_paise := CASE WHEN NEW.subtotal_paise > 0 AND v_total = NEW.subtotal_paise
                                    THEN v_tax - round(v_tax::NUMERIC * NEW.discount_paise / NEW.subtotal_paise)::INTEGER
                                    ELSE 0 END;
        IF NEW.status IN ('issued', 'paid') THEN
            IF v_items = 0 THEN RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001'; END IF;
            IF v_total <> NEW.subtotal_paise THEN RAISE EXCEPTION 'opd_invoice_subtotal_mismatch' USING ERRCODE = 'P0001'; END IF;
            NEW.invoice_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
            NEW.invoice_seq  := opd_next_counter(OLD.clinic_id, 'inv:' || NEW.invoice_year);
            NEW.issued_at    := now();
            NEW.clinic_gstin := opd_clinic_gstin(OLD.clinic_id);
            NEW.is_tax_invoice := NEW.total_tax_paise > 0;
            NEW.status       := CASE WHEN NEW.subtotal_paise - NEW.discount_paise = 0 THEN 'paid' ELSE 'issued' END;
        ELSIF NEW.status NOT IN ('draft', 'void') THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->%', NEW.status USING ERRCODE = 'P0001';
        ELSIF NEW.invoice_seq IS NOT NULL OR NEW.invoice_year IS NOT NULL THEN
            RAISE EXCEPTION 'opd_number_is_assigned_at_issue' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'void' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;

    -- issued / partially_paid / paid
    IF NEW.status = 'void' THEN
        IF OLD.paid_paise <> 0 THEN RAISE EXCEPTION 'opd_void_requires_refund_first' USING ERRCODE = 'P0001'; END IF;
        -- a credit note already reported against this invoice: it can only be credited further
        IF OLD.credited_paise <> 0 THEN RAISE EXCEPTION 'opd_void_has_credit_notes' USING ERRCODE = 'P0001'; END IF;
        v_mutable := v_mutable || ARRAY['status', 'voided_at', 'voided_by', 'void_reason'];
    ELSIF NEW.status = 'draft' THEN
        RAISE EXCEPTION 'opd_bad_transition:%->draft', OLD.status USING ERRCODE = 'P0001';
    ELSE
        v_mutable := v_mutable || ARRAY['status', 'paid_paise', 'credited_paise'];
    END IF;
    IF (to_jsonb(NEW) - v_mutable) <> (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- ipd_guard_admission (latest: 111_ipd_os_core.sql) + merge exemption
CREATE OR REPLACE FUNCTION ipd_guard_admission() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:ipd_admissions');
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.family_member_id IS DISTINCT FROM OLD.family_member_id
       OR NEW.admission_year <> OLD.admission_year OR NEW.admission_seq <> OLD.admission_seq
       OR NEW.admitting_doctor_id <> OLD.admitting_doctor_id OR NEW.created_at <> OLD.created_at THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_admissions');
    END IF;
    IF OLD.status IN ('discharged', 'cancelled') AND NOT ipd_purging(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_record_locked:ipd_admissions');
    END IF;
    IF (NEW.status, NEW.current_bed_id, NEW.admitted_at, NEW.discharged_at, NEW.discharge_type)
       IS DISTINCT FROM (OLD.status, OLD.current_bed_id, OLD.admitted_at, OLD.discharged_at, OLD.discharge_type)
       AND NOT ipd_in_tx(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_admission_state_via_function_only');
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- opd_guard_order (latest: 115_investigation_and_procedure_orders.sql) + merge exemption
CREATE OR REPLACE FUNCTION opd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['status', 'updated_at', 'cancellation_reason', 'cancelled_at', 'cancelled_by',
                              'specimen_barcode', 'sample_collected_at', 'sample_collected_by',
                              'sample_collected_by_name', 'resulted_at', 'resulted_by', 'resulted_by_name',
                              'result_summary'];
    v_ok BOOLEAN;
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        -- only the owner's clinic purge (cascading from encounters / IPD orders)
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'opd_append_only:opd_orders' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('completed', 'cancelled') THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.sample_collected_at IS NOT NULL
       AND (NEW.specimen_barcode, NEW.sample_collected_at, NEW.sample_collected_by, NEW.sample_collected_by_name)
           IS DISTINCT FROM
           (OLD.specimen_barcode, OLD.sample_collected_at, OLD.sample_collected_by, OLD.sample_collected_by_name) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.resulted_at IS NOT NULL
       AND (NEW.resulted_at, NEW.resulted_by, NEW.resulted_by_name, NEW.result_summary)
           IS DISTINCT FROM (OLD.resulted_at, OLD.resulted_by, OLD.resulted_by_name, OLD.result_summary) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_orders:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    v_ok := CASE OLD.status
        WHEN 'ordered'          THEN NEW.status IN ('billed', 'sample_collected', 'completed', 'cancelled')
        WHEN 'billed'           THEN NEW.status IN ('ordered', 'sample_collected', 'completed', 'cancelled')
        WHEN 'sample_collected' THEN NEW.status IN ('in_progress', 'resulted', 'completed', 'cancelled')
        WHEN 'in_progress'      THEN NEW.status IN ('resulted', 'completed', 'cancelled')
        WHEN 'resulted'         THEN NEW.status = 'completed'
        ELSE false END;
    IF NOT v_ok THEN
        RAISE EXCEPTION 'opd_order_bad_transition:%->%', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    -- billing state follows the bill (opd_invoice_orders_billing), never a direct write
    IF NEW.status IN ('ordered', 'billed') AND OLD.status IN ('ordered', 'billed')
       AND current_setting('kriya.opd_order_billing', true) IS DISTINCT FROM OLD.clinic_id::TEXT THEN
        RAISE EXCEPTION 'opd_order_billing_is_derived' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'completed' AND OLD.status IN ('ordered', 'billed') AND OLD.sample_type IS NOT NULL THEN
        RAISE EXCEPTION 'opd_order_sample_required' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- lis_guard_result (latest: 118_lab_information_system.sql) + merge exemption
CREATE OR REPLACE FUNCTION lis_guard_result() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_open   TEXT[] := ARRAY['status', 'updated_at', 'has_critical_value', 'technician_admin_id', 'technician_name',
                             'entered_at', 'pathologist_admin_id', 'pathologist_snapshot', 'letterhead_snapshot',
                             'patient_snapshot', 'validated_at', 'pdf_storage_path'];
    v_signed TEXT[] := ARRAY['status', 'updated_at', 'pdf_storage_path'];
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'lis_append_only:lab_results' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('validated', 'amended') THEN
        IF (to_jsonb(NEW) - v_signed) IS DISTINCT FROM (to_jsonb(OLD) - v_signed)
           OR (OLD.pdf_storage_path IS NOT NULL AND NEW.pdf_storage_path IS DISTINCT FROM OLD.pdf_storage_path)
           OR (NEW.status <> OLD.status AND NOT (OLD.status = 'validated' AND NEW.status = 'amended'
                                                AND COALESCE(current_setting('kriya.lis_amending', true), '')
                                                    = OLD.id::TEXT)) THEN
            RAISE EXCEPTION 'lis_report_locked:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - v_open) IS DISTINCT FROM (to_jsonb(OLD) - v_open) THEN
        RAISE EXCEPTION 'lis_report_locked:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'amended' THEN   -- only a validated report is superseded
        RAISE EXCEPTION 'lis_bad_transition:%->amended', OLD.status USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- docs_guard_consent (latest: 119_clinical_documents.sql) + merge exemption
CREATE OR REPLACE FUNCTION docs_guard_consent() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
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

-- docs_guard_certificate (latest: 119_clinical_documents.sql) + merge exemption
CREATE OR REPLACE FUNCTION docs_guard_certificate() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    -- certificate_number: generated, so NULL in a BEFORE trigger's NEW (and never writable)
    v_open TEXT[] := ARRAY['pdf_storage_path', 'status', 'void_reason', 'voided_at', 'voided_by',
                           'delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count',
                           'certificate_number'];
BEGIN
    -- 121: an emergency identity merge may move this row to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
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

-- ── 2. Emergency visits ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS emergency_visits (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id              UUID NULL,
    patient_id             UUID NOT NULL,
    original_patient_id    UUID NULL,          -- the temporary record, kept after a merge
    visit_year             SMALLINT NOT NULL,
    visit_seq              INTEGER NOT NULL CHECK (visit_seq > 0),
    visit_number           TEXT GENERATED ALWAYS AS (opd_format_number('EMG-', visit_year, visit_seq, 5)) STORED,
    is_unknown_patient     BOOLEAN NOT NULL DEFAULT false,
    estimated_age          SMALLINT NULL CHECK (estimated_age BETWEEN 0 AND 130),
    estimated_gender       TEXT NULL CHECK (estimated_gender IN ('male', 'female', 'other')),
    triage_category        TEXT NOT NULL CHECK (triage_category IN ('red', 'yellow', 'green', 'black')),
    triaged_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    arrival_mode           TEXT NOT NULL DEFAULT 'walk_in'
                           CHECK (arrival_mode IN ('ambulance', 'walk_in', 'police', 'transfer', 'other')),
    brought_by_name        TEXT NULL CHECK (char_length(brought_by_name) <= 120),
    brought_by_phone       TEXT NULL CHECK (brought_by_phone ~ '^\+?[0-9]{10,15}$'),
    chief_complaints       TEXT NOT NULL CHECK (char_length(btrim(chief_complaints)) BETWEEN 2 AND 2000),
    initial_vitals         JSONB NULL CHECK (initial_vitals IS NULL OR jsonb_typeof(initial_vitals) = 'object'),
    latest_vitals          JSONB NULL CHECK (latest_vitals IS NULL OR jsonb_typeof(latest_vitals) = 'object'),
    clinical_notes         TEXT NULL CHECK (char_length(clinical_notes) <= 8000),
    provisional_diagnosis  TEXT NULL CHECK (char_length(provisional_diagnosis) <= 500),
    attending_doctor_id    UUID NULL,
    status                 TEXT NOT NULL DEFAULT 'triage'
                           CHECK (status IN ('triage', 'under_treatment', 'admitted_ipd', 'transferred', 'discharged',
                                             'lama', 'deceased')),
    disposition_notes      TEXT NULL CHECK (char_length(disposition_notes) <= 1000),
    disposition_at         TIMESTAMPTZ NULL,
    disposed_by            UUID NULL,
    disposed_by_name       TEXT NULL CHECK (char_length(disposed_by_name) <= 100),
    transfer_to            TEXT NULL CHECK (char_length(transfer_to) <= 200),
    time_of_death          TIMESTAMPTZ NULL,
    ipd_admission_id       UUID NULL,
    lama_consent_id        UUID NULL,
    death_certificate_id   UUID NULL,
    merged_into_patient_id UUID NULL,
    registered_by          UUID NULL,
    registered_by_name     TEXT NOT NULL CHECK (char_length(btrim(registered_by_name)) BETWEEN 1 AND 100),
    idempotency_key        TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT emergency_visits_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT emergency_visits_seq_key UNIQUE (clinic_id, visit_year, visit_seq),
    CONSTRAINT emergency_visits_disposed CHECK (
        (status IN ('triage', 'under_treatment')) = (disposition_at IS NULL)),
    CONSTRAINT emergency_visits_death CHECK (status <> 'deceased' OR time_of_death IS NOT NULL),
    CONSTRAINT emergency_visits_transfer CHECK (status <> 'transferred' OR transfer_to IS NOT NULL),
    CONSTRAINT emergency_visits_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT emergency_visits_original_fk FOREIGN KEY (clinic_id, original_patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT emergency_visits_merged_fk FOREIGN KEY (clinic_id, merged_into_patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT emergency_visits_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT emergency_visits_doctor_fk FOREIGN KEY (clinic_id, attending_doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT emergency_visits_admission_fk FOREIGN KEY (clinic_id, ipd_admission_id)
        REFERENCES ipd_admissions (clinic_id, id) ON DELETE SET NULL (ipd_admission_id),
    CONSTRAINT emergency_visits_lama_fk FOREIGN KEY (clinic_id, lama_consent_id)
        REFERENCES clinical_consents (clinic_id, id) ON DELETE SET NULL (lama_consent_id),
    CONSTRAINT emergency_visits_death_cert_fk FOREIGN KEY (clinic_id, death_certificate_id)
        REFERENCES clinical_certificates (clinic_id, id) ON DELETE SET NULL (death_certificate_id)
);
CREATE INDEX IF NOT EXISTS idx_emergency_visits_status  ON emergency_visits (clinic_id, status, triage_category);
CREATE INDEX IF NOT EXISTS idx_emergency_visits_patient ON emergency_visits (clinic_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_emergency_visits_created ON emergency_visits (clinic_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_emergency_visits_idempotency
    ON emergency_visits (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
-- one open ER visit per patient
CREATE UNIQUE INDEX IF NOT EXISTS uq_emergency_visits_one_open
    ON emergency_visits (clinic_id, patient_id) WHERE status IN ('triage', 'under_treatment');

-- ── 3. Medico-legal case register ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS emergency_mlc_records (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id              UUID NULL,
    emergency_visit_id     UUID NOT NULL,
    patient_id             UUID NOT NULL,
    mlc_year               SMALLINT NOT NULL,
    mlc_seq                INTEGER NOT NULL CHECK (mlc_seq > 0),
    mlc_number             TEXT GENERATED ALWAYS AS (opd_format_number('MLC-', mlc_year, mlc_seq, 5)) STORED,
    police_station_name    TEXT NOT NULL CHECK (char_length(btrim(police_station_name)) BETWEEN 2 AND 150),
    police_intimation_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    officer_name           TEXT NULL CHECK (char_length(officer_name) <= 120),
    officer_badge          TEXT NULL CHECK (char_length(officer_badge) <= 60),
    incident_place         TEXT NULL CHECK (char_length(incident_place) <= 250),
    incident_time          TIMESTAMPTZ NULL,
    injury_type            TEXT NOT NULL CHECK (injury_type IN ('rto', 'physical_assault', 'poisoning', 'burns',
                                                                'fall_from_height', 'workplace', 'gunshot_stab',
                                                                'hanging', 'other')),
    injury_description     TEXT NOT NULL CHECK (char_length(btrim(injury_description)) BETWEEN 5 AND 4000),
    is_dying_declaration   BOOLEAN NOT NULL DEFAULT false,
    doctor_id              UUID NOT NULL,
    doctor_snapshot        JSONB NOT NULL,     -- {doctor_id, name, qualifications, registration_number, registration_council}
    letterhead_snapshot    JSONB NULL,
    patient_snapshot       JSONB NULL,         -- identity as known when recorded (an unknown patient stays "Unknown")
    is_locked              BOOLEAN NOT NULL DEFAULT false,
    locked_at              TIMESTAMPTZ NULL,
    locked_by              UUID NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT emergency_mlc_records_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT emergency_mlc_records_seq_key UNIQUE (clinic_id, mlc_year, mlc_seq),
    CONSTRAINT emergency_mlc_records_visit_key UNIQUE (clinic_id, emergency_visit_id),
    CONSTRAINT emergency_mlc_records_locked CHECK (is_locked = (locked_at IS NOT NULL)),
    CONSTRAINT emergency_mlc_records_signer CHECK (btrim(COALESCE(doctor_snapshot ->> 'registration_number', '')) <> ''),
    CONSTRAINT emergency_mlc_records_visit_fk FOREIGN KEY (clinic_id, emergency_visit_id)
        REFERENCES emergency_visits (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT emergency_mlc_records_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT emergency_mlc_records_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT emergency_mlc_records_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);

-- ── 4. Merge provenance ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patient_merge_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    source_patient_id UUID NOT NULL,
    target_patient_id UUID NOT NULL,
    source_snapshot   JSONB NOT NULL,          -- the temporary identity: name, mrn, phone placeholder
    moved             JSONB NOT NULL,          -- rows moved per table
    reason            TEXT NULL CHECK (char_length(reason) <= 300),
    actor_id          UUID NULL,
    actor_name        TEXT NOT NULL CHECK (char_length(btrim(actor_name)) BETWEEN 1 AND 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patient_merge_log_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT patient_merge_log_source_fk FOREIGN KEY (clinic_id, source_patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT patient_merge_log_target_fk FOREIGN KEY (clinic_id, target_patient_id) REFERENCES patients (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_patient_merge_log_target ON patient_merge_log (clinic_id, target_patient_id);

-- ── 5. Guards ──────────────────────────────────────────────────────────────
-- Set by the emergency_* functions for one clinic, transaction-local.
CREATE OR REPLACE FUNCTION emergency_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.emergency_tx', true), '') = p_clinic_id::TEXT
$$;

CREATE OR REPLACE FUNCTION emergency_guard_visit() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    -- what the ER team edits while the patient is on the floor (PATCH)
    v_open TEXT[] := ARRAY['updated_at', 'triage_category', 'triaged_at', 'attending_doctor_id', 'clinical_notes',
                           'latest_vitals', 'provisional_diagnosis', 'status',
                           'visit_number'];   -- generated: NULL in a BEFORE trigger's NEW
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'emergency_append_only' USING ERRCODE = 'P0001';
    END IF;
    IF (NEW.clinic_id, NEW.visit_year, NEW.visit_seq, NEW.created_at, NEW.registered_by)
       IS DISTINCT FROM (OLD.clinic_id, OLD.visit_year, OLD.visit_seq, OLD.created_at, OLD.registered_by) THEN
        RAISE EXCEPTION 'emergency_identity_immutable' USING ERRCODE = 'P0001';
    END IF;
    -- an IPD purge unlinks the admission (ON DELETE SET NULL); the functions do the rest
    IF emergency_in_tx(OLD.clinic_id) OR opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN
        RETURN NEW;
    END IF;
    IF OLD.status NOT IN ('triage', 'under_treatment') THEN
        RAISE EXCEPTION 'emergency_visit_closed' USING ERRCODE = 'P0001';
    END IF;
    IF (to_jsonb(NEW) - v_open) IS DISTINCT FROM (to_jsonb(OLD) - v_open)
       OR NEW.status NOT IN ('triage', 'under_treatment') THEN
        RAISE EXCEPTION 'emergency_via_function_only' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION emergency_guard_mlc() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'emergency_mlc_locked' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.is_locked THEN
        -- a locked MLC is a statutory record: only an identity merge may repoint it
        IF patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'emergency_mlc_locked' USING ERRCODE = 'P0001';
    END IF;
    IF NOT emergency_in_tx(OLD.clinic_id) THEN
        RAISE EXCEPTION 'emergency_via_function_only' USING ERRCODE = 'P0001';
    END IF;
    IF (NEW.clinic_id, NEW.emergency_visit_id, NEW.mlc_year, NEW.mlc_seq, NEW.created_at)
       IS DISTINCT FROM (OLD.clinic_id, OLD.emergency_visit_id, OLD.mlc_year, OLD.mlc_seq, OLD.created_at) THEN
        RAISE EXCEPTION 'emergency_identity_immutable' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION emergency_guard_merge_log() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND (opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id)) THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'emergency_append_only' USING ERRCODE = 'P0001';
END $$;

DROP TRIGGER IF EXISTS trg_emergency_visits_guard ON emergency_visits;
CREATE TRIGGER trg_emergency_visits_guard BEFORE UPDATE OR DELETE ON emergency_visits
    FOR EACH ROW EXECUTE FUNCTION emergency_guard_visit();
DROP TRIGGER IF EXISTS trg_emergency_visits_touch ON emergency_visits;
CREATE TRIGGER trg_emergency_visits_touch BEFORE UPDATE ON emergency_visits
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_emergency_mlc_guard ON emergency_mlc_records;
CREATE TRIGGER trg_emergency_mlc_guard BEFORE UPDATE OR DELETE ON emergency_mlc_records
    FOR EACH ROW EXECUTE FUNCTION emergency_guard_mlc();
DROP TRIGGER IF EXISTS trg_emergency_mlc_touch ON emergency_mlc_records;
CREATE TRIGGER trg_emergency_mlc_touch BEFORE UPDATE ON emergency_mlc_records
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_patient_merge_log_guard ON patient_merge_log;
CREATE TRIGGER trg_patient_merge_log_guard BEFORE UPDATE OR DELETE ON patient_merge_log
    FOR EACH ROW EXECUTE FUNCTION emergency_guard_merge_log();

-- ── 6. Rapid registration ──────────────────────────────────────────────────
-- p_payload: {is_unknown, estimated_gender, estimated_age} | {patient_id} | {name, phone, gender, age}
--            + triage_category, arrival_mode, brought_by_*, chief_complaints, initial_vitals,
--              attending_doctor_id, branch_id, idempotency_key
CREATE OR REPLACE FUNCTION emergency_register(p_clinic_id UUID, p_payload JSONB, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_key TEXT := NULLIF(p_payload ->> 'idempotency_key', '');
    v_year SMALLINT := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
    v_seq INTEGER; v_patient UUID; v_unknown BOOLEAN := COALESCE((p_payload ->> 'is_unknown')::BOOLEAN, false);
    v_gender TEXT := NULLIF(p_payload ->> 'estimated_gender', ''); v_age SMALLINT := NULLIF(p_payload ->> 'estimated_age', '')::SMALLINT;
    v_phone TEXT; v_name TEXT; e emergency_visits; p patients; v_constraint TEXT;
BEGIN
    PERFORM set_config('kriya.emergency_tx', p_clinic_id::TEXT, true);
    IF v_key IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('emg:' || p_clinic_id || ':' || v_key, 0));
        SELECT * INTO e FROM emergency_visits WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
        IF FOUND THEN
            SELECT * INTO p FROM patients WHERE clinic_id = p_clinic_id AND id = e.patient_id;
            RETURN to_jsonb(e) || jsonb_build_object('replayed', true, 'patient_name', p.name, 'mrn', p.mrn);
        END IF;
    END IF;
    v_seq := opd_next_counter(p_clinic_id, 'emg:' || v_year);

    IF v_unknown THEN
        v_name := 'Unknown ' || COALESCE(initcap(v_gender), 'Patient') || COALESCE(' ~' || v_age || 'y', '');
        INSERT INTO patients (clinic_id, phone, name, gender, age_years, age_recorded_on, is_temporary)
        VALUES (p_clinic_id, opd_format_number('EMG-', v_year, v_seq, 5), v_name, v_gender, v_age,
                CASE WHEN v_age IS NOT NULL THEN (now() AT TIME ZONE 'Asia/Kolkata')::DATE END, true)
        RETURNING id INTO v_patient;
    ELSIF NULLIF(p_payload ->> 'patient_id', '') IS NOT NULL THEN
        SELECT * INTO p FROM patients WHERE clinic_id = p_clinic_id AND id = (p_payload ->> 'patient_id')::UUID;
        IF NOT FOUND THEN RAISE EXCEPTION 'emergency_patient_not_found' USING ERRCODE = 'P0001'; END IF;
        IF p.merged_into_patient_id IS NOT NULL THEN RAISE EXCEPTION 'emergency_patient_merged' USING ERRCODE = 'P0001'; END IF;
        v_patient := p.id;
    ELSE
        v_phone := NULLIF(btrim(p_payload ->> 'phone'), '');
        v_name := NULLIF(btrim(p_payload ->> 'name'), '');
        IF v_phone IS NULL OR v_name IS NULL THEN RAISE EXCEPTION 'emergency_identity_required' USING ERRCODE = 'P0001'; END IF;
        SELECT id INTO v_patient FROM patients WHERE clinic_id = p_clinic_id AND phone = v_phone;
        IF v_patient IS NULL THEN
            INSERT INTO patients (clinic_id, phone, name, gender, age_years, age_recorded_on)
            VALUES (p_clinic_id, v_phone, left(v_name, 100), NULLIF(p_payload ->> 'gender', ''),
                    NULLIF(p_payload ->> 'age', '')::SMALLINT,
                    CASE WHEN NULLIF(p_payload ->> 'age', '') IS NOT NULL THEN (now() AT TIME ZONE 'Asia/Kolkata')::DATE END)
            ON CONFLICT (clinic_id, phone) DO NOTHING
            RETURNING id INTO v_patient;
            IF v_patient IS NULL THEN   -- registered concurrently
                SELECT id INTO v_patient FROM patients WHERE clinic_id = p_clinic_id AND phone = v_phone;
            END IF;
        END IF;
    END IF;
    PERFORM opd_assign_mrn(p_clinic_id, v_patient);

    BEGIN
        INSERT INTO emergency_visits (clinic_id, branch_id, patient_id, visit_year, visit_seq, is_unknown_patient,
                                      estimated_age, estimated_gender, triage_category, arrival_mode, brought_by_name,
                                      brought_by_phone, chief_complaints, initial_vitals, latest_vitals,
                                      attending_doctor_id, status, registered_by, registered_by_name, idempotency_key)
        VALUES (p_clinic_id, NULLIF(p_payload ->> 'branch_id', '')::UUID, v_patient, v_year, v_seq, v_unknown,
                v_age, v_gender, p_payload ->> 'triage_category', COALESCE(p_payload ->> 'arrival_mode', 'walk_in'),
                NULLIF(btrim(p_payload ->> 'brought_by_name'), ''), NULLIF(btrim(p_payload ->> 'brought_by_phone'), ''),
                btrim(p_payload ->> 'chief_complaints'), p_payload -> 'initial_vitals', p_payload -> 'initial_vitals',
                NULLIF(p_payload ->> 'attending_doctor_id', '')::UUID,
                CASE WHEN NULLIF(p_payload ->> 'attending_doctor_id', '') IS NOT NULL THEN 'under_treatment' ELSE 'triage' END,
                p_actor_id, left(p_actor_name, 100), v_key)
        RETURNING * INTO e;
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
        IF v_constraint = 'uq_emergency_visits_one_open' THEN
            RAISE EXCEPTION 'emergency_visit_open' USING ERRCODE = 'P0001';
        END IF;
        RAISE;
    END;
    SELECT * INTO p FROM patients WHERE clinic_id = p_clinic_id AND id = v_patient;
    PERFORM set_config('kriya.emergency_tx', '', true);
    RETURN to_jsonb(e) || jsonb_build_object('replayed', false, 'patient_name', p.name, 'mrn', p.mrn);
END $$;

-- ── 7. MLC: record / update until locked, then frozen ──────────────────────
CREATE OR REPLACE FUNCTION emergency_record_mlc(
    p_clinic_id UUID, p_visit_id UUID, p_payload JSONB, p_doctor_id UUID, p_doctor_snapshot JSONB,
    p_letterhead_snapshot JSONB, p_lock BOOLEAN, p_actor_id UUID
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE e emergency_visits; m emergency_mlc_records; p patients;
        v_year SMALLINT := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
BEGIN
    PERFORM set_config('kriya.emergency_tx', p_clinic_id::TEXT, true);
    SELECT * INTO e FROM emergency_visits WHERE clinic_id = p_clinic_id AND id = p_visit_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'emergency_visit_not_found' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO m FROM emergency_mlc_records WHERE clinic_id = p_clinic_id AND emergency_visit_id = p_visit_id FOR UPDATE;
    IF m.id IS NOT NULL AND m.is_locked THEN RAISE EXCEPTION 'emergency_mlc_locked' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO p FROM patients WHERE clinic_id = p_clinic_id AND id = e.patient_id;
    IF m.id IS NULL THEN
        INSERT INTO emergency_mlc_records (clinic_id, branch_id, emergency_visit_id, patient_id, mlc_year, mlc_seq,
                                           police_station_name, police_intimation_time, officer_name, officer_badge,
                                           incident_place, incident_time, injury_type, injury_description,
                                           is_dying_declaration, doctor_id, doctor_snapshot, letterhead_snapshot,
                                           patient_snapshot)
        VALUES (p_clinic_id, e.branch_id, e.id, e.patient_id, v_year, opd_next_counter(p_clinic_id, 'mlc:' || v_year),
                btrim(p_payload ->> 'police_station_name'),
                COALESCE(NULLIF(p_payload ->> 'police_intimation_time', '')::TIMESTAMPTZ, now()),
                NULLIF(btrim(p_payload ->> 'officer_name'), ''), NULLIF(btrim(p_payload ->> 'officer_badge'), ''),
                NULLIF(btrim(p_payload ->> 'incident_place'), ''), NULLIF(p_payload ->> 'incident_time', '')::TIMESTAMPTZ,
                p_payload ->> 'injury_type', btrim(p_payload ->> 'injury_description'),
                COALESCE((p_payload ->> 'is_dying_declaration')::BOOLEAN, false), p_doctor_id, p_doctor_snapshot,
                p_letterhead_snapshot,
                jsonb_build_object('name', p.name, 'mrn', p.mrn, 'gender', COALESCE(p.gender, e.estimated_gender),
                                   'age_years', COALESCE(p.age_years, e.estimated_age), 'visit_number', e.visit_number,
                                   'arrived_at', e.created_at, 'arrival_mode', e.arrival_mode,
                                   'brought_by_name', e.brought_by_name, 'brought_by_phone', e.brought_by_phone,
                                   'is_unknown', e.is_unknown_patient))
        RETURNING * INTO m;
    ELSE
        UPDATE emergency_mlc_records SET
            police_station_name = btrim(p_payload ->> 'police_station_name'),
            police_intimation_time = COALESCE(NULLIF(p_payload ->> 'police_intimation_time', '')::TIMESTAMPTZ, police_intimation_time),
            officer_name = NULLIF(btrim(p_payload ->> 'officer_name'), ''),
            officer_badge = NULLIF(btrim(p_payload ->> 'officer_badge'), ''),
            incident_place = NULLIF(btrim(p_payload ->> 'incident_place'), ''),
            incident_time = NULLIF(p_payload ->> 'incident_time', '')::TIMESTAMPTZ,
            injury_type = p_payload ->> 'injury_type',
            injury_description = btrim(p_payload ->> 'injury_description'),
            is_dying_declaration = COALESCE((p_payload ->> 'is_dying_declaration')::BOOLEAN, false),
            doctor_id = p_doctor_id, doctor_snapshot = p_doctor_snapshot, letterhead_snapshot = p_letterhead_snapshot
         WHERE clinic_id = p_clinic_id AND id = m.id
        RETURNING * INTO m;
    END IF;
    IF p_lock THEN
        UPDATE emergency_mlc_records SET is_locked = true, locked_at = now(), locked_by = p_actor_id
         WHERE clinic_id = p_clinic_id AND id = m.id
        RETURNING * INTO m;
    END IF;
    PERFORM set_config('kriya.emergency_tx', '', true);
    RETURN to_jsonb(m);
END $$;

-- ── 8. Disposition ─────────────────────────────────────────────────────────
-- p_action: admitted_ipd | transferred | discharged | lama | deceased
-- p_payload: notes, transfer_to, time_of_death, consent_id, certificate_id,
--            ipd: {admitting_doctor_id, attending_doctor_id, bed_id, provisional_diagnosis, payer_type, ...}
CREATE OR REPLACE FUNCTION emergency_disposition(
    p_clinic_id UUID, p_visit_id UUID, p_action TEXT, p_payload JSONB, p_doctor_id UUID,
    p_actor_id UUID, p_actor_name TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE e emergency_visits; v_adm JSONB; v_ipd JSONB; v_consent UUID; v_cert UUID; v_tod TIMESTAMPTZ;
BEGIN
    SELECT * INTO e FROM emergency_visits WHERE clinic_id = p_clinic_id AND id = p_visit_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'emergency_visit_not_found' USING ERRCODE = 'P0001'; END IF;
    IF e.status NOT IN ('triage', 'under_treatment') THEN
        IF e.status = p_action THEN RETURN to_jsonb(e) || jsonb_build_object('replayed', true); END IF;
        RAISE EXCEPTION 'emergency_visit_closed' USING ERRCODE = 'P0001';
    END IF;

    IF p_action = 'admitted_ipd' THEN
        v_ipd := COALESCE(p_payload -> 'ipd', '{}'::jsonb);
        v_adm := ipd_admit(p_clinic_id, v_ipd || jsonb_build_object(
                    'patient_id', e.patient_id, 'source', 'emergency', 'branch_id', e.branch_id,
                    'priority', CASE e.triage_category WHEN 'red' THEN 'emergency' WHEN 'yellow' THEN 'urgent' ELSE 'elective' END,
                    'reason', left('Emergency ' || e.visit_number || ': ' || e.chief_complaints, 500),
                    'provisional_diagnosis', COALESCE(NULLIF(v_ipd ->> 'provisional_diagnosis', ''), e.provisional_diagnosis),
                    'attendant_name', e.brought_by_name, 'attendant_phone', e.brought_by_phone,
                    'idempotency_key', 'emg-' || e.id),
                  NULLIF(v_ipd ->> 'bed_id', '')::UUID, p_actor_id, p_actor_name);
    ELSIF p_action = 'transferred' THEN
        IF NULLIF(btrim(p_payload ->> 'transfer_to'), '') IS NULL THEN
            RAISE EXCEPTION 'emergency_transfer_to_required' USING ERRCODE = 'P0001';
        END IF;
    ELSIF p_action = 'lama' THEN
        -- G9: leaving against medical advice needs the signed refusal on record
        SELECT c.id INTO v_consent FROM clinical_consents c
         WHERE c.clinic_id = p_clinic_id AND c.patient_id = e.patient_id AND c.consent_type = 'refusal_lama'
           AND c.is_signed AND c.signed_at >= e.created_at
           AND (NULLIF(p_payload ->> 'consent_id', '') IS NULL OR c.id = (p_payload ->> 'consent_id')::UUID)
         ORDER BY c.signed_at DESC LIMIT 1;
        IF v_consent IS NULL THEN RAISE EXCEPTION 'emergency_lama_consent_missing' USING ERRCODE = 'P0001'; END IF;
    ELSIF p_action = 'deceased' THEN
        IF p_doctor_id IS NULL THEN RAISE EXCEPTION 'emergency_doctor_required' USING ERRCODE = 'P0001'; END IF;
        v_tod := COALESCE(NULLIF(p_payload ->> 'time_of_death', '')::TIMESTAMPTZ, now());
        IF v_tod < e.created_at - INTERVAL '1 day' OR v_tod > now() + INTERVAL '5 minutes' THEN
            RAISE EXCEPTION 'emergency_bad_time_of_death' USING ERRCODE = 'P0001';
        END IF;
        -- G9 death intimation, when already issued (it may follow)
        SELECT c.id INTO v_cert FROM clinical_certificates c
         WHERE c.clinic_id = p_clinic_id AND c.patient_id = e.patient_id AND c.certificate_type = 'death_intimation'
           AND c.status = 'issued' AND c.created_at >= e.created_at
         ORDER BY c.created_at DESC LIMIT 1;
    ELSIF p_action <> 'discharged' THEN
        RAISE EXCEPTION 'emergency_bad_disposition' USING ERRCODE = 'P0001';
    END IF;

    PERFORM set_config('kriya.emergency_tx', p_clinic_id::TEXT, true);
    UPDATE emergency_visits SET
        status = p_action, disposition_at = now(), disposed_by = p_actor_id, disposed_by_name = left(p_actor_name, 100),
        disposition_notes = NULLIF(btrim(p_payload ->> 'notes'), ''),
        transfer_to = CASE WHEN p_action = 'transferred' THEN btrim(p_payload ->> 'transfer_to') END,
        time_of_death = v_tod, lama_consent_id = v_consent, death_certificate_id = v_cert,
        ipd_admission_id = (v_adm ->> 'id')::UUID,
        attending_doctor_id = COALESCE(attending_doctor_id, p_doctor_id)
     WHERE clinic_id = p_clinic_id AND id = e.id
    RETURNING * INTO e;
    PERFORM set_config('kriya.emergency_tx', '', true);
    RETURN to_jsonb(e) || jsonb_build_object('replayed', false, 'admission', v_adm);
END $$;

-- ── 9. Atomic identity merge ───────────────────────────────────────────────
CREATE OR REPLACE FUNCTION emergency_merge_patient(
    p_clinic_id UUID, p_temp_patient_id UUID, p_target_patient_id UUID, p_actor_id UUID, p_actor_name TEXT,
    p_reason TEXT DEFAULT NULL
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE src patients; tgt patients; t TEXT; n INTEGER; v_moved JSONB := '{}'::jsonb; v_constraint TEXT;
BEGIN
    IF p_temp_patient_id = p_target_patient_id THEN RAISE EXCEPTION 'emergency_merge_same' USING ERRCODE = 'P0001'; END IF;
    -- lock both identities in a fixed order: two merges never deadlock
    PERFORM 1 FROM patients WHERE clinic_id = p_clinic_id AND id IN (p_temp_patient_id, p_target_patient_id)
     ORDER BY id FOR UPDATE;
    SELECT * INTO src FROM patients WHERE clinic_id = p_clinic_id AND id = p_temp_patient_id;
    SELECT * INTO tgt FROM patients WHERE clinic_id = p_clinic_id AND id = p_target_patient_id;
    IF src.id IS NULL OR tgt.id IS NULL THEN RAISE EXCEPTION 'emergency_patient_not_found' USING ERRCODE = 'P0001'; END IF;
    IF NOT src.is_temporary THEN RAISE EXCEPTION 'emergency_merge_not_temporary' USING ERRCODE = 'P0001'; END IF;
    IF src.merged_into_patient_id IS NOT NULL THEN
        IF src.merged_into_patient_id = tgt.id THEN
            RETURN jsonb_build_object('replayed', true, 'target_patient_id', tgt.id, 'merged_target_name', tgt.name,
                                      'mrn', tgt.mrn);
        END IF;
        RAISE EXCEPTION 'emergency_patient_merged' USING ERRCODE = 'P0001';
    END IF;
    IF tgt.is_temporary OR tgt.merged_into_patient_id IS NOT NULL THEN
        RAISE EXCEPTION 'emergency_merge_bad_target' USING ERRCODE = 'P0001';
    END IF;

    PERFORM set_config('kriya.patient_merge', p_clinic_id || ':' || src.id || ':' || tgt.id, true);
    PERFORM set_config('kriya.emergency_tx', p_clinic_id::TEXT, true);
    BEGIN
        UPDATE emergency_visits SET original_patient_id = COALESCE(original_patient_id, patient_id),
               patient_id = tgt.id, merged_into_patient_id = tgt.id, is_unknown_patient = false
         WHERE clinic_id = p_clinic_id AND patient_id = src.id;
        GET DIAGNOSTICS n = ROW_COUNT; v_moved := v_moved || jsonb_build_object('emergency_visits', n);
        FOREACH t IN ARRAY ARRAY['emergency_mlc_records', 'appointments', 'opd_encounters', 'opd_prescriptions',
                                 'opd_invoices', 'ipd_admissions', 'opd_orders', 'lab_results', 'clinical_consents',
                                 'clinical_certificates', 'voice_calls'] LOOP
            EXECUTE format('UPDATE %I SET patient_id = $1 WHERE clinic_id = $2 AND patient_id = $3', t)
              USING tgt.id, p_clinic_id, src.id;
            GET DIAGNOSTICS n = ROW_COUNT; v_moved := v_moved || jsonb_build_object(t, n);
        END LOOP;
        UPDATE lab_reports SET matched_patient_id = tgt.id WHERE clinic_id = p_clinic_id AND matched_patient_id = src.id;
        GET DIAGNOSTICS n = ROW_COUNT; v_moved := v_moved || jsonb_build_object('lab_reports', n);
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
        IF v_constraint IN ('uq_ipd_admissions_one_active', 'uq_emergency_visits_one_open') THEN
            RAISE EXCEPTION 'emergency_merge_conflict:%', v_constraint USING ERRCODE = 'P0001';
        END IF;
        RAISE;
    END;
    PERFORM set_config('kriya.patient_merge', '', true);
    UPDATE patients SET merged_into_patient_id = tgt.id, merged_at = now() WHERE clinic_id = p_clinic_id AND id = src.id;
    INSERT INTO patient_merge_log (clinic_id, source_patient_id, target_patient_id, source_snapshot, moved, reason,
                                   actor_id, actor_name)
    VALUES (p_clinic_id, src.id, tgt.id,
            jsonb_build_object('name', src.name, 'mrn', src.mrn, 'phone', src.phone, 'gender', src.gender,
                               'age_years', src.age_years),
            v_moved, NULLIF(btrim(p_reason), ''), p_actor_id, left(p_actor_name, 100));
    PERFORM set_config('kriya.emergency_tx', '', true);
    RETURN jsonb_build_object('replayed', false, 'target_patient_id', tgt.id, 'merged_target_name', tgt.name,
                              'mrn', tgt.mrn, 'source_mrn', src.mrn, 'moved', v_moved);
END $$;

-- ── 10. Hygiene, lockout, RLS ──────────────────────────────────────────────
ALTER FUNCTION patient_merge_allows(oid, jsonb, jsonb) SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_signed_record() SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_invoice() SET search_path = public, pg_temp;
ALTER FUNCTION ipd_guard_admission() SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_order() SET search_path = public, pg_temp;
ALTER FUNCTION lis_guard_result() SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_consent() SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_certificate() SET search_path = public, pg_temp;
ALTER FUNCTION emergency_in_tx(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION emergency_guard_visit() SET search_path = public, pg_temp;
ALTER FUNCTION emergency_guard_mlc() SET search_path = public, pg_temp;
ALTER FUNCTION emergency_guard_merge_log() SET search_path = public, pg_temp;
ALTER FUNCTION emergency_register(uuid, jsonb, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION emergency_record_mlc(uuid, uuid, jsonb, uuid, jsonb, jsonb, boolean, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION emergency_disposition(uuid, uuid, text, jsonb, uuid, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION emergency_merge_patient(uuid, uuid, uuid, uuid, text, text) SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['emergency_register(uuid,jsonb,uuid,text)',
                             'emergency_record_mlc(uuid,uuid,jsonb,uuid,jsonb,jsonb,boolean,uuid)',
                             'emergency_disposition(uuid,uuid,text,jsonb,uuid,uuid,text)',
                             'emergency_merge_patient(uuid,uuid,uuid,uuid,text,text)']
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

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['emergency_visits', 'emergency_mlc_records', 'patient_merge_log'] LOOP
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

-- ── 11. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.emergency_visits') IS NULL OR to_regclass('public.emergency_mlc_records') IS NULL
       OR to_regclass('public.patient_merge_log') IS NULL THEN
        RAISE EXCEPTION '121 verify: emergency tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN ('emergency_visits_seq_key', 'emergency_visits_patient_fk',
          'emergency_mlc_records_visit_key', 'emergency_mlc_records_signer', 'patients_merged_into_fk',
          'patients_merge_consistent')) <> 6 THEN
        RAISE EXCEPTION '121 verify: constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('trg_emergency_visits_guard',
          'trg_emergency_mlc_guard', 'trg_patient_merge_log_guard')) <> 3 THEN
        RAISE EXCEPTION '121 verify: triggers missing';
    END IF;
    IF to_regprocedure('emergency_merge_patient(uuid,uuid,uuid,uuid,text,text)') IS NULL
       OR to_regprocedure('emergency_register(uuid,jsonb,uuid,text)') IS NULL
       OR to_regprocedure('emergency_disposition(uuid,uuid,text,jsonb,uuid,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '121 verify: functions missing';
    END IF;
    IF (SELECT count(*) FROM pg_proc WHERE proname IN ('opd_guard_signed_record', 'opd_guard_invoice',
          'ipd_guard_admission', 'opd_guard_order', 'lis_guard_result', 'docs_guard_consent', 'docs_guard_certificate')
          AND prosrc LIKE '%patient_merge_allows%') <> 7 THEN
        RAISE EXCEPTION '121 verify: guards missing the merge exemption';
    END IF;
END $$;
