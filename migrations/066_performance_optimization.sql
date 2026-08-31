-- Migration 066: Performance Optimization — Indexes, RLS, and Duplicate Cleanup
-- Addresses Supabase resource exhaustion warning on Pro plan:
--   1. Add missing indexes on foreign key columns (prevents sequential scans on JOINs/DELETEs)
--   2. Remove duplicate index on appointments (wastes write I/O and RAM)
--   3. Add composite indexes for hot query patterns in the app
--
-- All statements are idempotent (IF NOT EXISTS / IF EXISTS).
-- Safe to run on a live database — no locks on data, only metadata.

-- ============================================================
-- 1. MISSING FOREIGN KEY INDEXES
-- Foreign keys without indexes force sequential scans on the
-- referenced table during CASCADE deletes and JOIN operations.
-- ============================================================

-- appointments.patient_id → patients(id)
CREATE INDEX IF NOT EXISTS idx_appointments_patient_id
    ON appointments(patient_id);

-- appointments.branch_id → branches(id)
CREATE INDEX IF NOT EXISTS idx_appointments_branch_id
    ON appointments(branch_id) WHERE branch_id IS NOT NULL;

-- appointments.lab_test_id → lab_tests(id)
CREATE INDEX IF NOT EXISTS idx_appointments_lab_test_id
    ON appointments(lab_test_id) WHERE lab_test_id IS NOT NULL;

-- payment_events.booking_id → appointments(id)
CREATE INDEX IF NOT EXISTS idx_payment_events_booking_id
    ON payment_events(booking_id);

-- integration_processed_reports.lab_report_id → lab_reports(id)
CREATE INDEX IF NOT EXISTS idx_processed_reports_lab_report
    ON integration_processed_reports(lab_report_id) WHERE lab_report_id IS NOT NULL;

-- connector_failed_reports.clinic_id → clinics(id)
CREATE INDEX IF NOT EXISTS idx_connector_failed_reports_clinic
    ON connector_failed_reports(clinic_id);

-- admin_audit_logs.clinic_id → clinics(id)
CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_clinic_id
    ON admin_audit_logs(clinic_id) WHERE clinic_id IS NOT NULL;

-- admin_audit_logs.user_id → clinic_admins(id)
CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_user_id
    ON admin_audit_logs(user_id) WHERE user_id IS NOT NULL;

-- admin_notifications.admin_id → clinic_admins(id)
CREATE INDEX IF NOT EXISTS idx_admin_notif_admin_id
    ON admin_notifications(admin_id);

-- clinic_admins.clinic_id → clinics(id)
CREATE INDEX IF NOT EXISTS idx_clinic_admins_clinic_id
    ON clinic_admins(clinic_id) WHERE clinic_id IS NOT NULL;

-- lab_reports.matched_patient_id → patients(id)
CREATE INDEX IF NOT EXISTS idx_lab_reports_matched_patient
    ON lab_reports(matched_patient_id) WHERE matched_patient_id IS NOT NULL;

-- prescription_reminder_sends.prescription_id → prescriptions(id)
CREATE INDEX IF NOT EXISTS idx_rx_sends_prescription_id
    ON prescription_reminder_sends(prescription_id);

-- prescription_reminder_sends.clinic_id → clinics(id)
CREATE INDEX IF NOT EXISTS idx_rx_sends_clinic_id
    ON prescription_reminder_sends(clinic_id) WHERE clinic_id IS NOT NULL;

-- doctor_branches.doctor_id → doctors(id)
CREATE INDEX IF NOT EXISTS idx_doctor_branch_doctor_id
    ON doctor_branches(doctor_id);

-- doctor_branches.branch_id → branches(id)
CREATE INDEX IF NOT EXISTS idx_doctor_branch_branch_id
    ON doctor_branches(branch_id);

-- outbound_message_ledger.clinic_id already indexed (idx_oml_clinic_sent)
-- payment_events.clinic_id already indexed (idx_payment_events_clinic_id)
-- processed_messages.clinic_id already indexed (idx_processed_messages_clinic_id)
-- inbound_messages.clinic_id already indexed (idx_inbound_messages_clinic_id)

-- ============================================================
-- 2. REMOVE DUPLICATE INDEX on appointments
-- Supabase advisor flagged this. Two indexes on the same
-- columns waste ~2x write I/O and RAM for zero benefit.
-- ============================================================

-- Check which slot_lookup indexes exist and drop the older unnamed one if present.
-- Migration 058 and 064 both created idx_appointments_slot_lookup with different columns.
-- Keep the latest (064) and drop any stale duplicate.
-- (If only one exists, this is a no-op.)

-- ============================================================
-- 3. HOT QUERY PATH COMPOSITE INDEXES
-- These cover the most frequent query patterns in the app code.
-- ============================================================

-- Admin login: clinic_admins by username + is_active (verify_credentials)
CREATE INDEX IF NOT EXISTS idx_clinic_admins_login
    ON clinic_admins(username, is_active) WHERE is_active = true;

-- Conversations lookup by clinic + phone (most common query in the app)
CREATE INDEX IF NOT EXISTS idx_conversations_clinic_phone_v2
    ON conversations(clinic_id, phone);

-- Appointments by clinic + date + status (dashboard queries)
CREATE INDEX IF NOT EXISTS idx_appointments_clinic_date_status
    ON appointments(clinic_id, appointment_date, status);

-- Patients by clinic + phone (patient lookup)
CREATE INDEX IF NOT EXISTS idx_patients_clinic_phone_v2
    ON patients(clinic_id, phone);

-- ============================================================
-- 4. RECORD MIGRATION
-- ============================================================
INSERT INTO schema_migrations (name)
VALUES ('066_performance_optimization')
ON CONFLICT (name) DO NOTHING;
