-- ============================================================================
-- Migration 130: Kriya Diagnostic OS result workflow (DX-M6)
-- Value metadata (calculated / delta), verifications (third level), critical
-- value events (detection + the call with read-back), result attachments, the
-- verification gate on lab_results, and the functions the bench uses: record
-- results, verify / return, approve (single or batch), log a critical call.
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §7, ADR-DX-06, AUDIT A5/A26.
-- Additive: four new tables, one new trigger on lab_results that returns at
-- once unless the result belongs to a source = 'dx' order going entered ->
-- validated, new functions, dx_purge_clinic v6. OPD / IPD signing is unchanged.
-- ============================================================================

-- ── 1. Tables ──────────────────────────────────────────────────────────────
-- parameter_id is a reference only, like lab_result_values.parameter_id (118):
-- band rows may be edited or removed while the recorded result stays.
CREATE TABLE IF NOT EXISTS dx_result_value_meta (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    result_id            UUID NOT NULL,
    parameter_id         UUID NOT NULL,
    is_calculated        BOOLEAN NOT NULL DEFAULT false,
    formula_snapshot     TEXT NULL CHECK (char_length(formula_snapshot) <= 500),
    previous_value       TEXT NULL CHECK (char_length(previous_value) <= 500),
    previous_at          TIMESTAMPTZ NULL,
    delta_pct            NUMERIC(8,2) NULL,
    delta_failed         BOOLEAN NOT NULL DEFAULT false,
    source               TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'instrument', 'calculated')),
    instrument_result_id UUID NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_result_value_meta_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_result_value_meta_param_key UNIQUE (clinic_id, result_id, parameter_id),
    CONSTRAINT dx_result_value_meta_result_fk FOREIGN KEY (clinic_id, result_id)
        REFERENCES lab_results (clinic_id, id) ON DELETE CASCADE
);

-- append-only: a re-entry changes lab_results.updated_at, which retires earlier rows
CREATE TABLE IF NOT EXISTS dx_result_verifications (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    result_id         UUID NOT NULL,
    result_updated_at TIMESTAMPTZ NOT NULL,
    decision          TEXT NOT NULL CHECK (decision IN ('verified', 'returned')),
    comment           TEXT NULL CHECK (char_length(comment) <= 500),
    verifier_id       UUID NULL,
    verifier_name     TEXT NOT NULL CHECK (char_length(btrim(verifier_name)) BETWEEN 1 AND 100),
    is_auto           BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_result_verifications_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_result_verifications_result_fk FOREIGN KEY (clinic_id, result_id)
        REFERENCES lab_results (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_result_verifications_returned_comment CHECK (
        decision = 'verified' OR char_length(btrim(COALESCE(comment, ''))) >= 3),
    CONSTRAINT dx_result_verifications_never_auto CHECK (NOT is_auto)     -- human verification only (ADR-DX-06)
);
CREATE INDEX IF NOT EXISTS idx_dx_result_verifications_result ON dx_result_verifications (clinic_id, result_id, created_at);

-- append-only: a detection row (notified_at NULL), then one row per call (a correction is a new row)
CREATE TABLE IF NOT EXISTS dx_critical_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    result_id           UUID NOT NULL,
    accession_id        UUID NOT NULL,
    parameters          JSONB NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL,
    notified_to         TEXT NULL CHECK (char_length(notified_to) <= 120),
    notified_role       TEXT NULL CHECK (notified_role IN ('referrer', 'patient', 'ward', 'account', 'other')),
    channel             TEXT NULL CHECK (channel IN ('phone', 'whatsapp', 'in_person')),
    notified_at         TIMESTAMPTZ NULL,
    read_back_confirmed BOOLEAN NULL,
    recorded_by         UUID NULL,
    recorded_by_name    TEXT NULL CHECK (char_length(recorded_by_name) <= 100),
    notes               TEXT NULL CHECK (char_length(notes) <= 500),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_critical_events_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_critical_events_result_fk FOREIGN KEY (clinic_id, result_id)
        REFERENCES lab_results (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_critical_events_accession_fk FOREIGN KEY (clinic_id, accession_id)
        REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_critical_events_call_shape CHECK (notified_at IS NULL OR (notified_to IS NOT NULL
        AND notified_role IS NOT NULL AND channel IS NOT NULL AND read_back_confirmed IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_dx_critical_events_result ON dx_critical_events (clinic_id, result_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dx_critical_events_accession ON dx_critical_events (clinic_id, accession_id);

CREATE TABLE IF NOT EXISTS dx_result_attachments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    result_id       UUID NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('graph', 'image', 'pdf')),
    storage_path    TEXT NOT NULL CHECK (char_length(storage_path) <= 300),
    file_name       TEXT NOT NULL CHECK (char_length(btrim(file_name)) BETWEEN 1 AND 200),
    caption         TEXT NULL CHECK (char_length(caption) <= 200),
    sha256          TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    print_on_report BOOLEAN NOT NULL DEFAULT true,
    uploaded_by     UUID NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_result_attachments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_result_attachments_result_fk FOREIGN KEY (clinic_id, result_id)
        REFERENCES lab_results (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dx_result_attachments_result ON dx_result_attachments (clinic_id, result_id);

-- ── 2. Guards ──────────────────────────────────────────────────────────────
-- meta, verifications and critical events are written only inside dx_* functions
-- (127 guard); verifications and critical events are also never updated.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_result_value_meta', 'dx_result_verifications', 'dx_critical_events'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_tx()',
                       'trg_' || t || '_guard', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['dx_result_verifications', 'dx_critical_events'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_append_only()',
                       'trg_' || t || '_append_only', t);
    END LOOP;
END $$;

-- an attachment is added only while its result is open (draft / entered); never edited
CREATE OR REPLACE FUNCTION dx_guard_result_attachment() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_append_only:dx_result_attachments');
    END IF;
    IF TG_OP = 'UPDATE' THEN PERFORM dx_raise('dx_append_only:dx_result_attachments'); END IF;
    IF NOT EXISTS (SELECT 1 FROM lab_results WHERE clinic_id = NEW.clinic_id AND id = NEW.result_id
                                               AND status IN ('draft', 'entered')) THEN
        PERFORM dx_raise('dx_attachment_locked');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_result_attachments_guard ON dx_result_attachments;
CREATE TRIGGER trg_dx_result_attachments_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_result_attachments
    FOR EACH ROW EXECUTE FUNCTION dx_guard_result_attachment();

-- ── 3. Verification gate (ADR-DX-06) ───────────────────────────────────────
-- Only a DX result going entered -> validated is checked; every other row returns at once.
CREATE OR REPLACE FUNCTION dx_lab_results_gate() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (OLD.status = 'entered' AND NEW.status = 'validated') THEN RETURN NEW; END IF;
    IF (SELECT source FROM opd_orders WHERE clinic_id = OLD.clinic_id AND id = OLD.order_id) IS DISTINCT FROM 'dx' THEN
        RETURN NEW;
    END IF;
    IF COALESCE((SELECT (dx_settings ->> 'approval_levels')::INTEGER FROM clinics WHERE id = OLD.clinic_id), 2) = 3
       AND NOT EXISTS (
           SELECT 1 FROM dx_result_verifications v
            WHERE v.clinic_id = OLD.clinic_id AND v.result_id = OLD.id AND v.decision = 'verified'
              AND date_trunc('milliseconds', v.result_updated_at) = date_trunc('milliseconds', OLD.updated_at)
              AND NOT EXISTS (SELECT 1 FROM dx_result_verifications r
                               WHERE r.clinic_id = v.clinic_id AND r.result_id = v.result_id
                                 AND r.decision = 'returned' AND r.created_at > v.created_at)) THEN
        PERFORM dx_raise('dx_verification_required');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_lab_results_gate ON lab_results;
CREATE TRIGGER trg_dx_lab_results_gate BEFORE UPDATE OF status ON lab_results
    FOR EACH ROW EXECUTE FUNCTION dx_lab_results_gate();

-- ── 4. Record results (technician) ─────────────────────────────────────────
-- p_values: as lis_record_results (flags computed by app/services/dx_results.py);
-- p_meta: [{parameter_id, is_calculated, formula_snapshot, previous_value, previous_at,
--           delta_pct, delta_failed, source}].
CREATE OR REPLACE FUNCTION dx_record_results(p_clinic_id UUID, p_order_id UUID, p_values JSONB, p_meta JSONB,
                                             p_has_critical BOOLEAN, p_amendment_reason TEXT, p_actor_id UUID,
                                             p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o opd_orders; v_sample TEXT; r JSONB; v_rid UUID; v_crit JSONB; v_event UUID;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('lis_order_not_found'); END IF;
    IF o.source <> 'dx' THEN PERFORM dx_raise('dx_not_dx_order'); END IF;
    IF o.status = 'cancelled' THEN PERFORM dx_raise('dx_order_cancelled'); END IF;
    IF o.dx_sample_id IS NOT NULL THEN
        SELECT status INTO v_sample FROM dx_samples WHERE clinic_id = p_clinic_id AND id = o.dx_sample_id;
        IF v_sample NOT IN ('received', 'stored') THEN PERFORM dx_raise('dx_sample_awaiting_receipt'); END IF;
    END IF;
    r := lis_record_results(p_clinic_id, p_order_id, p_values, p_has_critical, p_amendment_reason, p_actor_id,
                            left(COALESCE(p_actor_name, 'technician'), 100));
    v_rid := (r ->> 'id')::UUID;
    DELETE FROM dx_result_value_meta WHERE clinic_id = p_clinic_id AND result_id = v_rid;
    INSERT INTO dx_result_value_meta (clinic_id, result_id, parameter_id, is_calculated, formula_snapshot,
                                      previous_value, previous_at, delta_pct, delta_failed, source)
    SELECT p_clinic_id, v_rid, x.parameter_id, COALESCE(x.is_calculated, false), x.formula_snapshot, x.previous_value,
           x.previous_at, x.delta_pct, COALESCE(x.delta_failed, false), COALESCE(x.source, 'manual')
      FROM jsonb_to_recordset(COALESCE(p_meta, '[]'::jsonb)) AS x(parameter_id UUID, is_calculated BOOLEAN,
           formula_snapshot TEXT, previous_value TEXT, previous_at TIMESTAMPTZ, delta_pct NUMERIC, delta_failed BOOLEAN,
           source TEXT);
    IF p_has_critical THEN
        SELECT COALESCE(jsonb_agg(jsonb_build_object('parameter_id', v ->> 'parameter_id', 'name', v ->> 'parameter_name',
                        'value', COALESCE(v ->> 'numeric_value', v ->> 'text_value'), 'unit', v ->> 'unit',
                        'flag', v ->> 'flag')), '[]'::jsonb)
          INTO v_crit FROM jsonb_array_elements(p_values) v WHERE v ->> 'flag' LIKE 'critical%';
        INSERT INTO dx_critical_events (clinic_id, result_id, accession_id, parameters, detected_at)
        VALUES (p_clinic_id, v_rid, o.accession_id, v_crit, now()) RETURNING id INTO v_event;
    END IF;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, order_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, o.accession_id, o.dx_sample_id, o.id, 'results_entered', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('result_id', v_rid, 'version', r -> 'version', 'critical', p_has_critical,
                               'critical_event_id', v_event));
    RETURN r || jsonb_build_object('critical_event_id', v_event);
END $$;

-- ── 5. Verify / return (supervisor, third level) ───────────────────────────
CREATE OR REPLACE FUNCTION dx_verify_result(p_clinic_id UUID, p_result_id UUID, p_expected_updated_at TIMESTAMPTZ,
                                            p_decision TEXT, p_comment TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE r lab_results; o opd_orders; v dx_result_verifications;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO r FROM lab_results WHERE clinic_id = p_clinic_id AND id = p_result_id;
    IF NOT FOUND THEN PERFORM dx_raise('lis_result_not_found'); END IF;
    -- lock order then result: the order 118 uses, so entry and verification never deadlock
    SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = r.order_id FOR UPDATE;
    SELECT * INTO r FROM lab_results WHERE clinic_id = p_clinic_id AND id = p_result_id FOR UPDATE;
    IF o.source <> 'dx' THEN PERFORM dx_raise('dx_not_dx_order'); END IF;
    IF p_decision NOT IN ('verified', 'returned') THEN PERFORM dx_raise('dx_decision_invalid'); END IF;
    IF r.status <> 'entered' THEN PERFORM dx_raise('lis_not_pending:' || r.status); END IF;
    IF p_expected_updated_at IS NULL OR date_trunc('milliseconds', r.updated_at)
                                         <> date_trunc('milliseconds', p_expected_updated_at) THEN
        PERFORM dx_raise('lis_result_changed');
    END IF;
    IF p_decision = 'verified'
       AND COALESCE((SELECT (dx_settings ->> 'approval_levels')::INTEGER FROM clinics WHERE id = p_clinic_id), 2) = 3
       AND p_actor_id IS NOT DISTINCT FROM r.technician_admin_id AND p_actor_id IS NOT NULL THEN
        PERFORM dx_raise('dx_self_verification');
    END IF;
    INSERT INTO dx_result_verifications (clinic_id, result_id, result_updated_at, decision, comment, verifier_id,
                                         verifier_name)
    VALUES (p_clinic_id, r.id, r.updated_at, p_decision, NULLIF(btrim(p_comment), ''), p_actor_id,
            left(COALESCE(p_actor_name, 'verifier'), 100))
    RETURNING * INTO v;
    INSERT INTO dx_events (clinic_id, accession_id, sample_id, order_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, o.accession_id, o.dx_sample_id, o.id, p_decision, p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('result_id', r.id, 'comment', v.comment));
    RETURN to_jsonb(v);
END $$;

-- ── 6. Critical call (W8 step 6, AUDIT A26) ────────────────────────────────
CREATE OR REPLACE FUNCTION dx_log_critical_call(p_clinic_id UUID, p_event_id UUID, p_notified_to TEXT,
                                                p_notified_role TEXT, p_channel TEXT, p_read_back BOOLEAN,
                                                p_notes TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE d dx_critical_events; c dx_critical_events;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO d FROM dx_critical_events WHERE clinic_id = p_clinic_id AND id = p_event_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_critical_event_not_found'); END IF;
    IF char_length(btrim(COALESCE(p_notified_to, ''))) < 2 THEN PERFORM dx_raise('dx_critical_call_incomplete'); END IF;
    INSERT INTO dx_critical_events (clinic_id, result_id, accession_id, parameters, detected_at, notified_to,
                                    notified_role, channel, notified_at, read_back_confirmed, recorded_by,
                                    recorded_by_name, notes)
    VALUES (p_clinic_id, d.result_id, d.accession_id, d.parameters, d.detected_at, btrim(p_notified_to), p_notified_role,
            p_channel, now(), COALESCE(p_read_back, false), p_actor_id, left(p_actor_name, 100),
            NULLIF(btrim(p_notes), ''))
    RETURNING * INTO c;
    INSERT INTO dx_events (clinic_id, accession_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, d.accession_id, 'critical_call', p_actor_id, left(p_actor_name, 100),
            jsonb_build_object('result_id', d.result_id, 'event_id', c.id, 'role', p_notified_role,
                               'channel', p_channel, 'read_back', c.read_back_confirmed));
    RETURN to_jsonb(c);
END $$;

-- A critical value on the open result whose latest detection has no confirmed call after it.
CREATE OR REPLACE FUNCTION dx_critical_pending(p_clinic_id UUID, p_result_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM lab_results r WHERE r.clinic_id = p_clinic_id AND r.id = p_result_id AND r.has_critical_value)
       AND EXISTS (SELECT 1 FROM dx_critical_events d
                    WHERE d.clinic_id = p_clinic_id AND d.result_id = p_result_id AND d.notified_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM dx_critical_events c
                                       WHERE c.clinic_id = d.clinic_id AND c.result_id = d.result_id
                                         AND c.notified_at IS NOT NULL AND c.read_back_confirmed
                                         AND c.created_at > d.created_at))
$$;

-- ── 7. Approve & sign (pathologist; one or a batch) ────────────────────────
-- p_items: [{result_id, expected_updated_at, pathologist_snapshot, letterhead_snapshot, patient_snapshot}].
-- Each item runs in its own sub-transaction; the answer lists signed / refused per item.
-- Never automatic: every call names a human actor (the service requires a doctor-linked login).
CREATE OR REPLACE FUNCTION dx_approve_results(p_clinic_id UUID, p_items JSONB, p_actor_id UUID, p_actor_name TEXT,
                                              p_batch BOOLEAN) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE it JSONB; r lab_results; o opd_orders; v_out JSONB := '[]'::jsonb; v_signed JSONB; v_rid UUID;
        v_call BOOLEAN; v_ok INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF p_actor_id IS NULL THEN PERFORM dx_raise('dx_actor_required'); END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) NOT BETWEEN 1 AND 50 THEN
        PERFORM dx_raise('dx_approve_items_invalid');
    END IF;
    v_call := COALESCE((SELECT (dx_settings ->> 'critical_call_required')::BOOLEAN FROM clinics WHERE id = p_clinic_id), true);
    FOR it IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_rid := NULLIF(it ->> 'result_id', '')::UUID;
        BEGIN
            SELECT * INTO r FROM lab_results WHERE clinic_id = p_clinic_id AND id = v_rid;
            IF NOT FOUND THEN PERFORM dx_raise('lis_result_not_found'); END IF;
            SELECT * INTO o FROM opd_orders WHERE clinic_id = p_clinic_id AND id = r.order_id;
            IF o.source <> 'dx' THEN PERFORM dx_raise('dx_not_dx_order'); END IF;
            IF p_batch AND r.status = 'entered' AND (r.has_critical_value
                   OR EXISTS (SELECT 1 FROM lab_result_values x WHERE x.clinic_id = p_clinic_id AND x.result_id = r.id
                                                                  AND x.flag <> 'normal')
                   OR EXISTS (SELECT 1 FROM dx_result_value_meta m WHERE m.clinic_id = p_clinic_id
                                                                     AND m.result_id = r.id AND m.delta_failed)) THEN
                PERFORM dx_raise('dx_batch_not_normal');
            END IF;
            IF v_call AND r.status = 'entered' AND dx_critical_pending(p_clinic_id, r.id) THEN
                PERFORM dx_raise('dx_critical_not_notified');
            END IF;
            v_signed := lis_validate_result(p_clinic_id, r.id, (it ->> 'expected_updated_at')::TIMESTAMPTZ, p_actor_id,
                                            it -> 'pathologist_snapshot', it -> 'letterhead_snapshot',
                                            it -> 'patient_snapshot');
            IF NOT (v_signed ->> 'replayed')::BOOLEAN THEN
                INSERT INTO dx_events (clinic_id, accession_id, sample_id, order_id, kind, actor_id, actor_name, details)
                VALUES (p_clinic_id, o.accession_id, o.dx_sample_id, o.id, 'approved', p_actor_id,
                        left(p_actor_name, 100), jsonb_build_object('result_id', r.id, 'version', r.version,
                                                                    'batch', p_batch));
            END IF;
            v_out := v_out || jsonb_build_object('result_id', v_rid, 'ok', true, 'status', v_signed ->> 'status',
                                                 'replayed', (v_signed ->> 'replayed')::BOOLEAN);
            v_ok := v_ok + 1;
        EXCEPTION WHEN OTHERS THEN
            v_out := v_out || jsonb_build_object('result_id', v_rid, 'ok', false, 'error', SQLERRM);
        END;
    END LOOP;
    RETURN jsonb_build_object('items', v_out, 'signed', v_ok, 'refused', jsonb_array_length(v_out) - v_ok);
END $$;

-- ── 8. Purge v6 (adds attachments, critical events, verifications, value meta) ─
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_result_attachments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_critical_events WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('critical_events', n);
    DELETE FROM dx_result_verifications WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_result_value_meta WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM dx_batch_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_batches WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('batches', n);
    DELETE FROM opd_orders WHERE clinic_id = p_clinic_id AND source = 'dx'; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('orders', n);
    DELETE FROM dx_events WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_items WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accessions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('visits', n);
    UPDATE family_members SET dx_b2b_account_id = NULL WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL;
    DELETE FROM dx_referrers WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    DELETE FROM dx_routing_rules WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('routing_rules', n);
    DELETE FROM dx_outsource_partners WHERE clinic_id = p_clinic_id;
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

-- ── 9. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_result_value_meta', 'dx_result_verifications', 'dx_critical_events',
                             'dx_result_attachments']
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

-- ── 10. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public'
          AND tablename IN ('dx_result_value_meta', 'dx_result_verifications', 'dx_critical_events',
                            'dx_result_attachments')) <> 4 THEN
        RAISE EXCEPTION '130 verify: DX result workflow tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_lab_results_gate', 'trg_dx_result_attachments_guard')) <> 2 THEN
        RAISE EXCEPTION '130 verify: DX result workflow triggers missing';
    END IF;
    IF to_regprocedure('dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_verify_result(uuid,uuid,timestamptz,text,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_approve_results(uuid,jsonb,uuid,text,boolean)') IS NULL
       OR to_regprocedure('dx_log_critical_call(uuid,uuid,text,text,text,boolean,text,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '130 verify: DX result workflow functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_result_value_meta', 'dx_result_verifications',
                                                        'dx_critical_events', 'dx_result_attachments')
                 AND relkind = 'r' AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '130 verify: RLS not forced on DX result workflow tables';
    END IF;
END $$;
