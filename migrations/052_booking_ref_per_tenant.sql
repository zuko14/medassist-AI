-- Migration 052: booking_ref becomes per-tenant unique and wider (KRIYA-001).
-- Migration 001 created it as globally UNIQUE VARCHAR(20). Global uniqueness
-- means one busy tenant exhausts the namespace for every other tenant.
--
-- EXPAND ONLY. The original global UNIQUE constraint is deliberately NOT dropped
-- here — it is dropped in migration 054 (Phase 7), one full release later, so a
-- rollback to the previous application version cannot orphan data.

-- 1. Widen first. The maximum of the new format is exactly 20 characters; do not
--    sit on the boundary.
ALTER TABLE appointments ALTER COLUMN booking_ref TYPE VARCHAR(32);

-- 2. Per-tenant uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_booking_ref
    ON appointments (clinic_id, booking_ref)
    WHERE booking_ref IS NOT NULL;
