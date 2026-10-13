-- Rollback Migration 107. Drops the per-clinic unique index only.
-- The old cross-tenant UNIQUE(doctor_name, leave_date) is NOT restored: it is the
-- defect 107 fixes (D-20), and re-adding it fails once two clinics have leave on
-- the same date for doctors with the same name. Rows merged by 107 stay merged.
DROP INDEX IF EXISTS uq_doctor_leaves_clinic_doctor_date;
