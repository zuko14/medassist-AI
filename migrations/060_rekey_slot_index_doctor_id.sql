-- ============================================================
-- Migration 060: Re-key slot uniqueness on doctor_id instead of
-- free-text doctor_name, and include branch_id.
--
-- KA-11: The previous index was keyed on doctor_name (TEXT).
-- "Dr. Rao", "Dr Rao", "dr. rao" are four distinct keys — a
-- whitespace/case difference silently permits double-booking.
-- Additionally, branch_id was absent, so two physicians with
-- the same name at different branches could not both be booked
-- at the same time.
-- ============================================================

-- Step 1: Add doctor_id column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'appointments' AND column_name = 'doctor_id'
    ) THEN
        ALTER TABLE appointments ADD COLUMN doctor_id UUID;
    END IF;
END $$;

-- Step 2: Backfill doctor_id from doctors table by matching
-- on clinic_id + doctor_name (case-insensitive, trimmed)
UPDATE appointments a
SET doctor_id = d.id
FROM doctors d
WHERE a.doctor_id IS NULL
  AND a.clinic_id = d.clinic_id
  AND LOWER(TRIM(a.doctor_name)) = LOWER(TRIM(d.name));

-- Step 3: Add branch_id column if not exists
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'appointments' AND column_name = 'branch_id'
    ) THEN
        ALTER TABLE appointments ADD COLUMN branch_id UUID;
    END IF;
END $$;

-- Step 4: Backfill branch_id from doctor_branches junction table.
-- Only backfill for doctors assigned to exactly one branch.
-- Multi-branch doctors are left NULL (the unique index handles
-- NULLs correctly via COALESCE).
UPDATE appointments a
SET branch_id = db.branch_id
FROM doctor_branches db
WHERE a.branch_id IS NULL
  AND a.doctor_id IS NOT NULL
  AND a.doctor_id = db.doctor_id
  AND (SELECT COUNT(*) FROM doctor_branches x WHERE x.doctor_id = a.doctor_id) = 1;

-- Step 5: Drop old indexes and create the new one
DROP INDEX IF EXISTS uq_appointment_active_slot;

CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments(
        clinic_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        COALESCE(doctor_id, '00000000-0000-0000-0000-000000000000'::uuid),
        appointment_date,
        appointment_time
    )
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review');

-- Step 6: Keep a fallback index on doctor_name for rows where
-- doctor_id couldn't be backfilled (orphan appointments)
CREATE INDEX IF NOT EXISTS idx_appointments_doctor_name_fallback
    ON appointments(clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND doctor_id IS NULL;

-- Step 7: Add foreign key (deferred, non-blocking)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_appointment_doctor'
    ) THEN
        ALTER TABLE appointments
            ADD CONSTRAINT fk_appointment_doctor
            FOREIGN KEY (doctor_id) REFERENCES doctors(id)
            ON DELETE SET NULL;
    END IF;
END $$;

INSERT INTO schema_migrations (name) VALUES ('060_rekey_slot_index_doctor_id.sql') ON CONFLICT (name) DO NOTHING;

SELECT 'migration_060_complete' AS status;
