-- ============================================================================
-- Migration 094: Photos of handwritten sitting notes (dental treatment plans)
-- ============================================================================
-- Front desks photograph the dentist's handwritten sitting notes instead of
-- typing them. The image goes to the existing private `lab-reports` storage
-- bucket under {clinic_id}/dental-sittings/{appointment_id}/{random}.{ext}
-- (never a user-supplied name) and is only ever served through short-lived
-- signed URLs. This table is the index of those files.
--
-- Additive: a new table only. Nothing existing reads it. RLS: same pattern as
-- migrations 088/089 (service_role for the backend; tenant policy for the
-- app role where it exists).
-- ============================================================================

CREATE TABLE IF NOT EXISTS dental_sitting_photos (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    appointment_id  UUID        NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    storage_path    TEXT        NOT NULL UNIQUE,
    content_type    TEXT        NOT NULL
                                CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    size_bytes      INTEGER     NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 3145728),
    uploaded_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dental_sitting_photos_appt
    ON dental_sitting_photos (clinic_id, appointment_id, created_at);

DO $$
DECLARE t TEXT := 'dental_sitting_photos';
BEGIN
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                       'service_role_all_' || t, t);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format(
            'CREATE POLICY %I ON %I FOR ALL TO kriya_app, authenticated, anon '
            'USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid) '
            'WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting(''app.clinic_id'', true), '''')::uuid)',
            'tenant_isolation_' || t, t);
    END IF;
END $$;
