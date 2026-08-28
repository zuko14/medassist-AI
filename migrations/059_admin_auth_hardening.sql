-- ============================================================
-- Migration 059: Admin authentication hardening
--
-- KA-15: Add UNIQUE(clinic_id, username) to prevent ambiguous
-- login resolution when the same username exists across clinics.
-- ============================================================

-- Deduplicate any existing violations before adding constraint
-- (keep the most recently created row per clinic+username pair)
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY clinic_id, username
               ORDER BY created_at DESC
           ) AS rn
    FROM clinic_admins
    WHERE clinic_id IS NOT NULL
)
DELETE FROM clinic_admins
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- Add the unique constraint
ALTER TABLE clinic_admins
    ADD CONSTRAINT uq_admin_clinic_username UNIQUE (clinic_id, username);

INSERT INTO schema_migrations (name) VALUES ('059_admin_auth_hardening.sql') ON CONFLICT (name) DO NOTHING;

-- Verify
SELECT 'migration_059_complete' AS status,
       (SELECT COUNT(*) FROM clinic_admins) AS admin_count;
