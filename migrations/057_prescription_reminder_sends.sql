-- ============================================================
-- Migration 057: Prescription reminder deduplication table
--
-- KA-09: Prevents duplicate prescription reminders when multiple
-- worker processes run send_due_reminders concurrently.
-- ============================================================

CREATE TABLE IF NOT EXISTS prescription_reminder_sends (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prescription_id UUID NOT NULL REFERENCES prescriptions(id) ON DELETE CASCADE,
    reminder_time TEXT NOT NULL,
    sent_date DATE NOT NULL,
    clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),

    -- Only one send per prescription per reminder time per day
    UNIQUE(prescription_id, reminder_time, sent_date)
);

CREATE INDEX IF NOT EXISTS idx_rx_sends_lookup
    ON prescription_reminder_sends(prescription_id, sent_date);

-- Auto-cleanup: remove records older than 7 days to prevent unbounded growth
-- (optional — can be run by a scheduled job instead)

-- ── RLS: prescription_reminder_sends ──
ALTER TABLE prescription_reminder_sends ENABLE ROW LEVEL SECURITY;
ALTER TABLE prescription_reminder_sends FORCE ROW LEVEL SECURITY;

-- service_role bypass (used by the backend)
DROP POLICY IF EXISTS "service_role_all_prescription_reminder_sends" ON prescription_reminder_sends;
CREATE POLICY "service_role_all_prescription_reminder_sends"
    ON prescription_reminder_sends FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- tenant isolation for non-service roles
DROP POLICY IF EXISTS "tenant_isolation_prescription_reminder_sends" ON prescription_reminder_sends;
CREATE POLICY "tenant_isolation_prescription_reminder_sends"
    ON prescription_reminder_sends
    FOR ALL TO kriya_app, authenticated, anon
    USING (
        clinic_id IS NOT NULL
        AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
    )
    WITH CHECK (
        clinic_id IS NOT NULL
        AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
    );

-- NOTE: schema_migrations is written by scripts/migrate.py:124 with the
-- file's SHA256. checksum is NOT NULL (scripts/migrate.py:60), so a
-- self-INSERT here omits it and aborts the migration on any fresh
-- database. Migrations must not record themselves.

SELECT 'migration_057_complete' AS status;
