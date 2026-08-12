-- ============================================================
-- Migration 029: Doctor → Branch Auto-Assignment (Backward Compat)
--
-- Problem: Doctors created via the admin panel have no record in
-- the doctor_branches junction table, making them invisible to
-- the WhatsApp chatbot when a patient selects a specific branch.
--
-- Fix:
--   1. For clinics that have branches but doctors aren't linked,
--      auto-assign every orphaned doctor to the oldest branch
--      belonging to their clinic.
--   2. For clinics that have NO branches at all, create a default
--      "Main Branch" and link all their doctors to it.
--
-- Safety: Fully idempotent (ON CONFLICT DO NOTHING), non-blocking,
-- zero-downtime. Re-running this migration is harmless.
-- ============================================================

-- ── Step 1: Create a default "Main Branch" for clinics that have ──
-- ── doctors but zero branches.                                    ──

INSERT INTO branches (clinic_id, name, short_name, address, is_active, display_order)
SELECT DISTINCT
    d.clinic_id,
    COALESCE(c.name, 'Main') || ' - Main Branch',
    'MAIN',
    COALESCE(
        c.config->>'hospital_address',
        ''
    ),
    true,
    0
FROM doctors d
JOIN clinics c ON c.id = d.clinic_id
WHERE d.clinic_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM branches b WHERE b.clinic_id = d.clinic_id
  )
ON CONFLICT DO NOTHING;


-- ── Step 2: Link orphaned doctors to their clinic's oldest branch ──
-- ── (first by display_order, then created_at).                     ──

INSERT INTO doctor_branches (doctor_id, branch_id, session)
SELECT
    d.id AS doctor_id,
    first_branch.id AS branch_id,
    'both' AS session
FROM doctors d
JOIN LATERAL (
    SELECT b.id
    FROM branches b
    WHERE b.clinic_id = d.clinic_id
      AND b.is_active = true
    ORDER BY b.display_order ASC, b.created_at ASC
    LIMIT 1
) first_branch ON true
WHERE d.clinic_id IS NOT NULL
  AND d.is_active = true
  AND NOT EXISTS (
      SELECT 1 FROM doctor_branches db WHERE db.doctor_id = d.id
  )
ON CONFLICT (doctor_id, branch_id) DO NOTHING;
