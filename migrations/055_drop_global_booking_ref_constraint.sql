-- Migration 055: Drop legacy global booking_ref unique constraint (T7.3 / KRIYA-001).
--
-- Migration 001 added `booking_ref VARCHAR(20) UNIQUE` which created the global
-- constraint `appointments_booking_ref_key`.
-- Migration 052 established the per-tenant composite index `uq_appointment_booking_ref`
-- ON appointments (clinic_id, booking_ref) WHERE booking_ref IS NOT NULL.
-- This migration drops the old global constraint so distinct tenants can generate
-- independently collision-resistant references without cross-tenant collisions.

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_booking_ref_key;
DROP INDEX IF EXISTS idx_appointments_booking_ref;
