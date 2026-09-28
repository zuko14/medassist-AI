-- ============================================================================
-- Migration 095: Corporate employee-health insights (diagnostic centres)
-- ============================================================================
-- A diagnostic centre runs a health package for a company's employees,
-- uploads each employee's report PDF, and the company logs in to see
-- AGGREGATE insights only (normal/abnormal by test, sex and age group).
--
-- Data minimisation is the design: the PDF, the employee's name and the file
-- name are NEVER stored. A report row holds only what the charts need —
-- sex, age, collection date, the lab bill id (so the same report cannot be
-- counted twice) and the parsed test values with their normal/low/high status.
--
-- Off by default. The platform owner switches it on per clinic
-- (clinics.features.corporate_health = true), diagstream/diagbooking only —
-- see app/services/tenant.py corporate_health_enabled().
--
-- Additive: two new tables and one nullable column on clinic_admins that no
-- existing query selects. RLS: same pattern as migrations 088/089/094.
-- ============================================================================

CREATE TABLE IF NOT EXISTS corporate_clients (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Target for the composite FK below: a report can only ever point at a
    -- company of its OWN clinic, enforced by the database, not just the app.
    UNIQUE (id, clinic_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_corporate_clients_name
    ON corporate_clients (clinic_id, lower(btrim(name)));

CREATE TABLE IF NOT EXISTS corporate_health_reports (
    id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID        NOT NULL,
    corporate_client_id  UUID        NOT NULL,
    sex                  TEXT        NOT NULL CHECK (sex IN ('M', 'F')),
    age_years            SMALLINT    NOT NULL CHECK (age_years BETWEEN 0 AND 120),
    bill_id              TEXT        CHECK (bill_id IS NULL OR char_length(bill_id) BETWEEN 1 AND 40),
    collected_on         DATE,
    results              JSONB       NOT NULL CHECK (jsonb_typeof(results) = 'object'),
    file_sha256          TEXT        NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    parser_version       TEXT        NOT NULL,
    uploaded_by          TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (corporate_client_id, clinic_id)
        REFERENCES corporate_clients (id, clinic_id) ON DELETE CASCADE
);

-- The duplicate guards. The app checks first for a friendly message; these
-- indexes are what actually hold under two concurrent uploads.
CREATE UNIQUE INDEX IF NOT EXISTS uq_corporate_reports_bill
    ON corporate_health_reports (corporate_client_id, bill_id) WHERE bill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_corporate_reports_file
    ON corporate_health_reports (corporate_client_id, file_sha256);
CREATE INDEX IF NOT EXISTS idx_corporate_reports_company
    ON corporate_health_reports (clinic_id, corporate_client_id, created_at DESC);

-- A company-viewer login (staff_role CORPORATE_VIEWER) is bound to ONE
-- company. SET NULL on company delete: the login then sees nothing (the app
-- fails closed on a viewer with no company) instead of being silently deleted.
ALTER TABLE clinic_admins
    ADD COLUMN IF NOT EXISTS corporate_client_id UUID
        REFERENCES corporate_clients(id) ON DELETE SET NULL;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['corporate_clients', 'corporate_health_reports'] LOOP
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
    END LOOP;
END $$;
