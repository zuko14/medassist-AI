-- ============================================================================
-- Rollback 103: Kriya OPD OS core (Phase 1)
-- DESTROYS OPD clinical + financial records. Take pg_dump -t 'opd_*' FIRST.
-- The normal rollback is the owner toggle (opd_state='DISABLED') + code revert.
-- Apply only when full schema teardown is explicitly intended.
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM appointments WHERE is_walk_in AND status IN ('confirmed','pending_payment','pending_review')
               AND appointment_date >= (now() + interval '5 hours 30 minutes')::date) THEN
        RAISE EXCEPTION '103_down: active walk-ins today/future — close the OPD day first';
    END IF;
END $$;

UPDATE appointments SET queue_status = CASE queue_status
        WHEN 'registered' THEN 'waiting' WHEN 'vitals_pending' THEN 'waiting'
        WHEN 'billing' THEN 'done' WHEN 'completed' THEN 'done' WHEN 'cancelled' THEN 'done'
        ELSE queue_status END
 WHERE queue_status IN ('registered','vitals_pending','billing','completed','cancelled');

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_queue_status_check;
ALTER TABLE appointments ADD CONSTRAINT appointments_queue_status_check
    CHECK (queue_status IN ('waiting', 'in_consultation', 'done'));

-- past walk-ins are retired so the 064 guards cannot collide on them
UPDATE appointments SET status = 'completed' WHERE is_walk_in AND status IN ('confirmed','pending_payment','pending_review');

DROP INDEX IF EXISTS uq_appointment_active_slot;
CREATE UNIQUE INDEX uq_appointment_active_slot ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
    WHERE status IN ('confirmed','pending_payment','pending_review') AND booking_type = 'consultation' AND doctor_id IS NOT NULL;

DROP INDEX IF EXISTS uq_appointment_active_slot_unassigned;
CREATE UNIQUE INDEX uq_appointment_active_slot_unassigned ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed','pending_payment','pending_review') AND booking_type = 'consultation' AND doctor_id IS NULL;

DROP INDEX IF EXISTS idx_appointments_opd_queue;

DROP TABLE IF EXISTS opd_receipts, opd_cashier_shifts, opd_invoice_items, opd_invoices,
                     opd_prescription_items, opd_prescriptions, opd_encounters CASCADE;

DROP FUNCTION IF EXISTS opd_purge_clinic(uuid), opd_close_shift(uuid,uuid,uuid,integer,text),
    opd_sign_prescription(uuid,uuid,uuid,uuid,jsonb,jsonb,jsonb,jsonb), opd_sign_encounter(uuid,uuid,uuid,uuid,jsonb),
    opd_guard_shift(), opd_guard_append_only(), opd_receipt_after_insert(), opd_receipt_before_insert(),
    opd_guard_invoice(), opd_recalc_invoice_subtotal(), opd_guard_invoice_items(), opd_guard_rx_items(),
    opd_guard_signed_record(), opd_purging(uuid), opd_touch_updated_at(), opd_assign_mrn(uuid,uuid,uuid),
    opd_format_number(text,integer,integer,integer), opd_next_counter(uuid,text);

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_family_member_fk,
                         DROP CONSTRAINT IF EXISTS appointments_opd_meta_check,
                         DROP COLUMN IF EXISTS queue_timeline, DROP COLUMN IF EXISTS checked_in_at,
                         DROP COLUMN IF EXISTS family_member_id, DROP COLUMN IF EXISTS visit_type,
                         DROP COLUMN IF EXISTS booking_channel, DROP COLUMN IF EXISTS is_walk_in;

ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_opd_demographics_check,
    DROP COLUMN IF EXISTS mrn, DROP COLUMN IF EXISTS date_of_birth, DROP COLUMN IF EXISTS age_years,
    DROP COLUMN IF EXISTS age_recorded_on, DROP COLUMN IF EXISTS gender, DROP COLUMN IF EXISTS allergies,
    DROP COLUMN IF EXISTS allergies_status;

ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_opd_demographics_check,
    DROP COLUMN IF EXISTS mrn, DROP COLUMN IF EXISTS date_of_birth, DROP COLUMN IF EXISTS age_years,
    DROP COLUMN IF EXISTS age_recorded_on, DROP COLUMN IF EXISTS gender, DROP COLUMN IF EXISTS address_line,
    DROP COLUMN IF EXISTS city, DROP COLUMN IF EXISTS pincode, DROP COLUMN IF EXISTS emergency_contact_name,
    DROP COLUMN IF EXISTS emergency_contact_phone, DROP COLUMN IF EXISTS emergency_contact_relation,
    DROP COLUMN IF EXISTS allergies, DROP COLUMN IF EXISTS allergies_status;

ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_fk,
    DROP CONSTRAINT IF EXISTS clinic_admins_doctor_needs_clinic, DROP COLUMN IF EXISTS doctor_id;

ALTER TABLE doctors DROP CONSTRAINT IF EXISTS doctors_registration_check,
    DROP COLUMN IF EXISTS registration_number, DROP COLUMN IF EXISTS registration_council;

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_state_check, DROP CONSTRAINT IF EXISTS clinics_opd_json_check,
    DROP COLUMN IF EXISTS opd_display_token_hash, DROP COLUMN IF EXISTS opd_counters,
    DROP COLUMN IF EXISTS opd_settings, DROP COLUMN IF EXISTS opd_state;

DROP INDEX IF EXISTS uq_patients_clinic_id_id, uq_family_members_clinic_id_id, uq_appointments_clinic_id_id,
                     uq_doctors_clinic_id_id, uq_branches_clinic_id_id;

-- then (manual, only after the code revert is live):
-- DELETE FROM schema_migrations WHERE name = '103_opd_os_core.sql';
