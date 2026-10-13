-- ============================================================================
-- Migration 107: one leave row per doctor per date, PER CLINIC
-- ============================================================================
-- doctor_leaves was created (migration 001) with UNIQUE(doctor_name, leave_date)
-- before the table had a clinic_id (migration 003). That constraint spans every
-- tenant: if two clinics each have a doctor with exactly the same name, the second
-- clinic cannot record leave for that date (create_leave now answers 409 instead
-- of a 500; Kriya Ketu bug register D-20).
--
-- This replaces it with the tenant-correct rule, UNIQUE(clinic_id, doctor_name,
-- leave_date), which is also the database-level guard behind create_leave's own
-- duplicate check (D-11, owner decision DP-13).
--
-- Data: if any database copy ever lost the old constraint and collected several
-- rows for one clinic/doctor/date, they are merged first: a day with a full-day row
-- or both halves becomes one full-day row, otherwise the earliest row is kept.
-- Exactly what slot logic already does with such rows (database.leave_blocked_sessions).
-- Idempotent. Rollback: migrations/rollback/107_down.sql.
-- ============================================================================


-- 1. Merge same-clinic duplicates (normally none).
WITH grouped AS (
    SELECT clinic_id, doctor_name, leave_date,
           bool_or(leave_type = 'full')
             OR (bool_or(leave_type = 'half_morning') AND bool_or(leave_type = 'half_evening')) AS whole_day
    FROM doctor_leaves
    GROUP BY clinic_id, doctor_name, leave_date
    HAVING count(*) > 1
)
UPDATE doctor_leaves d
SET leave_type = 'full'
FROM grouped g
WHERE g.whole_day
  AND d.clinic_id IS NOT DISTINCT FROM g.clinic_id
  AND d.doctor_name = g.doctor_name
  AND d.leave_date = g.leave_date;

WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY clinic_id, doctor_name, leave_date
               ORDER BY (leave_type = 'full') DESC, created_at, id
           ) AS rn
    FROM doctor_leaves
)
DELETE FROM doctor_leaves d
USING ranked r
WHERE d.id = r.id AND r.rn > 1;

-- 2. Drop the cross-tenant constraint (default name from migration 001).
ALTER TABLE doctor_leaves DROP CONSTRAINT IF EXISTS doctor_leaves_doctor_name_leave_date_key;

-- 3. The tenant-correct rule.
CREATE UNIQUE INDEX IF NOT EXISTS uq_doctor_leaves_clinic_doctor_date
    ON doctor_leaves (clinic_id, doctor_name, leave_date);

