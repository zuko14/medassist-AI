-- ============================================================================
-- Migration 122: Operation Theatre OS (G8) - theatres, surgery schedule, WHO
-- Surgical Safety Checklist, anaesthesia record, auto-posted surgery charge
-- ============================================================================
-- ot_theatres             the hospital's theatres / procedure rooms, per branch.
-- ot_surgeries            one booked surgery for an admitted patient, OT-YYYY-NNNNN
--                         (gapless: opd_next_counter row lock, not the spec's MAX+1,
--                         which hands two concurrent bookings the same number).
-- ot_who_checklists       one per surgery: Sign-In -> Time-Out -> Sign-Out, strictly
--                         in that order; a phase completes only with every item true.
-- ot_anaesthesia_records  one per surgery; editable until the anaesthetist signs it.
--
-- No double booking: a theatre (and a patient) never holds two active surgeries
-- (scheduled / in_theatre) whose times overlap. Enforced by a trigger on
-- ot_surgeries that first UPDATEs the theatre row - every booking of one theatre
-- is serialised, and a REPEATABLE READ transaction racing another booking fails
-- with a serialisation error instead of missing it. (An EXCLUDE constraint needs
-- btree_gist, which is not available on every target; the trigger covers every
-- write path, the functions included.)
--
-- Every state change is one function (ot_schedule_surgery, ot_update_checklist,
-- ot_record_anaesthesia, ot_set_status, ot_complete_surgery); guards refuse
-- direct writes. ot_complete_surgery refuses until all three checklist phases
-- are complete, then posts exactly one ipd_charges row (category 'procedure',
-- source 'order', source_ref 'ot_surgery:<id>' under 111's unique index), never
-- onto a stay whose final bill already exists.
--
-- The admission FK carries patient_id (ON UPDATE CASCADE), so a G7 identity
-- merge moves the surgery with the admission; ON DELETE CASCADE lets the owner
-- purge (ipd_purge_clinic) remove a stay's surgeries with it. A stay cannot
-- start its discharge while a surgery is scheduled, in theatre or in recovery.
-- Additive. Rollback: migrations/rollback/122_down.sql.
-- ============================================================================

-- ── 1. Theatres ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ot_theatres (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id        UUID NULL,
    name             TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
    theatre_type     TEXT NOT NULL DEFAULT 'major'
                     CHECK (theatre_type IN ('major', 'minor', 'labor_room', 'cath_lab', 'endoscopy')),
    is_active        BOOLEAN NOT NULL DEFAULT true,
    schedule_version INTEGER NOT NULL DEFAULT 0,   -- bumped by every booking: the booking lock
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ot_theatres_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ot_theatres_name_key UNIQUE NULLS NOT DISTINCT (clinic_id, branch_id, name),
    CONSTRAINT ot_theatres_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
DROP TRIGGER IF EXISTS trg_ot_theatres_touch ON ot_theatres;
CREATE TRIGGER trg_ot_theatres_touch BEFORE UPDATE ON ot_theatres FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- composite FK target: the surgery's admission AND its patient
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_admissions_clinic_id_patient ON ipd_admissions (clinic_id, id, patient_id);

-- ── 2. Surgeries ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ot_surgeries (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id              UUID NULL,
    admission_id           UUID NOT NULL,
    patient_id             UUID NOT NULL,
    theatre_id             UUID NOT NULL,
    surgery_year           SMALLINT NOT NULL,
    surgery_seq            INTEGER NOT NULL CHECK (surgery_seq > 0),
    surgery_number         TEXT GENERATED ALWAYS AS (opd_format_number('OT-', surgery_year, surgery_seq, 5)) STORED,
    procedure_name         TEXT NOT NULL CHECK (char_length(btrim(procedure_name)) BETWEEN 2 AND 200),
    service_id             UUID NULL,
    scheduled_start        TIMESTAMPTZ NOT NULL,
    scheduled_end          TIMESTAMPTZ NOT NULL,
    actual_start           TIMESTAMPTZ NULL,
    actual_end             TIMESTAMPTZ NULL,
    primary_surgeon_id     UUID NOT NULL,
    assistant_surgeon_name TEXT NULL CHECK (char_length(assistant_surgeon_name) <= 120),
    anaesthetist_id        UUID NULL,
    scrub_nurse_name       TEXT NULL CHECK (char_length(scrub_nurse_name) <= 120),
    priority               TEXT NOT NULL DEFAULT 'elective' CHECK (priority IN ('elective', 'urgent', 'emergency')),
    status                 TEXT NOT NULL DEFAULT 'scheduled'
                           CHECK (status IN ('scheduled', 'in_theatre', 'recovery', 'completed', 'cancelled', 'postponed')),
    cancellation_reason    TEXT NULL CHECK (char_length(btrim(cancellation_reason)) BETWEEN 3 AND 300),
    pre_op_diagnosis       TEXT NOT NULL CHECK (char_length(btrim(pre_op_diagnosis)) BETWEEN 2 AND 1000),
    post_op_diagnosis      TEXT NULL CHECK (char_length(post_op_diagnosis) <= 1000),
    surgical_findings      TEXT NULL CHECK (char_length(surgical_findings) <= 4000),
    procedure_notes        TEXT NULL CHECK (char_length(procedure_notes) <= 8000),
    implants_used          TEXT NULL CHECK (char_length(implants_used) <= 2000),
    specimens_to_lab       TEXT NULL CHECK (char_length(specimens_to_lab) <= 1000),
    post_op_instructions   TEXT NULL CHECK (char_length(post_op_instructions) <= 4000),
    charge_id              UUID NULL,          -- the ipd_charges row posted at completion
    completed_by           UUID NULL,
    completed_by_name      TEXT NULL CHECK (char_length(completed_by_name) <= 100),
    idempotency_key        TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_by             UUID NULL,
    created_by_name        TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 1 AND 100),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ot_surgeries_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ot_surgeries_seq_key UNIQUE (clinic_id, surgery_year, surgery_seq),
    CONSTRAINT ot_surgeries_times CHECK (scheduled_end > scheduled_start
                                         AND scheduled_end <= scheduled_start + INTERVAL '24 hours'),
    CONSTRAINT ot_surgeries_actual_times CHECK (actual_end IS NULL OR (actual_start IS NOT NULL AND actual_end >= actual_start)),
    CONSTRAINT ot_surgeries_started CHECK (status NOT IN ('in_theatre', 'recovery', 'completed') OR actual_start IS NOT NULL),
    CONSTRAINT ot_surgeries_completed CHECK (
        (status = 'completed') = (actual_end IS NOT NULL AND procedure_notes IS NOT NULL AND completed_by_name IS NOT NULL)),
    CONSTRAINT ot_surgeries_reason CHECK ((status IN ('cancelled', 'postponed')) = (cancellation_reason IS NOT NULL)),
    CONSTRAINT ot_surgeries_admission_fk FOREIGN KEY (clinic_id, admission_id, patient_id)
        REFERENCES ipd_admissions (clinic_id, id, patient_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT ot_surgeries_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT ot_surgeries_theatre_fk FOREIGN KEY (clinic_id, theatre_id) REFERENCES ot_theatres (clinic_id, id),
    CONSTRAINT ot_surgeries_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT ot_surgeries_service_fk FOREIGN KEY (clinic_id, service_id) REFERENCES billable_services (clinic_id, id),
    CONSTRAINT ot_surgeries_surgeon_fk FOREIGN KEY (clinic_id, primary_surgeon_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ot_surgeries_anaesthetist_fk FOREIGN KEY (clinic_id, anaesthetist_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ot_surgeries_charge_fk FOREIGN KEY (clinic_id, charge_id)
        REFERENCES ipd_charges (clinic_id, id) ON DELETE SET NULL (charge_id)
);
CREATE INDEX IF NOT EXISTS idx_ot_surgeries_theatre_time ON ot_surgeries (clinic_id, theatre_id, scheduled_start);
CREATE INDEX IF NOT EXISTS idx_ot_surgeries_day ON ot_surgeries (clinic_id, scheduled_start);
CREATE INDEX IF NOT EXISTS idx_ot_surgeries_admission ON ot_surgeries (clinic_id, admission_id);
CREATE INDEX IF NOT EXISTS idx_ot_surgeries_patient ON ot_surgeries (clinic_id, patient_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ot_surgeries_idempotency
    ON ot_surgeries (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ── 3. WHO Surgical Safety Checklist ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS ot_who_checklists (
    id                                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    surgery_id                          UUID NOT NULL,
    -- Sign-In (before induction of anaesthesia)
    patient_identity_confirmed          BOOLEAN NOT NULL DEFAULT false,
    site_marked                         BOOLEAN NOT NULL DEFAULT false,
    anaesthesia_machine_checked         BOOLEAN NOT NULL DEFAULT false,
    pulse_oximeter_functioning          BOOLEAN NOT NULL DEFAULT false,
    allergy_checked                     BOOLEAN NOT NULL DEFAULT false,
    airway_risk_assessed                BOOLEAN NOT NULL DEFAULT false,
    blood_loss_risk_assessed            BOOLEAN NOT NULL DEFAULT false,
    sign_in_completed                   BOOLEAN NOT NULL DEFAULT false,
    sign_in_at                          TIMESTAMPTZ NULL,
    sign_in_by                          UUID NULL,
    sign_in_by_name                     TEXT NULL CHECK (char_length(sign_in_by_name) <= 100),
    -- Time-Out (before skin incision)
    team_members_introduced             BOOLEAN NOT NULL DEFAULT false,
    patient_name_procedure_confirmed    BOOLEAN NOT NULL DEFAULT false,
    anticipated_critical_events         BOOLEAN NOT NULL DEFAULT false,
    antibiotic_prophylaxis_given        BOOLEAN NOT NULL DEFAULT false,
    essential_imaging_displayed         BOOLEAN NOT NULL DEFAULT false,
    time_out_completed                  BOOLEAN NOT NULL DEFAULT false,
    time_out_at                         TIMESTAMPTZ NULL,
    time_out_by                         UUID NULL,
    time_out_by_name                    TEXT NULL CHECK (char_length(time_out_by_name) <= 100),
    -- Sign-Out (before the patient leaves the theatre)
    procedure_recorded                  BOOLEAN NOT NULL DEFAULT false,
    instrument_swab_needle_count_correct BOOLEAN NOT NULL DEFAULT false,
    specimen_labelled                   BOOLEAN NOT NULL DEFAULT false,
    equipment_problems_addressed        BOOLEAN NOT NULL DEFAULT false,
    key_recovery_concerns_reviewed      BOOLEAN NOT NULL DEFAULT false,
    sign_out_completed                  BOOLEAN NOT NULL DEFAULT false,
    sign_out_at                         TIMESTAMPTZ NULL,
    sign_out_by                         UUID NULL,
    sign_out_by_name                    TEXT NULL CHECK (char_length(sign_out_by_name) <= 100),
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ot_who_checklists_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ot_who_checklists_surgery_key UNIQUE (clinic_id, surgery_id),
    CONSTRAINT ot_who_checklists_sign_in CHECK (sign_in_completed = (sign_in_at IS NOT NULL) AND (NOT sign_in_completed OR (
        patient_identity_confirmed AND site_marked AND anaesthesia_machine_checked AND pulse_oximeter_functioning
        AND allergy_checked AND airway_risk_assessed AND blood_loss_risk_assessed))),
    CONSTRAINT ot_who_checklists_time_out CHECK (time_out_completed = (time_out_at IS NOT NULL) AND (NOT time_out_completed OR (
        sign_in_completed AND team_members_introduced AND patient_name_procedure_confirmed
        AND anticipated_critical_events AND antibiotic_prophylaxis_given AND essential_imaging_displayed))),
    CONSTRAINT ot_who_checklists_sign_out CHECK (sign_out_completed = (sign_out_at IS NOT NULL) AND (NOT sign_out_completed OR (
        time_out_completed AND procedure_recorded AND instrument_swab_needle_count_correct AND specimen_labelled
        AND equipment_problems_addressed AND key_recovery_concerns_reviewed))),
    CONSTRAINT ot_who_checklists_surgery_fk FOREIGN KEY (clinic_id, surgery_id)
        REFERENCES ot_surgeries (clinic_id, id) ON DELETE CASCADE
);

-- ── 4. Anaesthesia record ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ot_anaesthesia_records (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    surgery_id             UUID NOT NULL,
    anaesthetist_id        UUID NULL,
    asa_status             TEXT NULL CHECK (asa_status IN ('I', 'II', 'III', 'IV', 'V', 'VI',
                                                           'IE', 'IIE', 'IIIE', 'IVE', 'VE')),
    mallampati             TEXT NULL CHECK (mallampati IN ('I', 'II', 'III', 'IV')),
    technique              TEXT NULL CHECK (technique IN ('general', 'spinal', 'epidural', 'nerve_block', 'local',
                                                          'sedation', 'mac', 'combined')),
    airway_device          TEXT NULL CHECK (char_length(airway_device) <= 100),
    induction_agents       TEXT NULL CHECK (char_length(induction_agents) <= 500),
    maintenance_agents     TEXT NULL CHECK (char_length(maintenance_agents) <= 500),
    blood_loss_ml          INTEGER NULL CHECK (blood_loss_ml BETWEEN 0 AND 50000),
    fluids_given_ml        INTEGER NULL CHECK (fluids_given_ml BETWEEN 0 AND 50000),
    urine_output_ml        INTEGER NULL CHECK (urine_output_ml BETWEEN 0 AND 50000),
    vital_signs_log        JSONB NOT NULL DEFAULT '[]'::jsonb
                           CHECK (jsonb_typeof(vital_signs_log) = 'array' AND jsonb_array_length(vital_signs_log) <= 500),
    extubation_notes       TEXT NULL CHECK (char_length(extubation_notes) <= 2000),
    recovery_aldrete_score SMALLINT NULL CHECK (recovery_aldrete_score BETWEEN 0 AND 10),
    is_signed              BOOLEAN NOT NULL DEFAULT false,
    signed_at              TIMESTAMPTZ NULL,
    signed_by              UUID NULL,
    doctor_snapshot        JSONB NULL,         -- {doctor_id, name, qualifications, registration_number, registration_council}
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ot_anaesthesia_records_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ot_anaesthesia_records_surgery_key UNIQUE (clinic_id, surgery_id),
    CONSTRAINT ot_anaesthesia_records_signed CHECK (is_signed = (signed_at IS NOT NULL) AND (NOT is_signed OR (
        anaesthetist_id IS NOT NULL AND asa_status IS NOT NULL AND technique IS NOT NULL
        AND btrim(COALESCE(doctor_snapshot ->> 'registration_number', '')) <> ''))),
    CONSTRAINT ot_anaesthesia_records_surgery_fk FOREIGN KEY (clinic_id, surgery_id)
        REFERENCES ot_surgeries (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT ot_anaesthesia_records_doctor_fk FOREIGN KEY (clinic_id, anaesthetist_id) REFERENCES doctors (clinic_id, id)
);

-- ── 5. Guards ──────────────────────────────────────────────────────────────
-- Set by the ot_* functions for one clinic, transaction-local.
CREATE OR REPLACE FUNCTION ot_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.ot_tx', true), '') = p_clinic_id::TEXT
$$;

-- Surgeries, checklists, anaesthesia: written by the ot_* functions only. A
-- closed surgery and a signed anaesthesia record never change again. The owner
-- purge may delete (cascade) and unlink a purged charge; a G7 identity merge
-- may move patient_id (admission FK cascade) and nothing else.
CREATE OR REPLACE FUNCTION ot_guard_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) OR opd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'ot_append_only' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ipd_purging(OLD.clinic_id) OR patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN
            RETURN NEW;
        END IF;
        IF NEW.id <> OLD.id OR NEW.clinic_id <> OLD.clinic_id OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'ot_identity_immutable' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    IF NOT ot_in_tx(NEW.clinic_id) THEN RAISE EXCEPTION 'ot_via_function_only' USING ERRCODE = 'P0001'; END IF;
    IF TG_OP = 'UPDATE' THEN
        IF TG_TABLE_NAME = 'ot_surgeries' THEN
            IF (NEW.admission_id, NEW.patient_id, NEW.surgery_year, NEW.surgery_seq, NEW.created_by)
               IS DISTINCT FROM (OLD.admission_id, OLD.patient_id, OLD.surgery_year, OLD.surgery_seq, OLD.created_by) THEN
                RAISE EXCEPTION 'ot_identity_immutable' USING ERRCODE = 'P0001';
            END IF;
            -- completed: only the posted charge is linked afterwards
            IF OLD.status IN ('cancelled', 'postponed')
               OR (OLD.status = 'completed' AND (to_jsonb(NEW) - 'charge_id' - 'updated_at' - 'surgery_number')
                                                <> (to_jsonb(OLD) - 'charge_id' - 'updated_at' - 'surgery_number')) THEN
                RAISE EXCEPTION 'ot_surgery_closed' USING ERRCODE = 'P0001';
            END IF;
        ELSIF NEW.surgery_id <> OLD.surgery_id THEN
            RAISE EXCEPTION 'ot_identity_immutable' USING ERRCODE = 'P0001';
        ELSIF TG_TABLE_NAME = 'ot_anaesthesia_records' THEN
            IF OLD.is_signed THEN RAISE EXCEPTION 'ot_anaesthesia_signed' USING ERRCODE = 'P0001'; END IF;
        END IF;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;

-- No double booking. The UPDATE on the theatre row serialises every booking of
-- that theatre (READ COMMITTED: the waiter re-reads and sees the winner's row;
-- REPEATABLE READ: the waiter gets a serialisation failure).
CREATE OR REPLACE FUNCTION ot_check_overlap() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status NOT IN ('scheduled', 'in_theatre') THEN RETURN NEW; END IF;
    IF TG_OP = 'UPDATE' AND OLD.status IN ('scheduled', 'in_theatre')
       AND (NEW.theatre_id, NEW.scheduled_start, NEW.scheduled_end)
           IS NOT DISTINCT FROM (OLD.theatre_id, OLD.scheduled_start, OLD.scheduled_end) THEN
        RETURN NEW;
    END IF;
    UPDATE ot_theatres SET schedule_version = schedule_version + 1
     WHERE id = NEW.theatre_id AND clinic_id = NEW.clinic_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_theatre_not_found' USING ERRCODE = 'P0001'; END IF;
    IF EXISTS (SELECT 1 FROM ot_surgeries
                WHERE clinic_id = NEW.clinic_id AND theatre_id = NEW.theatre_id AND id <> NEW.id
                  AND status IN ('scheduled', 'in_theatre')
                  AND scheduled_start < NEW.scheduled_end AND scheduled_end > NEW.scheduled_start) THEN
        RAISE EXCEPTION 'ot_theatre_busy' USING ERRCODE = 'P0001';
    END IF;
    -- one patient, one table at a time (the scheduling function holds the admission row lock)
    IF EXISTS (SELECT 1 FROM ot_surgeries
                WHERE clinic_id = NEW.clinic_id AND patient_id = NEW.patient_id AND id <> NEW.id
                  AND status IN ('scheduled', 'in_theatre')
                  AND scheduled_start < NEW.scheduled_end AND scheduled_end > NEW.scheduled_start) THEN
        RAISE EXCEPTION 'ot_patient_busy' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- A stay cannot start its discharge while one of its surgeries is still booked or
-- under way: finish, cancel or postpone it first, so the theatre list never holds a
-- surgery for a patient who has gone home. Named ipd_* so the IPD routes map it.
CREATE OR REPLACE FUNCTION ot_block_discharge() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'admitted' AND NEW.status = 'discharge_pending' AND EXISTS (
        SELECT 1 FROM ot_surgeries WHERE clinic_id = NEW.clinic_id AND admission_id = NEW.id
           AND status IN ('scheduled', 'in_theatre', 'recovery')) THEN
        RAISE EXCEPTION 'ipd_surgery_open' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_ipd_admissions_ot_open ON ipd_admissions;
CREATE TRIGGER trg_ipd_admissions_ot_open BEFORE UPDATE OF status ON ipd_admissions
    FOR EACH ROW EXECUTE FUNCTION ot_block_discharge();

DROP TRIGGER IF EXISTS trg_ot_surgeries_guard ON ot_surgeries;
CREATE TRIGGER trg_ot_surgeries_guard BEFORE INSERT OR UPDATE OR DELETE ON ot_surgeries
    FOR EACH ROW EXECUTE FUNCTION ot_guard_record();
-- after the guard (trigger names fire in order): a refused write never takes the theatre lock
DROP TRIGGER IF EXISTS trg_ot_surgeries_overlap ON ot_surgeries;
CREATE TRIGGER trg_ot_surgeries_overlap BEFORE INSERT OR UPDATE OF theatre_id, scheduled_start, scheduled_end, status
    ON ot_surgeries FOR EACH ROW EXECUTE FUNCTION ot_check_overlap();
DROP TRIGGER IF EXISTS trg_ot_who_checklists_guard ON ot_who_checklists;
CREATE TRIGGER trg_ot_who_checklists_guard BEFORE INSERT OR UPDATE OR DELETE ON ot_who_checklists
    FOR EACH ROW EXECUTE FUNCTION ot_guard_record();
DROP TRIGGER IF EXISTS trg_ot_anaesthesia_guard ON ot_anaesthesia_records;
CREATE TRIGGER trg_ot_anaesthesia_guard BEFORE INSERT OR UPDATE OR DELETE ON ot_anaesthesia_records
    FOR EACH ROW EXECUTE FUNCTION ot_guard_record();

-- ── 6. Schedule ────────────────────────────────────────────────────────────
-- p_payload: admission_id, theatre_id, procedure_name, service_id, scheduled_start,
-- scheduled_end, primary_surgeon_id, assistant_surgeon_name, anaesthetist_id,
-- scrub_nurse_name, priority, pre_op_diagnosis, idempotency_key.
CREATE OR REPLACE FUNCTION ot_schedule_surgery(p_clinic_id UUID, p_payload JSONB, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; t ot_theatres; s ot_surgeries; v_key TEXT := NULLIF(p_payload ->> 'idempotency_key', '');
        v_year SMALLINT := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
BEGIN
    PERFORM set_config('kriya.ot_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, (p_payload ->> 'admission_id')::UUID);   -- serialises replays too
    IF v_key IS NOT NULL THEN
        SELECT * INTO s FROM ot_surgeries WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
        IF FOUND THEN
            IF s.admission_id <> a.id THEN RAISE EXCEPTION 'ot_key_reused' USING ERRCODE = 'P0001'; END IF;
            RETURN to_jsonb(s) || jsonb_build_object('replayed', true);
        END IF;
    END IF;
    IF a.status <> 'admitted' THEN RAISE EXCEPTION 'ot_admission_not_active' USING ERRCODE = 'P0001'; END IF;
    SELECT * INTO t FROM ot_theatres WHERE id = (p_payload ->> 'theatre_id')::UUID AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_theatre_not_found' USING ERRCODE = 'P0001'; END IF;
    IF NOT t.is_active THEN RAISE EXCEPTION 'ot_theatre_inactive' USING ERRCODE = 'P0001'; END IF;
    IF t.branch_id IS NOT NULL AND a.branch_id IS NOT NULL AND t.branch_id <> a.branch_id THEN
        RAISE EXCEPTION 'ot_theatre_other_branch' USING ERRCODE = 'P0001';
    END IF;
    IF NULLIF(p_payload ->> 'service_id', '') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM billable_services WHERE id = (p_payload ->> 'service_id')::UUID AND clinic_id = p_clinic_id
           AND is_active) THEN
        RAISE EXCEPTION 'ot_service_not_found' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO ot_surgeries (clinic_id, branch_id, admission_id, patient_id, theatre_id, surgery_year, surgery_seq,
                              procedure_name, service_id, scheduled_start, scheduled_end, primary_surgeon_id,
                              assistant_surgeon_name, anaesthetist_id, scrub_nurse_name, priority, pre_op_diagnosis,
                              idempotency_key, created_by, created_by_name)
    VALUES (p_clinic_id, COALESCE(t.branch_id, a.branch_id), a.id, a.patient_id, t.id, v_year,
            opd_next_counter(p_clinic_id, 'ot:' || v_year),
            btrim(p_payload ->> 'procedure_name'), NULLIF(p_payload ->> 'service_id', '')::UUID,
            (p_payload ->> 'scheduled_start')::TIMESTAMPTZ, (p_payload ->> 'scheduled_end')::TIMESTAMPTZ,
            (p_payload ->> 'primary_surgeon_id')::UUID, NULLIF(btrim(p_payload ->> 'assistant_surgeon_name'), ''),
            NULLIF(p_payload ->> 'anaesthetist_id', '')::UUID, NULLIF(btrim(p_payload ->> 'scrub_nurse_name'), ''),
            COALESCE(p_payload ->> 'priority', 'elective'), btrim(p_payload ->> 'pre_op_diagnosis'),
            v_key, p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO s;
    INSERT INTO ot_who_checklists (clinic_id, surgery_id) VALUES (p_clinic_id, s.id);
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'surgery_scheduled', p_actor_id, left(p_actor_name, 100),
                    jsonb_build_object('surgery_id', s.id, 'procedure', s.procedure_name, 'theatre', t.name,
                                       'scheduled_start', s.scheduled_start));
    PERFORM set_config('kriya.ot_tx', '', true);
    RETURN to_jsonb(s) || jsonb_build_object('replayed', false);
END $$;

-- ── 7. WHO checklist: Sign-In -> Time-Out -> Sign-Out ───────────────────────
CREATE OR REPLACE FUNCTION ot_checklist_items(p_phase TEXT) RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_phase
        WHEN 'sign_in' THEN ARRAY['patient_identity_confirmed', 'site_marked', 'anaesthesia_machine_checked',
                                  'pulse_oximeter_functioning', 'allergy_checked', 'airway_risk_assessed',
                                  'blood_loss_risk_assessed']
        WHEN 'time_out' THEN ARRAY['team_members_introduced', 'patient_name_procedure_confirmed',
                                   'anticipated_critical_events', 'antibiotic_prophylaxis_given',
                                   'essential_imaging_displayed']
        WHEN 'sign_out' THEN ARRAY['procedure_recorded', 'instrument_swab_needle_count_correct', 'specimen_labelled',
                                   'equipment_problems_addressed', 'key_recovery_concerns_reviewed']
    END
$$;

-- p_items: {item: true|false} for this phase only. Saved as given; the phase
-- completes (and locks) when every one of its items is true.
CREATE OR REPLACE FUNCTION ot_update_checklist(p_clinic_id UUID, p_surgery_id UUID, p_phase TEXT, p_items JSONB,
                                               p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ot_surgeries; c ot_who_checklists; v JSONB; k TEXT; v_items TEXT[] := ot_checklist_items(p_phase);
BEGIN
    IF v_items IS NULL THEN RAISE EXCEPTION 'ot_checklist_bad_phase' USING ERRCODE = 'P0001'; END IF;
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'ot_checklist_bad_item' USING ERRCODE = 'P0001';
    END IF;
    FOR k IN SELECT jsonb_object_keys(p_items) LOOP
        IF NOT (k = ANY (v_items)) OR jsonb_typeof(p_items -> k) <> 'boolean' THEN
            RAISE EXCEPTION 'ot_checklist_bad_item' USING ERRCODE = 'P0001';
        END IF;
    END LOOP;
    PERFORM set_config('kriya.ot_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM ot_surgeries WHERE id = p_surgery_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_surgery_not_found' USING ERRCODE = 'P0001'; END IF;
    INSERT INTO ot_who_checklists (clinic_id, surgery_id) VALUES (p_clinic_id, s.id)
    ON CONFLICT (clinic_id, surgery_id) DO NOTHING;
    SELECT * INTO c FROM ot_who_checklists WHERE clinic_id = p_clinic_id AND surgery_id = s.id FOR UPDATE;
    v := to_jsonb(c);
    IF (v ->> (p_phase || '_completed'))::BOOLEAN THEN   -- a completed phase is final; a retry is a replay
        RETURN v || jsonb_build_object('replayed', true);
    END IF;
    IF s.status NOT IN ('scheduled', 'in_theatre', 'recovery') THEN
        RAISE EXCEPTION 'ot_surgery_closed' USING ERRCODE = 'P0001';
    END IF;
    IF p_phase = 'time_out' AND NOT c.sign_in_completed THEN
        RAISE EXCEPTION 'ot_checklist_order:sign_in' USING ERRCODE = 'P0001';
    END IF;
    IF p_phase = 'sign_out' AND NOT c.time_out_completed THEN
        RAISE EXCEPTION 'ot_checklist_order:time_out' USING ERRCODE = 'P0001';
    END IF;
    v := v || p_items;
    IF (SELECT bool_and((v ->> i)::BOOLEAN) FROM unnest(v_items) i) THEN
        v := v || jsonb_build_object(p_phase || '_completed', true, p_phase || '_at', now(),
                                     p_phase || '_by', p_actor_id, p_phase || '_by_name', left(p_actor_name, 100));
    END IF;
    UPDATE ot_who_checklists x SET
        patient_identity_confirmed = r.patient_identity_confirmed, site_marked = r.site_marked,
        anaesthesia_machine_checked = r.anaesthesia_machine_checked,
        pulse_oximeter_functioning = r.pulse_oximeter_functioning, allergy_checked = r.allergy_checked,
        airway_risk_assessed = r.airway_risk_assessed, blood_loss_risk_assessed = r.blood_loss_risk_assessed,
        sign_in_completed = r.sign_in_completed, sign_in_at = r.sign_in_at, sign_in_by = r.sign_in_by,
        sign_in_by_name = r.sign_in_by_name,
        team_members_introduced = r.team_members_introduced,
        patient_name_procedure_confirmed = r.patient_name_procedure_confirmed,
        anticipated_critical_events = r.anticipated_critical_events,
        antibiotic_prophylaxis_given = r.antibiotic_prophylaxis_given,
        essential_imaging_displayed = r.essential_imaging_displayed,
        time_out_completed = r.time_out_completed, time_out_at = r.time_out_at, time_out_by = r.time_out_by,
        time_out_by_name = r.time_out_by_name,
        procedure_recorded = r.procedure_recorded,
        instrument_swab_needle_count_correct = r.instrument_swab_needle_count_correct,
        specimen_labelled = r.specimen_labelled, equipment_problems_addressed = r.equipment_problems_addressed,
        key_recovery_concerns_reviewed = r.key_recovery_concerns_reviewed,
        sign_out_completed = r.sign_out_completed, sign_out_at = r.sign_out_at, sign_out_by = r.sign_out_by,
        sign_out_by_name = r.sign_out_by_name
      FROM jsonb_populate_record(NULL::ot_who_checklists, v) r
     WHERE x.id = c.id
    RETURNING x.* INTO c;
    IF (to_jsonb(c) ->> (p_phase || '_completed'))::BOOLEAN THEN
        PERFORM ipd_log(p_clinic_id, s.admission_id, NULL, 'surgery_checklist_' || p_phase, p_actor_id,
                        left(p_actor_name, 100), jsonb_build_object('surgery_id', s.id));
    END IF;
    PERFORM set_config('kriya.ot_tx', '', true);
    RETURN to_jsonb(c) || jsonb_build_object('replayed', false);
END $$;

-- ── 8. Anaesthesia record: create / update until signed; sign locks it ─────
CREATE OR REPLACE FUNCTION ot_record_anaesthesia(p_clinic_id UUID, p_surgery_id UUID, p_payload JSONB, p_sign BOOLEAN,
                                                 p_doctor_id UUID, p_doctor_snapshot JSONB, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ot_surgeries; r ot_anaesthesia_records; v JSONB;
        v_fields TEXT[] := ARRAY['anaesthetist_id', 'asa_status', 'mallampati', 'technique', 'airway_device',
                                 'induction_agents', 'maintenance_agents', 'blood_loss_ml', 'fluids_given_ml',
                                 'urine_output_ml', 'vital_signs_log', 'extubation_notes', 'recovery_aldrete_score'];
        k TEXT;
BEGIN
    IF jsonb_typeof(COALESCE(p_payload, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'ot_bad_field' USING ERRCODE = 'P0001';
    END IF;
    FOR k IN SELECT jsonb_object_keys(COALESCE(p_payload, '{}'::jsonb)) LOOP
        IF NOT (k = ANY (v_fields)) THEN RAISE EXCEPTION 'ot_bad_field' USING ERRCODE = 'P0001'; END IF;
    END LOOP;
    PERFORM set_config('kriya.ot_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM ot_surgeries WHERE id = p_surgery_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_surgery_not_found' USING ERRCODE = 'P0001'; END IF;
    IF s.status IN ('cancelled', 'postponed') THEN RAISE EXCEPTION 'ot_surgery_closed' USING ERRCODE = 'P0001'; END IF;
    INSERT INTO ot_anaesthesia_records (clinic_id, surgery_id, anaesthetist_id)
    VALUES (p_clinic_id, s.id, s.anaesthetist_id)
    ON CONFLICT (clinic_id, surgery_id) DO NOTHING;
    SELECT * INTO r FROM ot_anaesthesia_records WHERE clinic_id = p_clinic_id AND surgery_id = s.id FOR UPDATE;
    IF r.is_signed THEN RAISE EXCEPTION 'ot_anaesthesia_signed' USING ERRCODE = 'P0001'; END IF;
    v := to_jsonb(r) || COALESCE(p_payload, '{}'::jsonb);
    IF p_sign THEN
        IF p_doctor_id IS NULL OR btrim(COALESCE(p_doctor_snapshot ->> 'registration_number', '')) = '' THEN
            RAISE EXCEPTION 'ot_doctor_required' USING ERRCODE = 'P0001';
        END IF;
        IF v ->> 'asa_status' IS NULL OR v ->> 'technique' IS NULL THEN
            RAISE EXCEPTION 'ot_anaesthesia_incomplete' USING ERRCODE = 'P0001';
        END IF;
        v := v || jsonb_build_object('anaesthetist_id', p_doctor_id, 'is_signed', true, 'signed_at', now(),
                                     'signed_by', p_actor_id, 'doctor_snapshot', p_doctor_snapshot);
    END IF;
    UPDATE ot_anaesthesia_records x SET
        anaesthetist_id = n.anaesthetist_id, asa_status = n.asa_status, mallampati = n.mallampati,
        technique = n.technique, airway_device = n.airway_device, induction_agents = n.induction_agents,
        maintenance_agents = n.maintenance_agents, blood_loss_ml = n.blood_loss_ml,
        fluids_given_ml = n.fluids_given_ml, urine_output_ml = n.urine_output_ml,
        vital_signs_log = COALESCE(n.vital_signs_log, '[]'::jsonb), extubation_notes = n.extubation_notes,
        recovery_aldrete_score = n.recovery_aldrete_score, is_signed = n.is_signed, signed_at = n.signed_at,
        signed_by = n.signed_by, doctor_snapshot = n.doctor_snapshot
      FROM jsonb_populate_record(NULL::ot_anaesthesia_records, v) n
     WHERE x.id = r.id
    RETURNING x.* INTO r;
    IF r.is_signed THEN
        PERFORM ipd_log(p_clinic_id, s.admission_id, NULL, 'anaesthesia_signed', p_actor_id,
                        left(p_doctor_snapshot ->> 'name', 100), jsonb_build_object('surgery_id', s.id));
    END IF;
    PERFORM set_config('kriya.ot_tx', '', true);
    RETURN to_jsonb(r);
END $$;

-- ── 9. Status: start / recovery / cancel / postpone ────────────────────────
CREATE OR REPLACE FUNCTION ot_set_status(p_clinic_id UUID, p_surgery_id UUID, p_action TEXT, p_reason TEXT,
                                         p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ot_surgeries; v_to TEXT; v_from TEXT[]; v_adm TEXT;
BEGIN
    v_to := CASE p_action WHEN 'start' THEN 'in_theatre' WHEN 'recovery' THEN 'recovery'
                          WHEN 'cancel' THEN 'cancelled' WHEN 'postpone' THEN 'postponed' END;
    v_from := CASE p_action WHEN 'start' THEN ARRAY['scheduled'] WHEN 'recovery' THEN ARRAY['in_theatre']
                            WHEN 'cancel' THEN ARRAY['scheduled', 'in_theatre'] WHEN 'postpone' THEN ARRAY['scheduled'] END;
    IF v_to IS NULL THEN RAISE EXCEPTION 'ot_bad_action' USING ERRCODE = 'P0001'; END IF;
    PERFORM set_config('kriya.ot_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM ot_surgeries WHERE id = p_surgery_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_surgery_not_found' USING ERRCODE = 'P0001'; END IF;
    IF s.status = v_to THEN RETURN to_jsonb(s) || jsonb_build_object('replayed', true); END IF;
    IF NOT (s.status = ANY (v_from)) THEN
        RAISE EXCEPTION 'ot_bad_transition:%->%', s.status, v_to USING ERRCODE = 'P0001';
    END IF;
    IF p_action IN ('cancel', 'postpone') AND char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN
        RAISE EXCEPTION 'ot_reason_required' USING ERRCODE = 'P0001';
    END IF;
    IF p_action = 'start' THEN
        SELECT status INTO v_adm FROM ipd_admissions WHERE id = s.admission_id AND clinic_id = p_clinic_id;
        IF v_adm IS DISTINCT FROM 'admitted' THEN RAISE EXCEPTION 'ot_admission_not_active' USING ERRCODE = 'P0001'; END IF;
    END IF;
    UPDATE ot_surgeries SET status = v_to,
           actual_start = CASE WHEN p_action = 'start' THEN now() ELSE actual_start END,
           cancellation_reason = CASE WHEN p_action IN ('cancel', 'postpone') THEN left(btrim(p_reason), 300)
                                      ELSE cancellation_reason END
     WHERE id = s.id RETURNING * INTO s;
    PERFORM ipd_log(p_clinic_id, s.admission_id, NULL, 'surgery_' || v_to, p_actor_id, left(p_actor_name, 100),
                    jsonb_build_object('surgery_id', s.id, 'reason', s.cancellation_reason));
    PERFORM set_config('kriya.ot_tx', '', true);
    RETURN to_jsonb(s) || jsonb_build_object('replayed', false);
END $$;

-- ── 10. Complete: checklist gate, post-op note, the one surgery charge ─────
-- p_note: post_op_diagnosis, surgical_findings, procedure_notes (required),
-- implants_used, specimens_to_lab, post_op_instructions. Price: p_amount_paise
-- when given, else the linked service's standard price at this moment.
CREATE OR REPLACE FUNCTION ot_complete_surgery(p_clinic_id UUID, p_surgery_id UUID, p_note JSONB, p_amount_paise INTEGER,
                                               p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ot_surgeries; a ipd_admissions; c ot_who_checklists; ch ipd_charges; v_adm UUID;
        v_price INTEGER; v_tax SMALLINT := 0; v_sac TEXT;
BEGIN
    SELECT admission_id INTO v_adm FROM ot_surgeries WHERE id = p_surgery_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'ot_surgery_not_found' USING ERRCODE = 'P0001'; END IF;
    PERFORM set_config('kriya.ot_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, v_adm);   -- admission before surgery, as in scheduling and billing
    SELECT * INTO s FROM ot_surgeries WHERE id = p_surgery_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF s.status = 'completed' THEN   -- a retry: the surgery and its one charge, unchanged
        SELECT * INTO ch FROM ipd_charges WHERE id = s.charge_id AND clinic_id = p_clinic_id;
        RETURN jsonb_build_object('surgery', to_jsonb(s), 'charge', to_jsonb(ch), 'replayed', true);
    END IF;
    IF s.status NOT IN ('in_theatre', 'recovery') THEN
        RAISE EXCEPTION 'ot_bad_transition:%->completed', s.status USING ERRCODE = 'P0001';
    END IF;
    SELECT * INTO c FROM ot_who_checklists WHERE clinic_id = p_clinic_id AND surgery_id = s.id;
    IF NOT (COALESCE(c.sign_in_completed, false) AND c.time_out_completed AND c.sign_out_completed) THEN
        RAISE EXCEPTION 'ot_checklist_incomplete' USING ERRCODE = 'P0001';
    END IF;
    IF char_length(btrim(COALESCE(p_note ->> 'procedure_notes', ''))) < 3 THEN
        RAISE EXCEPTION 'ot_note_required' USING ERRCODE = 'P0001';
    END IF;
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN
        RAISE EXCEPTION 'ot_admission_not_active' USING ERRCODE = 'P0001';
    END IF;
    -- G5: a charge never lands after the final bill (it would sit outside it)
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void') THEN
        RAISE EXCEPTION 'ot_final_bill_exists' USING ERRCODE = 'P0001';
    END IF;
    IF s.service_id IS NOT NULL THEN
        SELECT standard_price_paise, tax_rate_bps, hsn_sac INTO v_price, v_tax, v_sac
          FROM billable_services WHERE id = s.service_id AND clinic_id = p_clinic_id;
    END IF;
    -- an entered amount replaces the tariff price only: the linked service's GST rate and SAC still apply
    IF p_amount_paise IS NOT NULL THEN v_price := p_amount_paise; END IF;
    IF v_price IS NULL THEN RAISE EXCEPTION 'ot_charge_amount_required' USING ERRCODE = 'P0001'; END IF;
    IF v_price NOT BETWEEN 0 AND 100000000 THEN RAISE EXCEPTION 'ot_charge_amount_invalid' USING ERRCODE = 'P0001'; END IF;
    IF opd_clinic_gstin(p_clinic_id) IS NULL THEN v_tax := 0; END IF;   -- an unregistered clinic charges no GST
    UPDATE ot_surgeries SET status = 'completed', actual_end = now(),
           post_op_diagnosis = NULLIF(btrim(p_note ->> 'post_op_diagnosis'), ''),
           surgical_findings = NULLIF(btrim(p_note ->> 'surgical_findings'), ''),
           procedure_notes = btrim(p_note ->> 'procedure_notes'),
           implants_used = NULLIF(btrim(p_note ->> 'implants_used'), ''),
           specimens_to_lab = NULLIF(btrim(p_note ->> 'specimens_to_lab'), ''),
           post_op_instructions = NULLIF(btrim(p_note ->> 'post_op_instructions'), ''),
           completed_by = p_actor_id, completed_by_name = left(p_actor_name, 100)
     WHERE id = s.id RETURNING * INTO s;
    INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity, unit_price_paise,
                             source, source_ref, status, posted_at, posted_by, created_by, created_by_name,
                             tax_rate_bps, hsn_sac)
    VALUES (p_clinic_id, a.id, 'procedure', left('Surgical Procedure: ' || s.procedure_name, 200),
            ipd_ist_date(now()), 1, v_price, 'order', 'ot_surgery:' || s.id, 'posted', now(), p_actor_id,
            p_actor_id, left(p_actor_name, 100), COALESCE(v_tax, 0), CASE WHEN COALESCE(v_tax, 0) > 0 THEN v_sac END)
    ON CONFLICT (clinic_id, admission_id, source_ref) WHERE source_ref IS NOT NULL AND status <> 'void' DO NOTHING
    RETURNING * INTO ch;
    IF ch.id IS NULL THEN
        SELECT * INTO ch FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id
           AND source_ref = 'ot_surgery:' || s.id AND status <> 'void';
    END IF;
    UPDATE ot_surgeries SET charge_id = ch.id WHERE id = s.id RETURNING * INTO s;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'surgery_completed', p_actor_id, left(p_actor_name, 100),
                    jsonb_build_object('surgery_id', s.id, 'charge_id', ch.id, 'amount_paise', ch.unit_price_paise));
    PERFORM set_config('kriya.ot_tx', '', true);
    RETURN jsonb_build_object('surgery', to_jsonb(s), 'charge', to_jsonb(ch), 'replayed', false);
END $$;

-- ── 11. Hygiene, lockout, RLS ──────────────────────────────────────────────
ALTER FUNCTION ot_in_tx(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ot_guard_record() SET search_path = public, pg_temp;
ALTER FUNCTION ot_check_overlap() SET search_path = public, pg_temp;
ALTER FUNCTION ot_block_discharge() SET search_path = public, pg_temp;
ALTER FUNCTION ot_checklist_items(text) SET search_path = public, pg_temp;
ALTER FUNCTION ot_schedule_surgery(uuid, jsonb, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ot_update_checklist(uuid, uuid, text, jsonb, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ot_record_anaesthesia(uuid, uuid, jsonb, boolean, uuid, jsonb, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ot_set_status(uuid, uuid, text, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ot_complete_surgery(uuid, uuid, jsonb, integer, uuid, text) SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY['ot_schedule_surgery(uuid,jsonb,uuid,text)',
                             'ot_update_checklist(uuid,uuid,text,jsonb,uuid,text)',
                             'ot_record_anaesthesia(uuid,uuid,jsonb,boolean,uuid,jsonb,uuid)',
                             'ot_set_status(uuid,uuid,text,text,uuid,text)',
                             'ot_complete_surgery(uuid,uuid,jsonb,integer,uuid,text)']
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

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ot_theatres', 'ot_surgeries', 'ot_who_checklists', 'ot_anaesthesia_records'] LOOP
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

-- ── 12. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.ot_theatres') IS NULL OR to_regclass('public.ot_surgeries') IS NULL
       OR to_regclass('public.ot_who_checklists') IS NULL OR to_regclass('public.ot_anaesthesia_records') IS NULL THEN
        RAISE EXCEPTION '122 verify: OT tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN ('ot_theatres_name_key', 'ot_surgeries_seq_key',
          'ot_surgeries_times', 'ot_surgeries_admission_fk', 'ot_surgeries_completed', 'ot_who_checklists_surgery_key',
          'ot_who_checklists_time_out', 'ot_who_checklists_sign_out', 'ot_anaesthesia_records_signed')) <> 9 THEN
        RAISE EXCEPTION '122 verify: constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('trg_ot_surgeries_guard',
          'trg_ot_surgeries_overlap', 'trg_ot_who_checklists_guard', 'trg_ot_anaesthesia_guard',
          'trg_ipd_admissions_ot_open')) <> 5 THEN
        RAISE EXCEPTION '122 verify: triggers missing';
    END IF;
    IF to_regprocedure('ot_schedule_surgery(uuid,jsonb,uuid,text)') IS NULL
       OR to_regprocedure('ot_complete_surgery(uuid,uuid,jsonb,integer,uuid,text)') IS NULL
       OR to_regprocedure('ot_update_checklist(uuid,uuid,text,jsonb,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '122 verify: functions missing';
    END IF;
    IF (SELECT count(*) FROM pg_class WHERE relname IN ('ot_theatres', 'ot_surgeries', 'ot_who_checklists',
          'ot_anaesthesia_records') AND relrowsecurity AND relforcerowsecurity) <> 4 THEN
        RAISE EXCEPTION '122 verify: RLS not forced';
    END IF;
END $$;
