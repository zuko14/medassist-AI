-- ============================================================================
-- Migration 118: In-house Lab Information System (G2)
-- ============================================================================
-- A lab test (lab_tests) gets parameter definitions with reference ranges; a
-- G1 worklist order (opd_orders, catalog_type = 'lab_test') with its sample
-- collected gets a result: a header (lab_results) and one row per parameter
-- (lab_result_values).
--
--   technician  lis_record_results   order sample_collected -> in_progress,
--                                    result 'entered' (re-entry replaces values)
--   pathologist lis_validate_result  result 'validated' (signed), order
--                                    -> completed, both in one transaction
--   correction  lis_record_results on a validated result: a NEW row
--                                    (version + 1, supersedes_id, reason);
--                                    validating it marks the old one 'amended'
--
-- Invariants held here, not only in the API:
--   * a validated or amended result and its values never change again (the
--     only write left is recording where the delivered PDF was stored, once);
--   * a validated result names its signer and the signer's registration number;
--   * one version number per order, at most one unsigned version per order;
--   * results are append-only (only the owner's clinic purge deletes them).
--
-- Flags (low / high / critical) are computed by app/services/lis.py from the
-- parameter definitions, never sent by the browser; no LLM is involved.
--
-- lab_results.lab_test_id and lab_result_values.parameter_id are references
-- only (no FK), like opd_orders.catalog_reference_id: a signed report must
-- outlive a catalogue edit or delete, so it carries name, unit and range as
-- text. Additive and idempotent. Rollback: migrations/rollback/118_down.sql.
-- ============================================================================

-- ── 1. lab_tests: tenant composite key for the parameter FK ────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lab_tests_clinic_id_id_key') THEN
        ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_clinic_id_id_key UNIQUE (clinic_id, id);
    END IF;
END $$;

-- ── 2. Parameter definitions (one row per parameter per age / sex band) ────
CREATE TABLE IF NOT EXISTS lab_test_parameters (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    lab_test_id       UUID NOT NULL,
    name              TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    unit              TEXT NULL CHECK (char_length(unit) <= 30),
    data_type         TEXT NOT NULL DEFAULT 'numeric' CHECK (data_type IN ('numeric', 'text', 'options')),
    options           JSONB NULL,   -- ["Positive", "Negative"] when data_type = 'options'
    sort_order        SMALLINT NOT NULL DEFAULT 1 CHECK (sort_order BETWEEN 0 AND 999),
    min_age_days      INTEGER NOT NULL DEFAULT 0,
    max_age_days      INTEGER NOT NULL DEFAULT 43800,   -- 120 years
    gender            TEXT NOT NULL DEFAULT 'all' CHECK (gender IN ('all', 'male', 'female', 'other')),
    normal_low        NUMERIC(10,3) NULL,
    normal_high       NUMERIC(10,3) NULL,
    critical_low      NUMERIC(10,3) NULL,
    critical_high     NUMERIC(10,3) NULL,
    normal_text_value TEXT NULL CHECK (char_length(normal_text_value) <= 120),
    is_active         BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lab_test_parameters_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT lab_test_parameters_test_fk FOREIGN KEY (clinic_id, lab_test_id)
        REFERENCES lab_tests (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT lab_test_parameters_age_band CHECK (min_age_days >= 0 AND max_age_days >= min_age_days),
    CONSTRAINT lab_test_parameters_ranges CHECK (
        (normal_low IS NULL OR normal_high IS NULL OR normal_low <= normal_high)
        AND (critical_low IS NULL OR normal_low IS NULL OR critical_low <= normal_low)
        AND (critical_high IS NULL OR normal_high IS NULL OR critical_high >= normal_high)),
    CONSTRAINT lab_test_parameters_options CHECK (
        data_type <> 'options' OR (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) BETWEEN 1 AND 20))
);
CREATE INDEX IF NOT EXISTS idx_lab_parameters_test ON lab_test_parameters (clinic_id, lab_test_id, sort_order);

-- ── 3. Result header ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lab_results (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id            UUID NULL,
    order_id             UUID NOT NULL,
    patient_id           UUID NOT NULL,
    family_member_id     UUID NULL,
    lab_test_id          UUID NOT NULL,   -- reference only (see header)
    test_name            TEXT NOT NULL CHECK (char_length(btrim(test_name)) BETWEEN 1 AND 200),
    version              SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id        UUID NULL,
    amendment_reason     TEXT NULL CHECK (char_length(btrim(amendment_reason)) BETWEEN 5 AND 500),
    status               TEXT NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'entered', 'validated', 'amended')),
    has_critical_value   BOOLEAN NOT NULL DEFAULT false,
    technician_admin_id  UUID NULL,       -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    technician_name      TEXT NULL CHECK (char_length(technician_name) <= 100),
    entered_at           TIMESTAMPTZ NULL,
    pathologist_admin_id UUID NULL,
    pathologist_snapshot JSONB NULL,      -- {doctor_id, name, qualifications, registration_number, registration_council}
    letterhead_snapshot  JSONB NULL,      -- frozen at signing so a reprint is identical
    patient_snapshot     JSONB NULL,
    validated_at         TIMESTAMPTZ NULL,
    pdf_storage_path     TEXT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lab_results_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT lab_results_order_version_key UNIQUE (clinic_id, order_id, version),
    CONSTRAINT lab_results_order_fk FOREIGN KEY (clinic_id, order_id)
        REFERENCES opd_orders (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT lab_results_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT lab_results_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT lab_results_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT lab_results_supersedes_fk FOREIGN KEY (clinic_id, supersedes_id) REFERENCES lab_results (clinic_id, id),
    CONSTRAINT lab_results_amendment_shape CHECK (
        (version = 1 AND supersedes_id IS NULL AND amendment_reason IS NULL)
     OR (version > 1 AND supersedes_id IS NOT NULL AND amendment_reason IS NOT NULL)),
    CONSTRAINT lab_results_entered_complete CHECK (
        status = 'draft' OR (entered_at IS NOT NULL AND technician_name IS NOT NULL)),
    CONSTRAINT lab_results_validated_complete CHECK (
        status NOT IN ('validated', 'amended')
        OR (pathologist_admin_id IS NOT NULL AND validated_at IS NOT NULL
            AND pathologist_snapshot ? 'registration_number'
            AND btrim(COALESCE(pathologist_snapshot ->> 'registration_number', '')) <> ''))
);
-- one unsigned working copy per order
CREATE UNIQUE INDEX IF NOT EXISTS uq_lab_results_open
    ON lab_results (clinic_id, order_id) WHERE status IN ('draft', 'entered');
CREATE INDEX IF NOT EXISTS idx_lab_results_queue   ON lab_results (clinic_id, status, entered_at);
CREATE INDEX IF NOT EXISTS idx_lab_results_patient ON lab_results (clinic_id, patient_id, created_at DESC);

-- ── 4. Result values ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lab_result_values (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id               UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    result_id               UUID NOT NULL,
    parameter_id            UUID NOT NULL,   -- reference only (see header)
    parameter_name          TEXT NOT NULL CHECK (char_length(btrim(parameter_name)) BETWEEN 1 AND 120),
    unit                    TEXT NULL CHECK (char_length(unit) <= 30),
    sort_order              SMALLINT NOT NULL DEFAULT 1,
    numeric_value           NUMERIC(10,3) NULL,
    text_value              TEXT NULL CHECK (char_length(text_value) <= 500),
    -- not_evaluated: no reference range covers this patient's age / sex
    flag                    TEXT NOT NULL DEFAULT 'normal'
                            CHECK (flag IN ('normal', 'low', 'high', 'critical_low', 'critical_high', 'abnormal',
                                            'not_evaluated')),
    reference_range_display TEXT NULL CHECK (char_length(reference_range_display) <= 120),
    remarks                 TEXT NULL CHECK (char_length(remarks) <= 500),
    CONSTRAINT lab_result_values_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT lab_result_values_result_param_key UNIQUE (clinic_id, result_id, parameter_id),
    CONSTRAINT lab_result_values_result_fk FOREIGN KEY (clinic_id, result_id)
        REFERENCES lab_results (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT lab_result_values_has_value CHECK (numeric_value IS NOT NULL OR text_value IS NOT NULL)
);

-- ── 5. Guards: signed reports are immutable, results append-only ───────────
CREATE OR REPLACE FUNCTION lis_guard_result() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_open   TEXT[] := ARRAY['status', 'updated_at', 'has_critical_value', 'technician_admin_id', 'technician_name',
                             'entered_at', 'pathologist_admin_id', 'pathologist_snapshot', 'letterhead_snapshot',
                             'patient_snapshot', 'validated_at', 'pdf_storage_path'];
    v_signed TEXT[] := ARRAY['status', 'updated_at', 'pdf_storage_path'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF opd_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'lis_append_only:lab_results' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status IN ('validated', 'amended') THEN
        IF (to_jsonb(NEW) - v_signed) IS DISTINCT FROM (to_jsonb(OLD) - v_signed)
           OR (OLD.pdf_storage_path IS NOT NULL AND NEW.pdf_storage_path IS DISTINCT FROM OLD.pdf_storage_path)
           OR (NEW.status <> OLD.status AND NOT (OLD.status = 'validated' AND NEW.status = 'amended'
                                                AND COALESCE(current_setting('kriya.lis_amending', true), '')
                                                    = OLD.id::TEXT)) THEN
            RAISE EXCEPTION 'lis_report_locked:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - v_open) IS DISTINCT FROM (to_jsonb(OLD) - v_open) THEN
        RAISE EXCEPTION 'lis_report_locked:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'amended' THEN   -- only a validated report is superseded
        RAISE EXCEPTION 'lis_bad_transition:%->amended', OLD.status USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION lis_guard_result_value() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
    v_status TEXT;
BEGIN
    IF TG_OP = 'DELETE' AND (opd_purging(v_clinic) OR ipd_purging(v_clinic)) THEN RETURN OLD; END IF;
    SELECT status INTO v_status FROM lab_results
     WHERE clinic_id = v_clinic AND id = COALESCE(NEW.result_id, OLD.result_id);
    IF v_status IS NULL OR v_status IN ('validated', 'amended') THEN
        RAISE EXCEPTION 'lis_report_locked:%', COALESCE(NEW.result_id, OLD.result_id) USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.result_id, NEW.clinic_id) IS DISTINCT FROM (OLD.result_id, OLD.clinic_id) THEN
        RAISE EXCEPTION 'lis_report_locked:%', OLD.result_id USING ERRCODE = 'P0001';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_lab_results_guard ON lab_results;
CREATE TRIGGER trg_lab_results_guard BEFORE UPDATE OR DELETE ON lab_results
    FOR EACH ROW EXECUTE FUNCTION lis_guard_result();
DROP TRIGGER IF EXISTS trg_lab_results_touch ON lab_results;
CREATE TRIGGER trg_lab_results_touch BEFORE UPDATE ON lab_results
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_lab_result_values_guard ON lab_result_values;
CREATE TRIGGER trg_lab_result_values_guard BEFORE INSERT OR UPDATE OR DELETE ON lab_result_values
    FOR EACH ROW EXECUTE FUNCTION lis_guard_result_value();
DROP TRIGGER IF EXISTS trg_lab_test_parameters_touch ON lab_test_parameters;
CREATE TRIGGER trg_lab_test_parameters_touch BEFORE UPDATE ON lab_test_parameters
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- ── 6. Record results (technician) ─────────────────────────────────────────
-- p_values: [{parameter_id, parameter_name, unit, sort_order, numeric_value, text_value, flag,
--             reference_range_display, remarks}], flags computed by app/services/lis.py.
CREATE OR REPLACE FUNCTION lis_record_results(
    p_clinic_id UUID, p_order_id UUID, p_values JSONB, p_has_critical BOOLEAN,
    p_amendment_reason TEXT, p_actor_id UUID, p_actor_name TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    o opd_orders;
    r lab_results;
    v_new BOOLEAN := false;
BEGIN
    IF jsonb_typeof(p_values) <> 'array' OR jsonb_array_length(p_values) NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'lis_values_empty' USING ERRCODE = 'P0001';
    END IF;
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'lis_order_not_found' USING ERRCODE = 'P0001'; END IF;
    IF o.catalog_type <> 'lab_test' THEN RAISE EXCEPTION 'lis_order_not_lab' USING ERRCODE = 'P0001'; END IF;

    SELECT * INTO r FROM lab_results WHERE clinic_id = p_clinic_id AND order_id = p_order_id
     ORDER BY version DESC LIMIT 1 FOR UPDATE;

    IF r.id IS NULL THEN
        IF o.status NOT IN ('sample_collected', 'in_progress') THEN
            RAISE EXCEPTION 'lis_order_not_ready:%', o.status USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO lab_results (clinic_id, branch_id, order_id, patient_id, family_member_id, lab_test_id, test_name,
                                 status, has_critical_value, technician_admin_id, technician_name, entered_at)
        VALUES (p_clinic_id, o.branch_id, o.id, o.patient_id, o.family_member_id, o.catalog_reference_id, o.item_name,
                'entered', p_has_critical, p_actor_id, p_actor_name, now())
        RETURNING * INTO r;
        v_new := true;
        IF o.status = 'sample_collected' THEN
            UPDATE opd_orders SET status = 'in_progress' WHERE clinic_id = p_clinic_id AND id = o.id;
        END IF;
    ELSIF r.status IN ('draft', 'entered') THEN
        DELETE FROM lab_result_values WHERE clinic_id = p_clinic_id AND result_id = r.id;
        UPDATE lab_results SET status = 'entered', has_critical_value = p_has_critical, technician_admin_id = p_actor_id,
               technician_name = p_actor_name, entered_at = now()
         WHERE clinic_id = p_clinic_id AND id = r.id
        RETURNING * INTO r;
    ELSE   -- latest version is signed: a correction is a new, versioned amendment
        IF p_amendment_reason IS NULL OR char_length(btrim(p_amendment_reason)) < 5 THEN
            RAISE EXCEPTION 'lis_amendment_reason_required' USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO lab_results (clinic_id, branch_id, order_id, patient_id, family_member_id, lab_test_id, test_name,
                                 version, supersedes_id, amendment_reason, status, has_critical_value,
                                 technician_admin_id, technician_name, entered_at)
        VALUES (p_clinic_id, r.branch_id, r.order_id, r.patient_id, r.family_member_id, r.lab_test_id, r.test_name,
                r.version + 1, r.id, btrim(p_amendment_reason), 'entered', p_has_critical,
                p_actor_id, p_actor_name, now())
        RETURNING * INTO r;
        v_new := true;
    END IF;

    INSERT INTO lab_result_values (clinic_id, result_id, parameter_id, parameter_name, unit, sort_order, numeric_value,
                                   text_value, flag, reference_range_display, remarks)
    SELECT p_clinic_id, r.id, x.parameter_id, x.parameter_name, x.unit, COALESCE(x.sort_order, 1), x.numeric_value,
           NULLIF(x.text_value, ''), COALESCE(x.flag, 'normal'), x.reference_range_display, NULLIF(x.remarks, '')
      FROM jsonb_to_recordset(p_values) AS x(parameter_id UUID, parameter_name TEXT, unit TEXT, sort_order SMALLINT,
                                             numeric_value NUMERIC, text_value TEXT, flag TEXT,
                                             reference_range_display TEXT, remarks TEXT);

    RETURN to_jsonb(r) || jsonb_build_object('created', v_new);
END $$;

-- ── 7. Validate & sign (pathologist) ───────────────────────────────────────
-- p_expected_updated_at: the version the pathologist reviewed. A re-entry in
-- between changes it, and the signature is refused instead of covering values
-- nobody reviewed.
CREATE OR REPLACE FUNCTION lis_validate_result(
    p_clinic_id UUID, p_result_id UUID, p_expected_updated_at TIMESTAMPTZ, p_actor_id UUID,
    p_pathologist_snapshot JSONB, p_letterhead_snapshot JSONB, p_patient_snapshot JSONB
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    r lab_results;
    o opd_orders;
    v_order UUID;
    v_name TEXT := left(COALESCE(p_pathologist_snapshot ->> 'name', 'pathologist'), 100);
BEGIN
    SELECT order_id INTO v_order FROM lab_results WHERE clinic_id = p_clinic_id AND id = p_result_id;
    IF v_order IS NULL THEN RAISE EXCEPTION 'lis_result_not_found' USING ERRCODE = 'P0001'; END IF;
    -- lock order then result: the same order as lis_record_results, so the two never deadlock
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = v_order FOR UPDATE;
    SELECT * INTO r FROM lab_results WHERE clinic_id = p_clinic_id AND id = p_result_id FOR UPDATE;

    IF r.status = 'validated' AND r.pathologist_admin_id IS NOT DISTINCT FROM p_actor_id THEN
        RETURN to_jsonb(r) || jsonb_build_object('replayed', true);   -- double click
    END IF;
    IF r.status <> 'entered' THEN
        RAISE EXCEPTION 'lis_not_pending:%', r.status USING ERRCODE = 'P0001';
    END IF;
    IF p_expected_updated_at IS NULL OR date_trunc('milliseconds', r.updated_at)
                                         <> date_trunc('milliseconds', p_expected_updated_at) THEN
        RAISE EXCEPTION 'lis_result_changed' USING ERRCODE = 'P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM lab_result_values WHERE clinic_id = p_clinic_id AND result_id = r.id) THEN
        RAISE EXCEPTION 'lis_values_empty' USING ERRCODE = 'P0001';
    END IF;
    IF btrim(COALESCE(p_pathologist_snapshot ->> 'registration_number', '')) = '' THEN
        RAISE EXCEPTION 'lis_signer_registration_missing' USING ERRCODE = 'P0001';
    END IF;

    UPDATE lab_results SET status = 'validated', pathologist_admin_id = p_actor_id,
           pathologist_snapshot = p_pathologist_snapshot, letterhead_snapshot = p_letterhead_snapshot,
           patient_snapshot = p_patient_snapshot, validated_at = now()
     WHERE clinic_id = p_clinic_id AND id = r.id
    RETURNING * INTO r;

    IF r.supersedes_id IS NOT NULL THEN
        PERFORM set_config('kriya.lis_amending', r.supersedes_id::TEXT, true);
        UPDATE lab_results SET status = 'amended'
         WHERE clinic_id = p_clinic_id AND id = r.supersedes_id AND status = 'validated';
        PERFORM set_config('kriya.lis_amending', '', true);
    END IF;

    -- the order is fulfilled by the first signed report; an amendment leaves it completed
    IF o.status IN ('sample_collected', 'in_progress', 'resulted') THEN
        UPDATE opd_orders SET status = 'completed', resulted_at = COALESCE(resulted_at, now()),
               resulted_by = COALESCE(resulted_by, p_actor_id), resulted_by_name = COALESCE(resulted_by_name, v_name),
               result_summary = COALESCE(result_summary,
                   'LIS report v' || r.version || ' validated' ||
                   CASE WHEN r.has_critical_value THEN ' (CRITICAL VALUES)' ELSE '' END)
         WHERE clinic_id = p_clinic_id AND id = o.id;
    END IF;

    RETURN to_jsonb(r) || jsonb_build_object('replayed', false);
END $$;

-- ── 8. Function hygiene + API-role lockout (103/115 pattern) ───────────────
ALTER FUNCTION lis_guard_result() SET search_path = public, pg_temp;
ALTER FUNCTION lis_guard_result_value() SET search_path = public, pg_temp;
ALTER FUNCTION lis_record_results(uuid, uuid, jsonb, boolean, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION lis_validate_result(uuid, uuid, timestamptz, uuid, jsonb, jsonb, jsonb) SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'lis_record_results(uuid,uuid,jsonb,boolean,text,uuid,text)',
        'lis_validate_result(uuid,uuid,timestamptz,uuid,jsonb,jsonb,jsonb)']
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

-- RLS parity (defense in depth); the service_role connection bypasses it and
-- the application's clinic predicate is the boundary (app/tenancy.py).
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['lab_test_parameters', 'lab_results', 'lab_result_values'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON %I', 'service_role_' || t, t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO service_role USING (true) WITH CHECK (true)',
                           'service_role_' || t, t);
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

-- ── 9. Verify (fails the transaction if anything is missing) ───────────────
DO $$
BEGIN
    IF to_regclass('public.lab_results') IS NULL OR to_regclass('public.lab_test_parameters') IS NULL
       OR to_regclass('public.lab_result_values') IS NULL THEN
        RAISE EXCEPTION '118 verify: LIS tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN (
          'lab_tests_clinic_id_id_key', 'lab_test_parameters_test_fk', 'lab_results_order_fk',
          'lab_results_validated_complete', 'lab_results_amendment_shape', 'lab_result_values_result_fk')) <> 6 THEN
        RAISE EXCEPTION '118 verify: LIS constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_lab_results_guard', 'trg_lab_results_touch', 'trg_lab_result_values_guard',
          'trg_lab_test_parameters_touch')) <> 4 THEN
        RAISE EXCEPTION '118 verify: LIS triggers missing';
    END IF;
    IF to_regprocedure('lis_record_results(uuid,uuid,jsonb,boolean,text,uuid,text)') IS NULL
       OR to_regprocedure('lis_validate_result(uuid,uuid,timestamptz,uuid,jsonb,jsonb,jsonb)') IS NULL THEN
        RAISE EXCEPTION '118 verify: LIS functions missing';
    END IF;
END $$;
