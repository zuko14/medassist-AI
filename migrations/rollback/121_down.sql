-- ============================================================================
-- Rollback 121: Emergency / Casualty OS, MLC register, identity merge
-- DESTROYS the ER register, every MLC and the merge provenance log. Take
-- pg_dump -t emergency_visits -t emergency_mlc_records -t patient_merge_log FIRST.
-- Refused while any ER visit, MLC, merge or temporary patient exists: MLCs are
-- statutory records. The normal rollback is a code revert: pre-121 code runs
-- unchanged on 121 (the guards behave exactly as before outside a merge).
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.emergency_visits') IS NOT NULL AND EXISTS (SELECT 1 FROM emergency_visits))
       OR (to_regclass('public.patient_merge_log') IS NOT NULL AND EXISTS (SELECT 1 FROM patient_merge_log))
       OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'patients' AND column_name = 'is_temporary')
          AND EXISTS (SELECT 1 FROM patients WHERE is_temporary) THEN
        RAISE EXCEPTION '121_down: emergency visits, merges or temporary patients exist';
    END IF;
END $$;

DROP TABLE IF EXISTS emergency_mlc_records;
DROP TABLE IF EXISTS emergency_visits;
DROP TABLE IF EXISTS patient_merge_log;
DROP FUNCTION IF EXISTS emergency_merge_patient(uuid, uuid, uuid, uuid, text, text);
DROP FUNCTION IF EXISTS emergency_disposition(uuid, uuid, text, jsonb, uuid, uuid, text);
DROP FUNCTION IF EXISTS emergency_record_mlc(uuid, uuid, jsonb, uuid, jsonb, jsonb, boolean, uuid);
DROP FUNCTION IF EXISTS emergency_register(uuid, jsonb, uuid, text);
DROP FUNCTION IF EXISTS emergency_guard_visit();
DROP FUNCTION IF EXISTS emergency_guard_mlc();
DROP FUNCTION IF EXISTS emergency_guard_merge_log();
DROP FUNCTION IF EXISTS emergency_in_tx(uuid);

-- pre-121 guard definitions, copied verbatim
-- opd_guard_signed_record: 103_opd_os_core.sql
CREATE OR REPLACE FUNCTION opd_guard_signed_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['updated_at', 'bmi'];
BEGIN
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

-- opd_guard_invoice: 117_gst_billing_and_credit_notes.sql
CREATE OR REPLACE FUNCTION opd_guard_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_items   INTEGER;
    v_total   INTEGER;
    v_tax     INTEGER;
    v_mutable TEXT[] := ARRAY['updated_at', 'total_paise', 'invoice_number',
                              'payment_link_gateway', 'payment_link_id', 'payment_link_url',
                              'payment_link_amount_paise', 'payment_link_created_at'];
BEGIN
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

-- ipd_guard_admission: 111_ipd_os_core.sql
CREATE OR REPLACE FUNCTION ipd_guard_admission() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
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

-- opd_guard_order: 115_investigation_and_procedure_orders.sql
CREATE OR REPLACE FUNCTION opd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['status', 'updated_at', 'cancellation_reason', 'cancelled_at', 'cancelled_by',
                              'specimen_barcode', 'sample_collected_at', 'sample_collected_by',
                              'sample_collected_by_name', 'resulted_at', 'resulted_by', 'resulted_by_name',
                              'result_summary'];
    v_ok BOOLEAN;
BEGIN
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

-- lis_guard_result: 118_lab_information_system.sql
CREATE OR REPLACE FUNCTION lis_guard_result() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_open   TEXT[] := ARRAY['status', 'updated_at', 'has_critical_value', 'technician_admin_id', 'technician_name',
                             'entered_at', 'pathologist_admin_id', 'pathologist_snapshot', 'letterhead_snapshot',
                             'patient_snapshot', 'validated_at', 'pdf_storage_path'];
    v_signed TEXT[] := ARRAY['status', 'updated_at', 'pdf_storage_path'];
BEGIN
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

-- docs_guard_consent: 119_clinical_documents.sql
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

-- docs_guard_certificate: 119_clinical_documents.sql
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

ALTER FUNCTION opd_guard_signed_record() SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_invoice() SET search_path = public, pg_temp;
ALTER FUNCTION ipd_guard_admission() SET search_path = public, pg_temp;
ALTER FUNCTION opd_guard_order() SET search_path = public, pg_temp;
ALTER FUNCTION lis_guard_result() SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_consent() SET search_path = public, pg_temp;
ALTER FUNCTION docs_guard_certificate() SET search_path = public, pg_temp;
DROP FUNCTION IF EXISTS patient_merge_allows(oid, jsonb, jsonb);

ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_merge_consistent;
ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_merged_into_fk;
ALTER TABLE patients DROP COLUMN IF EXISTS merged_at, DROP COLUMN IF EXISTS merged_into_patient_id,
                     DROP COLUMN IF EXISTS is_temporary;
