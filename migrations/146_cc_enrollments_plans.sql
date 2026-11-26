-- ============================================================================
-- Migration 146: Continuous Care enrollments and care plans (CC-M2 / CC-21; DATA-MODEL.md CC-M2)
-- Enrollments (no 'active' without a live consent — CHECK), versioned care plans that are verbatim projections of a
-- signed prescription (ADR-CC-05), their items, and structured IPD discharge follow-ups (latent L5).
-- Existing objects touched: none. Rollback: rollback/146_rollback_cc_enrollments_plans.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS cc_enrollments (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id           UUID NOT NULL,
    family_member_id     UUID NULL,
    activation_id        UUID NOT NULL,
    protocol_code        TEXT NOT NULL CHECK (protocol_code ~ '^[a-z0-9_]{2,40}$'),
    consent_id           UUID NULL,
    primary_doctor_id    UUID NOT NULL,
    coordinator_admin_id UUID NULL,
    branch_id            UUID NULL,
    status               TEXT NOT NULL DEFAULT 'invited' CHECK (status IN ('awaiting_doctor', 'invited', 'active', 'paused',
                                                                        'graduated', 'withdrawn', 'lost', 'ended_by_clinic')),
    language             TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'hi', 'te')),
    channel_pref         TEXT NOT NULL DEFAULT 'whatsapp' CHECK (channel_pref IN ('whatsapp', 'voice', 'both')),
    source               TEXT NOT NULL CHECK (source IN ('doctor_enrolled', 'radar_candidate', 'import', 'discharge')),
    enrolled_by          UUID NULL,
    enrolled_by_name     TEXT NULL CHECK (char_length(enrolled_by_name) <= 100),
    doctor_approved_at   TIMESTAMPTZ NULL,
    enrolled_at          TIMESTAMPTZ NULL,
    ended_at             TIMESTAMPTZ NULL,
    end_reason           TEXT NULL CHECK (char_length(btrim(end_reason)) BETWEEN 3 AND 300),
    last_rx_seen_at      TIMESTAMPTZ NULL,                    -- plan auto-rebuild watermark (W3, polling, no trigger)
    last_evaluation_id   UUID NULL,
    unanswered_streak    SMALLINT NOT NULL DEFAULT 0 CHECK (unanswered_streak BETWEEN 0 AND 100),
    last_voice_attempt_at TIMESTAMPTZ NULL,
    baseline             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_enrollments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_enrollments_activation_fk FOREIGN KEY (clinic_id, activation_id)
        REFERENCES cc_protocol_activations (clinic_id, id),
    CONSTRAINT cc_enrollments_consent_fk FOREIGN KEY (clinic_id, consent_id) REFERENCES cc_consents (clinic_id, id),
    -- ADR-CC-11: never active without a live consent; a doctor must have approved every enrollment that started
    CONSTRAINT cc_enrollments_active_consent CHECK (status NOT IN ('active', 'paused') OR (consent_id IS NOT NULL
                                                    AND enrolled_at IS NOT NULL AND doctor_approved_at IS NOT NULL)),
    CONSTRAINT cc_enrollments_ended CHECK ((status IN ('graduated', 'withdrawn', 'lost', 'ended_by_clinic'))
                                           = (ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_enrollment_open ON cc_enrollments
    (clinic_id, patient_id, COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid), protocol_code)
    WHERE status IN ('awaiting_doctor', 'invited', 'active', 'paused');
CREATE INDEX IF NOT EXISTS idx_cc_enrollments_list ON cc_enrollments (clinic_id, status, protocol_code);
CREATE INDEX IF NOT EXISTS idx_cc_enrollments_patient ON cc_enrollments (clinic_id, patient_id);

CREATE TABLE IF NOT EXISTS cc_care_plans (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    enrollment_id          UUID NOT NULL,
    version                SMALLINT NOT NULL CHECK (version BETWEEN 1 AND 999),
    source_prescription_id UUID NULL,
    status                 TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'ended')),
    valid_from             DATE NOT NULL,
    valid_to               DATE NULL,
    built_by               TEXT NOT NULL CHECK (char_length(built_by) BETWEEN 2 AND 100),
    superseded_at          TIMESTAMPTZ NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_care_plans_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_care_plans_version_key UNIQUE (clinic_id, enrollment_id, version),
    CONSTRAINT cc_care_plans_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_care_plans_superseded CHECK ((status = 'superseded') = (superseded_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_care_plan_active ON cc_care_plans (clinic_id, enrollment_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS cc_plan_items (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                   UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    plan_id                     UUID NOT NULL,
    kind                        TEXT NOT NULL CHECK (kind IN ('medication', 'measure', 'lab', 'visit', 'education',
                                                              'procedure_session', 'vaccine', 'anc_visit')),
    source_prescription_item_id UUID NULL,
    display_text                TEXT NOT NULL CHECK (char_length(btrim(display_text)) BETWEEN 1 AND 300),
    prompt_mode                 TEXT NOT NULL CHECK (prompt_mode IN ('adherence', 'display_only', 'reading', 'due_date')),
    measure_code                TEXT NULL CHECK (measure_code ~ '^[a-z0-9_]{2,20}$'),
    schedule                    JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(schedule) = 'object'),
    starts_on                   DATE NOT NULL,
    ends_on                     DATE NULL,
    lab_test_id                 UUID NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_plan_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_plan_items_plan_fk FOREIGN KEY (clinic_id, plan_id) REFERENCES cc_care_plans (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT cc_plan_items_dates CHECK (ends_on IS NULL OR ends_on >= starts_on),
    -- a medicine line is always a copy of a signed prescription line (ADR-CC-05): CC can never invent one
    CONSTRAINT cc_plan_items_med_source CHECK (kind <> 'medication' OR source_prescription_item_id IS NOT NULL),
    CONSTRAINT cc_plan_items_reading CHECK (prompt_mode <> 'reading' OR measure_code IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_cc_plan_items_plan ON cc_plan_items (clinic_id, plan_id);

CREATE TABLE IF NOT EXISTS cc_discharge_followups (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id UUID NOT NULL,
    patient_id   UUID NOT NULL,
    follow_up_on DATE NOT NULL,
    doctor_id    UUID NOT NULL,
    note         TEXT NULL CHECK (char_length(note) <= 300),
    created_by_name TEXT NULL CHECK (char_length(created_by_name) <= 100),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_discharge_followups_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_discharge_followups_admission_key UNIQUE (clinic_id, admission_id)
);
CREATE INDEX IF NOT EXISTS idx_cc_discharge_due ON cc_discharge_followups (clinic_id, follow_up_on);

-- references into pre-existing tables: same-clinic guard (kriya_guard_refs, migration 144), never an FK
DROP TRIGGER IF EXISTS trg_cc_enrollments_refs ON cc_enrollments;
CREATE TRIGGER trg_cc_enrollments_refs BEFORE INSERT OR UPDATE ON cc_enrollments FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members',
                                      'primary_doctor_id', 'doctors', 'branch_id', 'branches');
DROP TRIGGER IF EXISTS trg_cc_care_plans_refs ON cc_care_plans;
CREATE TRIGGER trg_cc_care_plans_refs BEFORE INSERT OR UPDATE ON cc_care_plans FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('source_prescription_id', 'opd_prescriptions');
DROP TRIGGER IF EXISTS trg_cc_plan_items_refs ON cc_plan_items;
CREATE TRIGGER trg_cc_plan_items_refs BEFORE INSERT OR UPDATE ON cc_plan_items FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('source_prescription_item_id', 'opd_prescription_items', 'lab_test_id', 'lab_tests');
DROP TRIGGER IF EXISTS trg_cc_discharge_followups_refs ON cc_discharge_followups;
CREATE TRIGGER trg_cc_discharge_followups_refs BEFORE INSERT OR UPDATE ON cc_discharge_followups FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('admission_id', 'ipd_admissions', 'patient_id', 'patients', 'doctor_id', 'doctors');

-- ── Atomic plan install (W3): insert plan vN+1 + items, supersede vN, skip vN's future schedule items ──
CREATE OR REPLACE FUNCTION cc_install_plan(p_clinic_id UUID, p_enrollment_id UUID, p_source_rx UUID, p_items JSONB,
                                           p_built_by TEXT, p_today DATE) RETURNS JSONB
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE e cc_enrollments; v_old UUID; v_ver SMALLINT; v_plan UUID; it JSONB; n INT := 0;
BEGIN
    SELECT * INTO e FROM cc_enrollments WHERE clinic_id = p_clinic_id AND id = p_enrollment_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'cc_not_found' USING ERRCODE = 'P0002'; END IF;
    IF e.status NOT IN ('active', 'paused') THEN RAISE EXCEPTION 'cc_enrollment_not_active' USING ERRCODE = 'P0001'; END IF;
    SELECT id INTO v_old FROM cc_care_plans WHERE clinic_id = p_clinic_id AND enrollment_id = p_enrollment_id
       AND status = 'active' FOR UPDATE;
    SELECT COALESCE(max(version), 0) + 1 INTO v_ver FROM cc_care_plans
     WHERE clinic_id = p_clinic_id AND enrollment_id = p_enrollment_id;
    IF v_old IS NOT NULL THEN
        UPDATE cc_care_plans SET status = 'superseded', superseded_at = now(), valid_to = p_today
         WHERE clinic_id = p_clinic_id AND id = v_old;
        IF to_regclass('public.cc_schedule_items') IS NOT NULL THEN
            EXECUTE 'UPDATE cc_schedule_items SET status = ''skipped'' WHERE clinic_id = $1 AND status = ''due''
                       AND due_at > now() AND plan_item_id IN (SELECT id FROM cc_plan_items WHERE clinic_id = $1 AND plan_id = $2)'
                USING p_clinic_id, v_old;
        END IF;
    END IF;
    INSERT INTO cc_care_plans (clinic_id, enrollment_id, version, source_prescription_id, valid_from, built_by)
    VALUES (p_clinic_id, p_enrollment_id, v_ver, p_source_rx, p_today, p_built_by) RETURNING id INTO v_plan;
    FOR it IN SELECT * FROM jsonb_array_elements(COALESCE(p_items, '[]'::jsonb)) LOOP
        INSERT INTO cc_plan_items (clinic_id, plan_id, kind, source_prescription_item_id, display_text, prompt_mode,
                                   measure_code, schedule, starts_on, ends_on, lab_test_id)
        VALUES (p_clinic_id, v_plan, it ->> 'kind', NULLIF(it ->> 'source_prescription_item_id', '')::uuid,
                it ->> 'display_text', it ->> 'prompt_mode', NULLIF(it ->> 'measure_code', ''),
                COALESCE(it -> 'schedule', '{}'::jsonb), COALESCE(NULLIF(it ->> 'starts_on', '')::date, p_today),
                NULLIF(it ->> 'ends_on', '')::date, NULLIF(it ->> 'lab_test_id', '')::uuid);
        n := n + 1;
    END LOOP;
    UPDATE cc_enrollments SET last_rx_seen_at = COALESCE((SELECT signed_at FROM opd_prescriptions
                                                          WHERE clinic_id = p_clinic_id AND id = p_source_rx), last_rx_seen_at),
                              updated_at = now()
     WHERE clinic_id = p_clinic_id AND id = p_enrollment_id;
    RETURN jsonb_build_object('plan_id', v_plan, 'version', v_ver, 'items', n, 'superseded', v_old);
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cc_enrollments', 'cc_care_plans', 'cc_plan_items', 'cc_discharge_followups']
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
BEGIN
    IF to_regclass('public.cc_enrollments') IS NULL OR to_regclass('public.cc_care_plans') IS NULL
       OR to_regclass('public.cc_plan_items') IS NULL OR to_regclass('public.cc_discharge_followups') IS NULL THEN
        RAISE EXCEPTION '146 verify: cc enrollment / plan tables missing';
    END IF;
    IF to_regprocedure('cc_install_plan(uuid,uuid,uuid,jsonb,text,date)') IS NULL THEN
        RAISE EXCEPTION '146 verify: cc_install_plan missing';
    END IF;
    IF to_regclass('public.uq_cc_enrollment_open') IS NULL OR to_regclass('public.uq_cc_care_plan_active') IS NULL THEN
        RAISE EXCEPTION '146 verify: cc unique indexes missing';
    END IF;
END $$;
