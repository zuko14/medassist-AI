-- ============================================================================
-- Migration 144: Kriya Data Import — "Switch to Kriya" (lane MI, MI-01; docs/data-import/SPEC.md §4)
-- Batches (dry run → apply → undo), row errors, what each batch created or changed (for undo), legacy patient links
-- and the staff-confirmed phone match queue, read-only legacy history (encounters, lab values), opening balances,
-- the per-clinic legacy-diagnosis → ICD-10 map and chronic-patient candidates for Continuous Care.
--
-- Principles (SPEC §2): consent is never set by an import; legacy history is never a signed Kriya record; one batch =
-- one undo. Existing objects touched: none (no new column, no trigger on an existing table).
-- Rollback: rollback/144_rollback_data_import.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS mi_batches (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('doctors', 'services', 'packages', 'payers', 'patients', 'history',
                                                 'lab_values', 'dues', 'appointments_future', 'inpatients_cutover',
                                                 'chronic_list')),
    preset         TEXT NOT NULL DEFAULT 'generic' CHECK (preset IN ('generic', 'mocdoc', 'practo_insta')),
    file_name      TEXT NULL CHECK (char_length(file_name) <= 200),
    sha256         TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    rows_total     INTEGER NOT NULL DEFAULT 0 CHECK (rows_total BETWEEN 0 AND 25000),
    rows_ok        INTEGER NOT NULL DEFAULT 0 CHECK (rows_ok >= 0),
    rows_error     INTEGER NOT NULL DEFAULT 0 CHECK (rows_error >= 0),
    rows_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (rows_duplicate >= 0),
    status         TEXT NOT NULL DEFAULT 'dry_run' CHECK (status IN ('dry_run', 'applied', 'undone')),
    payload        JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(payload) = 'array'),   -- validated rows
    result         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by     UUID NULL,
    created_by_name TEXT NULL CHECK (char_length(created_by_name) <= 100),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at     TIMESTAMPTZ NULL,
    applied_by_name TEXT NULL CHECK (char_length(applied_by_name) <= 100),
    undone_at      TIMESTAMPTZ NULL,
    CONSTRAINT mi_batches_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT mi_batches_state CHECK ((status = 'dry_run' AND applied_at IS NULL AND undone_at IS NULL)
                                    OR (status = 'applied' AND applied_at IS NOT NULL AND undone_at IS NULL)
                                    OR (status = 'undone' AND applied_at IS NOT NULL AND undone_at IS NOT NULL))
);
-- the same file applied twice changes nothing: one live applied batch per (kind, file content)
CREATE UNIQUE INDEX IF NOT EXISTS uq_mi_batches_applied ON mi_batches (clinic_id, kind, sha256) WHERE status = 'applied';
CREATE INDEX IF NOT EXISTS idx_mi_batches_list ON mi_batches (clinic_id, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS mi_row_errors (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id    UUID NOT NULL,
    row_no      INTEGER NOT NULL CHECK (row_no >= 1),
    column_name TEXT NOT NULL CHECK (char_length(column_name) <= 60),
    problem     TEXT NOT NULL CHECK (char_length(problem) BETWEEN 1 AND 300),
    severity    TEXT NOT NULL DEFAULT 'error' CHECK (severity IN ('error', 'warning')),
    CONSTRAINT mi_row_errors_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_row_errors_batch ON mi_row_errors (clinic_id, batch_id, row_no);

-- what an applied batch created or changed in an existing table (doctors, services, packages, payers, appointments,
-- patient_import_batches): undo deletes what it created and restores what it changed, while nothing references it
CREATE TABLE IF NOT EXISTS mi_batch_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id     UUID NOT NULL,
    target_table TEXT NOT NULL CHECK (target_table IN ('doctors', 'billable_services', 'ipd_packages',
                                                       'insurance_payers', 'appointments', 'patient_import_batches')),
    target_id    UUID NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('created', 'updated')),
    previous     JSONB NULL,
    CONSTRAINT mi_batch_items_shape CHECK ((action = 'updated') = (previous IS NOT NULL)),
    CONSTRAINT mi_batch_items_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_batch_items_batch ON mi_batch_items (clinic_id, batch_id);

-- a legacy record linked to the live patient (front desk "Use this record", or a confirmed phone match)
CREATE TABLE IF NOT EXISTS mi_patient_links (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_record_id UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    patient_id        UUID NOT NULL,
    legacy_mrn        TEXT NULL CHECK (char_length(legacy_mrn) <= 64),
    source            TEXT NOT NULL CHECK (source IN ('front_desk', 'match_queue', 'import')),
    linked_by         UUID NULL,
    linked_by_name    TEXT NULL CHECK (char_length(linked_by_name) <= 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_patient_links_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT mi_patient_links_record_key UNIQUE (clinic_id, patient_record_id),
    CONSTRAINT mi_patient_links_patient_fk FOREIGN KEY (patient_id) REFERENCES patients (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_patient_links_patient ON mi_patient_links (clinic_id, patient_id);

-- WhatsApp / front-desk patients whose phone equals a legacy record's: staff confirm or reject, never auto-merged
CREATE TABLE IF NOT EXISTS mi_match_queue (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_record_id UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    patient_id        UUID NOT NULL REFERENCES patients (id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    decided_by        UUID NULL,
    decided_by_name   TEXT NULL CHECK (char_length(decided_by_name) <= 100),
    decided_at        TIMESTAMPTZ NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_match_queue_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT mi_match_queue_pair_key UNIQUE (clinic_id, patient_record_id, patient_id),
    CONSTRAINT mi_match_queue_decided CHECK ((status = 'pending') = (decided_at IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_mi_match_queue_pending ON mi_match_queue (clinic_id, status, created_at);

CREATE TABLE IF NOT EXISTS mi_legacy_encounters (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id          UUID NOT NULL,
    patient_record_id UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    visit_date        DATE NOT NULL,
    doctor_name       TEXT NULL CHECK (char_length(doctor_name) <= 120),
    diagnosis_text    TEXT NULL CHECK (char_length(diagnosis_text) <= 1000),
    icd10_code        TEXT NULL CHECK (icd10_code ~ '^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$'),
    medications_text  TEXT NULL CHECK (char_length(medications_text) <= 4000),
    vitals_text       TEXT NULL CHECK (char_length(vitals_text) <= 1000),
    notes             TEXT NULL CHECK (char_length(notes) <= 4000),
    source_system     TEXT NOT NULL CHECK (char_length(source_system) BETWEEN 2 AND 40),
    row_key           TEXT NOT NULL CHECK (char_length(row_key) <= 300),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_legacy_encounters_row_key UNIQUE (clinic_id, row_key),
    CONSTRAINT mi_legacy_encounters_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_legacy_encounters_record ON mi_legacy_encounters (clinic_id, patient_record_id, visit_date DESC);

CREATE TABLE IF NOT EXISTS mi_legacy_lab_values (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id          UUID NOT NULL,
    patient_record_id UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    test_name         TEXT NOT NULL CHECK (char_length(btrim(test_name)) BETWEEN 1 AND 120),
    value_text        TEXT NOT NULL CHECK (char_length(btrim(value_text)) BETWEEN 1 AND 60),
    unit              TEXT NULL CHECK (char_length(unit) <= 30),
    result_date       DATE NOT NULL,
    flag              TEXT NULL CHECK (flag IN ('high', 'low', 'normal', 'abnormal', 'critical')),
    source_system     TEXT NOT NULL CHECK (char_length(source_system) BETWEEN 2 AND 40),
    row_key           TEXT NOT NULL CHECK (char_length(row_key) <= 300),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_legacy_lab_values_row_key UNIQUE (clinic_id, row_key),
    CONSTRAINT mi_legacy_lab_values_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_legacy_lab_values_record ON mi_legacy_lab_values (clinic_id, patient_record_id, result_date DESC);

CREATE TABLE IF NOT EXISTS mi_opening_balances (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id             UUID NOT NULL,
    patient_record_id    UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    amount_paise         BIGINT NOT NULL CHECK (amount_paise BETWEEN 1 AND 10000000000),
    as_of                DATE NOT NULL,
    reference            TEXT NULL CHECK (char_length(reference) <= 80),
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'collected', 'written_off')),
    collected_invoice_id UUID NULL,
    closed_at            TIMESTAMPTZ NULL,
    closed_by_name       TEXT NULL CHECK (char_length(closed_by_name) <= 100),
    row_key              TEXT NOT NULL CHECK (char_length(row_key) <= 300),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_opening_balances_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT mi_opening_balances_row_key UNIQUE (clinic_id, row_key),
    CONSTRAINT mi_opening_balances_closed CHECK ((status = 'open') = (closed_at IS NULL)
                                                AND (status <> 'collected' OR collected_invoice_id IS NOT NULL)),
    CONSTRAINT mi_opening_balances_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_opening_balances_open ON mi_opening_balances (clinic_id, patient_record_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS mi_diagnosis_map (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    legacy_text_norm TEXT NOT NULL CHECK (char_length(legacy_text_norm) BETWEEN 2 AND 200 AND legacy_text_norm = lower(legacy_text_norm)),
    icd10_code       TEXT NOT NULL CHECK (icd10_code ~ '^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$'),
    updated_by_name  TEXT NULL CHECK (char_length(updated_by_name) <= 100),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_diagnosis_map_text_key UNIQUE (clinic_id, legacy_text_norm)
);

-- chronic-patient candidates for Continuous Care's radar (reason 'candidate', source 'import'); never an enrolment
CREATE TABLE IF NOT EXISTS mi_chronic_candidates (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    batch_id          UUID NOT NULL,
    patient_record_id UUID NOT NULL REFERENCES patient_records (id) ON DELETE CASCADE,
    condition_text    TEXT NOT NULL CHECK (char_length(btrim(condition_text)) BETWEEN 2 AND 200),
    icd10_code        TEXT NULL CHECK (icd10_code ~ '^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$'),
    last_visit_date   DATE NULL,
    source_system     TEXT NOT NULL CHECK (char_length(source_system) BETWEEN 2 AND 40),
    status            TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'on_radar', 'dismissed')),
    row_key           TEXT NOT NULL CHECK (char_length(row_key) <= 300),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mi_chronic_candidates_row_key UNIQUE (clinic_id, row_key),
    CONSTRAINT mi_chronic_candidates_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES mi_batches (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mi_chronic_candidates_new ON mi_chronic_candidates (clinic_id, status, created_at);

-- ── Same-clinic guard for references into pre-existing tables (shared by every later lane) ──
-- New lanes put no FOREIGN KEY on a table owned by an earlier migration (103 onwards): it would make that
-- migration's rollback fail (DROP TABLE … depends on it). The reference column is checked by this guard instead — a
-- trigger on the NEW table only (no trigger on an existing table): the row must exist in the same clinic.
CREATE OR REPLACE FUNCTION kriya_guard_refs() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE i INT := 0; v_col TEXT; v_tab TEXT; v_id TEXT; ok BOOLEAN;
BEGIN
    WHILE i < TG_NARGS LOOP
        v_col := TG_ARGV[i]; v_tab := TG_ARGV[i + 1]; i := i + 2;
        v_id := to_jsonb(NEW) ->> v_col;
        IF v_id IS NULL THEN CONTINUE; END IF;
        IF TG_OP = 'UPDATE' AND (to_jsonb(OLD) ->> v_col) IS NOT DISTINCT FROM v_id THEN CONTINUE; END IF;
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE id = $1::uuid AND clinic_id::text = $2)', v_tab)
            INTO ok USING v_id, NEW.clinic_id::text;
        IF NOT ok THEN
            RAISE EXCEPTION 'kriya_cross_tenant_ref:%.%', TG_TABLE_NAME, v_col USING ERRCODE = '23503';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_mi_opening_balances_refs ON mi_opening_balances;
CREATE TRIGGER trg_mi_opening_balances_refs BEFORE INSERT OR UPDATE ON mi_opening_balances FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('collected_invoice_id', 'opd_invoices');

DROP TRIGGER IF EXISTS trg_mi_patient_links_refs ON mi_patient_links;
CREATE TRIGGER trg_mi_patient_links_refs BEFORE INSERT OR UPDATE ON mi_patient_links FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_mi_match_queue_refs ON mi_match_queue;
CREATE TRIGGER trg_mi_match_queue_refs BEFORE INSERT OR UPDATE ON mi_match_queue FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients');

-- ── Phone match queue refresh: a live patient whose last 10 digits equal an unlinked legacy record's ───────
CREATE OR REPLACE FUNCTION mi_refresh_matches(p_clinic_id UUID) RETURNS INTEGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_n INTEGER;
BEGIN
    IF p_clinic_id IS NULL THEN RAISE EXCEPTION 'mi_bad_args' USING ERRCODE = 'P0001'; END IF;
    INSERT INTO mi_match_queue (clinic_id, patient_record_id, patient_id)
    SELECT p_clinic_id, r.id, p.id
      FROM patient_records r
      JOIN patients p ON p.clinic_id = r.clinic_id
                     AND right(regexp_replace(p.phone, '\D', '', 'g'), 10) = right(regexp_replace(r.phone, '\D', '', 'g'), 10)
     WHERE r.clinic_id = p_clinic_id AND r.phone IS NOT NULL AND p.phone IS NOT NULL
       AND length(regexp_replace(r.phone, '\D', '', 'g')) >= 10
       AND NOT EXISTS (SELECT 1 FROM mi_patient_links l WHERE l.clinic_id = r.clinic_id AND l.patient_record_id = r.id)
    ON CONFLICT (clinic_id, patient_record_id, patient_id) DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END $$;

-- ── Undo one applied batch, atomically: delete what it created, restore what it changed, drop its legacy rows ──
-- Refused (nothing changed) once anything references what the batch wrote: an FK violation from a booked encounter,
-- an invoice line on an imported service, a collected opening balance. Restores only the columns it recorded.
CREATE OR REPLACE FUNCTION mi_undo(p_clinic_id UUID, p_batch_id UUID) RETURNS JSONB
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE b mi_batches; it RECORD; v_sets TEXT[]; k TEXT; v_deleted INT := 0; v_restored INT := 0; v_n INT;
BEGIN
    SELECT * INTO b FROM mi_batches WHERE clinic_id = p_clinic_id AND id = p_batch_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'mi_not_found' USING ERRCODE = 'P0002'; END IF;
    IF b.status <> 'applied' THEN RAISE EXCEPTION 'mi_not_applied' USING ERRCODE = 'P0001'; END IF;
    IF EXISTS (SELECT 1 FROM mi_opening_balances WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id AND status <> 'open') THEN
        RAISE EXCEPTION 'mi_batch_referenced' USING ERRCODE = 'P0001';
    END IF;
    BEGIN
        FOR it IN SELECT * FROM mi_batch_items WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id ORDER BY id DESC LOOP
            -- a legacy patient list whose records staff already linked to live patients is in use
            IF it.target_table = 'patient_import_batches' AND EXISTS (
                SELECT 1 FROM mi_patient_links l JOIN patient_records r ON r.id = l.patient_record_id
                 WHERE r.clinic_id = p_clinic_id AND r.import_batch_id = it.target_id) THEN
                RAISE EXCEPTION 'mi_batch_referenced' USING ERRCODE = 'P0001';
            END IF;
            IF it.action = 'created' THEN
                EXECUTE format('DELETE FROM %I WHERE clinic_id = $1 AND id = $2', it.target_table) USING p_clinic_id, it.target_id;
                GET DIAGNOSTICS v_n = ROW_COUNT; v_deleted := v_deleted + v_n;
            ELSE
                v_sets := '{}';
                FOR k IN SELECT jsonb_object_keys(it.previous) LOOP
                    v_sets := v_sets || format('%I = (jsonb_populate_record(NULL::%I, $1)).%I', k, it.target_table, k);
                END LOOP;
                EXECUTE format('UPDATE %I SET %s WHERE clinic_id = $2 AND id = $3', it.target_table, array_to_string(v_sets, ', '))
                    USING it.previous, p_clinic_id, it.target_id;
                GET DIAGNOSTICS v_n = ROW_COUNT; v_restored := v_restored + v_n;
            END IF;
        END LOOP;
    EXCEPTION WHEN foreign_key_violation OR raise_exception THEN
        RAISE EXCEPTION 'mi_batch_referenced' USING ERRCODE = 'P0001';
    END;
    DELETE FROM mi_legacy_encounters WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id;
    DELETE FROM mi_legacy_lab_values WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id;
    DELETE FROM mi_opening_balances WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id;
    DELETE FROM mi_chronic_candidates WHERE clinic_id = p_clinic_id AND batch_id = p_batch_id;
    UPDATE mi_batches SET status = 'undone', undone_at = now() WHERE clinic_id = p_clinic_id AND id = p_batch_id;
    RETURN jsonb_build_object('deleted', v_deleted, 'restored', v_restored);
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['mi_batches', 'mi_row_errors', 'mi_batch_items', 'mi_patient_links', 'mi_match_queue',
                             'mi_legacy_encounters', 'mi_legacy_lab_values', 'mi_opening_balances', 'mi_diagnosis_map',
                             'mi_chronic_candidates']
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
    FOREACH t IN ARRAY ARRAY['mi_refresh_matches(uuid)', 'mi_undo(uuid, uuid)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', t);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', t);
        END IF;
    END LOOP;
END $$;

-- ── Verify ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity
           AND c.relname IN ('mi_batches', 'mi_row_errors', 'mi_batch_items', 'mi_patient_links', 'mi_match_queue',
                             'mi_legacy_encounters', 'mi_legacy_lab_values', 'mi_opening_balances', 'mi_diagnosis_map',
                             'mi_chronic_candidates')) <> 10 THEN
        RAISE EXCEPTION '144 verify: mi tables missing or RLS not forced';
    END IF;
    IF to_regclass('public.uq_mi_batches_applied') IS NULL THEN
        RAISE EXCEPTION '144 verify: uq_mi_batches_applied missing';
    END IF;
    IF to_regprocedure('mi_refresh_matches(uuid)') IS NULL OR to_regprocedure('mi_undo(uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION '144 verify: mi_refresh_matches missing';
    END IF;
END $$;
