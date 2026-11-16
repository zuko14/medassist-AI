-- ============================================================================
-- Migration 134: Kriya Pharmacy OS foundation (RX-M1)
-- clinics.rx_state / rx_settings, rx_stores (central store, retail counter,
-- ward sub-stores, OT / emergency trays), the purge helpers and the settings
-- CAS write.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §1, ADR-RX-03 / ADR-RX-09.
-- Additive + backward compatible: pre-134 code runs unchanged (new columns
-- have defaults; no existing constraint is narrowed; no trigger on any
-- existing table). Nothing here is used until the platform owner enables
-- pharmacy_enabled for one clinic.
--
-- Dependencies: clinics, and branches through 103's uq_branches_clinic_id_id (the
-- composite branch FK). So rolling back 103 needs 134 unwound first (newest-first
-- unwind chain, tests/test_migration_rx_m1.py::unwind_134). rx_stores.ward_id gets
-- its FK to ipd_wards only in 139 (RX-M6), keeping this migration off IPD tables.
-- ============================================================================

-- ── 1. clinics: pharmacy module state + settings ───────────────────────────
-- OFF / ACTIVE / WIND_DOWN. WIND_DOWN = owner disabled the module with work in
-- flight: nothing new starts; returns, exports and audits continue (ADR-RX-09).
ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS rx_state    TEXT  NOT NULL DEFAULT 'OFF',
    ADD COLUMN IF NOT EXISTS rx_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_rx_state_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_rx_state_check
    CHECK (rx_state IN ('OFF', 'ACTIVE', 'WIND_DOWN') AND jsonb_typeof(rx_settings) = 'object');

-- ── 2. Helpers ─────────────────────────────────────────────────────────────
-- Transaction-local purge flag: RX guards let rows go only while it names
-- their clinic (ipd_purging / dx_purging pattern).
CREATE OR REPLACE FUNCTION rx_purging(p_clinic_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT COALESCE(current_setting('kriya.rx_purge_clinic', true), '') = p_clinic_id::TEXT
$$;

CREATE OR REPLACE FUNCTION rx_raise(p_code TEXT) RETURNS VOID
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN RAISE EXCEPTION '%', p_code USING ERRCODE = 'P0001'; END $$;

-- ── 3. Stores ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_stores (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id        UUID NULL,                    -- NULL = clinic-wide store
    parent_store_id  UUID NULL,                    -- the store this one is replenished from
    code             TEXT NOT NULL CHECK (code ~ '^[A-Z0-9_-]{2,20}$'),
    name             TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 100),
    store_type       TEXT NOT NULL CHECK (store_type IN ('central', 'retail_counter', 'ward_substore', 'ot_tray',
                                                         'emergency_tray')),
    ward_id          UUID NULL,                    -- ward_substore only; FK to ipd_wards added in 139
    can_sell         BOOLEAN NOT NULL DEFAULT false,
    can_receive_grn  BOOLEAN NOT NULL DEFAULT false,
    drug_licence_no  TEXT NULL CHECK (drug_licence_no IS NULL OR char_length(drug_licence_no) <= 60),
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_stores_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_stores_code_key UNIQUE (clinic_id, code),
    CONSTRAINT rx_stores_ward_shape CHECK ((store_type = 'ward_substore') = (ward_id IS NOT NULL)),
    CONSTRAINT rx_stores_sell_shape CHECK (NOT can_sell OR store_type = 'retail_counter'),
    CONSTRAINT rx_stores_no_self_parent CHECK (parent_store_id IS DISTINCT FROM id),
    -- composite FKs: a store can only sit on a branch / under a store of its own clinic
    CONSTRAINT rx_stores_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT rx_stores_parent_fk FOREIGN KEY (clinic_id, parent_store_id) REFERENCES rx_stores (clinic_id, id)
);
-- one active central store per branch (and one clinic-wide one)
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_stores_one_central
    ON rx_stores (clinic_id, COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE store_type = 'central' AND is_active;
CREATE INDEX IF NOT EXISTS idx_rx_stores_branch ON rx_stores (clinic_id, branch_id);

-- Identity never changes (documents will carry the code); stores are never
-- deleted except by the owner purge — deactivate instead.
CREATE OR REPLACE FUNCTION rx_guard_store() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_store_delete_refused');
    END IF;
    IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id THEN PERFORM rx_raise('rx_store_field_locked:clinic_id'); END IF;
    IF NEW.code IS DISTINCT FROM OLD.code THEN PERFORM rx_raise('rx_store_field_locked:code'); END IF;
    IF NEW.store_type IS DISTINCT FROM OLD.store_type THEN PERFORM rx_raise('rx_store_field_locked:store_type'); END IF;
    IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN PERFORM rx_raise('rx_store_field_locked:created_at'); END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_stores_guard ON rx_stores;
CREATE TRIGGER trg_rx_stores_guard BEFORE UPDATE OR DELETE ON rx_stores
    FOR EACH ROW EXECUTE FUNCTION rx_guard_store();

-- ── 4. Settings CAS ────────────────────────────────────────────────────────
-- Compare-and-swap on rx_settings.version: two admins saving at once can never
-- silently overwrite each other. p_settings is the full, already-validated
-- object (app/services/rx.py); version is bumped here.
CREATE OR REPLACE FUNCTION rx_update_settings(p_clinic_id UUID, p_settings JSONB, p_expected_version INTEGER)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v JSONB;
BEGIN
    IF jsonb_typeof(p_settings) IS DISTINCT FROM 'object' THEN
        PERFORM rx_raise('rx_settings_invalid');
    END IF;
    UPDATE clinics
       SET rx_settings = p_settings || jsonb_build_object('version', p_expected_version + 1)
     WHERE id = p_clinic_id
       AND COALESCE((rx_settings ->> 'version')::INTEGER, 0) = p_expected_version
    RETURNING rx_settings INTO v;
    IF v IS NULL THEN
        IF EXISTS (SELECT 1 FROM clinics WHERE id = p_clinic_id) THEN
            PERFORM rx_raise('rx_settings_conflict');
        END IF;
        PERFORM rx_raise('rx_clinic_not_found');
    END IF;
    RETURN v;
END $$;

-- ── 5. Owner purge (v1) ────────────────────────────────────────────────────
-- Owner-approved clinic deletion only. Every later RX migration redefines it
-- (CREATE OR REPLACE) to delete its own tables first. Runs before
-- ipd_purge_clinic / opd_purge_clinic (later RX rows reference OPD/IPD rows).
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    -- one statement: the self-referencing parent FK is checked at its end
    DELETE FROM rx_stores WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('stores', n);
    UPDATE clinics SET rx_state = 'OFF', rx_settings = '{}'::jsonb WHERE id = p_clinic_id;
    PERFORM set_config('kriya.rx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 6. RLS parity (049/103/111/125 pattern) + API-role lockout ─────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_stores']
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
    FOREACH f IN ARRAY ARRAY['rx_purging(uuid)', 'rx_raise(text)', 'rx_purge_clinic(uuid)',
                             'rx_update_settings(uuid,jsonb,integer)']
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

-- ── 7. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'clinics'
          AND column_name IN ('rx_state', 'rx_settings')) <> 2 THEN
        RAISE EXCEPTION '134 verify: clinics.rx_state / rx_settings missing';
    END IF;
    IF to_regclass('public.rx_stores') IS NULL THEN
        RAISE EXCEPTION '134 verify: rx_stores missing';
    END IF;
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'public.rx_stores'::regclass) THEN
        RAISE EXCEPTION '134 verify: rx_stores RLS not enabled + forced';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid = 'public.rx_stores'::regclass AND conname IN (
          'rx_stores_clinic_id_id_key', 'rx_stores_code_key', 'rx_stores_ward_shape', 'rx_stores_sell_shape',
          'rx_stores_no_self_parent', 'rx_stores_branch_fk', 'rx_stores_parent_fk')) <> 7 THEN
        RAISE EXCEPTION '134 verify: rx_stores constraints missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_rx_stores_guard') THEN
        RAISE EXCEPTION '134 verify: trg_rx_stores_guard missing';
    END IF;
    IF to_regprocedure('rx_purging(uuid)') IS NULL OR to_regprocedure('rx_purge_clinic(uuid)') IS NULL
       OR to_regprocedure('rx_update_settings(uuid,jsonb,integer)') IS NULL OR to_regprocedure('rx_raise(text)') IS NULL THEN
        RAISE EXCEPTION '134 verify: rx functions missing';
    END IF;
END $$;
