-- ============================================================================
-- Migration 153: Specialty OS — Eye and Dental depth (lane SP; docs/specialty-os/EYE.md, DENTAL.md)
--   Eye exam / biometry / dental chart / perio chart are S2 forms (opd_encounter_forms, migration 151: no table).
--   sp_optical_jobs (S7, EYE-3): spectacle job order Advance paid → At lab → Ready → Delivered (or remake); the Rx is a
--      snapshot copied from the frozen eye_exam_v1 form (never retyped); "Ready" messages the patient only after staff
--      confirm; balance = total − advance (generated).
--   sp_dental_lab_orders (S8, DEN-6): crown / bridge / denture / aligner work at an external lab, FDI teeth list.
--   sp_compliance_items (DEN-10, also eye / IVF / women & child registrations): licence numbers + renewal dates.
-- Existing objects touched: none. Old-table references use kriya_guard_refs (144). Rollback: rollback/153_*.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sp_optical_jobs (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id             UUID NOT NULL,
    form_id                UUID NOT NULL,                     -- the eye_exam_v1 form the Rx was copied from
    rx_snapshot            JSONB NOT NULL CHECK (jsonb_typeof(rx_snapshot) = 'object' AND rx_snapshot ? 'od' AND rx_snapshot ? 'os'),
    frame_desc             TEXT NULL CHECK (char_length(frame_desc) <= 200),
    lens_desc              TEXT NOT NULL CHECK (char_length(btrim(lens_desc)) BETWEEN 2 AND 200),
    lab_name               TEXT NULL CHECK (char_length(lab_name) <= 120),
    due_on                 DATE NULL,
    total_paise            INTEGER NOT NULL CHECK (total_paise >= 0),
    advance_paise          INTEGER NOT NULL DEFAULT 0 CHECK (advance_paise >= 0),
    balance_paise          INTEGER GENERATED ALWAYS AS (total_paise - advance_paise) STORED,
    status                 TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'advance_paid', 'sent_to_lab', 'received',
                                                                          'qc_passed', 'ready', 'delivered', 'remake',
                                                                          'cancelled')),
    remake_reason          TEXT NULL CHECK (char_length(btrim(remake_reason)) BETWEEN 3 AND 300),
    ready_confirmed_by_name TEXT NULL CHECK (char_length(ready_confirmed_by_name) <= 100),
    ready_notified_at      TIMESTAMPTZ NULL,
    delivered_at           TIMESTAMPTZ NULL,
    created_by_name        TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_optical_jobs_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_optical_jobs_form_fk FOREIGN KEY (clinic_id, form_id) REFERENCES opd_encounter_forms (clinic_id, id),
    CONSTRAINT sp_optical_jobs_advance CHECK (advance_paise <= total_paise),
    CONSTRAINT sp_optical_jobs_remake CHECK ((status = 'remake') = (remake_reason IS NOT NULL)),
    CONSTRAINT sp_optical_jobs_notified CHECK (ready_notified_at IS NULL OR ready_confirmed_by_name IS NOT NULL),
    CONSTRAINT sp_optical_jobs_delivered CHECK ((status = 'delivered') = (delivered_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_sp_optical_jobs_board ON sp_optical_jobs (clinic_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS sp_dental_lab_orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id      UUID NOT NULL,
    plan_id         UUID NULL,                                 -- dental_treatment_plans (089)
    lab_name        TEXT NOT NULL CHECK (char_length(btrim(lab_name)) BETWEEN 2 AND 120),
    work_type       TEXT NOT NULL CHECK (work_type IN ('crown', 'bridge', 'denture', 'aligner', 'implant_crown', 'veneer',
                                                       'night_guard', 'other')),
    teeth           TEXT NOT NULL CHECK (teeth ~ '^[1-8][1-8](,[1-8][1-8]){0,31}$'),
    shade           TEXT NULL CHECK (char_length(shade) <= 20),
    due_on          DATE NULL,
    cost_paise      INTEGER NULL CHECK (cost_paise >= 0),
    status          TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'received', 'fitted', 'remake', 'cancelled')),
    remake_reason   TEXT NULL CHECK (char_length(btrim(remake_reason)) BETWEEN 3 AND 300),
    received_at     TIMESTAMPTZ NULL,
    fitted_at       TIMESTAMPTZ NULL,
    created_by_name TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_dental_lab_orders_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT sp_dental_lab_orders_remake CHECK ((status = 'remake') = (remake_reason IS NOT NULL)),
    CONSTRAINT sp_dental_lab_orders_fitted CHECK ((status = 'fitted') = (fitted_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_sp_dental_lab_orders_open ON sp_dental_lab_orders (clinic_id, status, due_on);

CREATE TABLE IF NOT EXISTS sp_compliance_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('aerb_registration', 'bmw_authorisation', 'fire_noc', 'pcpndt_registration',
                                                  'art_registration', 'drug_licence', 'other')),
    label           TEXT NOT NULL CHECK (char_length(btrim(label)) BETWEEN 3 AND 120),
    reference_no    TEXT NULL CHECK (char_length(reference_no) <= 80),
    valid_until     DATE NOT NULL,
    remind_days     SMALLINT NOT NULL DEFAULT 60 CHECK (remind_days BETWEEN 7 AND 180),
    created_by_name TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 2 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sp_compliance_items_clinic_id_id_key UNIQUE (clinic_id, id)
);

DROP TRIGGER IF EXISTS trg_sp_optical_jobs_refs ON sp_optical_jobs;
CREATE TRIGGER trg_sp_optical_jobs_refs BEFORE INSERT OR UPDATE ON sp_optical_jobs FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients');
DROP TRIGGER IF EXISTS trg_sp_dental_lab_orders_refs ON sp_dental_lab_orders;
CREATE TRIGGER trg_sp_dental_lab_orders_refs BEFORE INSERT OR UPDATE ON sp_dental_lab_orders FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'plan_id', 'dental_treatment_plans');

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['sp_optical_jobs', 'sp_dental_lab_orders', 'sp_compliance_items']
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

-- ── 153 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.sp_optical_jobs') IS NULL OR to_regclass('public.sp_dental_lab_orders') IS NULL
       OR to_regclass('public.sp_compliance_items') IS NULL THEN
        RAISE EXCEPTION '153 verify: eye / dental tables missing';
    END IF;
END $$;
