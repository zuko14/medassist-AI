-- ============================================================================
-- Migration 125: Kriya Diagnostic OS foundation (DX-M1)
-- clinics.dx_state / dx_settings, dx_sites (one per branch: collection and/or
-- processing site), the purge helpers, and the settings CAS write.
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §2, ADR-DX-02 / ADR-DX-14.
-- Additive + backward compatible: pre-125 code runs unchanged (new columns
-- have defaults; no existing constraint is narrowed). Nothing here is used
-- until the platform owner enables dx_enabled for one clinic.
-- ============================================================================

-- ── 1. clinics: DX module state + settings ─────────────────────────────────
-- OFF / ACTIVE / WIND_DOWN. WIND_DOWN = owner disabled DX with work in flight:
-- nothing new starts; existing visits run to release (ADR-DX-14).
ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS dx_state    TEXT  NOT NULL DEFAULT 'OFF',
    ADD COLUMN IF NOT EXISTS dx_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_dx_state_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_dx_state_check
    CHECK (dx_state IN ('OFF', 'ACTIVE', 'WIND_DOWN') AND jsonb_typeof(dx_settings) = 'object');

-- ── 2. Sites ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_sites (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'collection_and_processing'
                             CHECK (kind IN ('collection', 'processing', 'collection_and_processing')),
    code                TEXT NOT NULL CHECK (code ~ '^[A-Z0-9]{2,6}$'),
    display_name        TEXT NOT NULL CHECK (char_length(btrim(display_name)) BETWEEN 2 AND 120),
    legal_name          TEXT NULL CHECK (legal_name IS NULL OR char_length(legal_name) <= 200),
    registration_no     TEXT NULL CHECK (registration_no IS NULL OR char_length(registration_no) <= 80),
    nabl_certificate_no TEXT NULL CHECK (nabl_certificate_no IS NULL OR char_length(nabl_certificate_no) <= 80),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_sites_clinic_id_id_key     UNIQUE (clinic_id, id),
    CONSTRAINT dx_sites_clinic_branch_key    UNIQUE (clinic_id, branch_id),
    CONSTRAINT dx_sites_clinic_code_key      UNIQUE (clinic_id, code),
    -- composite FK: a site can only sit on a branch of its own clinic
    CONSTRAINT dx_sites_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);

DROP TRIGGER IF EXISTS trg_dx_sites_updated_at ON dx_sites;
CREATE TRIGGER trg_dx_sites_updated_at BEFORE UPDATE ON dx_sites
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ── 3. Helpers ─────────────────────────────────────────────────────────────
-- Transaction-local purge flag; later DX append-only guards let rows go only
-- while it names their clinic (same pattern as ipd_purging / opd purge).
CREATE OR REPLACE FUNCTION dx_purging(p_clinic_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT COALESCE(current_setting('kriya.dx_purge_clinic', true), '') = p_clinic_id::TEXT
$$;

-- Owner-approved clinic deletion only. v1 deletes sites; every later DX
-- migration redefines it (CREATE OR REPLACE) to delete its own tables first.
-- Runs BEFORE ipd_purge_clinic / opd_purge_clinic (DX rows will reference
-- OPD orders and invoices from DX-M3 on).
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- Settings write with compare-and-swap on dx_settings.version: two admins
-- saving at once can never silently overwrite each other. p_settings is the
-- full, already-validated object (app/services/dx.py); version is bumped here.
CREATE OR REPLACE FUNCTION dx_update_settings(p_clinic_id UUID, p_settings JSONB, p_expected_version INTEGER)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v JSONB;
BEGIN
    IF jsonb_typeof(p_settings) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'dx_settings_invalid' USING ERRCODE = 'P0001';
    END IF;
    UPDATE clinics
       SET dx_settings = p_settings || jsonb_build_object('version', p_expected_version + 1)
     WHERE id = p_clinic_id
       AND COALESCE((dx_settings ->> 'version')::INTEGER, 0) = p_expected_version
    RETURNING dx_settings INTO v;
    IF v IS NULL THEN
        IF EXISTS (SELECT 1 FROM clinics WHERE id = p_clinic_id) THEN
            RAISE EXCEPTION 'dx_settings_conflict' USING ERRCODE = 'P0001';
        END IF;
        RAISE EXCEPTION 'dx_clinic_not_found' USING ERRCODE = 'P0001';
    END IF;
    RETURN v;
END $$;

-- ── 4. RLS parity (049/103/111 pattern) + API-role lockout ─────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_sites']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_all_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_all_' || t, t);
        END IF;
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'tenant_isolation_' || t, t);
        EXECUTE format($p$CREATE POLICY %I ON %I FOR ALL
                         USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
                         WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)$p$,
                       'tenant_isolation_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON TABLE %I FROM authenticated', t);
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['dx_purging(uuid)', 'dx_purge_clinic(uuid)', 'dx_update_settings(uuid,jsonb,integer)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f);
        END IF;
    END LOOP;
END $$;

-- ── 5. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'clinics'
          AND column_name IN ('dx_state', 'dx_settings')) <> 2 THEN
        RAISE EXCEPTION '125 verify: clinics.dx_state / dx_settings missing';
    END IF;
    IF to_regclass('public.dx_sites') IS NULL THEN
        RAISE EXCEPTION '125 verify: dx_sites missing';
    END IF;
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'public.dx_sites'::regclass) THEN
        RAISE EXCEPTION '125 verify: dx_sites RLS not enabled + forced';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.dx_sites'::regclass AND conname IN (
          'dx_sites_clinic_id_id_key', 'dx_sites_clinic_branch_key', 'dx_sites_clinic_code_key',
          'dx_sites_branch_fk')) <> 4 THEN
        RAISE EXCEPTION '125 verify: dx_sites constraints missing';
    END IF;
    IF to_regprocedure('dx_purging(uuid)') IS NULL OR to_regprocedure('dx_purge_clinic(uuid)') IS NULL
       OR to_regprocedure('dx_update_settings(uuid,jsonb,integer)') IS NULL THEN
        RAISE EXCEPTION '125 verify: dx functions missing';
    END IF;
END $$;
