-- ============================================================================
-- Migration 111: Kriya IPD OS core (inpatient department)
-- Wards, beds, admissions, bed-assignment history, observations (vitals and
-- intake/output), clinical/nursing notes, orders, medication administration
-- (MAR), charges (proposed -> posted), discharge summaries, admission events.
--
-- Design: docs/ipd-os/ARCHITECTURE.md, DATA-MODEL.md. Builds on 103 (OPD OS):
-- reuses patients/family_members (MRN), doctors (registration), branches,
-- opd_next_counter/opd_format_number, and opd_invoices for the final bill.
--
-- Additive + backward compatible: pre-111 code runs unchanged (new columns
-- have defaults; no existing constraint is narrowed). Every state change of an
-- admission or a bed happens inside one SQL function that row-locks what it
-- touches, so a bed can never hold two active admissions and a transfer can
-- never leave a patient in two beds or none. Partial unique indexes back up
-- every invariant even if a future code path bypasses the functions.
-- ============================================================================

-- ── 1. clinics: IPD module state + settings ────────────────────────────────
-- OFF / ACTIVE / WIND_DOWN. WIND_DOWN = owner disabled IPD while patients were
-- still admitted: no new admissions, existing stays continue to discharge.
ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS ipd_state    TEXT  NOT NULL DEFAULT 'OFF',
    ADD COLUMN IF NOT EXISTS ipd_settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_ipd_state_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_ipd_state_check
    CHECK (ipd_state IN ('OFF', 'ACTIVE', 'WIND_DOWN') AND jsonb_typeof(ipd_settings) = 'object');

-- Composite FK target on the OPD encounter (an admission may come from one).
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_encounters_clinic_id_id ON opd_encounters (clinic_id, id);

-- ── 2. Helpers ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION ipd_purging(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.ipd_purge_clinic', true), '') = p_clinic_id::TEXT
$$;

-- Set by the state-changing ipd_* functions for one clinic, transaction-local.
CREATE OR REPLACE FUNCTION ipd_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.ipd_tx', true), '') = p_clinic_id::TEXT
$$;

CREATE OR REPLACE FUNCTION ipd_raise(p_code TEXT) RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '%', p_code USING ERRCODE = 'P0001'; END $$;

CREATE OR REPLACE FUNCTION ipd_ist_date(p_ts TIMESTAMPTZ) RETURNS DATE LANGUAGE sql IMMUTABLE AS $$
    SELECT (p_ts AT TIME ZONE INTERVAL '+05:30')::DATE
$$;

-- ── 3. Wards ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_wards (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    code                TEXT NOT NULL CHECK (code ~ '^[A-Za-z0-9_\-]{1,20}$'),
    name                TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
    ward_type           TEXT NOT NULL DEFAULT 'general' CHECK (ward_type IN (
                            'general', 'semi_private', 'private', 'icu', 'hdu', 'nicu', 'picu',
                            'maternity', 'isolation', 'daycare', 'emergency', 'other')),
    gender_restriction  TEXT NOT NULL DEFAULT 'any' CHECK (gender_restriction IN ('any', 'male', 'female')),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_wards_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_wards_code_key UNIQUE (clinic_id, code),
    CONSTRAINT ipd_wards_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);

-- ── 4. Beds ────────────────────────────────────────────────────────────────
-- Status is changed ONLY by the ipd_* functions below. occupied <=> an active
-- admission is in it; reserved <=> a pending admission holds it until a time.
CREATE TABLE IF NOT EXISTS ipd_beds (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    ward_id               UUID NOT NULL,
    code                  TEXT NOT NULL CHECK (code ~ '^[A-Za-z0-9_\-]{1,20}$'),
    room_label            TEXT NULL CHECK (char_length(room_label) <= 40),
    bed_category          TEXT NOT NULL CHECK (bed_category ~ '^[a-z0-9_]{2,30}$'),
    features              TEXT[] NOT NULL DEFAULT '{}' CHECK (cardinality(features) <= 20),
    status                TEXT NOT NULL DEFAULT 'available' CHECK (status IN (
                              'available', 'reserved', 'occupied', 'cleaning', 'maintenance', 'blocked')),
    status_reason         TEXT NULL CHECK (char_length(status_reason) <= 200),
    current_admission_id  UUID NULL,
    reserved_admission_id UUID NULL,
    reserved_until        TIMESTAMPTZ NULL,
    is_active             BOOLEAN NOT NULL DEFAULT true,
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_beds_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_beds_code_key UNIQUE (clinic_id, ward_id, code),
    CONSTRAINT ipd_beds_ward_fk FOREIGN KEY (clinic_id, ward_id) REFERENCES ipd_wards (clinic_id, id),
    CONSTRAINT ipd_beds_occupied_consistent CHECK ((status = 'occupied') = (current_admission_id IS NOT NULL)),
    CONSTRAINT ipd_beds_reserved_consistent CHECK (
        (status = 'reserved') = (reserved_admission_id IS NOT NULL AND reserved_until IS NOT NULL)),
    CONSTRAINT ipd_beds_inactive_not_in_use CHECK (is_active OR status NOT IN ('occupied', 'reserved'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_beds_current_admission
    ON ipd_beds (clinic_id, current_admission_id) WHERE current_admission_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_beds_reserved_admission
    ON ipd_beds (clinic_id, reserved_admission_id) WHERE reserved_admission_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_beds_ward ON ipd_beds (clinic_id, ward_id, status);

-- ── 5. Admissions (the IPD encounter) ──────────────────────────────────────
-- pending -> admitted -> discharge_pending -> discharged; pending -> cancelled;
-- discharge_pending -> admitted (reverted). A bed is held iff admitted or
-- discharge_pending.
CREATE TABLE IF NOT EXISTS ipd_admissions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id              UUID NULL,
    admission_year         SMALLINT NOT NULL,
    admission_seq          INTEGER  NOT NULL CHECK (admission_seq > 0),
    admission_number       TEXT GENERATED ALWAYS AS (opd_format_number('IPD-', admission_year, admission_seq, 5)) STORED,
    patient_id             UUID NOT NULL,
    family_member_id       UUID NULL,
    admitting_doctor_id    UUID NOT NULL,
    attending_doctor_id    UUID NOT NULL,
    source                 TEXT NOT NULL CHECK (source IN ('opd', 'emergency', 'direct', 'referral', 'transfer_in')),
    source_appointment_id  UUID NULL,
    source_encounter_id    UUID NULL,
    priority               TEXT NOT NULL DEFAULT 'elective' CHECK (priority IN ('elective', 'urgent', 'emergency')),
    reason                 TEXT NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 3 AND 500),
    provisional_diagnosis  TEXT NULL CHECK (char_length(provisional_diagnosis) <= 500),
    requested_bed_category TEXT NULL CHECK (requested_bed_category ~ '^[a-z0-9_]{2,30}$'),
    payer_type             TEXT NOT NULL DEFAULT 'self' CHECK (payer_type IN ('self', 'insurance', 'corporate', 'government_scheme')),
    payer_name             TEXT NULL CHECK (char_length(payer_name) <= 120),
    policy_number          TEXT NULL CHECK (char_length(policy_number) <= 60),
    attendant_name         TEXT NULL CHECK (char_length(attendant_name) <= 100),
    attendant_phone        TEXT NULL CHECK (attendant_phone ~ '^\+?[0-9]{10,15}$'),
    attendant_relation     TEXT NULL CHECK (char_length(attendant_relation) <= 40),
    consent_recorded       BOOLEAN NOT NULL DEFAULT false,
    status                 TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                               'pending', 'admitted', 'discharge_pending', 'discharged', 'cancelled')),
    current_bed_id         UUID NULL,
    admitted_at            TIMESTAMPTZ NULL,
    expected_discharge_on  DATE NULL,
    discharge_type         TEXT NULL CHECK (discharge_type IN ('regular', 'lama', 'dama', 'absconded', 'death', 'transfer_out')),
    discharge_initiated_at TIMESTAMPTZ NULL,
    discharge_initiated_by UUID NULL,
    discharged_at          TIMESTAMPTZ NULL,
    discharged_by          UUID NULL,
    settlement_waiver_reason TEXT NULL CHECK (char_length(settlement_waiver_reason) BETWEEN 5 AND 300),
    cancelled_at           TIMESTAMPTZ NULL,
    cancel_reason          TEXT NULL CHECK (char_length(cancel_reason) BETWEEN 3 AND 300),
    idempotency_key        TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_by             UUID NULL,
    created_by_name        TEXT NULL,
    version                INTEGER NOT NULL DEFAULT 1,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_admissions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_admissions_number_key UNIQUE (clinic_id, admission_year, admission_seq),
    CONSTRAINT ipd_admissions_bed_iff_active CHECK (
        (status IN ('admitted', 'discharge_pending')) = (current_bed_id IS NOT NULL)),
    CONSTRAINT ipd_admissions_admitted_at CHECK (
        status IN ('pending', 'cancelled') OR admitted_at IS NOT NULL),
    CONSTRAINT ipd_admissions_discharge_type CHECK (
        status NOT IN ('discharge_pending', 'discharged') OR discharge_type IS NOT NULL),
    CONSTRAINT ipd_admissions_discharged_complete CHECK (status <> 'discharged' OR discharged_at IS NOT NULL),
    CONSTRAINT ipd_admissions_cancelled_complete CHECK (
        status <> 'cancelled' OR (cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL)),
    CONSTRAINT ipd_admissions_patient_fk    FOREIGN KEY (clinic_id, patient_id)          REFERENCES patients (clinic_id, id),
    CONSTRAINT ipd_admissions_family_fk     FOREIGN KEY (clinic_id, family_member_id)    REFERENCES family_members (clinic_id, id),
    CONSTRAINT ipd_admissions_admitting_fk  FOREIGN KEY (clinic_id, admitting_doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ipd_admissions_attending_fk  FOREIGN KEY (clinic_id, attending_doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ipd_admissions_appt_fk       FOREIGN KEY (clinic_id, source_appointment_id) REFERENCES appointments (clinic_id, id),
    CONSTRAINT ipd_admissions_encounter_fk  FOREIGN KEY (clinic_id, source_encounter_id) REFERENCES opd_encounters (clinic_id, id),
    CONSTRAINT ipd_admissions_branch_fk     FOREIGN KEY (clinic_id, branch_id)           REFERENCES branches (clinic_id, id),
    CONSTRAINT ipd_admissions_bed_fk        FOREIGN KEY (clinic_id, current_bed_id)      REFERENCES ipd_beds (clinic_id, id)
);
-- one active stay per person (account holder or a dependant)
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_admissions_one_active
    ON ipd_admissions (clinic_id, patient_id, COALESCE(family_member_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE status IN ('pending', 'admitted', 'discharge_pending');
-- one active stay per bed (backs up ipd_beds.current_admission_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_admissions_one_per_bed
    ON ipd_admissions (clinic_id, current_bed_id) WHERE current_bed_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_admissions_idempotency
    ON ipd_admissions (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_admissions_status  ON ipd_admissions (clinic_id, status);
CREATE INDEX IF NOT EXISTS idx_ipd_admissions_patient ON ipd_admissions (clinic_id, patient_id, created_at DESC);

ALTER TABLE ipd_beds DROP CONSTRAINT IF EXISTS ipd_beds_current_admission_fk;
ALTER TABLE ipd_beds ADD  CONSTRAINT ipd_beds_current_admission_fk
    FOREIGN KEY (clinic_id, current_admission_id) REFERENCES ipd_admissions (clinic_id, id);
ALTER TABLE ipd_beds DROP CONSTRAINT IF EXISTS ipd_beds_reserved_admission_fk;
ALTER TABLE ipd_beds ADD  CONSTRAINT ipd_beds_reserved_admission_fk
    FOREIGN KEY (clinic_id, reserved_admission_id) REFERENCES ipd_admissions (clinic_id, id);

-- ── 6. Bed-assignment history (the chronology of the stay) ─────────────────
CREATE TABLE IF NOT EXISTS ipd_bed_assignments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id    UUID NOT NULL,
    bed_id          UUID NOT NULL,
    ward_id         UUID NOT NULL,
    bed_category    TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ NULL,
    end_reason      TEXT NULL CHECK (end_reason IN ('transfer', 'discharge')),
    transfer_reason TEXT NULL CHECK (char_length(transfer_reason) <= 300),
    assigned_by     UUID NULL,
    ended_by        UUID NULL,
    CONSTRAINT ipd_bed_assignments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_bed_assignments_end_consistent CHECK ((ended_at IS NULL) = (end_reason IS NULL)),
    CONSTRAINT ipd_bed_assignments_order CHECK (ended_at IS NULL OR ended_at >= started_at),
    CONSTRAINT ipd_bed_assignments_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_bed_assignments_bed_fk       FOREIGN KEY (clinic_id, bed_id)       REFERENCES ipd_beds (clinic_id, id),
    CONSTRAINT ipd_bed_assignments_ward_fk      FOREIGN KEY (clinic_id, ward_id)      REFERENCES ipd_wards (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_bed_assignments_open_bed
    ON ipd_bed_assignments (clinic_id, bed_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_bed_assignments_open_admission
    ON ipd_bed_assignments (clinic_id, admission_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_bed_assignments_admission ON ipd_bed_assignments (clinic_id, admission_id, started_at);

-- ── 7. Admission events (append-only timeline, written inside the functions) ─
CREATE TABLE IF NOT EXISTS ipd_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id UUID NULL,
    bed_id       UUID NULL,
    event_type   TEXT NOT NULL CHECK (event_type ~ '^[a-z_]{3,40}$'),
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id     UUID NULL,
    actor_name   TEXT NULL,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ipd_events_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_events_bed_fk       FOREIGN KEY (clinic_id, bed_id)       REFERENCES ipd_beds (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_ipd_events_admission ON ipd_events (clinic_id, admission_id, occurred_at);

-- ── 8. Observations: vitals + intake/output. Never edited: an error is marked
-- entered_in_error and (optionally) replaced by a new row that points at it.
CREATE TABLE IF NOT EXISTS ipd_observations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id        UUID NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('vitals', 'intake', 'output')),
    observed_at         TIMESTAMPTZ NOT NULL,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_by         UUID NULL,
    recorded_by_name    TEXT NOT NULL CHECK (char_length(recorded_by_name) BETWEEN 1 AND 100),
    -- vitals: same bounds as OPD (typo guards, not clinical thresholds)
    bp_systolic         SMALLINT NULL CHECK (bp_systolic  BETWEEN 50 AND 300),
    bp_diastolic        SMALLINT NULL CHECK (bp_diastolic BETWEEN 20 AND 200),
    pulse_bpm           SMALLINT NULL CHECK (pulse_bpm    BETWEEN 20 AND 250),
    resp_rate           SMALLINT NULL CHECK (resp_rate    BETWEEN 4 AND 80),
    temperature_c       NUMERIC(4,1) NULL CHECK (temperature_c BETWEEN 30 AND 45),
    spo2_pct            SMALLINT NULL CHECK (spo2_pct     BETWEEN 50 AND 100),
    pain_score          SMALLINT NULL CHECK (pain_score   BETWEEN 0 AND 10),
    blood_glucose_mg_dl SMALLINT NULL CHECK (blood_glucose_mg_dl BETWEEN 10 AND 1500),
    consciousness       TEXT NULL CHECK (consciousness IN ('alert', 'confused', 'voice', 'pain', 'unresponsive')),
    -- intake / output
    io_route            TEXT NULL CHECK (io_route IN ('oral', 'iv', 'ng_tube', 'urine', 'drain', 'vomitus', 'stool', 'other')),
    volume_ml           INTEGER NULL CHECK (volume_ml BETWEEN 1 AND 10000),
    note                TEXT NULL CHECK (char_length(note) <= 500),
    status              TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded', 'entered_in_error')),
    corrects_id         UUID NULL,
    error_reason        TEXT NULL CHECK (char_length(error_reason) BETWEEN 3 AND 300),
    errored_at          TIMESTAMPTZ NULL,
    errored_by          UUID NULL,
    CONSTRAINT ipd_observations_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_observations_not_future CHECK (observed_at <= recorded_at + interval '5 minutes'),
    CONSTRAINT ipd_observations_not_stale  CHECK (observed_at >= recorded_at - interval '7 days'),
    CONSTRAINT ipd_observations_vitals_shape CHECK (kind <> 'vitals' OR (
        volume_ml IS NULL AND io_route IS NULL AND num_nonnulls(bp_systolic, pulse_bpm, resp_rate, temperature_c,
            spo2_pct, pain_score, blood_glucose_mg_dl, consciousness) > 0)),
    CONSTRAINT ipd_observations_bp_pair CHECK ((bp_systolic IS NULL) = (bp_diastolic IS NULL)),
    CONSTRAINT ipd_observations_io_shape CHECK (kind = 'vitals' OR (volume_ml IS NOT NULL AND io_route IS NOT NULL)),
    CONSTRAINT ipd_observations_error_complete CHECK (
        (status = 'entered_in_error') = (error_reason IS NOT NULL AND errored_at IS NOT NULL)),
    CONSTRAINT ipd_observations_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_observations_corrects_fk  FOREIGN KEY (clinic_id, corrects_id)  REFERENCES ipd_observations (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_ipd_observations_admission ON ipd_observations (clinic_id, admission_id, observed_at DESC);

-- ── 9. Notes: nursing / progress (doctor round) / handover / procedure / plan.
-- Authenticated authorship is the signature. Final notes are immutable; an
-- amendment is a new row (amends_id) and the old one becomes 'amended'.
CREATE TABLE IF NOT EXISTS ipd_notes (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id      UUID NOT NULL,
    note_type         TEXT NOT NULL CHECK (note_type IN ('nursing', 'progress', 'handover', 'procedure', 'plan')),
    body              TEXT NOT NULL CHECK (char_length(btrim(body)) BETWEEN 1 AND 8000),
    author_id         UUID NULL,
    author_name       TEXT NOT NULL CHECK (char_length(author_name) BETWEEN 1 AND 100),
    author_doctor_id  UUID NULL,
    authored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    status            TEXT NOT NULL DEFAULT 'final' CHECK (status IN ('final', 'amended', 'entered_in_error')),
    amends_id         UUID NULL,
    amend_reason      TEXT NULL CHECK (char_length(amend_reason) BETWEEN 3 AND 300),
    error_reason      TEXT NULL CHECK (char_length(error_reason) BETWEEN 3 AND 300),
    CONSTRAINT ipd_notes_clinic_id_id_key UNIQUE (clinic_id, id),
    -- clinician notes need a login linked to a doctor of this clinic
    CONSTRAINT ipd_notes_doctor_types CHECK (note_type NOT IN ('progress', 'procedure', 'plan') OR author_doctor_id IS NOT NULL),
    CONSTRAINT ipd_notes_amend_complete CHECK ((amends_id IS NULL) = (amend_reason IS NULL)),
    CONSTRAINT ipd_notes_error_complete CHECK ((status = 'entered_in_error') = (error_reason IS NOT NULL)),
    CONSTRAINT ipd_notes_admission_fk FOREIGN KEY (clinic_id, admission_id)     REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_notes_doctor_fk    FOREIGN KEY (clinic_id, author_doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ipd_notes_amends_fk    FOREIGN KEY (clinic_id, amends_id)        REFERENCES ipd_notes (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_ipd_notes_admission ON ipd_notes (clinic_id, admission_id, authored_at DESC);

-- ── 10. Orders (doctor-authored) and medication administration (nurse) ─────
CREATE TABLE IF NOT EXISTS ipd_orders (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id         UUID NOT NULL,
    order_type           TEXT NOT NULL CHECK (order_type IN ('medication', 'investigation', 'procedure', 'diet', 'nursing', 'other')),
    description          TEXT NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 300),
    drug_name            TEXT NULL CHECK (char_length(drug_name) <= 120),
    dose                 TEXT NULL CHECK (char_length(dose) <= 60),
    route                TEXT NULL CHECK (route IN ('oral', 'iv', 'im', 'sc', 'topical', 'inhalation', 'pr', 'sl', 'other')),
    frequency            TEXT NULL CHECK (char_length(frequency) <= 60),
    is_prn               BOOLEAN NOT NULL DEFAULT false,
    instructions         TEXT NULL CHECK (char_length(instructions) <= 500),
    lab_test_id          UUID NULL,   -- reference only (lab_tests); results stay in the lab system
    priority             TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine', 'urgent', 'stat')),
    ordered_by_doctor_id UUID NOT NULL,
    ordered_by           UUID NULL,
    ordered_by_name      TEXT NOT NULL,
    ordered_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    status               TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'discontinued')),
    ended_at             TIMESTAMPTZ NULL,
    ended_by             UUID NULL,
    end_reason           TEXT NULL CHECK (char_length(end_reason) <= 300),
    idempotency_key      TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    CONSTRAINT ipd_orders_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_orders_medication_shape CHECK (order_type <> 'medication' OR (
        drug_name IS NOT NULL AND dose IS NOT NULL AND route IS NOT NULL AND frequency IS NOT NULL)),
    CONSTRAINT ipd_orders_end_consistent CHECK ((status = 'active') = (ended_at IS NULL)),
    CONSTRAINT ipd_orders_discontinue_reason CHECK (status <> 'discontinued' OR end_reason IS NOT NULL),
    CONSTRAINT ipd_orders_admission_fk FOREIGN KEY (clinic_id, admission_id)         REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_orders_doctor_fk    FOREIGN KEY (clinic_id, ordered_by_doctor_id) REFERENCES doctors (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_orders_idempotency
    ON ipd_orders (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_orders_admission ON ipd_orders (clinic_id, admission_id, status);

CREATE TABLE IF NOT EXISTS ipd_med_administrations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id     UUID NOT NULL,
    order_id         UUID NOT NULL,
    outcome          TEXT NOT NULL CHECK (outcome IN ('given', 'omitted', 'refused', 'delayed', 'held')),
    administered_at  TIMESTAMPTZ NOT NULL,
    dose_given       TEXT NULL CHECK (char_length(dose_given) <= 60),
    reason           TEXT NULL CHECK (char_length(reason) <= 300),
    recorded_by      UUID NULL,
    recorded_by_name TEXT NOT NULL,
    recorded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded', 'entered_in_error')),
    error_reason     TEXT NULL CHECK (char_length(error_reason) BETWEEN 3 AND 300),
    errored_at       TIMESTAMPTZ NULL,
    errored_by       UUID NULL,
    idempotency_key  TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    CONSTRAINT ipd_mar_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_mar_reason_when_not_given CHECK (outcome = 'given' OR char_length(btrim(COALESCE(reason, ''))) >= 3),
    CONSTRAINT ipd_mar_not_future CHECK (administered_at <= recorded_at + interval '5 minutes'),
    CONSTRAINT ipd_mar_error_complete CHECK ((status = 'entered_in_error') = (error_reason IS NOT NULL)),
    CONSTRAINT ipd_mar_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_mar_order_fk     FOREIGN KEY (clinic_id, order_id)     REFERENCES ipd_orders (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_mar_idempotency
    ON ipd_med_administrations (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_mar_order ON ipd_med_administrations (clinic_id, order_id, administered_at DESC);

-- ── 11. Charges: proposed -> posted -> billed (stamped with the final bill) ─
-- The final bill IS an opd_invoices row (numbering, receipts, shifts, payment
-- links reused). opd_invoices.admission_id links it back.
ALTER TABLE opd_invoices ADD COLUMN IF NOT EXISTS admission_id UUID NULL;
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_admission_fk;
ALTER TABLE opd_invoices ADD  CONSTRAINT opd_invoices_admission_fk
    FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_invoices_one_per_admission
    ON opd_invoices (clinic_id, admission_id) WHERE admission_id IS NOT NULL AND status <> 'void';

CREATE TABLE IF NOT EXISTS ipd_charges (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id     UUID NOT NULL,
    category         TEXT NOT NULL CHECK (category IN ('bed', 'nursing', 'doctor_visit', 'procedure', 'investigation',
                                                       'pharmacy', 'consumable', 'package', 'other')),
    description      TEXT NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 200),
    service_date     DATE NOT NULL,
    quantity         SMALLINT NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 999),
    unit_price_paise INTEGER NOT NULL CHECK (unit_price_paise BETWEEN 0 AND 100000000),
    total_paise      INTEGER GENERATED ALWAYS AS (quantity * unit_price_paise) STORED,
    source           TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'bed_day', 'order')),
    source_ref       TEXT NULL CHECK (char_length(source_ref) <= 120),
    status           TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'posted', 'void')),
    created_by       UUID NULL,
    created_by_name  TEXT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_at        TIMESTAMPTZ NULL,
    posted_by        UUID NULL,
    voided_at        TIMESTAMPTZ NULL,
    voided_by        UUID NULL,
    void_reason      TEXT NULL CHECK (char_length(void_reason) BETWEEN 3 AND 300),
    invoice_id       UUID NULL,
    CONSTRAINT ipd_charges_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_charges_posted_complete CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    CONSTRAINT ipd_charges_void_complete CHECK ((status = 'void') = (voided_at IS NOT NULL AND void_reason IS NOT NULL)),
    CONSTRAINT ipd_charges_billed_posted CHECK (invoice_id IS NULL OR status = 'posted'),
    CONSTRAINT ipd_charges_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_charges_invoice_fk   FOREIGN KEY (clinic_id, invoice_id)   REFERENCES opd_invoices (clinic_id, id)
);
-- retries/accruals can never create the same charge twice
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_charges_source_ref
    ON ipd_charges (clinic_id, admission_id, source_ref) WHERE source_ref IS NOT NULL AND status <> 'void';
CREATE INDEX IF NOT EXISTS idx_ipd_charges_admission ON ipd_charges (clinic_id, admission_id, status);

-- ── 12. Discharge summaries (doctor-signed; signed rows immutable) ─────────
CREATE TABLE IF NOT EXISTS ipd_discharge_summaries (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id              UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id           UUID NOT NULL,
    version                SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id          UUID NULL,
    status                 TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed', 'superseded')),
    final_diagnosis        TEXT NULL CHECK (char_length(final_diagnosis) <= 2000),
    presenting_complaints  TEXT NULL CHECK (char_length(presenting_complaints) <= 4000),
    course_in_hospital     TEXT NULL CHECK (char_length(course_in_hospital) <= 8000),
    procedures_performed   TEXT NULL CHECK (char_length(procedures_performed) <= 4000),
    condition_at_discharge TEXT NULL CHECK (char_length(condition_at_discharge) <= 2000),
    discharge_medications  TEXT NULL CHECK (char_length(discharge_medications) <= 4000),
    follow_up              TEXT NULL CHECK (char_length(follow_up) <= 2000),
    advice                 TEXT NULL CHECK (char_length(advice) <= 4000),
    prepared_by            UUID NULL,
    prepared_by_name       TEXT NULL,
    signed_by_doctor_id    UUID NULL,
    signed_by              UUID NULL,
    signed_at              TIMESTAMPTZ NULL,
    doctor_snapshot        JSONB NULL,
    amend_reason           TEXT NULL CHECK (char_length(amend_reason) BETWEEN 3 AND 300),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_discharge_summaries_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_ds_signed_complete CHECK (status = 'draft' OR (
        signed_by_doctor_id IS NOT NULL AND signed_at IS NOT NULL AND final_diagnosis IS NOT NULL)),
    CONSTRAINT ipd_ds_admission_fk  FOREIGN KEY (clinic_id, admission_id)        REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_ds_doctor_fk     FOREIGN KEY (clinic_id, signed_by_doctor_id) REFERENCES doctors (clinic_id, id),
    CONSTRAINT ipd_ds_supersedes_fk FOREIGN KEY (clinic_id, supersedes_id)       REFERENCES ipd_discharge_summaries (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_ds_one_draft
    ON ipd_discharge_summaries (clinic_id, admission_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_ds_one_signed
    ON ipd_discharge_summaries (clinic_id, admission_id) WHERE status = 'signed';

-- ── 13. Guard triggers (fire for service_role too; RLS does not) ───────────

CREATE OR REPLACE FUNCTION ipd_touch_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- Bed state changes only inside the ipd_* functions. Config edits (room,
-- category, features, is_active) stay open to the API, guarded by CHECKs.
CREATE OR REPLACE FUNCTION ipd_guard_bed() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        IF EXISTS (SELECT 1 FROM ipd_bed_assignments WHERE clinic_id = OLD.clinic_id AND bed_id = OLD.id) THEN
            PERFORM ipd_raise('ipd_bed_has_history');
        END IF;
        IF OLD.status IN ('occupied', 'reserved') THEN PERFORM ipd_raise('ipd_bed_in_use'); END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.ward_id <> OLD.ward_id THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_beds');
    END IF;
    IF (NEW.status, NEW.current_admission_id, NEW.reserved_admission_id, NEW.reserved_until)
       IS DISTINCT FROM (OLD.status, OLD.current_admission_id, OLD.reserved_admission_id, OLD.reserved_until)
       AND NOT ipd_in_tx(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_bed_state_via_function_only');
    END IF;
    IF OLD.status IN ('occupied', 'reserved') AND NEW.bed_category <> OLD.bed_category THEN
        PERFORM ipd_raise('ipd_bed_in_use');
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION ipd_guard_admission() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:ipd_admissions');
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.family_member_id IS DISTINCT FROM OLD.family_member_id
       OR NEW.admission_year <> OLD.admission_year OR NEW.admission_seq <> OLD.admission_seq
       OR NEW.admitting_doctor_id <> OLD.admitting_doctor_id OR NEW.created_at <> OLD.created_at THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_admissions');
    END IF;
    IF OLD.status IN ('discharged', 'cancelled') AND NOT ipd_purging(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_record_locked:ipd_admissions');
    END IF;
    IF (NEW.status, NEW.current_bed_id, NEW.admitted_at, NEW.discharged_at, NEW.discharge_type)
       IS DISTINCT FROM (OLD.status, OLD.current_bed_id, OLD.admitted_at, OLD.discharged_at, OLD.discharge_type)
       AND NOT ipd_in_tx(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_admission_state_via_function_only');
    END IF;
    NEW.version := OLD.version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- Append-only tables (events): no UPDATE, DELETE only during purge.
CREATE OR REPLACE FUNCTION ipd_guard_append_only() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
    PERFORM ipd_raise('ipd_append_only:' || TG_TABLE_NAME);
    RETURN NULL;
END $$;

-- Assignments: only ended_at / end_reason / ended_by may be set, once.
CREATE OR REPLACE FUNCTION ipd_guard_assignment() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:ipd_bed_assignments');
    END IF;
    IF OLD.ended_at IS NOT NULL
       OR (NEW.id, NEW.clinic_id, NEW.admission_id, NEW.bed_id, NEW.ward_id, NEW.bed_category, NEW.started_at, NEW.assigned_by)
          IS DISTINCT FROM (OLD.id, OLD.clinic_id, OLD.admission_id, OLD.bed_id, OLD.ward_id, OLD.bed_category, OLD.started_at, OLD.assigned_by)
       OR NOT ipd_in_tx(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_record_locked:ipd_bed_assignments');
    END IF;
    RETURN NEW;
END $$;

-- Clinical records (observations, MAR, notes): content immutable; only the
-- status may move forward with its reason fields.
CREATE OR REPLACE FUNCTION ipd_guard_clinical_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_old JSONB; v_new JSONB; k TEXT; v_open TEXT;
        v_mutable TEXT[] := ARRAY['status', 'error_reason', 'errored_at', 'errored_by'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:' || TG_TABLE_NAME);
    END IF;
    v_open := (CASE WHEN TG_TABLE_NAME = 'ipd_notes' THEN 'final' ELSE 'recorded' END);
    IF OLD.status <> v_open THEN
        PERFORM ipd_raise('ipd_record_locked:' || TG_TABLE_NAME);
    END IF;
    v_old := to_jsonb(OLD); v_new := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(v_old) LOOP
        IF NOT (k = ANY (v_mutable)) AND v_old -> k IS DISTINCT FROM v_new -> k THEN
            PERFORM ipd_raise('ipd_record_locked:' || TG_TABLE_NAME || ':' || k);
        END IF;
    END LOOP;
    RETURN NEW;
END $$;

-- MAR insert: the order must be an active medication order of the SAME
-- admission, and the patient must still be in a bed.
CREATE OR REPLACE FUNCTION ipd_check_mar_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_type TEXT; v_status TEXT; v_adm UUID; v_adm_status TEXT;
BEGIN
    SELECT order_type, status, admission_id INTO v_type, v_status, v_adm
      FROM ipd_orders WHERE id = NEW.order_id AND clinic_id = NEW.clinic_id;
    IF v_adm IS DISTINCT FROM NEW.admission_id THEN PERFORM ipd_raise('ipd_order_not_found'); END IF;
    IF v_type <> 'medication' THEN PERFORM ipd_raise('ipd_order_not_medication'); END IF;
    IF v_status <> 'active' THEN PERFORM ipd_raise('ipd_order_not_active'); END IF;
    SELECT status INTO v_adm_status FROM ipd_admissions WHERE id = NEW.admission_id AND clinic_id = NEW.clinic_id;
    IF v_adm_status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    RETURN NEW;
END $$;

-- Writes on a stay (observations, notes, orders, charges) only while it holds a bed.
CREATE OR REPLACE FUNCTION ipd_check_admission_active() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM ipd_admissions WHERE id = NEW.admission_id AND clinic_id = NEW.clinic_id;
    IF v_status IS NULL THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    IF v_status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    RETURN NEW;
END $$;

-- Orders: content immutable; active -> completed/discontinued once.
CREATE OR REPLACE FUNCTION ipd_guard_order() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_old JSONB; v_new JSONB; k TEXT;
        v_mutable TEXT[] := ARRAY['status', 'ended_at', 'ended_by', 'end_reason'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:ipd_orders');
    END IF;
    IF OLD.status <> 'active' THEN PERFORM ipd_raise('ipd_record_locked:ipd_orders'); END IF;
    v_old := to_jsonb(OLD); v_new := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(v_old) LOOP
        IF NOT (k = ANY (v_mutable)) AND v_old -> k IS DISTINCT FROM v_new -> k THEN
            PERFORM ipd_raise('ipd_record_locked:ipd_orders:' || k);
        END IF;
    END LOOP;
    RETURN NEW;
END $$;

-- Charges: proposed -> posted | void; posted -> void (unbilled only) | billed
-- (invoice stamp, inside ipd_generate_final_bill); billed is final unless its
-- invoice was voided (then the stamp may be cleared). Amounts never change.
CREATE OR REPLACE FUNCTION ipd_guard_charge() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_inv_status TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:ipd_charges');
    END IF;
    IF (NEW.id, NEW.clinic_id, NEW.admission_id, NEW.category, NEW.description, NEW.service_date, NEW.quantity,
        NEW.unit_price_paise, NEW.source, NEW.source_ref, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.clinic_id, OLD.admission_id, OLD.category, OLD.description, OLD.service_date, OLD.quantity,
        OLD.unit_price_paise, OLD.source, OLD.source_ref, OLD.created_at) THEN
        PERFORM ipd_raise('ipd_record_locked:ipd_charges');
    END IF;
    IF OLD.status = 'void' THEN PERFORM ipd_raise('ipd_record_locked:ipd_charges'); END IF;
    IF OLD.invoice_id IS NOT NULL THEN
        SELECT status INTO v_inv_status FROM opd_invoices WHERE id = OLD.invoice_id AND clinic_id = OLD.clinic_id;
        IF NOT (v_inv_status = 'void' AND NEW.invoice_id IS NULL AND NEW.status = OLD.status) THEN
            PERFORM ipd_raise('ipd_charge_billed');
        END IF;
    ELSIF NEW.invoice_id IS NOT NULL AND NOT ipd_in_tx(OLD.clinic_id) THEN
        PERFORM ipd_raise('ipd_charge_bill_via_function_only');
    END IF;
    IF OLD.status = 'posted' AND NEW.status = 'proposed' THEN PERFORM ipd_raise('ipd_bad_transition:posted->proposed'); END IF;
    RETURN NEW;
END $$;

-- Discharge summary: drafts editable; signed only -> superseded (sign RPC).
CREATE OR REPLACE FUNCTION ipd_guard_discharge_summary() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'draft' OR ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.admission_id <> OLD.admission_id OR NEW.version <> OLD.version
       OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        PERFORM ipd_raise('ipd_identity_immutable:ipd_discharge_summaries');
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'signed' AND NOT ipd_in_tx(OLD.clinic_id) THEN
            PERFORM ipd_raise('ipd_sign_via_function_only');
        END IF;
        IF NEW.status = 'superseded' THEN PERFORM ipd_raise('ipd_bad_transition:draft->superseded'); END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' AND NEW.status = 'superseded' AND ipd_in_tx(OLD.clinic_id)
       AND (to_jsonb(NEW) - 'status' - 'updated_at') = (to_jsonb(OLD) - 'status' - 'updated_at') THEN
        NEW.updated_at := now();
        RETURN NEW;
    END IF;
    PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries');
    RETURN NULL;
END $$;

-- The final bill's lines come only from posted charges (ipd_generate_final_bill);
-- the OPD billing page cannot edit them.
CREATE OR REPLACE FUNCTION ipd_guard_bill_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id); v_adm UUID;
BEGIN
    IF opd_purging(v_clinic) OR ipd_in_tx(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT admission_id INTO v_adm FROM opd_invoices
     WHERE id = COALESCE(NEW.invoice_id, OLD.invoice_id) AND clinic_id = v_clinic;
    IF v_adm IS NOT NULL THEN PERFORM ipd_raise('ipd_bill_lines_locked'); END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_ipd_wards_touch ON ipd_wards;
CREATE TRIGGER trg_ipd_wards_touch BEFORE UPDATE ON ipd_wards FOR EACH ROW EXECUTE FUNCTION ipd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_ipd_beds_guard ON ipd_beds;
CREATE TRIGGER trg_ipd_beds_guard BEFORE UPDATE OR DELETE ON ipd_beds FOR EACH ROW EXECUTE FUNCTION ipd_guard_bed();
DROP TRIGGER IF EXISTS trg_ipd_admissions_guard ON ipd_admissions;
CREATE TRIGGER trg_ipd_admissions_guard BEFORE UPDATE OR DELETE ON ipd_admissions FOR EACH ROW EXECUTE FUNCTION ipd_guard_admission();
DROP TRIGGER IF EXISTS trg_ipd_assignments_guard ON ipd_bed_assignments;
CREATE TRIGGER trg_ipd_assignments_guard BEFORE UPDATE OR DELETE ON ipd_bed_assignments FOR EACH ROW EXECUTE FUNCTION ipd_guard_assignment();
DROP TRIGGER IF EXISTS trg_ipd_events_guard ON ipd_events;
CREATE TRIGGER trg_ipd_events_guard BEFORE UPDATE OR DELETE ON ipd_events FOR EACH ROW EXECUTE FUNCTION ipd_guard_append_only();
DROP TRIGGER IF EXISTS trg_ipd_observations_guard ON ipd_observations;
CREATE TRIGGER trg_ipd_observations_guard BEFORE UPDATE OR DELETE ON ipd_observations FOR EACH ROW EXECUTE FUNCTION ipd_guard_clinical_record();
DROP TRIGGER IF EXISTS trg_ipd_observations_active ON ipd_observations;
CREATE TRIGGER trg_ipd_observations_active BEFORE INSERT ON ipd_observations FOR EACH ROW EXECUTE FUNCTION ipd_check_admission_active();
DROP TRIGGER IF EXISTS trg_ipd_notes_guard ON ipd_notes;
CREATE TRIGGER trg_ipd_notes_guard BEFORE UPDATE OR DELETE ON ipd_notes FOR EACH ROW EXECUTE FUNCTION ipd_guard_clinical_record();
DROP TRIGGER IF EXISTS trg_ipd_notes_active ON ipd_notes;
CREATE TRIGGER trg_ipd_notes_active BEFORE INSERT ON ipd_notes FOR EACH ROW EXECUTE FUNCTION ipd_check_admission_active();
DROP TRIGGER IF EXISTS trg_ipd_orders_guard ON ipd_orders;
CREATE TRIGGER trg_ipd_orders_guard BEFORE UPDATE OR DELETE ON ipd_orders FOR EACH ROW EXECUTE FUNCTION ipd_guard_order();
DROP TRIGGER IF EXISTS trg_ipd_orders_active ON ipd_orders;
CREATE TRIGGER trg_ipd_orders_active BEFORE INSERT ON ipd_orders FOR EACH ROW EXECUTE FUNCTION ipd_check_admission_active();
DROP TRIGGER IF EXISTS trg_ipd_mar_guard ON ipd_med_administrations;
CREATE TRIGGER trg_ipd_mar_guard BEFORE UPDATE OR DELETE ON ipd_med_administrations FOR EACH ROW EXECUTE FUNCTION ipd_guard_clinical_record();
DROP TRIGGER IF EXISTS trg_ipd_mar_insert ON ipd_med_administrations;
CREATE TRIGGER trg_ipd_mar_insert BEFORE INSERT ON ipd_med_administrations FOR EACH ROW EXECUTE FUNCTION ipd_check_mar_insert();
DROP TRIGGER IF EXISTS trg_ipd_charges_guard ON ipd_charges;
CREATE TRIGGER trg_ipd_charges_guard BEFORE UPDATE OR DELETE ON ipd_charges FOR EACH ROW EXECUTE FUNCTION ipd_guard_charge();
DROP TRIGGER IF EXISTS trg_ipd_charges_active ON ipd_charges;
CREATE TRIGGER trg_ipd_charges_active BEFORE INSERT ON ipd_charges FOR EACH ROW EXECUTE FUNCTION ipd_check_admission_active();
DROP TRIGGER IF EXISTS trg_ipd_ds_guard ON ipd_discharge_summaries;
CREATE TRIGGER trg_ipd_ds_guard BEFORE UPDATE OR DELETE ON ipd_discharge_summaries FOR EACH ROW EXECUTE FUNCTION ipd_guard_discharge_summary();
DROP TRIGGER IF EXISTS trg_ipd_bill_items_guard ON opd_invoice_items;
CREATE TRIGGER trg_ipd_bill_items_guard BEFORE INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_bill_items();

-- ── 14. State-changing functions ───────────────────────────────────────────
-- Conventions: every function takes p_clinic_id and treats ids of another
-- clinic as not found; errors are P0001 with an 'ipd_*' code the API maps to
-- 4xx; lock order is admission row first, then bed rows by id.

CREATE OR REPLACE FUNCTION ipd_log(p_clinic UUID, p_admission UUID, p_bed UUID, p_type TEXT,
                                   p_actor UUID, p_actor_name TEXT, p_details JSONB)
RETURNS VOID LANGUAGE sql AS $$
    INSERT INTO ipd_events (clinic_id, admission_id, bed_id, event_type, actor_id, actor_name, details)
    VALUES (p_clinic, p_admission, p_bed, p_type, p_actor, p_actor_name, COALESCE(p_details, '{}'::jsonb))
$$;

CREATE OR REPLACE FUNCTION ipd_lock_admission(p_clinic UUID, p_admission UUID)
RETURNS ipd_admissions LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE id = p_admission AND clinic_id = p_clinic FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    RETURN a;
END $$;

-- Lock a bed and check it can take this admission now. Expired reservations
-- count as free; a reservation held for this admission is honoured.
CREATE OR REPLACE FUNCTION ipd_lock_eligible_bed(p_clinic UUID, p_bed UUID, p_admission UUID)
RETURNS ipd_beds LANGUAGE plpgsql AS $$
DECLARE b ipd_beds; w ipd_wards; a ipd_admissions; v_gender TEXT;
BEGIN
    SELECT * INTO b FROM ipd_beds WHERE id = p_bed AND clinic_id = p_clinic FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_bed_not_found'); END IF;
    SELECT * INTO w FROM ipd_wards WHERE id = b.ward_id AND clinic_id = p_clinic;
    IF NOT b.is_active OR NOT w.is_active THEN PERFORM ipd_raise('ipd_bed_ineligible:inactive'); END IF;
    IF NOT (b.status = 'available'
            OR (b.status = 'reserved' AND (b.reserved_admission_id = p_admission OR b.reserved_until < now()))) THEN
        PERFORM ipd_raise('ipd_bed_unavailable:' || b.status);
    END IF;
    SELECT * INTO a FROM ipd_admissions WHERE id = p_admission AND clinic_id = p_clinic;
    IF a.branch_id IS NOT NULL AND w.branch_id IS NOT NULL AND a.branch_id <> w.branch_id THEN
        PERFORM ipd_raise('ipd_bed_ineligible:branch');
    END IF;
    IF w.gender_restriction <> 'any' THEN
        IF a.family_member_id IS NOT NULL THEN
            SELECT gender INTO v_gender FROM family_members WHERE id = a.family_member_id AND clinic_id = p_clinic;
        ELSE
            SELECT gender INTO v_gender FROM patients WHERE id = a.patient_id AND clinic_id = p_clinic;
        END IF;
        IF v_gender IS DISTINCT FROM w.gender_restriction THEN
            PERFORM ipd_raise(CASE WHEN v_gender IS NULL OR v_gender = 'undisclosed'
                                   THEN 'ipd_bed_ineligible:gender_unknown' ELSE 'ipd_bed_ineligible:gender' END);
        END IF;
    END IF;
    RETURN b;
END $$;

-- Occupy a locked, eligible bed. The caller holds the admission lock.
CREATE OR REPLACE FUNCTION ipd_occupy_bed(p_clinic UUID, p_admission UUID, b ipd_beds, p_actor UUID,
                                         p_reason TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    -- this admission's reservation on some OTHER bed is released first
    UPDATE ipd_beds SET status = 'available', reserved_admission_id = NULL, reserved_until = NULL
     WHERE clinic_id = p_clinic AND reserved_admission_id = p_admission AND id <> b.id;
    -- an expired reservation of another pending admission is released here too
    UPDATE ipd_beds SET status = 'occupied', current_admission_id = p_admission,
                        reserved_admission_id = NULL, reserved_until = NULL, status_reason = NULL
     WHERE id = b.id AND clinic_id = p_clinic;
    INSERT INTO ipd_bed_assignments (clinic_id, admission_id, bed_id, ward_id, bed_category, assigned_by, transfer_reason)
    VALUES (p_clinic, p_admission, b.id, b.ward_id, b.bed_category, p_actor, p_reason);
END $$;

CREATE OR REPLACE FUNCTION ipd_admit(p_clinic_id UUID, p_payload JSONB, p_bed_id UUID,
                                     p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; b ipd_beds; v_year SMALLINT; v_key TEXT := NULLIF(p_payload ->> 'idempotency_key', '');
        v_constraint TEXT; v_fm UUID := NULLIF(p_payload ->> 'family_member_id', '')::UUID;
        v_patient UUID := (p_payload ->> 'patient_id')::UUID;
        v_admitting UUID := (p_payload ->> 'admitting_doctor_id')::UUID;
        v_attending UUID := COALESCE(NULLIF(p_payload ->> 'attending_doctor_id', '')::UUID,
                                     (p_payload ->> 'admitting_doctor_id')::UUID);
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    IF v_key IS NOT NULL THEN
        SELECT * INTO a FROM ipd_admissions WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
        IF FOUND THEN RETURN to_jsonb(a) || jsonb_build_object('replayed', true); END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM patients WHERE id = v_patient AND clinic_id = p_clinic_id) THEN
        PERFORM ipd_raise('ipd_patient_not_found');
    END IF;
    IF v_fm IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM family_members fm JOIN patients p ON p.clinic_id = fm.clinic_id AND p.phone = fm.primary_phone
         WHERE fm.id = v_fm AND fm.clinic_id = p_clinic_id AND p.id = v_patient) THEN
        PERFORM ipd_raise('ipd_family_member_not_found');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM doctors WHERE id = v_admitting AND clinic_id = p_clinic_id AND is_active IS NOT FALSE)
       OR NOT EXISTS (SELECT 1 FROM doctors WHERE id = v_attending AND clinic_id = p_clinic_id AND is_active IS NOT FALSE) THEN
        PERFORM ipd_raise('ipd_doctor_not_found');
    END IF;
    v_year := EXTRACT(YEAR FROM (now() AT TIME ZONE INTERVAL '+05:30'))::SMALLINT;
    BEGIN
        INSERT INTO ipd_admissions (
            clinic_id, branch_id, admission_year, admission_seq, patient_id, family_member_id,
            admitting_doctor_id, attending_doctor_id, source, source_appointment_id, source_encounter_id,
            priority, reason, provisional_diagnosis, requested_bed_category, payer_type, payer_name, policy_number,
            attendant_name, attendant_phone, attendant_relation, consent_recorded, expected_discharge_on,
            idempotency_key, created_by, created_by_name)
        VALUES (
            p_clinic_id, NULLIF(p_payload ->> 'branch_id', '')::UUID, v_year,
            opd_next_counter(p_clinic_id, 'ipd:' || v_year), v_patient, v_fm, v_admitting, v_attending,
            COALESCE(p_payload ->> 'source', 'direct'),
            NULLIF(p_payload ->> 'source_appointment_id', '')::UUID, NULLIF(p_payload ->> 'source_encounter_id', '')::UUID,
            COALESCE(p_payload ->> 'priority', 'elective'), p_payload ->> 'reason',
            NULLIF(p_payload ->> 'provisional_diagnosis', ''), NULLIF(p_payload ->> 'requested_bed_category', ''),
            COALESCE(p_payload ->> 'payer_type', 'self'), NULLIF(p_payload ->> 'payer_name', ''),
            NULLIF(p_payload ->> 'policy_number', ''), NULLIF(p_payload ->> 'attendant_name', ''),
            NULLIF(p_payload ->> 'attendant_phone', ''), NULLIF(p_payload ->> 'attendant_relation', ''),
            COALESCE((p_payload ->> 'consent_recorded')::BOOLEAN, false),
            NULLIF(p_payload ->> 'expected_discharge_on', '')::DATE, v_key, p_actor_id, p_actor_name)
        RETURNING * INTO a;
    EXCEPTION WHEN unique_violation THEN
        GET STACKED DIAGNOSTICS v_constraint = CONSTRAINT_NAME;
        IF v_constraint = 'uq_ipd_admissions_one_active' THEN PERFORM ipd_raise('ipd_active_admission_exists'); END IF;
        IF v_constraint = 'uq_ipd_admissions_idempotency' THEN
            SELECT * INTO a FROM ipd_admissions WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
            RETURN to_jsonb(a) || jsonb_build_object('replayed', true);
        END IF;
        RAISE;
    END;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'admission_created', p_actor_id, p_actor_name,
                    jsonb_build_object('source', a.source, 'priority', a.priority));
    IF p_bed_id IS NOT NULL THEN
        PERFORM 1 FROM ipd_admissions WHERE id = a.id FOR UPDATE;
        b := ipd_lock_eligible_bed(p_clinic_id, p_bed_id, a.id);
        PERFORM ipd_occupy_bed(p_clinic_id, a.id, b, p_actor_id);
        UPDATE ipd_admissions SET status = 'admitted', current_bed_id = b.id, admitted_at = now()
         WHERE id = a.id RETURNING * INTO a;
        PERFORM ipd_log(p_clinic_id, a.id, b.id, 'bed_assigned', p_actor_id, p_actor_name,
                        jsonb_build_object('bed', b.code, 'category', b.bed_category));
    END IF;
    RETURN to_jsonb(a);
END $$;

CREATE OR REPLACE FUNCTION ipd_assign_bed(p_clinic_id UUID, p_admission_id UUID, p_bed_id UUID,
                                          p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; b ipd_beds;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status <> 'pending' THEN PERFORM ipd_raise('ipd_bad_transition:' || a.status || '->admitted'); END IF;
    b := ipd_lock_eligible_bed(p_clinic_id, p_bed_id, a.id);
    PERFORM ipd_occupy_bed(p_clinic_id, a.id, b, p_actor_id);
    UPDATE ipd_admissions SET status = 'admitted', current_bed_id = b.id, admitted_at = now()
     WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, b.id, 'bed_assigned', p_actor_id, p_actor_name,
                    jsonb_build_object('bed', b.code, 'category', b.bed_category));
    RETURN to_jsonb(a);
END $$;

CREATE OR REPLACE FUNCTION ipd_reserve_bed(p_clinic_id UUID, p_admission_id UUID, p_bed_id UUID, p_minutes INTEGER,
                                           p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; b ipd_beds;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    IF p_minutes IS NULL OR p_minutes NOT BETWEEN 5 AND 1440 THEN PERFORM ipd_raise('ipd_reservation_minutes'); END IF;
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status <> 'pending' THEN PERFORM ipd_raise('ipd_bad_transition:' || a.status || '->reserved'); END IF;
    b := ipd_lock_eligible_bed(p_clinic_id, p_bed_id, a.id);
    -- one reservation per admission: moving it frees the previous bed
    UPDATE ipd_beds SET status = 'available', reserved_admission_id = NULL, reserved_until = NULL
     WHERE clinic_id = p_clinic_id AND reserved_admission_id = a.id AND id <> b.id;
    UPDATE ipd_beds SET status = 'reserved', reserved_admission_id = a.id,
                        reserved_until = now() + make_interval(mins => p_minutes)
     WHERE id = b.id RETURNING * INTO b;
    PERFORM ipd_log(p_clinic_id, a.id, b.id, 'bed_reserved', p_actor_id, p_actor_name,
                    jsonb_build_object('bed', b.code, 'until', b.reserved_until));
    RETURN to_jsonb(b);
END $$;

CREATE OR REPLACE FUNCTION ipd_release_reservation(p_clinic_id UUID, p_bed_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE b ipd_beds; v_adm UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO b FROM ipd_beds WHERE id = p_bed_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_bed_not_found'); END IF;
    IF b.status <> 'reserved' THEN PERFORM ipd_raise('ipd_bed_not_reserved'); END IF;
    v_adm := b.reserved_admission_id;
    UPDATE ipd_beds SET status = 'available', reserved_admission_id = NULL, reserved_until = NULL
     WHERE id = b.id RETURNING * INTO b;
    PERFORM ipd_log(p_clinic_id, v_adm, b.id, 'reservation_released', p_actor_id, p_actor_name,
                    jsonb_build_object('bed', b.code));
    RETURN to_jsonb(b);
END $$;

-- Transfer: one transaction ends the old assignment, starts the new one, moves
-- the admission and sends the old bed to cleaning. p_expected_bed_id guards
-- against a stale screen (someone else moved the patient meanwhile).
CREATE OR REPLACE FUNCTION ipd_transfer(p_clinic_id UUID, p_admission_id UUID, p_to_bed_id UUID,
                                        p_expected_bed_id UUID, p_reason TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; b_from ipd_beds; b_to ipd_beds;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF p_expected_bed_id IS DISTINCT FROM a.current_bed_id THEN PERFORM ipd_raise('ipd_stale_bed'); END IF;
    IF p_to_bed_id = a.current_bed_id THEN PERFORM ipd_raise('ipd_same_bed'); END IF;
    -- deterministic lock order on the two beds
    IF a.current_bed_id < p_to_bed_id THEN
        SELECT * INTO b_from FROM ipd_beds WHERE id = a.current_bed_id AND clinic_id = p_clinic_id FOR UPDATE;
        b_to := ipd_lock_eligible_bed(p_clinic_id, p_to_bed_id, a.id);
    ELSE
        b_to := ipd_lock_eligible_bed(p_clinic_id, p_to_bed_id, a.id);
        SELECT * INTO b_from FROM ipd_beds WHERE id = a.current_bed_id AND clinic_id = p_clinic_id FOR UPDATE;
    END IF;
    UPDATE ipd_bed_assignments SET ended_at = now(), end_reason = 'transfer', ended_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND ended_at IS NULL;
    -- free the old bed first: uq_ipd_beds_current_admission allows one bed per admission
    UPDATE ipd_beds SET status = 'cleaning', current_admission_id = NULL, status_reason = 'after transfer'
     WHERE id = b_from.id;
    PERFORM ipd_occupy_bed(p_clinic_id, a.id, b_to, p_actor_id, p_reason);
    UPDATE ipd_admissions SET current_bed_id = b_to.id WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, b_to.id, 'transferred', p_actor_id, p_actor_name,
                    jsonb_build_object('from_bed', b_from.code, 'to_bed', b_to.code,
                                       'from_category', b_from.bed_category, 'to_category', b_to.bed_category,
                                       'reason', p_reason));
    RETURN to_jsonb(a);
END $$;

-- Operational bed states. occupied/reserved are never set or cleared here.
CREATE OR REPLACE FUNCTION ipd_set_bed_status(p_clinic_id UUID, p_bed_id UUID, p_status TEXT, p_reason TEXT,
                                              p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE b ipd_beds;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO b FROM ipd_beds WHERE id = p_bed_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_bed_not_found'); END IF;
    IF p_status NOT IN ('available', 'cleaning', 'maintenance', 'blocked') THEN
        PERFORM ipd_raise('ipd_bad_bed_status');
    END IF;
    IF b.status IN ('occupied', 'reserved') THEN PERFORM ipd_raise('ipd_bed_in_use'); END IF;
    IF b.status = p_status THEN RETURN to_jsonb(b); END IF;   -- idempotent retry
    IF p_status IN ('maintenance', 'blocked') AND char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN
        PERFORM ipd_raise('ipd_reason_required');
    END IF;
    UPDATE ipd_beds SET status = p_status, status_reason = NULLIF(btrim(COALESCE(p_reason, '')), '')
     WHERE id = b.id RETURNING * INTO b;
    PERFORM ipd_log(p_clinic_id, NULL, b.id, 'bed_status', p_actor_id, p_actor_name,
                    jsonb_build_object('bed', b.code, 'status', p_status, 'reason', p_reason));
    RETURN to_jsonb(b);
END $$;

CREATE OR REPLACE FUNCTION ipd_cancel_admission(p_clinic_id UUID, p_admission_id UUID, p_reason TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status <> 'pending' THEN PERFORM ipd_raise('ipd_bad_transition:' || a.status || '->cancelled'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    UPDATE ipd_beds SET status = 'available', reserved_admission_id = NULL, reserved_until = NULL
     WHERE clinic_id = p_clinic_id AND reserved_admission_id = a.id;
    UPDATE ipd_admissions SET status = 'cancelled', cancelled_at = now(), cancel_reason = btrim(p_reason)
     WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'admission_cancelled', p_actor_id, p_actor_name,
                    jsonb_build_object('reason', p_reason));
    RETURN to_jsonb(a);
END $$;

CREATE OR REPLACE FUNCTION ipd_initiate_discharge(p_clinic_id UUID, p_admission_id UUID, p_discharge_type TEXT,
                                                  p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status = 'discharge_pending' AND a.discharge_type = p_discharge_type THEN RETURN to_jsonb(a); END IF;
    IF a.status <> 'admitted' THEN PERFORM ipd_raise('ipd_bad_transition:' || a.status || '->discharge_pending'); END IF;
    UPDATE ipd_admissions SET status = 'discharge_pending', discharge_type = p_discharge_type,
                              discharge_initiated_at = now(), discharge_initiated_by = p_actor_id
     WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, a.current_bed_id, 'discharge_initiated', p_actor_id, p_actor_name,
                    jsonb_build_object('discharge_type', p_discharge_type));
    RETURN to_jsonb(a);
END $$;

CREATE OR REPLACE FUNCTION ipd_revert_discharge(p_clinic_id UUID, p_admission_id UUID, p_reason TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status <> 'discharge_pending' THEN PERFORM ipd_raise('ipd_bad_transition:' || a.status || '->admitted'); END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void') THEN
        PERFORM ipd_raise('ipd_bill_exists_void_first');
    END IF;
    UPDATE ipd_admissions SET status = 'admitted', discharge_type = NULL,
                              discharge_initiated_at = NULL, discharge_initiated_by = NULL
     WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, a.current_bed_id, 'discharge_reverted', p_actor_id, p_actor_name,
                    jsonb_build_object('reason', p_reason));
    RETURN to_jsonb(a);
END $$;

-- What still blocks the discharge. Read-only; ipd_complete_discharge runs it
-- again under the admission lock.
CREATE OR REPLACE FUNCTION ipd_discharge_checklist(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_items JSONB := '[]'::jsonb; v_inv RECORD; v_n INTEGER;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE id = p_admission_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    v_items := v_items || jsonb_build_object('key', 'discharge_initiated', 'waivable', false,
        'done', a.status IN ('discharge_pending', 'discharged'));
    v_items := v_items || jsonb_build_object('key', 'summary_signed', 'waivable', false,
        'done', a.discharge_type = 'absconded' OR EXISTS (SELECT 1 FROM ipd_discharge_summaries
                 WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'signed'));
    SELECT count(*) INTO v_n FROM ipd_orders WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active';
    v_items := v_items || jsonb_build_object('key', 'orders_closed', 'done', v_n = 0, 'count', v_n, 'waivable', false);
    SELECT count(*) INTO v_n FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'proposed';
    v_items := v_items || jsonb_build_object('key', 'charges_reviewed', 'done', v_n = 0, 'count', v_n, 'waivable', false);
    SELECT id, status, total_paise, paid_paise, invoice_number INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void' LIMIT 1;
    v_items := v_items || jsonb_build_object('key', 'final_bill_issued', 'waivable', false,
        'done', v_inv.id IS NOT NULL AND v_inv.status <> 'draft', 'invoice_id', v_inv.id,
        'invoice_number', v_inv.invoice_number);
    SELECT count(*) INTO v_n FROM ipd_charges
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'posted' AND invoice_id IS NULL;
    v_items := v_items || jsonb_build_object('key', 'all_charges_billed', 'done', v_n = 0, 'count', v_n, 'waivable', false);
    v_items := v_items || jsonb_build_object('key', 'bill_settled', 'waivable', true,
        'done', v_inv.id IS NOT NULL AND v_inv.status = 'paid',
        'balance_paise', CASE WHEN v_inv.id IS NULL THEN NULL ELSE v_inv.total_paise - v_inv.paid_paise END);
    RETURN jsonb_build_object('admission_id', a.id, 'status', a.status, 'items', v_items,
        'ready', NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                              WHERE NOT (i ->> 'done')::BOOLEAN AND NOT (i ->> 'waivable')::BOOLEAN),
        'needs_waiver', EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                                 WHERE NOT (i ->> 'done')::BOOLEAN AND (i ->> 'waivable')::BOOLEAN));
END $$;

-- Final step: ends the bed assignment, sends the bed to cleaning, closes the stay.
CREATE OR REPLACE FUNCTION ipd_complete_discharge(p_clinic_id UUID, p_admission_id UUID, p_waiver_reason TEXT,
                                                  p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_check JSONB; v_bed UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status = 'discharged' THEN RETURN to_jsonb(a) || jsonb_build_object('replayed', true); END IF;
    v_check := ipd_discharge_checklist(p_clinic_id, a.id);
    IF NOT (v_check ->> 'ready')::BOOLEAN THEN PERFORM ipd_raise('ipd_discharge_blocked'); END IF;
    IF (v_check ->> 'needs_waiver')::BOOLEAN AND char_length(btrim(COALESCE(p_waiver_reason, ''))) < 5 THEN
        PERFORM ipd_raise('ipd_settlement_waiver_required');
    END IF;
    v_bed := a.current_bed_id;
    PERFORM 1 FROM ipd_beds WHERE id = v_bed AND clinic_id = p_clinic_id FOR UPDATE;
    UPDATE ipd_bed_assignments SET ended_at = now(), end_reason = 'discharge', ended_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND ended_at IS NULL;
    UPDATE ipd_beds SET status = 'cleaning', current_admission_id = NULL, status_reason = 'after discharge'
     WHERE id = v_bed AND clinic_id = p_clinic_id;
    UPDATE ipd_admissions SET status = 'discharged', current_bed_id = NULL, discharged_at = now(),
                              discharged_by = p_actor_id,
                              settlement_waiver_reason = CASE WHEN (v_check ->> 'needs_waiver')::BOOLEAN
                                                              THEN btrim(p_waiver_reason) END
     WHERE id = a.id RETURNING * INTO a;
    PERFORM ipd_log(p_clinic_id, a.id, v_bed, 'discharged', p_actor_id, p_actor_name,
                    jsonb_build_object('discharge_type', a.discharge_type,
                                       'waiver', a.settlement_waiver_reason IS NOT NULL));
    RETURN to_jsonb(a);
END $$;

-- Observation correction: mark the wrong row and (optionally) add the right one.
CREATE OR REPLACE FUNCTION ipd_correct_observation(p_clinic_id UUID, p_observation_id UUID, p_reason TEXT,
                                                   p_replacement JSONB, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o ipd_observations; n ipd_observations;
BEGIN
    SELECT * INTO o FROM ipd_observations WHERE id = p_observation_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_observation_not_found'); END IF;
    IF o.status <> 'recorded' THEN PERFORM ipd_raise('ipd_record_locked:ipd_observations'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    UPDATE ipd_observations SET status = 'entered_in_error', error_reason = btrim(p_reason),
                                errored_at = now(), errored_by = p_actor_id
     WHERE id = o.id;
    IF p_replacement IS NOT NULL THEN
        n := jsonb_populate_record(NULL::ipd_observations, p_replacement || jsonb_build_object(
                 'id', gen_random_uuid(), 'clinic_id', p_clinic_id, 'admission_id', o.admission_id,
                 'kind', o.kind, 'recorded_at', now(), 'recorded_by', p_actor_id, 'recorded_by_name', p_actor_name,
                 'status', 'recorded', 'corrects_id', o.id, 'error_reason', NULL, 'errored_at', NULL,
                 'errored_by', NULL, 'observed_at', COALESCE(p_replacement ->> 'observed_at', o.observed_at::TEXT)));
        INSERT INTO ipd_observations SELECT n.*;
    END IF;
    PERFORM ipd_log(p_clinic_id, o.admission_id, NULL, 'observation_corrected', p_actor_id, p_actor_name,
                    jsonb_build_object('observation_id', o.id, 'replacement_id', n.id, 'reason', p_reason));
    RETURN jsonb_build_object('corrected_id', o.id, 'replacement', CASE WHEN n.id IS NULL THEN NULL ELSE to_jsonb(n) END);
END $$;

-- Note amendment: the new note and the old note's status change are one write.
CREATE OR REPLACE FUNCTION ipd_amend_note(p_clinic_id UUID, p_note_id UUID, p_body TEXT, p_reason TEXT,
                                         p_author_doctor_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE o ipd_notes; n ipd_notes;
BEGIN
    SELECT * INTO o FROM ipd_notes WHERE id = p_note_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_note_not_found'); END IF;
    IF o.status <> 'final' THEN PERFORM ipd_raise('ipd_record_locked:ipd_notes'); END IF;
    -- only the author amends their note (others write a new note)
    IF o.author_id IS DISTINCT FROM p_actor_id THEN PERFORM ipd_raise('ipd_not_author'); END IF;
    UPDATE ipd_notes SET status = 'amended' WHERE id = o.id;
    INSERT INTO ipd_notes (clinic_id, admission_id, note_type, body, author_id, author_name, author_doctor_id,
                           amends_id, amend_reason)
    VALUES (p_clinic_id, o.admission_id, o.note_type, p_body, p_actor_id, p_actor_name,
            COALESCE(p_author_doctor_id, o.author_doctor_id), o.id, btrim(p_reason))
    RETURNING * INTO n;
    PERFORM ipd_log(p_clinic_id, o.admission_id, NULL, 'note_amended', p_actor_id, p_actor_name,
                    jsonb_build_object('note_id', o.id, 'new_note_id', n.id));
    RETURN to_jsonb(n);
END $$;

-- Sign a draft summary as the attending doctor. Supersedes the previous signed
-- version in the same transaction (amendment chain).
CREATE OR REPLACE FUNCTION ipd_sign_discharge_summary(p_clinic_id UUID, p_summary_id UUID, p_doctor_id UUID,
                                                      p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE s ipd_discharge_summaries; a ipd_admissions; d RECORD;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO s FROM ipd_discharge_summaries WHERE id = p_summary_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_summary_not_found'); END IF;
    IF s.status <> 'draft' THEN PERFORM ipd_raise('ipd_record_locked:ipd_discharge_summaries'); END IF;
    a := ipd_lock_admission(p_clinic_id, s.admission_id);
    IF a.status NOT IN ('admitted', 'discharge_pending', 'discharged') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF p_doctor_id IS DISTINCT FROM a.attending_doctor_id THEN PERFORM ipd_raise('ipd_not_attending_doctor'); END IF;
    SELECT name, registration_number, registration_council, qualifications INTO d
      FROM doctors WHERE id = p_doctor_id AND clinic_id = p_clinic_id;
    IF d.registration_number IS NULL THEN PERFORM ipd_raise('ipd_doctor_registration_missing'); END IF;
    IF char_length(btrim(COALESCE(s.final_diagnosis, ''))) = 0 THEN PERFORM ipd_raise('ipd_summary_incomplete:final_diagnosis'); END IF;
    UPDATE ipd_discharge_summaries SET status = 'superseded'
     WHERE clinic_id = p_clinic_id AND admission_id = s.admission_id AND status = 'signed';
    UPDATE ipd_discharge_summaries SET status = 'signed', signed_by_doctor_id = p_doctor_id, signed_by = p_actor_id,
                                       signed_at = now(), doctor_snapshot = to_jsonb(d)
     WHERE id = s.id RETURNING * INTO s;
    PERFORM ipd_log(p_clinic_id, s.admission_id, NULL, 'summary_signed', p_actor_id, p_actor_name,
                    jsonb_build_object('summary_id', s.id, 'version', s.version));
    RETURN to_jsonb(s);
END $$;

-- Bed-day charges, deterministic and idempotent (one per IST date per stay).
-- Policy (docs/ipd-os/WORKFLOWS.md §billing): every IST calendar date the
-- patient holds a bed counts, except — once discharge is initiated — the end
-- date when it is later than the start date and the time is before
-- ipd_settings.discharge_cutoff_hour (default 12). A date is charged at the
-- category of the bed held at the end of that date.
CREATE OR REPLACE FUNCTION ipd_accrue_bed_charges(p_clinic_id UUID, p_admission_id UUID,
                                                  p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_settings JSONB; v_cutoff INTEGER; v_end TIMESTAMPTZ; v_start_d DATE; v_end_d DATE;
        d DATE; v_cat TEXT; v_price INTEGER; v_added INTEGER := 0; v_missing TEXT[] := '{}'; v_rows INTEGER;
BEGIN
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    SELECT ipd_settings INTO v_settings FROM clinics WHERE id = p_clinic_id;
    v_cutoff := COALESCE((v_settings ->> 'discharge_cutoff_hour')::INTEGER, 12);
    v_end := now();
    v_start_d := ipd_ist_date(a.admitted_at);
    v_end_d := ipd_ist_date(v_end);
    IF a.status = 'discharge_pending' AND v_end_d > v_start_d
       AND EXTRACT(HOUR FROM (v_end AT TIME ZONE INTERVAL '+05:30')) < v_cutoff THEN
        v_end_d := v_end_d - 1;
    END IF;
    d := v_start_d;
    WHILE d <= v_end_d LOOP
        SELECT bed_category INTO v_cat FROM ipd_bed_assignments
         WHERE clinic_id = p_clinic_id AND admission_id = a.id
           AND started_at < ((d + 1)::TIMESTAMP AT TIME ZONE INTERVAL '+05:30')
           AND (ended_at IS NULL OR ended_at >= (d::TIMESTAMP AT TIME ZONE INTERVAL '+05:30'))
         ORDER BY started_at DESC LIMIT 1;
        v_price := (v_settings -> 'bed_tariffs' ->> v_cat)::INTEGER;
        IF v_price IS NULL THEN
            v_missing := array_append(v_missing, v_cat);
        ELSE
            INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity,
                                     unit_price_paise, source, source_ref, created_by, created_by_name)
            VALUES (p_clinic_id, a.id, 'bed', 'Bed charges - ' || v_cat, d, 1, v_price, 'bed_day',
                    'bed_day:' || d::TEXT, p_actor_id, p_actor_name)
            ON CONFLICT (clinic_id, admission_id, source_ref) WHERE source_ref IS NOT NULL AND status <> 'void'
            DO NOTHING;
            GET DIAGNOSTICS v_rows = ROW_COUNT;
            v_added := v_added + v_rows;
        END IF;
        d := d + 1;
    END LOOP;
    RETURN jsonb_build_object('added', v_added, 'through', v_end_d,
        'missing_tariffs', (SELECT COALESCE(jsonb_agg(DISTINCT m), '[]'::jsonb) FROM unnest(v_missing) m));
END $$;

CREATE OR REPLACE FUNCTION ipd_post_charges(p_clinic_id UUID, p_admission_id UUID, p_charge_ids UUID[],
                                            p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v_n INTEGER;
BEGIN
    PERFORM ipd_lock_admission(p_clinic_id, p_admission_id);
    UPDATE ipd_charges SET status = 'posted', posted_at = now(), posted_by = p_actor_id
     WHERE clinic_id = p_clinic_id AND admission_id = p_admission_id AND id = ANY (p_charge_ids) AND status = 'proposed';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n > 0 THEN
        PERFORM ipd_log(p_clinic_id, p_admission_id, NULL, 'charges_posted', p_actor_id, p_actor_name,
                        jsonb_build_object('count', v_n));
    END IF;
    RETURN jsonb_build_object('posted', v_n);
END $$;

CREATE OR REPLACE FUNCTION ipd_void_charge(p_clinic_id UUID, p_charge_id UUID, p_reason TEXT,
                                           p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE c ipd_charges;
BEGIN
    SELECT * INTO c FROM ipd_charges WHERE id = p_charge_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_charge_not_found'); END IF;
    IF c.status = 'void' THEN RETURN to_jsonb(c); END IF;
    IF c.invoice_id IS NOT NULL THEN PERFORM ipd_raise('ipd_charge_billed'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    UPDATE ipd_charges SET status = 'void', voided_at = now(), voided_by = p_actor_id, void_reason = btrim(p_reason)
     WHERE id = c.id RETURNING * INTO c;
    PERFORM ipd_log(p_clinic_id, c.admission_id, NULL, 'charge_voided', p_actor_id, p_actor_name,
                    jsonb_build_object('charge_id', c.id, 'reason', p_reason));
    RETURN to_jsonb(c);
END $$;

-- Final bill = a draft opd_invoices row whose lines are the posted charges,
-- grouped by (category, description, unit price). Idempotent: an existing live
-- bill is returned. A voided bill releases its charges for a new one.
CREATE OR REPLACE FUNCTION ipd_generate_final_bill(p_clinic_id UUID, p_admission_id UUID,
                                                   p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_inv UUID; v_line INTEGER := 0; r RECORD; v_patient JSONB;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status <> 'discharge_pending' THEN PERFORM ipd_raise('ipd_bill_needs_discharge_pending'); END IF;
    SELECT id INTO v_inv FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void';
    IF v_inv IS NOT NULL THEN RETURN jsonb_build_object('invoice_id', v_inv, 'replayed', true); END IF;
    UPDATE ipd_charges c SET invoice_id = NULL
      FROM opd_invoices i
     WHERE c.clinic_id = p_clinic_id AND c.admission_id = a.id AND c.invoice_id = i.id AND i.status = 'void';
    IF EXISTS (SELECT 1 FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'proposed') THEN
        PERFORM ipd_raise('ipd_charges_pending_review');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id
                     AND status = 'posted' AND invoice_id IS NULL) THEN
        PERFORM ipd_raise('ipd_no_charges');
    END IF;
    IF a.family_member_id IS NOT NULL THEN
        SELECT jsonb_build_object('name', fm.full_name, 'phone', p.phone, 'mrn', fm.mrn, 'gender', fm.gender,
                                  'age_years', fm.age_years) INTO v_patient
          FROM family_members fm JOIN patients p ON p.id = a.patient_id AND p.clinic_id = fm.clinic_id
         WHERE fm.id = a.family_member_id AND fm.clinic_id = p_clinic_id;
    ELSE
        SELECT jsonb_build_object('name', name, 'phone', phone, 'mrn', mrn, 'gender', gender,
                                  'age_years', age_years) INTO v_patient
          FROM patients WHERE id = a.patient_id AND clinic_id = p_clinic_id;
    END IF;
    INSERT INTO opd_invoices (clinic_id, branch_id, patient_id, family_member_id, admission_id, status,
                              patient_snapshot, notes, created_by)
    VALUES (p_clinic_id, a.branch_id, a.patient_id, a.family_member_id, a.id, 'draft',
            v_patient || jsonb_build_object('admission_number', a.admission_number),
            'Inpatient final bill ' || a.admission_number, p_actor_id)
    RETURNING id INTO v_inv;
    FOR r IN
        SELECT category, description, unit_price_paise, SUM(quantity)::INTEGER AS qty, min(service_date) AS first_day
          FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'posted' AND invoice_id IS NULL
         GROUP BY category, description, unit_price_paise ORDER BY first_day, category, description
    LOOP
        v_line := v_line + 1;
        IF v_line > 100 THEN PERFORM ipd_raise('ipd_bill_too_many_lines'); END IF;
        IF r.qty > 999 THEN PERFORM ipd_raise('ipd_bill_line_quantity'); END IF;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
        VALUES (p_clinic_id, v_inv, v_line,
                CASE r.category WHEN 'nursing' THEN 'nursing' WHEN 'doctor_visit' THEN 'consultation'
                                WHEN 'procedure' THEN 'procedure' WHEN 'package' THEN 'procedure'
                                WHEN 'investigation' THEN 'diagnostic' ELSE 'other' END,
                left(r.description, 200), r.qty, r.unit_price_paise);
    END LOOP;
    UPDATE ipd_charges SET invoice_id = v_inv
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'posted' AND invoice_id IS NULL;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'final_bill_created', p_actor_id, p_actor_name,
                    jsonb_build_object('invoice_id', v_inv, 'lines', v_line));
    RETURN jsonb_build_object('invoice_id', v_inv, 'lines', v_line);
END $$;

-- Owner-approved clinic deletion only. Purges OPD too (opd_purge_clinic runs
-- in the middle): charges reference invoices, invoices reference admissions.
CREATE OR REPLACE FUNCTION ipd_purge_clinic(p_clinic_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.ipd_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    DELETE FROM ipd_med_administrations WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_orders              WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_observations        WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_notes               WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_charges             WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_discharge_summaries WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_events              WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_bed_assignments     WHERE clinic_id = p_clinic_id;
    v := v || jsonb_build_object('opd', opd_purge_clinic(p_clinic_id));
    UPDATE ipd_beds SET status = 'available', current_admission_id = NULL, reserved_admission_id = NULL,
                        reserved_until = NULL WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_admissions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('admissions', n);
    DELETE FROM ipd_beds       WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('beds', n);
    DELETE FROM ipd_wards      WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.ipd_purge_clinic', '', true);
    PERFORM set_config('kriya.ipd_tx', '', true);
    RETURN v;
END $$;

-- ── 15. RLS parity (049/103 pattern) + API-role lockout ────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ipd_wards', 'ipd_beds', 'ipd_admissions', 'ipd_bed_assignments', 'ipd_events',
                             'ipd_observations', 'ipd_notes', 'ipd_orders', 'ipd_med_administrations',
                             'ipd_charges', 'ipd_discharge_summaries']
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
    FOREACH f IN ARRAY ARRAY[
        'ipd_admit(uuid,jsonb,uuid,uuid,text)', 'ipd_assign_bed(uuid,uuid,uuid,uuid,text)',
        'ipd_reserve_bed(uuid,uuid,uuid,integer,uuid,text)', 'ipd_release_reservation(uuid,uuid,uuid,text)',
        'ipd_transfer(uuid,uuid,uuid,uuid,text,uuid,text)', 'ipd_set_bed_status(uuid,uuid,text,text,uuid,text)',
        'ipd_cancel_admission(uuid,uuid,text,uuid,text)', 'ipd_initiate_discharge(uuid,uuid,text,uuid,text)',
        'ipd_revert_discharge(uuid,uuid,text,uuid,text)', 'ipd_discharge_checklist(uuid,uuid)',
        'ipd_complete_discharge(uuid,uuid,text,uuid,text)', 'ipd_correct_observation(uuid,uuid,text,jsonb,uuid,text)',
        'ipd_amend_note(uuid,uuid,text,text,uuid,uuid,text)', 'ipd_sign_discharge_summary(uuid,uuid,uuid,uuid,text)',
        'ipd_accrue_bed_charges(uuid,uuid,uuid,text)', 'ipd_post_charges(uuid,uuid,uuid[],uuid,text)',
        'ipd_void_charge(uuid,uuid,text,uuid,text)', 'ipd_generate_final_bill(uuid,uuid,uuid,text)',
        'ipd_purge_clinic(uuid)']
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

-- Pin search_path on every ipd_* function (migration 105 convention, Supabase lint 0011).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'ipd\_%'
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 16. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN (
          'ipd_wards', 'ipd_beds', 'ipd_admissions', 'ipd_bed_assignments', 'ipd_events', 'ipd_observations',
          'ipd_notes', 'ipd_orders', 'ipd_med_administrations', 'ipd_charges', 'ipd_discharge_summaries')) <> 11 THEN
        RAISE EXCEPTION '111 verify: IPD tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_indexes WHERE indexname IN (
          'uq_ipd_beds_current_admission', 'uq_ipd_admissions_one_per_bed', 'uq_ipd_admissions_one_active',
          'uq_ipd_bed_assignments_open_bed', 'uq_ipd_bed_assignments_open_admission', 'uq_ipd_charges_source_ref',
          'uq_opd_invoices_one_per_admission')) <> 7 THEN
        RAISE EXCEPTION '111 verify: IPD invariant indexes missing';
    END IF;
END $$;
