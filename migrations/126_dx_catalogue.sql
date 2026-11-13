-- ============================================================================
-- Migration 126: Kriya Diagnostic OS catalogue (DX-M2)
-- Departments, containers (2-digit barcode codes), signatories; additive DX
-- columns on lab_tests / lab_test_parameters; panels and packages through
-- dx_test_components; approval with a content digest that any clinical edit
-- clears in the same transaction.
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §3, REPORT-TEMPLATES.md §1–§3.
-- Additive + backward compatible: every new lab_tests / lab_test_parameters
-- column is NULL or defaulted, so the lab-test admin, WhatsApp booking and the
-- LIS (118) behave exactly as before (low/high_exclusive default false).
-- ============================================================================

-- ── 1. Departments ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_departments (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code           TEXT NOT NULL CHECK (code ~ '^[A-Z]{2,8}$'),
    name           TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    report_heading TEXT NOT NULL CHECK (char_length(btrim(report_heading)) BETWEEN 2 AND 120),
    sort_order     SMALLINT NOT NULL DEFAULT 100 CHECK (sort_order BETWEEN 0 AND 999),
    is_active      BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_departments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_departments_clinic_code_key  UNIQUE (clinic_id, code)
);

-- ── 2. Containers (code = the 2 barcode digits: YY + cc + SSSSSSSS) ─────────
CREATE TABLE IF NOT EXISTS dx_containers (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code                 TEXT NOT NULL CHECK (code ~ '^[0-9]{2}$'),
    name                 TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    colour_hex           TEXT NOT NULL CHECK (colour_hex ~ '^#[0-9A-Fa-f]{6}$'),
    sample_type          TEXT NOT NULL CHECK (sample_type IN ('serum', 'whole_blood', 'plasma', 'urine', 'stool',
                                                               'swab', 'semen', 'csf', 'fluid', 'other')),
    default_volume_ml    NUMERIC(5,1) NULL CHECK (default_volume_ml IS NULL OR default_volume_ml > 0),
    stability_room_hours SMALLINT NULL CHECK (stability_room_hours IS NULL OR stability_room_hours > 0),
    stability_cold_hours SMALLINT NULL CHECK (stability_cold_hours IS NULL OR stability_cold_hours > 0),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_containers_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_containers_clinic_code_key  UNIQUE (clinic_id, code)
);

-- ── 3. Signatories (who may approve which department; how the signature prints)
CREATE TABLE IF NOT EXISTS dx_signatories (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    doctor_id            UUID NOT NULL,
    department_id        UUID NULL,          -- NULL = every department
    designation          TEXT NOT NULL CHECK (char_length(btrim(designation)) BETWEEN 2 AND 120),
    qualification_text   TEXT NOT NULL CHECK (char_length(btrim(qualification_text)) BETWEEN 2 AND 200),
    signature_asset_path TEXT NULL CHECK (signature_asset_path IS NULL OR char_length(signature_asset_path) <= 300),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_signatories_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_signatories_doctor_dept_key  UNIQUE (clinic_id, doctor_id, department_id),
    CONSTRAINT dx_signatories_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT dx_signatories_department_fk FOREIGN KEY (clinic_id, department_id)
        REFERENCES dx_departments (clinic_id, id)
);
-- UNIQUE treats NULLs as distinct: one "all departments" row per doctor needs its own index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_signatories_doctor_all
    ON dx_signatories (clinic_id, doctor_id) WHERE department_id IS NULL;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_departments', 'dx_containers', 'dx_signatories']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_updated_at', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()',
                       'trg_' || t || '_updated_at', t);
    END LOOP;
END $$;

-- ── 4. lab_tests: additive DX columns (WhatsApp booking ignores them) ───────
ALTER TABLE lab_tests
    ADD COLUMN IF NOT EXISTS test_code          TEXT NULL,
    ADD COLUMN IF NOT EXISTS kind               TEXT NOT NULL DEFAULT 'test',
    ADD COLUMN IF NOT EXISTS department_id      UUID NULL,
    ADD COLUMN IF NOT EXISTS container_id       UUID NULL,
    ADD COLUMN IF NOT EXISTS sample_volume_ml   NUMERIC(5,1) NULL,
    ADD COLUMN IF NOT EXISTS method             TEXT NULL,
    ADD COLUMN IF NOT EXISTS tat_minutes        INTEGER NULL,
    ADD COLUMN IF NOT EXISTS stat_tat_minutes   INTEGER NULL,
    ADD COLUMN IF NOT EXISTS print_title        TEXT NULL,
    ADD COLUMN IF NOT EXISTS interpretation     TEXT NULL,
    ADD COLUMN IF NOT EXISTS report_notes       TEXT NULL,
    ADD COLUMN IF NOT EXISTS gender_restriction TEXT NOT NULL DEFAULT 'all',
    ADD COLUMN IF NOT EXISTS loinc_code         TEXT NULL,
    ADD COLUMN IF NOT EXISTS print_order        SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS dx_result_mode     TEXT NOT NULL DEFAULT 'parameters',
    ADD COLUMN IF NOT EXISTS approved_at        TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS approved_by        UUID NULL,          -- the approving doctor (doctors.id)
    ADD COLUMN IF NOT EXISTS approval_digest    TEXT NULL;
ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_dx_check;
ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_dx_check CHECK (
    (test_code IS NULL OR test_code ~ '^[A-Z0-9._-]{1,20}$')
    AND kind IN ('test', 'panel', 'package')
    AND (sample_volume_ml IS NULL OR sample_volume_ml > 0)
    AND (method IS NULL OR char_length(method) <= 120)
    AND (tat_minutes IS NULL OR tat_minutes BETWEEN 5 AND 43200)
    AND (stat_tat_minutes IS NULL OR stat_tat_minutes BETWEEN 5 AND 43200)
    AND (print_title IS NULL OR char_length(print_title) <= 200)
    AND (interpretation IS NULL OR char_length(interpretation) <= 6000)
    AND (report_notes IS NULL OR char_length(report_notes) <= 2000)
    AND gender_restriction IN ('all', 'male', 'female')
    AND (loinc_code IS NULL OR loinc_code ~ '^[0-9]{1,7}-[0-9]$')
    AND dx_result_mode IN ('parameters', 'document', 'none')
    AND ((approved_at IS NULL) = (approval_digest IS NULL)));
-- a department or container in use cannot be deleted (deactivate it); the lab-test delete itself is unaffected
ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_department_fk;
ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_department_fk FOREIGN KEY (clinic_id, department_id)
    REFERENCES dx_departments (clinic_id, id) ON DELETE RESTRICT;
ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_container_fk;
ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_container_fk FOREIGN KEY (clinic_id, container_id)
    REFERENCES dx_containers (clinic_id, id) ON DELETE RESTRICT;
-- one code per clinic + branch (branch copies of a test, mig 076, may reuse it)
CREATE UNIQUE INDEX IF NOT EXISTS uq_lab_tests_test_code ON lab_tests (
    clinic_id, COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid), upper(test_code))
    WHERE test_code IS NOT NULL;

-- ── 5. lab_test_parameters: additive DX columns (bands stay one row each) ───
ALTER TABLE lab_test_parameters
    ADD COLUMN IF NOT EXISTS parameter_code      TEXT NULL,
    ADD COLUMN IF NOT EXISTS method              TEXT NULL,
    ADD COLUMN IF NOT EXISTS decimal_places      SMALLINT NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS rounding_mode       TEXT NOT NULL DEFAULT 'half_up',
    ADD COLUMN IF NOT EXISTS formula             TEXT NULL,
    ADD COLUMN IF NOT EXISTS reference_text      TEXT NULL,
    ADD COLUMN IF NOT EXISTS machine_code        TEXT NULL,
    ADD COLUMN IF NOT EXISTS delta_direction     TEXT NULL,
    ADD COLUMN IF NOT EXISTS delta_limit_percent NUMERIC(5,2) NULL,
    ADD COLUMN IF NOT EXISTS delta_limit_abs     NUMERIC(10,3) NULL,
    ADD COLUMN IF NOT EXISTS delta_lookback_days SMALLINT NOT NULL DEFAULT 30,
    ADD COLUMN IF NOT EXISTS low_exclusive       BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS high_exclusive      BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE lab_test_parameters DROP CONSTRAINT IF EXISTS lab_test_parameters_dx_check;
ALTER TABLE lab_test_parameters ADD CONSTRAINT lab_test_parameters_dx_check CHECK (
    (parameter_code IS NULL OR parameter_code ~ '^[A-Z0-9._-]{1,30}$')
    AND (method IS NULL OR char_length(method) <= 120)
    AND decimal_places BETWEEN 0 AND 4
    AND rounding_mode IN ('half_up', 'half_even', 'truncate')
    AND (formula IS NULL OR char_length(formula) <= 500)
    AND (reference_text IS NULL OR char_length(reference_text) <= 2000)
    AND (machine_code IS NULL OR char_length(machine_code) <= 40)
    AND (delta_direction IS NULL OR delta_direction IN ('increase', 'decrease', 'both'))
    AND (delta_limit_percent IS NULL OR delta_limit_percent > 0)
    AND (delta_limit_abs IS NULL OR delta_limit_abs > 0)
    AND delta_lookback_days BETWEEN 1 AND 3650);

-- ── 6. Panels and packages ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_test_components (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    parent_test_id UUID NOT NULL,
    child_test_id  UUID NOT NULL,
    sort_order     SMALLINT NOT NULL DEFAULT 10 CHECK (sort_order BETWEEN 0 AND 999),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_test_components_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_test_components_pair_key UNIQUE (clinic_id, parent_test_id, child_test_id),
    CONSTRAINT dx_test_components_not_self CHECK (parent_test_id <> child_test_id),
    -- deleting a lab test (admin delete / bulk delete) removes its component links (AUDIT A22)
    CONSTRAINT dx_test_components_parent_fk FOREIGN KEY (clinic_id, parent_test_id)
        REFERENCES lab_tests (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_test_components_child_fk FOREIGN KEY (clinic_id, child_test_id)
        REFERENCES lab_tests (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dx_test_components_child ON dx_test_components (clinic_id, child_test_id);

-- ── 7. Guards ──────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_raise(p_code TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', p_code USING ERRCODE = 'P0001'; END $$;

-- Kinds form a strict hierarchy (package > panel > test), so depth <= 2 and no
-- cycle can exist: a package holds panels or tests, a panel holds tests only.
CREATE OR REPLACE FUNCTION dx_guard_components() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE pk TEXT; ck TEXT;
BEGIN
    IF NEW.parent_test_id = NEW.child_test_id THEN PERFORM dx_raise('dx_component_self'); END IF;
    SELECT kind INTO pk FROM lab_tests WHERE clinic_id = NEW.clinic_id AND id = NEW.parent_test_id;
    SELECT kind INTO ck FROM lab_tests WHERE clinic_id = NEW.clinic_id AND id = NEW.child_test_id;
    IF pk IS NULL OR ck IS NULL THEN RETURN NEW; END IF;          -- the composite FK reports it
    IF pk = 'test' THEN PERFORM dx_raise('dx_component_parent_kind'); END IF;
    IF ck = 'package' OR (ck = 'panel' AND pk <> 'package') THEN
        PERFORM dx_raise('dx_component_depth');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_test_components_guard ON dx_test_components;
CREATE TRIGGER trg_dx_test_components_guard BEFORE INSERT OR UPDATE ON dx_test_components
    FOR EACH ROW EXECUTE FUNCTION dx_guard_components();

-- A test owns parameters; a panel or package owns none (only checked for DX kinds,
-- so every existing lab test, all kind = 'test', is unaffected).
CREATE OR REPLACE FUNCTION dx_guard_parameter_kind() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM lab_tests WHERE clinic_id = NEW.clinic_id AND id = NEW.lab_test_id AND kind <> 'test') THEN
        PERFORM dx_raise('dx_parameters_on_group');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_parameters_kind ON lab_test_parameters;
CREATE TRIGGER trg_dx_parameters_kind BEFORE INSERT OR UPDATE OF lab_test_id ON lab_test_parameters
    FOR EACH ROW EXECUTE FUNCTION dx_guard_parameter_kind();

-- On lab_tests: changing kind is refused once the row is part of a group or holds
-- parameters that would no longer fit; editing a clinical field of an approved
-- test clears its approval in the same statement.
CREATE OR REPLACE FUNCTION dx_lab_tests_guard() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        IF EXISTS (SELECT 1 FROM dx_test_components
                    WHERE clinic_id = NEW.clinic_id AND (parent_test_id = NEW.id OR child_test_id = NEW.id)) THEN
            PERFORM dx_raise('dx_kind_locked');
        END IF;
        IF NEW.kind <> 'test' AND EXISTS (SELECT 1 FROM lab_test_parameters
                                           WHERE clinic_id = NEW.clinic_id AND lab_test_id = NEW.id) THEN
            PERFORM dx_raise('dx_parameters_on_group');
        END IF;
    END IF;
    IF OLD.approved_at IS NOT NULL AND NEW.approved_at IS NOT DISTINCT FROM OLD.approved_at
       AND (NEW.name, NEW.kind, NEW.department_id, NEW.container_id, NEW.sample_volume_ml, NEW.method,
            NEW.print_title, NEW.interpretation, NEW.report_notes, NEW.gender_restriction, NEW.dx_result_mode)
           IS DISTINCT FROM
           (OLD.name, OLD.kind, OLD.department_id, OLD.container_id, OLD.sample_volume_ml, OLD.method,
            OLD.print_title, OLD.interpretation, OLD.report_notes, OLD.gender_restriction, OLD.dx_result_mode) THEN
        NEW.approved_at := NULL; NEW.approved_by := NULL; NEW.approval_digest := NULL;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_lab_tests_clear_approval ON lab_tests;
CREATE TRIGGER trg_dx_lab_tests_clear_approval BEFORE UPDATE ON lab_tests
    FOR EACH ROW EXECUTE FUNCTION dx_lab_tests_guard();

-- A band, unit, formula or component change clears the owning test's approval.
-- AFTER triggers: when the test itself is being deleted, the cascade finds no row
-- to update. Analyser machine_code edits alone do not need re-approval.
CREATE OR REPLACE FUNCTION dx_clear_test_approval() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_clinic UUID; v_test UUID;
BEGIN
    IF TG_TABLE_NAME = 'lab_test_parameters' THEN
        IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - 'machine_code' - 'updated_at')
                              = (to_jsonb(OLD) - 'machine_code' - 'updated_at') THEN
            RETURN NULL;
        END IF;
        v_clinic := COALESCE(NEW.clinic_id, OLD.clinic_id);
        v_test := CASE WHEN TG_OP = 'DELETE' THEN OLD.lab_test_id ELSE NEW.lab_test_id END;
        IF TG_OP = 'UPDATE' AND NEW.lab_test_id <> OLD.lab_test_id THEN
            UPDATE lab_tests SET approved_at = NULL, approved_by = NULL, approval_digest = NULL
             WHERE clinic_id = v_clinic AND id = OLD.lab_test_id AND approved_at IS NOT NULL;
        END IF;
    ELSE
        v_clinic := COALESCE(NEW.clinic_id, OLD.clinic_id);
        v_test := CASE WHEN TG_OP = 'DELETE' THEN OLD.parent_test_id ELSE NEW.parent_test_id END;
    END IF;
    -- Lock the test even when it is not approved yet: otherwise an approval running
    -- concurrently could read the pre-edit digest and approve stale content. NO KEY
    -- UPDATE does not conflict with the KEY SHARE locks FK checks take (no deadlock).
    PERFORM 1 FROM lab_tests WHERE clinic_id = v_clinic AND id = v_test FOR NO KEY UPDATE;
    UPDATE lab_tests SET approved_at = NULL, approved_by = NULL, approval_digest = NULL
     WHERE clinic_id = v_clinic AND id = v_test AND approved_at IS NOT NULL;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_parameters_clear_approval ON lab_test_parameters;
CREATE TRIGGER trg_dx_parameters_clear_approval AFTER INSERT OR UPDATE OR DELETE ON lab_test_parameters
    FOR EACH ROW EXECUTE FUNCTION dx_clear_test_approval();
DROP TRIGGER IF EXISTS trg_dx_components_clear_approval ON dx_test_components;
CREATE TRIGGER trg_dx_components_clear_approval AFTER INSERT OR UPDATE OR DELETE ON dx_test_components
    FOR EACH ROW EXECUTE FUNCTION dx_clear_test_approval();

-- ── 8. Approval digest + approval (CAS) ────────────────────────────────────
-- Everything a lab director approves: the test's clinical fields, its active
-- bands and its components, in a fixed order. jsonb text output is canonical.
CREATE OR REPLACE FUNCTION dx_test_digest(p_clinic_id UUID, p_test_id UUID) RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT encode(sha256(convert_to(jsonb_build_object(
        'test', (SELECT jsonb_build_object('name', t.name, 'kind', t.kind, 'department_id', t.department_id,
                    'container_id', t.container_id, 'sample_volume_ml', t.sample_volume_ml, 'method', t.method,
                    'print_title', t.print_title, 'interpretation', t.interpretation, 'report_notes', t.report_notes,
                    'gender_restriction', t.gender_restriction, 'dx_result_mode', t.dx_result_mode)
                   FROM lab_tests t WHERE t.clinic_id = p_clinic_id AND t.id = p_test_id),
        'parameters', COALESCE((SELECT jsonb_agg(to_jsonb(p) - 'machine_code' - 'created_at' - 'updated_at'
                                                  - 'clinic_id' - 'lab_test_id'
                                        ORDER BY p.sort_order, p.name, p.gender, p.min_age_days, p.id)
                                  FROM lab_test_parameters p
                                 WHERE p.clinic_id = p_clinic_id AND p.lab_test_id = p_test_id AND p.is_active),
                               '[]'::jsonb),
        'components', COALESCE((SELECT jsonb_agg(jsonb_build_object('child', c.child_test_id, 'sort', c.sort_order)
                                        ORDER BY c.sort_order, c.child_test_id)
                                  FROM dx_test_components c
                                 WHERE c.clinic_id = p_clinic_id AND c.parent_test_id = p_test_id), '[]'::jsonb)
    )::text, 'UTF8')), 'hex')
$$;

-- The row lock makes approval and edits serialise: an edit committed first changes
-- the digest (409); an edit arriving later waits, then clears the approval.
CREATE OR REPLACE FUNCTION dx_approve_test(p_clinic_id UUID, p_test_id UUID, p_digest TEXT,
                                           p_doctor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v_digest TEXT; v_at TIMESTAMPTZ;
BEGIN
    PERFORM 1 FROM lab_tests WHERE clinic_id = p_clinic_id AND id = p_test_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_test_not_found'); END IF;
    v_digest := dx_test_digest(p_clinic_id, p_test_id);
    IF p_digest IS DISTINCT FROM v_digest THEN PERFORM dx_raise('dx_test_changed'); END IF;
    UPDATE lab_tests SET approved_at = now(), approved_by = p_doctor_id, approval_digest = v_digest
     WHERE clinic_id = p_clinic_id AND id = p_test_id RETURNING approved_at INTO v_at;
    RETURN jsonb_build_object('approved_at', v_at, 'approval_digest', v_digest, 'approved_by', p_doctor_id,
                              'approved_by_name', left(COALESCE(p_actor_name, ''), 120));
END $$;

-- ── 9. Purge v2 (children first; lab_tests stay — they predate DX) ─────────
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM dx_test_components WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('components', n);
    DELETE FROM dx_signatories WHERE clinic_id = p_clinic_id;
    UPDATE lab_tests SET department_id = NULL, container_id = NULL
     WHERE clinic_id = p_clinic_id AND (department_id IS NOT NULL OR container_id IS NOT NULL);
    DELETE FROM dx_containers  WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_departments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 10. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_departments', 'dx_containers', 'dx_signatories', 'dx_test_components']
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
    FOREACH f IN ARRAY ARRAY['dx_purge_clinic(uuid)', 'dx_test_digest(uuid,uuid)',
                             'dx_approve_test(uuid,uuid,text,uuid,text)', 'dx_raise(text)']
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

-- Pin search_path on every dx_* function (migration 105 convention, Supabase lint 0011).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'dx\_%'
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 11. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN (
          'dx_departments', 'dx_containers', 'dx_signatories', 'dx_test_components')) <> 4 THEN
        RAISE EXCEPTION '126 verify: DX catalogue tables missing';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'lab_tests'
          AND column_name IN ('test_code', 'kind', 'department_id', 'container_id', 'approved_at',
                              'approval_digest', 'dx_result_mode')) <> 7 THEN
        RAISE EXCEPTION '126 verify: lab_tests DX columns missing';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public'
          AND table_name = 'lab_test_parameters'
          AND column_name IN ('parameter_code', 'formula', 'low_exclusive', 'high_exclusive', 'decimal_places',
                              'rounding_mode')) <> 6 THEN
        RAISE EXCEPTION '126 verify: lab_test_parameters DX columns missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_test_components_guard',
          'trg_dx_lab_tests_clear_approval', 'trg_dx_parameters_clear_approval',
          'trg_dx_components_clear_approval', 'trg_dx_parameters_kind')) <> 5 THEN
        RAISE EXCEPTION '126 verify: DX catalogue triggers missing';
    END IF;
    IF to_regprocedure('dx_approve_test(uuid,uuid,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_test_digest(uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION '126 verify: approval functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_departments', 'dx_containers', 'dx_signatories',
                 'dx_test_components') AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '126 verify: RLS not forced on DX catalogue tables';
    END IF;
END $$;
