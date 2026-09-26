-- ============================================================================
-- Migration 088: Client data portability + clinic -> owner support inbox
-- ============================================================================
-- 1. patient_records  — a clinic's EXISTING patient list, imported from its old
--    software (CSV). Deliberately a separate table from `patients`:
--      * a `patients` row changes live WhatsApp behaviour — consent
--        (accepts_engagement reads opted_in), the STOP/START keywords, and
--        lab-report patient_match auto-delivery all key on it;
--      * an old-software export carries no WhatsApp opt-in, so inserting it
--        into `patients` would either fabricate consent (opted_in=true) or
--        silently mute reminders for those numbers (opted_in=false).
--    Nothing in the bot, scheduler or report pipeline reads this table.
--    The per-clinic row quota lives in clinics.config.patient_records_limit.
--
-- 2. support_messages — clinic admin -> platform owner messages (concerns,
--    feature requests, extra-storage requests), answered from /platform.
--
-- 3. platform_invoices.storage_addon_paise — owner-set monthly charge for
--    extra patient-record storage, snapshotted per invoice. DEFAULT 0, so
--    every existing invoice and every clinic without an add-on is unchanged.
--
-- PURELY ADDITIVE. Re-runnable. Safe with the previous build still running.
-- ============================================================================

SET LOCAL lock_timeout = '5s';

-- ── 1. patient_import_batches + patient_records ─────────────────────────────
-- One row per uploaded file, so an admin can see what was imported and undo a
-- wrong file. Deleting the batch row removes its records (FK cascade) in one
-- statement — no half-undone imports.
CREATE TABLE IF NOT EXISTS patient_import_batches (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    file_name           TEXT        CHECK (file_name IS NULL OR length(file_name) <= 200),
    rows_in_file        INTEGER     NOT NULL DEFAULT 0 CHECK (rows_in_file >= 0),
    inserted_count      INTEGER     NOT NULL DEFAULT 0 CHECK (inserted_count >= 0),
    skipped_existing    INTEGER     NOT NULL DEFAULT 0 CHECK (skipped_existing >= 0),
    duplicates_in_file  INTEGER     NOT NULL DEFAULT 0 CHECK (duplicates_in_file >= 0),
    warning_count       INTEGER     NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    status              TEXT        NOT NULL DEFAULT 'importing'
                                    CHECK (status IN ('importing', 'completed')),
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_patient_import_batches_clinic_created
    ON patient_import_batches (clinic_id, created_at DESC);

ALTER TABLE patient_import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_import_batches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_patient_import_batches" ON patient_import_batches;
CREATE POLICY "service_role_all_patient_import_batches" ON patient_import_batches
    FOR ALL TO service_role USING (true) WITH CHECK (true);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        DROP POLICY IF EXISTS "tenant_isolation_patient_import_batches" ON patient_import_batches;
        CREATE POLICY "tenant_isolation_patient_import_batches" ON patient_import_batches
            FOR ALL TO kriya_app, authenticated, anon
            USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS patient_records (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    full_name        TEXT        NOT NULL CHECK (length(full_name) BETWEEN 1 AND 150),
    phone            TEXT        CHECK (phone IS NULL OR phone ~ '^\+?[0-9]{10,15}$'),
    external_id      TEXT        CHECK (external_id IS NULL OR length(external_id) <= 64),
    gender           TEXT        CHECK (gender IS NULL OR length(gender) <= 20),
    date_of_birth    DATE,
    age_years        SMALLINT    CHECK (age_years IS NULL OR age_years BETWEEN 0 AND 130),
    email            TEXT        CHECK (email IS NULL OR length(email) <= 254),
    address          TEXT        CHECK (address IS NULL OR length(address) <= 500),
    last_visit_date  DATE,
    notes            TEXT        CHECK (notes IS NULL OR length(notes) <= 4000),
    extra            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Identity used to make re-importing the same file a no-op. Computed by
    -- the app: old-software patient id if present, else phone+name, else
    -- name+dob. See app/services/client_data.py:dedupe_key.
    dedupe_key       TEXT        NOT NULL CHECK (length(dedupe_key) <= 300),
    import_batch_id  UUID        NOT NULL REFERENCES patient_import_batches(id) ON DELETE CASCADE,
    source           TEXT        NOT NULL DEFAULT 'csv_import',
    created_by       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patient_records_clinic_dedupe_key UNIQUE (clinic_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_patient_records_clinic_created
    ON patient_records (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_patient_records_clinic_batch
    ON patient_records (clinic_id, import_batch_id);
CREATE INDEX IF NOT EXISTS idx_patient_records_clinic_phone
    ON patient_records (clinic_id, phone) WHERE phone IS NOT NULL;

ALTER TABLE patient_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_records FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_patient_records" ON patient_records;
CREATE POLICY "service_role_all_patient_records" ON patient_records
    FOR ALL TO service_role USING (true) WITH CHECK (true);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        DROP POLICY IF EXISTS "tenant_isolation_patient_records" ON patient_records;
        CREATE POLICY "tenant_isolation_patient_records" ON patient_records
            FOR ALL TO kriya_app, authenticated, anon
            USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);
    END IF;
END $$;

COMMENT ON TABLE patient_records IS
    'A clinic''s legacy patient list imported from its previous software. Never read by the WhatsApp bot, scheduler or report routing.';

-- ── 2. support_messages ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS support_messages (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    category             TEXT        NOT NULL
                                     CHECK (category IN ('concern', 'feature_request', 'storage_request', 'billing', 'other')),
    subject              TEXT        NOT NULL CHECK (length(subject) BETWEEN 1 AND 150),
    message              TEXT        NOT NULL CHECK (length(message) BETWEEN 1 AND 4000),
    status               TEXT        NOT NULL DEFAULT 'open'
                                     CHECK (status IN ('open', 'in_progress', 'resolved')),
    created_by_username  TEXT        NOT NULL,
    created_by_role      TEXT        NOT NULL,
    owner_reply          TEXT        CHECK (owner_reply IS NULL OR length(owner_reply) <= 4000),
    replied_at           TIMESTAMPTZ,
    owner_seen_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_support_messages_clinic_created
    ON support_messages (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_messages_status_created
    ON support_messages (status, created_at DESC);

ALTER TABLE support_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_support_messages" ON support_messages;
CREATE POLICY "service_role_all_support_messages" ON support_messages
    FOR ALL TO service_role USING (true) WITH CHECK (true);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kriya_app') THEN
        DROP POLICY IF EXISTS "tenant_isolation_support_messages" ON support_messages;
        CREATE POLICY "tenant_isolation_support_messages" ON support_messages
            FOR ALL TO kriya_app, authenticated, anon
            USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);
    END IF;
END $$;

-- ── 3. storage add-on on invoices ───────────────────────────────────────────
ALTER TABLE platform_invoices
    ADD COLUMN IF NOT EXISTS storage_addon_paise INTEGER NOT NULL DEFAULT 0;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'platform_invoices_storage_addon_nonneg'
    ) THEN
        ALTER TABLE platform_invoices
            ADD CONSTRAINT platform_invoices_storage_addon_nonneg CHECK (storage_addon_paise >= 0);
    END IF;
END $$;
