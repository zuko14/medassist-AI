-- ============================================================================
-- Rollback 122: Operation Theatre OS (G8)
-- DESTROYS the theatre list, every surgery, WHO checklist and anaesthesia
-- record. Take pg_dump -t ot_theatres -t ot_surgeries -t ot_who_checklists
-- -t ot_anaesthesia_records FIRST. Refused while any surgery exists: the
-- checklists and anaesthesia records are clinical records. Posted surgery
-- charges stay on their stays (ipd_charges is 111's). The normal rollback is a
-- code revert: pre-122 code runs unchanged on 122.
-- ============================================================================

DO $$ BEGIN
    IF to_regclass('public.ot_surgeries') IS NOT NULL AND EXISTS (SELECT 1 FROM ot_surgeries) THEN
        RAISE EXCEPTION '122_down: surgeries exist (clinical records); export them and use the owner purge first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_admissions_ot_open ON ipd_admissions;
DROP FUNCTION IF EXISTS ot_block_discharge();
DROP TABLE IF EXISTS ot_anaesthesia_records;
DROP TABLE IF EXISTS ot_who_checklists;
DROP TABLE IF EXISTS ot_surgeries;
DROP TABLE IF EXISTS ot_theatres;
DROP FUNCTION IF EXISTS ot_complete_surgery(uuid, uuid, jsonb, integer, uuid, text);
DROP FUNCTION IF EXISTS ot_set_status(uuid, uuid, text, text, uuid, text);
DROP FUNCTION IF EXISTS ot_record_anaesthesia(uuid, uuid, jsonb, boolean, uuid, jsonb, uuid);
DROP FUNCTION IF EXISTS ot_update_checklist(uuid, uuid, text, jsonb, uuid, text);
DROP FUNCTION IF EXISTS ot_checklist_items(text);
DROP FUNCTION IF EXISTS ot_schedule_surgery(uuid, jsonb, uuid, text);
DROP FUNCTION IF EXISTS ot_check_overlap();
DROP FUNCTION IF EXISTS ot_guard_record();
DROP FUNCTION IF EXISTS ot_in_tx(uuid);
DROP INDEX IF EXISTS uq_ipd_admissions_clinic_id_patient;
-- opd_counters 'ot:YYYY' keys are left in clinics.opd_counters: harmless, and
-- keeping them means a re-apply never reissues a surgery number.
