-- ============================================================================
-- Migration 149: Continuous Care verification and outcomes (CC-M5 / CC-46; DATA-MODEL.md CC-M5)
-- Verifications ("counts only when recorded" — check-in, signed encounter, validated result, completed sale; never
-- appointments.status='completed', ADR-CC-08), monthly outcome snapshots, and cc_purge_patient for DPDP erasure.
-- Existing objects touched: none. Rollback: rollback/149_rollback_cc_verify_outcomes.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS cc_verifications (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    action_id     UUID NOT NULL,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('check_in', 'signed_encounter', 'validated_result',
                                                         'completed_sale')),
    evidence_ref  JSONB NOT NULL CHECK (jsonb_typeof(evidence_ref) = 'object' AND evidence_ref ? 'table' AND evidence_ref ? 'id'),
    verified_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revenue_paise BIGINT NULL CHECK (revenue_paise >= 0),
    CONSTRAINT cc_verifications_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_verifications_action_key UNIQUE (clinic_id, action_id),
    CONSTRAINT cc_verifications_action_fk FOREIGN KEY (clinic_id, action_id) REFERENCES cc_actions (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS cc_outcome_snapshots (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    protocol_code     TEXT NOT NULL CHECK (protocol_code ~ '^[a-z0-9_]{2,40}$'),
    period_month      DATE NOT NULL CHECK (extract(day FROM period_month) = 1),
    enrolled          INTEGER NOT NULL DEFAULT 0 CHECK (enrolled >= 0),
    active            INTEGER NOT NULL DEFAULT 0 CHECK (active >= 0),
    returned_90d      INTEGER NOT NULL DEFAULT 0 CHECK (returned_90d >= 0),
    at_target         INTEGER NOT NULL DEFAULT 0 CHECK (at_target >= 0),
    labs_on_time      INTEGER NOT NULL DEFAULT 0 CHECK (labs_on_time >= 0),
    refills_on_time   INTEGER NOT NULL DEFAULT 0 CHECK (refills_on_time >= 0),
    visits_verified   INTEGER NOT NULL DEFAULT 0 CHECK (visits_verified >= 0),
    labs_verified     INTEGER NOT NULL DEFAULT 0 CHECK (labs_verified >= 0),
    refills_verified  INTEGER NOT NULL DEFAULT 0 CHECK (refills_verified >= 0),
    revenue_paise     BIGINT NOT NULL DEFAULT 0 CHECK (revenue_paise >= 0),
    report            JSONB NOT NULL DEFAULT '{}'::jsonb,
    baseline          JSONB NOT NULL DEFAULT '{}'::jsonb,
    method_version    TEXT NOT NULL DEFAULT 'outcomes-1',
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_outcome_snapshots_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_outcome_snapshots_period_key UNIQUE (clinic_id, protocol_code, period_month)
);

-- DPDP erasure (VERIFICATION D6): stop every prompt, close open work, remove the person's CC rows; keep the consent
-- rows (proof, withdrawal recorded) and de-identified outcome counts. Called by data_retention before it redacts.
CREATE OR REPLACE FUNCTION cc_purge_patient(p_clinic_id UUID, p_patient_id UUID, p_family_member_id UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_enr UUID[]; v_cases UUID[]; v JSONB := '{}'::jsonb; n INT;
BEGIN
    IF p_clinic_id IS NULL OR p_patient_id IS NULL THEN RAISE EXCEPTION 'cc_bad_args' USING ERRCODE = 'P0001'; END IF;
    SELECT COALESCE(array_agg(id), '{}') INTO v_enr FROM cc_enrollments
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id
       AND (p_family_member_id IS NULL OR family_member_id = p_family_member_id);
    SELECT COALESCE(array_agg(id), '{}') INTO v_cases FROM cc_cases
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id
       AND (p_family_member_id IS NULL OR family_member_id IS NOT DISTINCT FROM p_family_member_id OR enrollment_id = ANY (v_enr));
    PERFORM set_config('kriya.cc_purge', 'on', true);
    DELETE FROM cc_verifications WHERE clinic_id = p_clinic_id AND action_id IN (SELECT id FROM cc_actions
                                                                               WHERE clinic_id = p_clinic_id AND case_id = ANY (v_cases));
    DELETE FROM cc_actions WHERE clinic_id = p_clinic_id AND case_id = ANY (v_cases);
    DELETE FROM cc_case_events WHERE clinic_id = p_clinic_id AND case_id = ANY (v_cases);
    DELETE FROM cc_cases WHERE clinic_id = p_clinic_id AND id = ANY (v_cases);
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('cases', n);
    DELETE FROM cc_radar_rows WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id
       AND (p_family_member_id IS NULL OR family_member_id IS NOT DISTINCT FROM p_family_member_id);
    DELETE FROM cc_evaluations WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr);
    DELETE FROM cc_observations WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr);
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('observations', n);
    DELETE FROM cc_prompts WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr);
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('prompts', n);
    DELETE FROM cc_schedule_items WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr);
    DELETE FROM cc_plan_items WHERE clinic_id = p_clinic_id AND plan_id IN (SELECT id FROM cc_care_plans
                                                                            WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr));
    DELETE FROM cc_care_plans WHERE clinic_id = p_clinic_id AND enrollment_id = ANY (v_enr);
    DELETE FROM cc_discharge_followups WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id AND p_family_member_id IS NULL;
    DELETE FROM cc_enrollments WHERE clinic_id = p_clinic_id AND id = ANY (v_enr);
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('enrollments', n);
    UPDATE cc_consents SET withdrawn_at = COALESCE(withdrawn_at, now()), withdrawn_via = COALESCE(withdrawn_via, 'erasure'),
                           updated_at = now()
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id
       AND (p_family_member_id IS NULL OR family_member_id = p_family_member_id);
    PERFORM set_config('kriya.cc_purge', '', true);
    RETURN v;
END $$;

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cc_verifications', 'cc_outcome_snapshots']
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
    FOREACH f IN ARRAY ARRAY['cc_purge_patient(uuid, uuid, uuid)']
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', f);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', f); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', f); END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', f); END IF;
    END LOOP;
END $$;

DO $$
BEGIN
    IF to_regclass('public.cc_verifications') IS NULL OR to_regclass('public.cc_outcome_snapshots') IS NULL THEN
        RAISE EXCEPTION '149 verify: cc verify / outcome tables missing';
    END IF;
    IF to_regprocedure('cc_purge_patient(uuid,uuid,uuid)') IS NULL THEN
        RAISE EXCEPTION '149 verify: cc_purge_patient missing';
    END IF;
END $$;
