-- ============================================================================
-- Migration 073: Unique queue tokens for lab-test check-ins
-- ============================================================================
-- idx_unique_queue_token (migration 021) keys on
--   (clinic_id, doctor_name, appointment_date, token_number)
-- A lab-test booking carries doctor_name = NULL by design (migration 039), and
-- Postgres treats every NULL in a unique index as distinct from every other
-- NULL — so that index silently does not constrain lab rows at all. Combined
-- with `.eq("doctor_name", None)` never matching a NULL row in PostgREST, the
-- max-token lookup always returned nothing and every sample-collection walk-in
-- was handed token #1.
--
-- Lab rows get their own queue key: the collection centre (branch) for the
-- day, which is the queue the patient actually stands in. branch_id is
-- nullable for single-centre clinics, so it is COALESCEd to a fixed sentinel
-- rather than left NULL — otherwise this index would have the same hole.
--
-- ADDITIVE for consultations: the partial predicate covers only rows the
-- existing index already ignores (doctor_name IS NULL), so no consultation row
-- is touched and no existing check-in behaviour changes. Re-runnable.
-- ============================================================================

-- ── Step 1: renumber any duplicate lab tokens already stored ────────────────
-- Without this the CREATE UNIQUE INDEX below would fail on a clinic that had
-- already checked lab patients in under the broken sequence. Only rows that
-- are ALREADY wrong are rewritten: the earliest booking in each queue keeps
-- its number and later ones are renumbered by created_at, which is the order
-- they arrived at the counter. Consultations are untouched (doctor_name
-- IS NOT NULL is excluded by the WHERE clause).
DO $$
DECLARE
    fixed INT;
BEGIN
    WITH ranked AS (
        SELECT
            id,
            ROW_NUMBER() OVER (
                PARTITION BY clinic_id,
                             COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
                             appointment_date
                ORDER BY created_at, id
            ) AS seq,
            token_number
        FROM appointments
        WHERE token_number IS NOT NULL AND doctor_name IS NULL
    )
    UPDATE appointments a
    SET token_number = r.seq
    FROM ranked r
    WHERE a.id = r.id AND a.token_number IS DISTINCT FROM r.seq;

    GET DIAGNOSTICS fixed = ROW_COUNT;
    IF fixed > 0 THEN
        RAISE NOTICE 'Renumbered % lab-test queue token(s) that collided under the old sequence', fixed;
    END IF;
END $$;

-- ── Step 2: make the collision impossible from here on ──────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_lab_queue_token
    ON appointments (
        clinic_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        appointment_date,
        token_number
    )
    WHERE token_number IS NOT NULL AND doctor_name IS NULL;

COMMENT ON INDEX idx_unique_lab_queue_token IS
    'Per-branch, per-day token uniqueness for lab-test check-ins. '
    'Consultations are covered by idx_unique_queue_token (migration 021).';

-- Verify
SELECT indexname FROM pg_indexes
WHERE tablename = 'appointments' AND indexname = 'idx_unique_lab_queue_token';
