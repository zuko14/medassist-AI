-- ============================================================================
-- Rollback 111: Kriya IPD OS core
-- DESTROYS inpatient clinical + financial records. Take pg_dump -t 'ipd_*' FIRST.
-- The normal rollback is the owner toggle (ipd_state OFF/WIND_DOWN) + code revert:
-- pre-111 code runs unchanged on a 111 schema. Apply only for full teardown.
-- IPD final bills (opd_invoices rows) are kept; they lose only their admission link.
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM ipd_admissions WHERE status IN ('pending', 'admitted', 'discharge_pending')) THEN
        RAISE EXCEPTION '111_down: patients are still admitted - discharge or cancel every active stay first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_bill_items_guard ON opd_invoice_items;
DROP INDEX IF EXISTS uq_opd_invoices_one_per_admission;
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_admission_fk;

DROP TABLE IF EXISTS ipd_med_administrations, ipd_orders, ipd_observations, ipd_notes, ipd_charges,
                     ipd_discharge_summaries, ipd_events, ipd_bed_assignments CASCADE;
ALTER TABLE IF EXISTS ipd_beds DROP CONSTRAINT IF EXISTS ipd_beds_current_admission_fk;
ALTER TABLE IF EXISTS ipd_beds DROP CONSTRAINT IF EXISTS ipd_beds_reserved_admission_fk;
DROP TABLE IF EXISTS ipd_admissions, ipd_beds, ipd_wards CASCADE;
ALTER TABLE opd_invoices DROP COLUMN IF EXISTS admission_id;

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'ipd\_%'
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.sig);
    END LOOP;
END $$;

DROP INDEX IF EXISTS uq_opd_encounters_clinic_id_id;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_ipd_state_check;
ALTER TABLE clinics DROP COLUMN IF EXISTS ipd_state, DROP COLUMN IF EXISTS ipd_settings;
