-- ============================================================
-- Migration 043: Tenant Routing Upgrade + Double-Booking Prevention
-- 
-- Fixes:
--   1. Adds phone_number_id column for dual-key tenant resolution
--   2. Adds is_sandbox flag for test/demo number routing
--   3. Deduplicates existing double-booked appointments
--   4. Adds partial UNIQUE constraint to prevent future double-bookings
--   5. Adds optimized index for slot availability queries
-- ============================================================

-- ── 1. Dual-key tenant lookup ────────────────────────────────────────────────

-- phone_number_id is the immutable ID Meta assigns to each WhatsApp number.
-- More reliable than display_phone_number which can change format.
ALTER TABLE clinics ADD COLUMN IF NOT EXISTS phone_number_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_clinics_phone_number_id
  ON clinics(phone_number_id)
  WHERE phone_number_id IS NOT NULL;

-- Backfill from existing config JSONB (meta_phone_number_id stored there)
UPDATE clinics
SET phone_number_id = config->>'meta_phone_number_id'
WHERE phone_number_id IS NULL
  AND config->>'meta_phone_number_id' IS NOT NULL
  AND config->>'meta_phone_number_id' != '';

-- ── 2. Sandbox / test clinic flag ────────────────────────────────────────────

ALTER TABLE clinics ADD COLUMN IF NOT EXISTS is_sandbox BOOLEAN NOT NULL DEFAULT false;

-- ── 3. Deduplicate existing double-booked appointments ───────────────────────
-- The book_appointment() SELECT-then-INSERT race (TOCTOU) has existed since
-- launch. We must clean duplicates before adding the UNIQUE constraint.

-- Mark duplicate confirmed rows (keep the earliest created_at per slot)
WITH ranked AS (
  SELECT id,
         ROW_NUMBER() OVER (
           PARTITION BY clinic_id, doctor_name, appointment_date, appointment_time
           ORDER BY created_at ASC
         ) AS rn
  FROM appointments
  WHERE status = 'confirmed'
)
UPDATE appointments
SET status = 'cancelled_dedup'
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- ── 4. Partial UNIQUE constraint on active appointment slots ─────────────────
-- Only enforced for confirmed/pending_payment — cancelled slots can be rebooked.
-- Uses a partial unique index (Postgres-specific) since standard UNIQUE doesn't
-- support WHERE clauses.

CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_active_slot
  ON appointments(clinic_id, doctor_name, appointment_date, appointment_time)
  WHERE status IN ('confirmed', 'pending_payment');

-- ── 5. Optimized composite index for slot availability queries ───────────────
-- get_available_slots() queries: clinic_id + doctor_name + date + status
CREATE INDEX IF NOT EXISTS idx_appointments_slot_lookup
  ON appointments(clinic_id, doctor_name, appointment_date, status)
  WHERE status IN ('confirmed', 'pending_payment');

-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT 'clinics.phone_number_id' AS check_item,
       COUNT(*) FILTER (WHERE phone_number_id IS NOT NULL) AS backfilled,
       COUNT(*) AS total
FROM clinics;

SELECT 'dedup_cancelled' AS check_item,
       COUNT(*) AS cancelled_duplicates
FROM appointments
WHERE status = 'cancelled_dedup';
