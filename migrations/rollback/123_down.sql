-- ============================================================================
-- Rollback 123: Radiology Information System (G13) and PCPNDT Form F
-- DESTROYS every radiology report and every Form F. Form F is a statutory
-- record (PCPNDT Act, 1994: kept at least two years and produced on
-- inspection), so this is refused while any report or form exists. Take
-- pg_dump -t radiology_reports -t pcpndt_form_f_records FIRST and use the
-- owner purge. Orders a report completed stay completed (opd_orders is 115's).
-- The normal rollback is a code revert: pre-123 code runs unchanged on 123.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.pcpndt_form_f_records') IS NOT NULL AND EXISTS (SELECT 1 FROM pcpndt_form_f_records))
       OR (to_regclass('public.radiology_reports') IS NOT NULL AND EXISTS (SELECT 1 FROM radiology_reports)) THEN
        RAISE EXCEPTION '123_down: radiology reports / Form F exist (statutory records); export them and use the owner purge first';
    END IF;
END $$;

DROP TABLE IF EXISTS pcpndt_form_f_records;
DROP TABLE IF EXISTS radiology_reports;
DROP FUNCTION IF EXISTS radiology_finalize_report(uuid, uuid, timestamptz, uuid, uuid, jsonb, jsonb, jsonb);
DROP FUNCTION IF EXISTS radiology_record_form_f(uuid, uuid, jsonb, uuid, jsonb, uuid);
DROP FUNCTION IF EXISTS radiology_save_report(uuid, uuid, jsonb, text, timestamptz, uuid, text);
DROP FUNCTION IF EXISTS radiology_guard_form_f();
DROP FUNCTION IF EXISTS radiology_guard_report();
DROP FUNCTION IF EXISTS radiology_in_tx(uuid);
DROP FUNCTION IF EXISTS radiology_fetal_sex_text(text, boolean);
DROP FUNCTION IF EXISTS radiology_is_obstetric_text(text);
DROP INDEX IF EXISTS uq_opd_orders_clinic_id_patient;
-- opd_counters 'rad:YYYY' / 'pndt:YYYY' keys are left in clinics.opd_counters:
-- harmless, and keeping them means a re-apply never reissues a number.
