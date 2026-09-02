-- ============================================================
-- Migration 069: Cleanup Visakha Multi Speciality Clinic Test Patients
-- 
-- Scope: clinic_id = '9d9e9f12-c775-49c0-a326-98a59cdcc2e4' (Visakha Multispeciality Clinics)
-- Purpose: Remove test/phantom patient entries created during pre-launch webhook testing.
-- Zero-tolerance safety: Only removes records that have 0 appointments, 0 lab reports, and 0 prescriptions.
-- ============================================================

-- 1. Remove test conversations for Visakha clinic
DELETE FROM conversations
WHERE clinic_id = '9d9e9f12-c775-49c0-a326-98a59cdcc2e4';

-- 2. Remove unengaged test patients for Visakha clinic
DELETE FROM patients
WHERE clinic_id = '9d9e9f12-c775-49c0-a326-98a59cdcc2e4'
  AND (visit_count IS NULL OR visit_count = 0)
  AND id NOT IN (SELECT DISTINCT patient_id FROM appointments WHERE patient_id IS NOT NULL)
  AND id NOT IN (SELECT DISTINCT matched_patient_id FROM lab_reports WHERE matched_patient_id IS NOT NULL);
