-- ============================================================================
-- Migration 133: Kriya Diagnostic OS instruments and QC (DX-M9) — analyser
-- bridge agents, instruments, raw instrument results, QC materials and the
-- append-only QC run log.
--
-- ADR-DX-13: the lab runs an outbound-only bridge; Kriya stores only the SHA-256
-- of its key. Instrument results are raw and replay-safe (one row per
-- instrument + message id); they PREFILL the worksheet and are saved as
-- 'entered' only when dx_settings.instrument_auto_enter is on — verification
-- and approval stay human. Zero-LLM (ADR-DX-10).
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §10, ADR-DX-13, SECURITY §2.
-- Additive: five new tables, dx_purge_clinic v9 and one guarded text patch of
-- dx_record_results (value meta keeps the instrument result id), both reversed
-- by the rollback. Clinics without instruments are unchanged.
-- ============================================================================

-- ── 1. Bridge agents (one per lab PC; key shown once, only its hash kept) ──
CREATE TABLE IF NOT EXISTS dx_bridge_agents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    site_id      UUID NOT NULL,
    name         TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    key_hash     TEXT NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    key_prefix   TEXT NOT NULL CHECK (key_prefix ~ '^kba_[A-Za-z0-9_-]{8}$'),
    last_seen_at TIMESTAMPTZ NULL,
    is_active    BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_bridge_agents_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_bridge_agents_prefix_key UNIQUE (key_prefix),
    CONSTRAINT dx_bridge_agents_site_fk FOREIGN KEY (clinic_id, site_id) REFERENCES dx_sites (clinic_id, id)
        ON DELETE CASCADE
);

-- ── 2. Instruments ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_instruments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    site_id       UUID NOT NULL,
    agent_id      UUID NOT NULL REFERENCES dx_bridge_agents (id) ON DELETE CASCADE,
    name          TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    model         TEXT NULL CHECK (char_length(model) <= 80),
    protocol      TEXT NOT NULL CHECK (protocol IN ('astm', 'hl7v2')),
    department_id UUID NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_instruments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_instruments_site_fk FOREIGN KEY (clinic_id, site_id) REFERENCES dx_sites (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT dx_instruments_agent_fk FOREIGN KEY (clinic_id, agent_id) REFERENCES dx_bridge_agents (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT dx_instruments_department_fk FOREIGN KEY (clinic_id, department_id)
        REFERENCES dx_departments (clinic_id, id)
);

-- ── 3. Raw instrument results (replay-safe; the raw message never changes) ─
-- `mapping` is Kriya's prefill: {"orders": {order_id: [{parameter_id, value, unit, machine_code}]},
-- "unmatched_codes": [...]}; only mapping, mapping_status and applied_result_id may change.
CREATE TABLE IF NOT EXISTS dx_instrument_results (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    instrument_id     UUID NOT NULL REFERENCES dx_instruments (id) ON DELETE CASCADE,
    message_id        TEXT NOT NULL CHECK (char_length(message_id) BETWEEN 1 AND 120),
    barcode           TEXT NOT NULL CHECK (char_length(barcode) BETWEEN 1 AND 64),
    raw_message       TEXT NOT NULL CHECK (char_length(raw_message) <= 200000),
    parsed            JSONB NOT NULL,
    mapping           JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    mapping_status    TEXT NOT NULL DEFAULT 'pending'
                      CHECK (mapping_status IN ('pending', 'applied', 'unmatched', 'ignored')),
    applied_result_id UUID NULL,
    mapped_by_name    TEXT NULL CHECK (char_length(mapped_by_name) <= 100),
    CONSTRAINT dx_instrument_results_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_instrument_results_message_key UNIQUE (clinic_id, instrument_id, message_id),
    CONSTRAINT dx_instrument_results_instrument_fk FOREIGN KEY (clinic_id, instrument_id)
        REFERENCES dx_instruments (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dx_instrument_results_barcode ON dx_instrument_results (clinic_id, barcode, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_instrument_results_status ON dx_instrument_results (clinic_id, mapping_status, received_at DESC);

CREATE OR REPLACE FUNCTION dx_guard_instrument_result() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_instrument_result_append_only');
    END IF;
    IF (to_jsonb(NEW) - 'mapping' - 'mapping_status' - 'applied_result_id' - 'mapped_by_name')
       IS DISTINCT FROM (to_jsonb(OLD) - 'mapping' - 'mapping_status' - 'applied_result_id' - 'mapped_by_name') THEN
        PERFORM dx_raise('dx_instrument_result_append_only');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_instrument_results_guard ON dx_instrument_results;
CREATE TRIGGER trg_dx_instrument_results_guard BEFORE UPDATE OR DELETE ON dx_instrument_results
    FOR EACH ROW EXECUTE FUNCTION dx_guard_instrument_result();

-- ── 4. QC materials and the append-only QC run log ─────────────────────────
CREATE TABLE IF NOT EXISTS dx_qc_materials (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    instrument_id  UUID NOT NULL REFERENCES dx_instruments (id) ON DELETE CASCADE,
    parameter_code TEXT NOT NULL CHECK (char_length(btrim(parameter_code)) BETWEEN 1 AND 40),
    level          TEXT NOT NULL CHECK (char_length(btrim(level)) BETWEEN 1 AND 20),
    lot            TEXT NOT NULL CHECK (char_length(btrim(lot)) BETWEEN 1 AND 40),
    target_mean    NUMERIC NOT NULL,
    target_sd      NUMERIC NOT NULL CHECK (target_sd > 0),
    valid_from     DATE NOT NULL,
    valid_to       DATE NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_qc_materials_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_qc_materials_valid CHECK (valid_to >= valid_from),
    CONSTRAINT dx_qc_materials_instrument_fk FOREIGN KEY (clinic_id, instrument_id)
        REFERENCES dx_instruments (clinic_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS dx_qc_results (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    material_id    UUID NOT NULL REFERENCES dx_qc_materials (id) ON DELETE CASCADE,
    value          NUMERIC NOT NULL,
    z_score        NUMERIC NOT NULL,
    westgard_flags TEXT[] NOT NULL DEFAULT '{}',
    run_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    entered_by     UUID NULL,
    entered_by_name TEXT NULL CHECK (char_length(entered_by_name) <= 100),
    accepted       BOOLEAN NOT NULL DEFAULT true,
    reviewer_note  TEXT NULL CHECK (char_length(reviewer_note) <= 500),
    CONSTRAINT dx_qc_results_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_qc_results_flags CHECK (westgard_flags <@ ARRAY['1_2s', '1_3s', '2_2s', 'R_4s', '4_1s', '10_x']::TEXT[]),
    CONSTRAINT dx_qc_results_material_fk FOREIGN KEY (clinic_id, material_id)
        REFERENCES dx_qc_materials (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dx_qc_results_material ON dx_qc_results (clinic_id, material_id, run_at);

CREATE OR REPLACE FUNCTION dx_guard_qc_result() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF dx_purging(OLD.clinic_id) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    PERFORM dx_raise('dx_qc_result_append_only');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_dx_qc_results_append_only ON dx_qc_results;
CREATE TRIGGER trg_dx_qc_results_append_only BEFORE UPDATE OR DELETE ON dx_qc_results
    FOR EACH ROW EXECUTE FUNCTION dx_guard_qc_result();

-- ── 5. Value meta keeps which instrument result filled a value ─────────────
DO $$
DECLARE p TEXT[]; v_def TEXT; n INTEGER;
BEGIN
    FOREACH p SLICE 1 IN ARRAY ARRAY[
        ['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
         $s$previous_value, previous_at, delta_pct, delta_failed, source)$s$,
         $s$previous_value, previous_at, delta_pct, delta_failed, source, instrument_result_id)$s$],
        ['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
         $s$COALESCE(x.source, 'manual')$s$,
         $s$COALESCE(x.source, 'manual'), x.instrument_result_id$s$],
        ['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
         $s$source TEXT);$s$,
         $s$source TEXT, instrument_result_id UUID);$s$]
    ] LOOP
        v_def := pg_get_functiondef(p[1]::regprocedure);
        IF position(p[3] IN v_def) > 0 THEN CONTINUE; END IF;                  -- re-run: already patched
        n := (char_length(v_def) - char_length(replace(v_def, p[2], ''))) / char_length(p[2]);
        IF n <> 1 THEN RAISE EXCEPTION '133: % needle found % times: %', p[1], n, p[2]; END IF;
        EXECUTE replace(v_def, p[2], p[3]);
    END LOOP;
END $$;

-- ── 6. Purge v9 (instrument and QC rows first: they reference sites) ──────
DO $$
DECLARE v_def TEXT;
    v_from TEXT := $s$PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders$s$;
    v_to   TEXT := $s$PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    -- v9 (133): instruments and QC
    DELETE FROM dx_qc_results WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('qc_results', n);
    DELETE FROM dx_qc_materials WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_instrument_results WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('instrument_results', n);
    DELETE FROM dx_instruments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_bridge_agents WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('bridge_agents', n);$s$;
BEGIN
    v_def := pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure);
    IF position('v9 (133)' IN v_def) > 0 THEN RETURN; END IF;
    IF (char_length(v_def) - char_length(replace(v_def, v_from, ''))) / char_length(v_from) <> 1 THEN
        RAISE EXCEPTION '133: dx_purge_clinic is not v8';
    END IF;
    EXECUTE replace(v_def, v_from, v_to);
END $$;

-- ── 7. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_bridge_agents', 'dx_instruments', 'dx_instrument_results', 'dx_qc_materials',
                             'dx_qc_results']
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
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'dx\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
        END IF;
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 8. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relrowsecurity AND relforcerowsecurity
          AND relname IN ('dx_bridge_agents', 'dx_instruments', 'dx_instrument_results', 'dx_qc_materials',
                          'dx_qc_results')) <> 5 THEN
        RAISE EXCEPTION '133 verify: instrument / QC tables missing or RLS not forced';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_instrument_results_guard',
                                                         'trg_dx_qc_results_append_only')) <> 2 THEN
        RAISE EXCEPTION '133 verify: append-only guards missing';
    END IF;
    IF position('v9 (133)' IN pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure)) = 0
       OR position('instrument_result_id UUID' IN pg_get_functiondef(
            'dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)'::regprocedure)) = 0 THEN
        RAISE EXCEPTION '133 verify: function patches missing';
    END IF;
END $$;
