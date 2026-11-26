-- ============================================================================
-- Migration 148: Continuous Care act (CC-M4 / CC-39; DATA-MODEL.md CC-M4)
-- Radar read model (rebuilt; no trigger on any existing table, ADR-CC-13), cases with one open case per
-- (enrollment, reason) (ADR-CC-14), the append-only case timeline, and actions — a doctor's or coordinator's
-- decision that books / orders through existing services and never carries a drug or dose (ADR-CC-04).
-- Existing objects touched: none. Rollback: rollback/148_rollback_cc_act.sql.
-- ============================================================================

CREATE TABLE IF NOT EXISTS cc_radar_rows (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id       UUID NOT NULL,
    family_member_id UUID NULL,
    enrollment_id    UUID NULL,
    reason           TEXT NOT NULL CHECK (reason IN ('followup_overdue', 'visit_missed', 'lab_overdue', 'refill_lapsed',
                                                     'off_target', 'not_responding', 'candidate', 'discharged_followup')),
    detail           JSONB NOT NULL DEFAULT '{}'::jsonb,
    score            INTEGER NOT NULL DEFAULT 0 CHECK (score BETWEEN 0 AND 1000),
    source_key       TEXT NOT NULL CHECK (char_length(source_key) BETWEEN 3 AND 200),   -- "<table>:<id>"
    first_seen_on    DATE NOT NULL,
    last_seen_on     DATE NOT NULL,
    closed_on        DATE NULL,
    close_reason     TEXT NULL CHECK (close_reason IN ('resolved', 'enrolled', 'declined', 'dismissed', 'superseded')),
    dismiss_reason   TEXT NULL CHECK (char_length(btrim(dismiss_reason)) BETWEEN 5 AND 300),
    dismissed_by_name TEXT NULL CHECK (char_length(dismissed_by_name) <= 100),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_radar_rows_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_radar_rows_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_radar_rows_closed CHECK ((closed_on IS NULL) = (close_reason IS NULL)),
    CONSTRAINT cc_radar_rows_dismissed CHECK (close_reason <> 'dismissed' OR dismiss_reason IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_radar_open ON cc_radar_rows (clinic_id, patient_id,
    COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid), reason, source_key) WHERE closed_on IS NULL;
CREATE INDEX IF NOT EXISTS idx_cc_radar_score ON cc_radar_rows (clinic_id, score DESC) WHERE closed_on IS NULL;
CREATE INDEX IF NOT EXISTS idx_cc_radar_dismissed ON cc_radar_rows (clinic_id, patient_id, reason, closed_on)
    WHERE close_reason IN ('dismissed', 'declined');

CREATE TABLE IF NOT EXISTS cc_cases (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id     UUID NOT NULL,
    family_member_id UUID NULL,
    enrollment_id  UUID NULL,
    branch_id      UUID NULL,
    reason         TEXT NOT NULL CHECK (char_length(reason) BETWEEN 3 AND 60),
    tier           TEXT NOT NULL CHECK (tier IN ('urgent', 'watch', 'routine')),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'awaiting_doctor', 'actioned',
                                                                  'closed', 'cancelled')),
    assigned_to    UUID NULL,
    assigned_to_name TEXT NULL CHECK (char_length(assigned_to_name) <= 100),
    due_by         TIMESTAMPTZ NOT NULL,
    escalated_at   TIMESTAMPTZ NULL,
    evaluation_id  UUID NULL,
    radar_row_id   UUID NULL,
    summary        JSONB NOT NULL DEFAULT '{}'::jsonb,           -- the "why": reason codes with values and dates
    closed_reason  TEXT NULL CHECK (closed_reason IN ('verified', 'patient_declined', 'unreachable', 'transferred',
                                                      'duplicate', 'clinical_decision', 'enrollment_ended')),
    closed_at      TIMESTAMPTZ NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_cases_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_cases_enrollment_fk FOREIGN KEY (clinic_id, enrollment_id) REFERENCES cc_enrollments (clinic_id, id),
    CONSTRAINT cc_cases_evaluation_fk FOREIGN KEY (clinic_id, evaluation_id) REFERENCES cc_evaluations (clinic_id, id),
    CONSTRAINT cc_cases_radar_fk FOREIGN KEY (clinic_id, radar_row_id) REFERENCES cc_radar_rows (clinic_id, id),
    CONSTRAINT cc_cases_closed CHECK ((status IN ('closed', 'cancelled')) = (closed_at IS NOT NULL)
                                     AND (status <> 'closed' OR closed_reason IS NOT NULL))
);
-- ADR-CC-14: one open case per (person, enrollment, reason)
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_case_open ON cc_cases (clinic_id, patient_id,
    COALESCE(enrollment_id, '00000000-0000-0000-0000-000000000000'::uuid), reason)
    WHERE status IN ('open', 'acknowledged', 'awaiting_doctor', 'actioned');
CREATE INDEX IF NOT EXISTS idx_cc_cases_sla ON cc_cases (clinic_id, status, due_by)
    WHERE status IN ('open', 'acknowledged', 'awaiting_doctor');

CREATE TABLE IF NOT EXISTS cc_case_events (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    case_id    UUID NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('opened', 'note', 'call_out', 'call_in', 'message', 'assigned', 'escalated',
                                             'doctor_decision', 'status_change', 'reason_seen_again')),
    body       TEXT NULL CHECK (char_length(body) <= 1000),           -- staff text, never sent to a patient
    actor_id   UUID NULL,
    actor_name TEXT NULL CHECK (char_length(actor_name) <= 100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_case_events_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_case_events_case_fk FOREIGN KEY (clinic_id, case_id) REFERENCES cc_cases (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_cc_case_events_case ON cc_case_events (clinic_id, case_id, created_at);
CREATE OR REPLACE FUNCTION cc_guard_case_event() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF current_setting('kriya.cc_purge', true) = 'on' THEN RETURN COALESCE(NEW, OLD); END IF;
    RAISE EXCEPTION 'cc_case_event_append_only' USING ERRCODE = 'P0001';
END $$;
DROP TRIGGER IF EXISTS trg_cc_case_events_guard ON cc_case_events;
CREATE TRIGGER trg_cc_case_events_guard BEFORE UPDATE OR DELETE ON cc_case_events
    FOR EACH ROW EXECUTE FUNCTION cc_guard_case_event();

CREATE TABLE IF NOT EXISTS cc_actions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    case_id         UUID NOT NULL,
    action_code     TEXT NOT NULL CHECK (action_code IN ('book_review_visit', 'order_lab', 'book_home_sample',
                                                         'recheck_in_days', 'continue_plan', 'call_patient_now', 'refer',
                                                         'enroll', 'pause', 'end')),
    params          JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(params) = 'object'),
    decided_by      UUID NULL,
    decided_by_name TEXT NOT NULL CHECK (char_length(decided_by_name) BETWEEN 1 AND 100),
    decider_role    TEXT NOT NULL CHECK (decider_role IN ('doctor', 'coordinator', 'admin')),
    idempotency_key TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    appointment_id  UUID NULL,
    order_id        UUID NULL,
    voice_job_id    UUID NULL,
    due_on          DATE NULL,
    status          TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'booked', 'verified', 'not_verified',
                                                                      'cancelled')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT cc_actions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT cc_actions_case_fk FOREIGN KEY (clinic_id, case_id) REFERENCES cc_cases (clinic_id, id),
    -- ADR-CC-04: no action ever carries a medicine or a dose
    CONSTRAINT cc_actions_no_drug CHECK (NOT (params ?| ARRAY['drug', 'drug_name', 'dose', 'dosage', 'medicine',
                                                                'frequency', 'strength', 'prescription'])),
    CONSTRAINT cc_actions_booked CHECK (status <> 'booked' OR appointment_id IS NOT NULL OR order_id IS NOT NULL
                                        OR voice_job_id IS NOT NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cc_actions_idem ON cc_actions (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cc_actions_verify ON cc_actions (clinic_id, status, due_on) WHERE status = 'booked';

DROP TRIGGER IF EXISTS trg_cc_radar_rows_refs ON cc_radar_rows;
CREATE TRIGGER trg_cc_radar_rows_refs BEFORE INSERT OR UPDATE ON cc_radar_rows FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'family_member_id', 'family_members');
DROP TRIGGER IF EXISTS trg_cc_cases_refs ON cc_cases;
CREATE TRIGGER trg_cc_cases_refs BEFORE INSERT OR UPDATE ON cc_cases FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('patient_id', 'patients', 'branch_id', 'branches');
DROP TRIGGER IF EXISTS trg_cc_actions_refs ON cc_actions;
CREATE TRIGGER trg_cc_actions_refs BEFORE INSERT OR UPDATE ON cc_actions FOR EACH ROW
    EXECUTE FUNCTION kriya_guard_refs('appointment_id', 'appointments', 'order_id', 'opd_orders',
                                      'voice_job_id', 'voice_outbound_jobs');

-- ── RLS parity + API-role lockout ──────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['cc_radar_rows', 'cc_cases', 'cc_case_events', 'cc_actions']
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
    IF to_regclass('public.cc_radar_rows') IS NULL OR to_regclass('public.cc_cases') IS NULL
       OR to_regclass('public.cc_case_events') IS NULL OR to_regclass('public.cc_actions') IS NULL THEN
        RAISE EXCEPTION '148 verify: cc act tables missing';
    END IF;
    IF to_regclass('public.uq_cc_case_open') IS NULL THEN
        RAISE EXCEPTION '148 verify: one-open-case index missing';
    END IF;
END $$;
