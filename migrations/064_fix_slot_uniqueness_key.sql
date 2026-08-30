-- ============================================================================
-- Migration 064: Re-key the active-slot uniqueness index on physician identity
-- ============================================================================
-- KA-P0-01 (launch blocker, confirmed by execution against real PostgreSQL).
--
-- Migration 060 keyed uq_appointment_active_slot on:
--     (clinic_id, COALESCE(branch_id, sentinel), COALESCE(doctor_id, sentinel),
--      appointment_date, appointment_time)
--
-- Folding a NULL branch_id to a sentinel does not merge the two rows — it
-- creates a SECOND distinct key. An appointment written without a branch and
-- one written with a branch therefore occupy different index keys even when
-- they name the same physician at the same minute, and BOTH INSERTs succeed.
--
-- Both writer paths set branch_id conditionally:
--   app/services/conversation.py:3096  (only when the context carries one)
--   app/services/payment.py:200        (only when the caller passes one)
-- The branch-first menu populates it; the department-first menu does not.
-- Two patients entering the same clinic through the two different menus
-- double-book one physician. This is deterministic, not a race.
--
-- FIX: branch_id does not belong in this key. A physician is one person and
-- cannot be at two branches in the same minute. Branch is an attribute of the
-- appointment, not part of the identity being made unique.
--
-- ── Behaviour preserved exactly (verified before writing this migration) ────
-- Lab-test bookings write appointment_time = NULL
-- (app/services/conversation.py:4044). PostgreSQL treats NULLs as distinct in
-- a unique index, so the migration-060 index has ALWAYS been a no-op for
-- lab_test rows — no two lab bookings have ever collided under it. Excluding
-- booking_type='lab_test' from the new index is therefore not a behaviour
-- change; it makes the existing behaviour explicit. Do NOT add a lab-test
-- uniqueness index here: that would be a new restriction, not a fix.
-- ============================================================================

-- ── Step 1: Pre-flight report ───────────────────────────────────────────────
-- Surface every live double-booking already in the data BEFORE changing
-- anything. CREATE UNIQUE INDEX fails outright if duplicates remain, which
-- would abort the deploy mid-flight, so these must be reported and resolved
-- in the same transaction.
DO $$
DECLARE
    v_row   RECORD;
    v_count INT := 0;
BEGIN
    FOR v_row IN
        SELECT a.clinic_id,
               a.doctor_id,
               a.doctor_name,
               a.appointment_date,
               a.appointment_time,
               COUNT(*)                          AS dupe_count,
               STRING_AGG(a.booking_ref, ', ')   AS booking_refs
        FROM appointments a
        WHERE a.status IN ('confirmed', 'pending_payment', 'pending_review')
          AND a.booking_type = 'consultation'
          AND a.doctor_id IS NOT NULL
        GROUP BY a.clinic_id, a.doctor_id, a.doctor_name,
                 a.appointment_date, a.appointment_time
        HAVING COUNT(*) > 1
    LOOP
        v_count := v_count + 1;
        RAISE NOTICE
            'KA-P0-01 DOUBLE-BOOKING clinic=% doctor=% (%) date=% time=% count=% refs=[%]',
            v_row.clinic_id, v_row.doctor_name, v_row.doctor_id,
            v_row.appointment_date, v_row.appointment_time,
            v_row.dupe_count, v_row.booking_refs;
    END LOOP;

    IF v_count = 0 THEN
        RAISE NOTICE 'Pre-flight: no existing double-bookings found (assigned-doctor consultations).';
    ELSE
        RAISE WARNING
            'Pre-flight: % slot(s) hold more than one active appointment. '
            'Step 2 quarantines the surplus rows to status=cancelled with '
            'refund_reason=duplicate_slot_quarantine. Staff MUST contact the '
            'affected patients using the booking_refs logged above.', v_count;
    END IF;
END $$;

-- ── Step 2: Quarantine surplus rows ─────────────────────────────────────────
-- The row kept per slot is the one with the strongest claim:
--   1. a booking that has actually been paid for (payment_id IS NOT NULL)
--   2. then the earliest created_at
--   3. then id, purely to make the choice deterministic
--
-- Surplus rows move to 'cancelled', NOT 'pending_review' — pending_review is
-- inside the index predicate, so quarantining into it would leave the
-- duplicate in place and the index creation in step 3 would still fail.
-- payment_id / amount_paise are deliberately left intact so any refund owed
-- remains traceable from the appointment row.
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY clinic_id, doctor_id, appointment_date, appointment_time
               ORDER BY (payment_id IS NULL), created_at, id
           ) AS rn
    FROM appointments
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NOT NULL
)
UPDATE appointments a
SET status        = 'cancelled',
    refund_reason = 'duplicate_slot_quarantine'
FROM ranked r
WHERE a.id = r.id
  AND r.rn > 1;

-- Same treatment for consultations whose doctor could not be resolved. Under
-- migration 060 these all collapsed onto the doctor sentinel, so they were
-- deduplicated against each other by clinic+branch+date+time. Step 3 replaces
-- that with a name-keyed index, which is strictly more accurate; this pass
-- clears any duplicate the old sentinel key allowed through.
WITH ranked_unassigned AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY clinic_id, doctor_name, appointment_date, appointment_time
               ORDER BY (payment_id IS NULL), created_at, id
           ) AS rn
    FROM appointments
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NULL
)
UPDATE appointments a
SET status        = 'cancelled',
    refund_reason = 'duplicate_slot_quarantine'
FROM ranked_unassigned r
WHERE a.id = r.id
  AND r.rn > 1;

-- ── Step 3: Replace the index ───────────────────────────────────────────────
DROP INDEX IF EXISTS uq_appointment_active_slot;

-- Primary guarantee: one physician, one clinic, one minute, at most one
-- active appointment. branch_id is gone from the key; doctor_id IS NOT NULL
-- in the predicate replaces the COALESCE sentinel, so unresolved-doctor rows
-- no longer collide with each other or with a real physician.
--
-- The name is kept as uq_appointment_active_slot because
-- app/utils/helpers.py:is_slot_conflict() matches on it to distinguish a
-- genuine "slot taken" from an unrelated integrity error.
CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NOT NULL;

-- Safety net for legacy consultation rows with no doctor_id. New bookings
-- cannot reach this state — app/database.py:book_appointment() and
-- app/services/payment.py:create_booking_with_payment() now refuse to insert
-- a consultation without a resolved doctor_id — but historical rows exist and
-- must not silently lose their guard.
--
-- The name intentionally shares the 'uq_appointment_active_slot' prefix so
-- is_slot_conflict()'s substring match covers this index too, with no change
-- required in helpers.py.
CREATE UNIQUE INDEX uq_appointment_active_slot_unassigned
    ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NULL;

-- Step 4: the migration-060 fallback lookup index is now redundant — the
-- unassigned unique index above covers exactly the same rows and columns.
DROP INDEX IF EXISTS idx_appointments_doctor_name_fallback;

-- Step 5: keep the slot-lookup index aligned with how book_appointment()
-- pre-checks (clinic + doctor_id + date). Migration 058 keyed it on
-- doctor_name, which no longer matches the query shape.
DROP INDEX IF EXISTS idx_appointments_slot_lookup;
CREATE INDEX idx_appointments_slot_lookup
    ON appointments (clinic_id, doctor_id, appointment_date)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

-- ── Record migration ──
-- NOTE: schema_migrations is written by scripts/migrate.py:124 with the
-- file's SHA256. checksum is NOT NULL (scripts/migrate.py:60), so a
-- self-INSERT here omits it and aborts the migration on any fresh
-- database. Migrations must not record themselves.

SELECT 'migration_064_complete' AS status;
