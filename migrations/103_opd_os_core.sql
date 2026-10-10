-- ============================================================================
-- Migration 103: Kriya OPD OS core (Phase 1)
-- Walk-ins, OPD queue stages, demographics/MRN, clinical encounters,
-- clinician-signed e-prescriptions, invoices, receipts, cashier shifts.
-- Additive + backward compatible: pre-103 application code runs unchanged.
-- Apply BEFORE deploying code that ships this file (lifespan parity check).
-- Apply off-peak: index rebuilds on appointments take a SHARE lock.
-- ============================================================================

-- ── 1. Composite tenant keys on existing parents (FK targets) ───────────────
-- id is already unique, so these can never fail; they let child tables use
-- FOREIGN KEY (clinic_id, x_id) and make cross-tenant references impossible.

-- Align family_members.clinic_id to UUID (from legacy varchar in 020) so composite FKs match appointments & OPD tables
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'family_members'
          AND column_name = 'clinic_id'
          AND data_type = 'character varying'
    ) THEN
        DROP POLICY IF EXISTS tenant_isolation_family_members ON family_members;
        ALTER TABLE family_members ALTER COLUMN clinic_id TYPE UUID USING clinic_id::uuid;
        CREATE POLICY tenant_isolation_family_members ON family_members
            FOR ALL TO authenticated
            USING (clinic_id IS NOT NULL AND clinic_id::text = NULLIF(current_setting('app.clinic_id', true), ''))
            WITH CHECK (clinic_id IS NOT NULL AND clinic_id::text = NULLIF(current_setting('app.clinic_id', true), ''));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_clinic_id_id       ON patients       (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_family_members_clinic_id_id ON family_members (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_clinic_id_id   ON appointments   (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_doctors_clinic_id_id        ON doctors        (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_branches_clinic_id_id       ON branches       (clinic_id, id);

-- ── 2. clinics: module state, settings, counters, TV display token ─────────
ALTER TABLE clinics
    ADD COLUMN IF NOT EXISTS opd_state              TEXT  NOT NULL DEFAULT 'NOT_CONFIGURED',
    ADD COLUMN IF NOT EXISTS opd_settings           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS opd_counters           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS opd_display_token_hash TEXT  NULL;
-- DEGRADED is computed at read time from the checklist, never stored (it would go stale).
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_state_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_opd_state_check
    CHECK (opd_state IN ('NOT_CONFIGURED', 'CONFIGURING', 'READY', 'DISABLED'));
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_opd_json_check;
ALTER TABLE clinics ADD  CONSTRAINT clinics_opd_json_check
    CHECK (jsonb_typeof(opd_settings) = 'object' AND jsonb_typeof(opd_counters) = 'object');
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinics_opd_display_token_hash
    ON clinics (opd_display_token_hash) WHERE opd_display_token_hash IS NOT NULL;

-- ── 3. doctors: NMC registration stamped on every signed record ────────────
ALTER TABLE doctors
    ADD COLUMN IF NOT EXISTS registration_number  TEXT NULL,
    ADD COLUMN IF NOT EXISTS registration_council TEXT NULL;
ALTER TABLE doctors DROP CONSTRAINT IF EXISTS doctors_registration_check;
ALTER TABLE doctors ADD  CONSTRAINT doctors_registration_check CHECK (
    (registration_number  IS NULL OR registration_number ~ '^[A-Za-z0-9/.\- ]{3,40}$')
AND (registration_council IS NULL OR char_length(registration_council) BETWEEN 2 AND 120));

-- ── 4. clinic_admins: a login may BE a doctor (required to sign) ───────────
ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS doctor_id UUID NULL;
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_fk;
ALTER TABLE clinic_admins ADD  CONSTRAINT clinic_admins_doctor_fk
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id)
    ON DELETE SET NULL (doctor_id);                       -- PG >= 15, see F17
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_doctor_needs_clinic;
ALTER TABLE clinic_admins ADD  CONSTRAINT clinic_admins_doctor_needs_clinic
    CHECK (doctor_id IS NULL OR clinic_id IS NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinic_admins_doctor
    ON clinic_admins (clinic_id, doctor_id) WHERE doctor_id IS NOT NULL;

-- ── 5. patients + family_members: demographics, MRN, allergies ─────────────
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS mrn                        TEXT     NULL,
    ADD COLUMN IF NOT EXISTS date_of_birth              DATE     NULL,
    ADD COLUMN IF NOT EXISTS age_years                  SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS age_recorded_on            DATE     NULL,
    ADD COLUMN IF NOT EXISTS gender                     TEXT     NULL,
    ADD COLUMN IF NOT EXISTS address_line               TEXT     NULL,
    ADD COLUMN IF NOT EXISTS city                       TEXT     NULL,
    ADD COLUMN IF NOT EXISTS pincode                    TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_name     TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_phone    TEXT     NULL,
    ADD COLUMN IF NOT EXISTS emergency_contact_relation TEXT     NULL,
    ADD COLUMN IF NOT EXISTS allergies                  TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS allergies_status           TEXT     NOT NULL DEFAULT 'unknown';
ALTER TABLE patients DROP CONSTRAINT IF EXISTS patients_opd_demographics_check;
ALTER TABLE patients ADD  CONSTRAINT patients_opd_demographics_check CHECK (
    (gender IS NULL OR gender IN ('male', 'female', 'other', 'undisclosed'))
AND (age_years IS NULL OR (age_years BETWEEN 0 AND 130 AND age_recorded_on IS NOT NULL))
AND (date_of_birth IS NULL OR date_of_birth >= DATE '1900-01-01')
AND (pincode IS NULL OR pincode ~ '^[1-9][0-9]{5}$')
AND (address_line IS NULL OR char_length(address_line) <= 300)
AND (city IS NULL OR char_length(city) <= 80)
AND (emergency_contact_name IS NULL OR char_length(emergency_contact_name) <= 100)
AND (emergency_contact_phone IS NULL OR emergency_contact_phone ~ '^\+?[0-9]{10,15}$')
AND (emergency_contact_relation IS NULL OR char_length(emergency_contact_relation) <= 40)
-- "unknown" (never asked) is clinically different from "none known" (asked, NKA).
AND allergies_status IN ('unknown', 'none_known', 'recorded')
AND ((allergies_status = 'recorded') = (cardinality(allergies) > 0))
AND cardinality(allergies) <= 50);
CREATE UNIQUE INDEX IF NOT EXISTS uq_patients_clinic_mrn
    ON patients (clinic_id, mrn) WHERE mrn IS NOT NULL;

ALTER TABLE family_members
    ADD COLUMN IF NOT EXISTS mrn              TEXT     NULL,
    ADD COLUMN IF NOT EXISTS date_of_birth    DATE     NULL,
    ADD COLUMN IF NOT EXISTS age_years        SMALLINT NULL,
    ADD COLUMN IF NOT EXISTS age_recorded_on  DATE     NULL,
    ADD COLUMN IF NOT EXISTS gender           TEXT     NULL,
    ADD COLUMN IF NOT EXISTS allergies        TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS allergies_status TEXT     NOT NULL DEFAULT 'unknown';
ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_opd_demographics_check;
ALTER TABLE family_members ADD  CONSTRAINT family_members_opd_demographics_check CHECK (
    (gender IS NULL OR gender IN ('male', 'female', 'other', 'undisclosed'))
AND (age_years IS NULL OR (age_years BETWEEN 0 AND 130 AND age_recorded_on IS NOT NULL))
AND (date_of_birth IS NULL OR date_of_birth >= DATE '1900-01-01')
AND allergies_status IN ('unknown', 'none_known', 'recorded')
AND ((allergies_status = 'recorded') = (cardinality(allergies) > 0))
AND cardinality(allergies) <= 50);
CREATE UNIQUE INDEX IF NOT EXISTS uq_family_members_clinic_mrn
    ON family_members (clinic_id, mrn) WHERE mrn IS NOT NULL;

-- ── 6. appointments: walk-ins, channel, visit type, OPD stages ─────────────
ALTER TABLE appointments
    ADD COLUMN IF NOT EXISTS is_walk_in       BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS booking_channel  TEXT        NULL,   -- NULL = pre-103 row, unknown
    ADD COLUMN IF NOT EXISTS visit_type       TEXT        NULL,
    ADD COLUMN IF NOT EXISTS family_member_id UUID        NULL,
    ADD COLUMN IF NOT EXISTS checked_in_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS queue_timeline   JSONB       NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_opd_meta_check;
ALTER TABLE appointments ADD  CONSTRAINT appointments_opd_meta_check CHECK (
    (booking_channel IS NULL OR booking_channel IN ('whatsapp', 'voice', 'front_desk', 'web'))
AND (visit_type IS NULL OR visit_type IN ('new', 'followup', 'review'))
AND (NOT is_walk_in OR (booking_type = 'consultation'
                        AND doctor_id IS NOT NULL
                        AND booking_channel = 'front_desk'))
AND jsonb_typeof(queue_timeline) = 'object');

ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointments_family_member_fk;
ALTER TABLE appointments ADD  CONSTRAINT appointments_family_member_fk
    FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id)
    ON DELETE SET NULL (family_member_id);                -- PG >= 15, see F17

-- 6b. Widen queue_status. Migration 019 declared the CHECK inline, so its
-- name is generated; drop every CHECK that mentions queue_status by definition.
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
             WHERE conrelid = 'public.appointments'::regclass AND contype = 'c'
               AND pg_get_constraintdef(oid) ILIKE '%queue_status%'
    LOOP
        EXECUTE format('ALTER TABLE appointments DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE appointments ADD CONSTRAINT appointments_queue_status_check CHECK (
    queue_status IS NULL OR queue_status IN (
        'registered', 'vitals_pending', 'waiting', 'in_consultation',
        'billing', 'completed', 'cancelled',
        'done'   -- legacy terminal state, still written by non-OPD clinics
    ));

-- 6c. Scheduled-slot guards ignore walk-ins. SAME NAMES: is_slot_conflict()
-- matches the 'uq_appointment_active_slot' prefix. Inside this transaction
-- there is no moment without a guard; the predicate only narrows, and no
-- walk-in row exists yet, so the rebuild cannot fail on existing data.
DROP INDEX IF EXISTS uq_appointment_active_slot;
CREATE UNIQUE INDEX uq_appointment_active_slot
    ON appointments (clinic_id, doctor_id, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NOT NULL
      AND is_walk_in = false;
DROP INDEX IF EXISTS uq_appointment_active_slot_unassigned;
CREATE UNIQUE INDEX uq_appointment_active_slot_unassigned
    ON appointments (clinic_id, doctor_name, appointment_date, appointment_time)
    WHERE status IN ('confirmed', 'pending_payment', 'pending_review')
      AND booking_type = 'consultation'
      AND doctor_id IS NULL
      AND is_walk_in = false;
-- Token uniqueness (idx_unique_queue_token / idx_unique_lab_queue_token) is
-- deliberately untouched: walk-ins and booked arrivals share one token line.

CREATE INDEX IF NOT EXISTS idx_appointments_opd_queue
    ON appointments (clinic_id, appointment_date, doctor_id, queue_status)
    WHERE token_number IS NOT NULL;

-- ── 7. Counters: gapless per-clinic numbering under a row lock ─────────────
CREATE OR REPLACE FUNCTION opd_next_counter(p_clinic_id UUID, p_key TEXT)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE v INTEGER;
BEGIN
    UPDATE clinics
       SET opd_counters = jsonb_set(opd_counters, ARRAY[p_key],
                                    to_jsonb(COALESCE((opd_counters ->> p_key)::INTEGER, 0) + 1), true)
     WHERE id = p_clinic_id
    RETURNING (opd_counters ->> p_key)::INTEGER INTO v;
    IF v IS NULL THEN
        RAISE EXCEPTION 'opd_unknown_clinic:%', p_clinic_id USING ERRCODE = 'P0002';
    END IF;
    RETURN v;
END $$;

-- p_width digits until 10^p_width - 1, then the full number: lpad() would
-- TRUNCATE 123456 to 12345 and mint a duplicate-looking invoice number.
CREATE OR REPLACE FUNCTION opd_format_number(p_prefix TEXT, p_year INTEGER, p_seq INTEGER, p_width INTEGER)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT p_prefix
        || CASE WHEN p_year IS NULL THEN '' ELSE p_year::TEXT || '-' END
        || CASE WHEN p_seq < (10 ^ p_width)::INTEGER THEN lpad(p_seq::TEXT, p_width, '0') ELSE p_seq::TEXT END
$$;

-- MRN is shared by patients and family_members (one counter), so it is unique per clinic across both.
CREATE OR REPLACE FUNCTION opd_assign_mrn(p_clinic_id UUID, p_patient_id UUID, p_family_member_id UUID DEFAULT NULL)
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE v_mrn TEXT;
BEGIN
    IF p_family_member_id IS NULL THEN
        SELECT mrn INTO v_mrn FROM patients
         WHERE id = p_patient_id AND clinic_id = p_clinic_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_patient_not_found' USING ERRCODE = 'P0002'; END IF;
        IF v_mrn IS NULL THEN
            v_mrn := opd_format_number('MRN-', NULL, opd_next_counter(p_clinic_id, 'mrn'), 6);
            UPDATE patients SET mrn = v_mrn WHERE id = p_patient_id AND clinic_id = p_clinic_id;
        END IF;
    ELSE
        -- the dependant must belong to THIS account holder's phone in THIS clinic
        SELECT fm.mrn INTO v_mrn
          FROM family_members fm
          JOIN patients p ON p.clinic_id = fm.clinic_id AND p.phone = fm.primary_phone
         WHERE fm.id = p_family_member_id AND fm.clinic_id = p_clinic_id AND p.id = p_patient_id
           FOR UPDATE OF fm;
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_family_member_not_found' USING ERRCODE = 'P0002'; END IF;
        IF v_mrn IS NULL THEN
            v_mrn := opd_format_number('MRN-', NULL, opd_next_counter(p_clinic_id, 'mrn'), 6);
            UPDATE family_members SET mrn = v_mrn WHERE id = p_family_member_id AND clinic_id = p_clinic_id;
        END IF;
    END IF;
    RETURN v_mrn;
END $$;

-- ── 8. Shared trigger helpers ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION opd_touch_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

-- Only opd_purge_clinic() sets this, for exactly one clinic, transaction-local.
CREATE OR REPLACE FUNCTION opd_purging(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.opd_purge_clinic', true), '') = p_clinic_id::TEXT
$$;

-- ── 9. Tables ──────────────────────────────────────────────────────────────

-- 9.1 Clinical encounter: vitals + structured notes. Signed rows are immutable;
-- an amendment is a NEW row (version+1, supersedes_id) — that row chain IS the
-- revision history. Drafts are working copies, not legal records.
CREATE TABLE IF NOT EXISTS opd_encounters (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    appointment_id      UUID NOT NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    doctor_id           UUID NOT NULL,
    version             SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id       UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed', 'superseded')),
    -- vitals (ranges reject typos such as 1200 for 120; the UI shows the same limits)
    bp_systolic         SMALLINT NULL CHECK (bp_systolic  BETWEEN 50 AND 300),
    bp_diastolic        SMALLINT NULL CHECK (bp_diastolic BETWEEN 20 AND 200),
    pulse_bpm           SMALLINT NULL CHECK (pulse_bpm    BETWEEN 20 AND 250),
    temperature_c       NUMERIC(4,1) NULL CHECK (temperature_c BETWEEN 30 AND 45),
    spo2_pct            SMALLINT NULL CHECK (spo2_pct     BETWEEN 50 AND 100),
    weight_kg           NUMERIC(5,2) NULL CHECK (weight_kg > 0 AND weight_kg <= 400),
    height_cm           NUMERIC(5,1) NULL CHECK (height_cm BETWEEN 30 AND 250),
    -- NUMERIC(6,1): 400 kg at 30 cm is 4444.4 and must not overflow the insert
    bmi                 NUMERIC(6,1) GENERATED ALWAYS AS (
                            CASE WHEN weight_kg IS NOT NULL AND height_cm IS NOT NULL
                                 THEN round(weight_kg / ((height_cm / 100.0) ^ 2), 1) END) STORED,
    vitals_recorded_at  TIMESTAMPTZ NULL,
    vitals_recorded_by  UUID NULL,   -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    vitals_recorded_by_name TEXT NULL,
    -- clinical documentation
    chief_complaints    TEXT NULL CHECK (char_length(chief_complaints)   <= 4000),
    clinical_findings   TEXT NULL CHECK (char_length(clinical_findings)  <= 8000),
    examination_notes   TEXT NULL CHECK (char_length(examination_notes)  <= 8000),
    diagnoses           JSONB NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(diagnoses) = 'array' AND jsonb_array_length(diagnoses) <= 20),
                        -- [{"system":"ICD-10"|null, "code":"J06.9"|null, "text":"Acute URTI"}]
    advice              TEXT NULL CHECK (char_length(advice) <= 4000),
    follow_up_date      DATE NULL,
    -- lifecycle
    started_at          TIMESTAMPTZ NULL,
    signed_at           TIMESTAMPTZ NULL,
    signed_by_admin_id  UUID NULL,
    signer_snapshot     JSONB NULL,  -- {doctor_id,name,qualifications,registration_number,registration_council}
    superseded_at       TIMESTAMPTZ NULL,
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_encounters_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_encounters_version_key UNIQUE (clinic_id, appointment_id, version),
    CONSTRAINT opd_encounters_signed_complete CHECK (
        status = 'draft' OR (signed_at IS NOT NULL AND signed_by_admin_id IS NOT NULL
                             AND signer_snapshot ? 'registration_number')),
    CONSTRAINT opd_encounters_supersede_shape CHECK ((version = 1) = (supersedes_id IS NULL)),
    CONSTRAINT opd_encounters_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_encounters_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_encounters_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_encounters_doctor_fk      FOREIGN KEY (clinic_id, doctor_id)        REFERENCES doctors (clinic_id, id),
    CONSTRAINT opd_encounters_branch_fk      FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id),
    CONSTRAINT opd_encounters_supersedes_fk  FOREIGN KEY (clinic_id, supersedes_id)    REFERENCES opd_encounters (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_encounters_one_draft  ON opd_encounters (clinic_id, appointment_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_encounters_one_signed ON opd_encounters (clinic_id, appointment_id) WHERE status = 'signed';
CREATE INDEX IF NOT EXISTS idx_opd_encounters_patient ON opd_encounters (clinic_id, patient_id, family_member_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opd_encounters_doctor  ON opd_encounters (clinic_id, doctor_id, created_at DESC);

-- 9.2 Prescription header. Same immutability + amendment model as encounters.
CREATE TABLE IF NOT EXISTS opd_prescriptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    encounter_id        UUID NOT NULL,
    appointment_id      UUID NOT NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    doctor_id           UUID NOT NULL,
    version             SMALLINT NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 99),
    supersedes_id       UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signed', 'superseded')),
    general_instructions TEXT NULL CHECK (char_length(general_instructions) <= 2000),
    allergy_review      JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(allergy_review) = 'array'),
                        -- [{"line_no":2,"allergen":"penicillin","drug":"Amoxicillin","override_reason":"…"}]
    signed_at           TIMESTAMPTZ NULL,
    signed_by_admin_id  UUID NULL,
    signer_snapshot     JSONB NULL,
    letterhead_snapshot JSONB NULL,  -- clinic name/address/phone/branch at signing → deterministic reprint
    patient_snapshot    JSONB NULL,  -- name, mrn, age, gender, allergies at signing
    superseded_at       TIMESTAMPTZ NULL,
    -- delivery (the only columns that may change after signing)
    delivery_status     TEXT NOT NULL DEFAULT 'not_sent' CHECK (delivery_status IN ('not_sent', 'sent', 'failed')),
    whatsapp_message_id TEXT NULL,
    last_sent_at        TIMESTAMPTZ NULL,
    send_count          SMALLINT NOT NULL DEFAULT 0 CHECK (send_count BETWEEN 0 AND 50),
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_prescriptions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_prescriptions_version_key UNIQUE (clinic_id, appointment_id, version),
    CONSTRAINT opd_prescriptions_signed_complete CHECK (
        status = 'draft' OR (signed_at IS NOT NULL AND signed_by_admin_id IS NOT NULL
                             AND signer_snapshot ? 'registration_number'
                             AND letterhead_snapshot IS NOT NULL AND patient_snapshot IS NOT NULL)),
    CONSTRAINT opd_prescriptions_supersede_shape CHECK ((version = 1) = (supersedes_id IS NULL)),
    CONSTRAINT opd_prescriptions_encounter_fk   FOREIGN KEY (clinic_id, encounter_id)     REFERENCES opd_encounters (clinic_id, id),
    CONSTRAINT opd_prescriptions_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_prescriptions_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_prescriptions_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_prescriptions_doctor_fk      FOREIGN KEY (clinic_id, doctor_id)        REFERENCES doctors (clinic_id, id),
    CONSTRAINT opd_prescriptions_supersedes_fk  FOREIGN KEY (clinic_id, supersedes_id)    REFERENCES opd_prescriptions (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_prescriptions_one_draft  ON opd_prescriptions (clinic_id, appointment_id) WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_prescriptions_one_signed ON opd_prescriptions (clinic_id, appointment_id) WHERE status = 'signed';
CREATE INDEX IF NOT EXISTS idx_opd_prescriptions_patient ON opd_prescriptions (clinic_id, patient_id, family_member_id, created_at DESC);

-- 9.3 Prescription lines. Writable only while the parent is a draft (trigger).
CREATE TABLE IF NOT EXISTS opd_prescription_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    prescription_id     UUID NOT NULL,
    line_no             SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 40),
    drug_name           TEXT NOT NULL CHECK (char_length(btrim(drug_name)) BETWEEN 2 AND 200),
    formulation         TEXT NOT NULL CHECK (formulation IN ('tablet', 'capsule', 'syrup', 'suspension', 'injection',
                            'drops', 'ointment', 'cream', 'gel', 'inhaler', 'powder', 'lotion', 'spray', 'patch', 'other')),
    strength            TEXT NULL CHECK (char_length(strength) <= 60),
    dosage              TEXT NOT NULL CHECK (char_length(btrim(dosage)) BETWEEN 1 AND 60),
    route               TEXT NOT NULL DEFAULT 'oral' CHECK (route IN ('oral', 'topical', 'iv', 'im', 'sc', 'inhalation',
                            'nasal', 'ophthalmic', 'otic', 'rectal', 'vaginal', 'sublingual', 'other')),
    frequency           TEXT NOT NULL CHECK (char_length(btrim(frequency)) BETWEEN 1 AND 40),  -- OD/BD/TDS/QID/HS/SOS/STAT or custom
    timing              TEXT NOT NULL DEFAULT 'any' CHECK (timing IN ('before_food', 'after_food', 'with_food',
                            'empty_stomach', 'bedtime', 'any')),
    duration_days       SMALLINT NULL CHECK (duration_days BETWEEN 1 AND 365),               -- NULL = SOS / until review
    instructions        TEXT NULL CHECK (char_length(instructions) <= 500),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_prescription_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_prescription_items_line_key UNIQUE (prescription_id, line_no),
    CONSTRAINT opd_prescription_items_rx_fk FOREIGN KEY (clinic_id, prescription_id)
        REFERENCES opd_prescriptions (clinic_id, id) ON DELETE CASCADE
);

-- 9.4 Invoice header. Number assigned atomically at issue (gapless). Subtotal
-- is maintained by the item trigger; paid_paise ONLY by the receipt trigger.
CREATE TABLE IF NOT EXISTS opd_invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    appointment_id      UUID NULL,
    encounter_id        UUID NULL,
    patient_id          UUID NOT NULL,
    family_member_id    UUID NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'issued', 'partially_paid', 'paid', 'void')),
    invoice_year        SMALLINT NULL,
    invoice_seq         INTEGER  NULL CHECK (invoice_seq > 0),
    invoice_number      TEXT GENERATED ALWAYS AS (
                            CASE WHEN invoice_seq IS NULL THEN NULL
                                 ELSE opd_format_number('INV-', invoice_year, invoice_seq, 5) END) STORED,
    subtotal_paise      INTEGER NOT NULL DEFAULT 0 CHECK (subtotal_paise >= 0),
    discount_paise      INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    total_paise         INTEGER GENERATED ALWAYS AS (subtotal_paise - discount_paise) STORED,
    paid_paise          INTEGER NOT NULL DEFAULT 0 CHECK (paid_paise >= 0),
    discount_reason     TEXT NULL CHECK (char_length(discount_reason) <= 200),
    patient_snapshot    JSONB NULL,
    notes               TEXT NULL CHECK (char_length(notes) <= 1000),
    payment_link_gateway TEXT NULL CHECK (payment_link_gateway IN ('razorpay', 'phonepe')),
    payment_link_id     TEXT NULL,
    payment_link_url    TEXT NULL,
    payment_link_amount_paise INTEGER NULL CHECK (payment_link_amount_paise > 0),
    payment_link_created_at TIMESTAMPTZ NULL,
    issued_at           TIMESTAMPTZ NULL,
    issued_by           UUID NULL,
    voided_at           TIMESTAMPTZ NULL,
    voided_by           UUID NULL,
    void_reason         TEXT NULL CHECK (char_length(void_reason) BETWEEN 5 AND 300),
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_invoices_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_invoices_number_key UNIQUE (clinic_id, invoice_year, invoice_seq),
    CONSTRAINT opd_invoices_discount_le_subtotal CHECK (discount_paise <= subtotal_paise),
    CONSTRAINT opd_invoices_numbered_when_issued CHECK (status IN ('draft', 'void') OR invoice_seq IS NOT NULL),
    CONSTRAINT opd_invoices_void_complete CHECK (status <> 'void' OR (voided_at IS NOT NULL AND void_reason IS NOT NULL)),
    -- status is a pure function of paid vs total; over-payment and over-refund fail here
    CONSTRAINT opd_invoices_paid_consistent CHECK (
        (status IN ('draft', 'issued', 'void') AND paid_paise = 0)
     OR (status = 'partially_paid' AND paid_paise > 0 AND paid_paise < subtotal_paise - discount_paise)
     OR (status = 'paid' AND paid_paise = subtotal_paise - discount_paise)),
    CONSTRAINT opd_invoices_appointment_fk FOREIGN KEY (clinic_id, appointment_id)   REFERENCES appointments (clinic_id, id),
    CONSTRAINT opd_invoices_encounter_fk   FOREIGN KEY (clinic_id, encounter_id)     REFERENCES opd_encounters (clinic_id, id),
    CONSTRAINT opd_invoices_patient_fk     FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT opd_invoices_family_fk      FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT opd_invoices_branch_fk      FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id)
);
-- one live invoice per visit: no double billing
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_invoices_one_per_visit
    ON opd_invoices (clinic_id, appointment_id) WHERE appointment_id IS NOT NULL AND status <> 'void';
CREATE INDEX IF NOT EXISTS idx_opd_invoices_day     ON opd_invoices (clinic_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opd_invoices_open    ON opd_invoices (clinic_id, status) WHERE status IN ('issued', 'partially_paid');
CREATE INDEX IF NOT EXISTS idx_opd_invoices_patient ON opd_invoices (clinic_id, patient_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_invoices_payment_link
    ON opd_invoices (payment_link_gateway, payment_link_id) WHERE payment_link_id IS NOT NULL;

-- 9.5 Invoice lines (snapshotted description + price).
CREATE TABLE IF NOT EXISTS opd_invoice_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id          UUID NOT NULL,
    line_no             SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 100),
    item_type           TEXT NOT NULL CHECK (item_type IN ('consultation', 'nursing', 'diagnostic', 'procedure', 'other')),
    catalog_code        TEXT NULL CHECK (char_length(catalog_code) <= 40),
    doctor_id           UUID NULL,
    lab_test_id         UUID NULL,   -- reference only; price is snapshotted below
    description         TEXT NOT NULL CHECK (char_length(btrim(description)) BETWEEN 1 AND 200),
    quantity            SMALLINT NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 999),
    unit_price_paise    INTEGER NOT NULL CHECK (unit_price_paise BETWEEN 0 AND 100000000),
    discount_paise      INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    line_total_paise    INTEGER GENERATED ALWAYS AS (quantity * unit_price_paise - discount_paise) STORED,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_invoice_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_invoice_items_line_key UNIQUE (invoice_id, line_no),
    CONSTRAINT opd_invoice_items_discount_ok CHECK (discount_paise <= quantity * unit_price_paise),
    CONSTRAINT opd_invoice_items_invoice_fk FOREIGN KEY (clinic_id, invoice_id)
        REFERENCES opd_invoices (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_invoice_items_doctor_fk FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors (clinic_id, id)
);

-- 9.6 Cashier shift (7th table — see F11). One open shift per cashier.
CREATE TABLE IF NOT EXISTS opd_cashier_shifts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    cashier_admin_id    UUID NOT NULL,
    cashier_name        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    opened_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    opening_float_paise INTEGER NOT NULL DEFAULT 0 CHECK (opening_float_paise BETWEEN 0 AND 100000000),
    closed_at           TIMESTAMPTZ NULL,
    expected_cash_paise INTEGER NULL,
    declared_cash_paise INTEGER NULL CHECK (declared_cash_paise >= 0),
    variance_paise      INTEGER GENERATED ALWAYS AS (declared_cash_paise - expected_cash_paise) STORED,
    close_notes         TEXT NULL CHECK (char_length(close_notes) <= 500),
    CONSTRAINT opd_cashier_shifts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_cashier_shifts_closed_complete CHECK (
        status = 'open' OR (closed_at IS NOT NULL AND expected_cash_paise IS NOT NULL AND declared_cash_paise IS NOT NULL)),
    CONSTRAINT opd_cashier_shifts_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_cashier_shifts_one_open
    ON opd_cashier_shifts (clinic_id, cashier_admin_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_opd_cashier_shifts_day ON opd_cashier_shifts (clinic_id, opened_at DESC);

-- 9.7 Receipts: append-only money ledger. A refund is a new row (kind=refund).
CREATE TABLE IF NOT EXISTS opd_receipts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id          UUID NOT NULL,
    shift_id            UUID NULL,           -- NULL only for online money (payment_link / prepaid_online)
    kind                TEXT NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment', 'refund')),
    mode                TEXT NOT NULL CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online')),
    amount_paise        INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    reference           TEXT NULL CHECK (char_length(reference) <= 100),  -- UPI UTR / card RRN
    gateway             TEXT NULL CHECK (gateway IN ('razorpay', 'phonepe')),
    gateway_payment_id  TEXT NULL,
    receipt_year        SMALLINT NULL,
    receipt_seq         INTEGER  NULL,
    receipt_number      TEXT GENERATED ALWAYS AS (
                            CASE WHEN receipt_seq IS NULL THEN NULL
                                 ELSE opd_format_number('RCT-', receipt_year, receipt_seq, 5) END) STORED,
    received_by_admin_id UUID NULL,          -- NULL = gateway webhook
    received_by_name    TEXT NULL,
    reason              TEXT NULL CHECK (char_length(reason) <= 300),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_receipts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_receipts_number_key UNIQUE (clinic_id, receipt_year, receipt_seq),
    CONSTRAINT opd_receipts_reference_required CHECK (mode NOT IN ('upi', 'card') OR reference IS NOT NULL),
    CONSTRAINT opd_receipts_online_shape CHECK (
        (mode IN ('payment_link', 'prepaid_online')) = (gateway_payment_id IS NOT NULL AND shift_id IS NULL)),
    CONSTRAINT opd_receipts_counter_needs_shift CHECK (mode IN ('payment_link', 'prepaid_online') OR shift_id IS NOT NULL),
    CONSTRAINT opd_receipts_refund_manual CHECK (kind = 'payment' OR (mode IN ('cash', 'upi') AND reason IS NOT NULL)),
    CONSTRAINT opd_receipts_invoice_fk FOREIGN KEY (clinic_id, invoice_id) REFERENCES opd_invoices (clinic_id, id),
    CONSTRAINT opd_receipts_shift_fk   FOREIGN KEY (clinic_id, shift_id)   REFERENCES opd_cashier_shifts (clinic_id, id)
);
-- webhook idempotency: a gateway payment settles at most once
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_receipts_gateway_payment
    ON opd_receipts (clinic_id, gateway, gateway_payment_id) WHERE gateway_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_receipts_invoice ON opd_receipts (clinic_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_opd_receipts_shift   ON opd_receipts (clinic_id, shift_id) WHERE shift_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_receipts_day     ON opd_receipts (clinic_id, created_at DESC);

-- ── 10. Integrity triggers (they fire for service_role; RLS does not) ──────

-- 10.1 Signed encounters/prescriptions are immutable. In BEFORE triggers
-- generated columns (bmi) are not yet computed in NEW, so they are excluded
-- from the comparison along with the columns each transition may change.
CREATE OR REPLACE FUNCTION opd_guard_signed_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_mutable TEXT[] := ARRAY['updated_at', 'bmi'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.doctor_id <> OLD.doctor_id OR NEW.appointment_id <> OLD.appointment_id
       OR NEW.version <> OLD.version OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'superseded' THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->superseded' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'signed' THEN
        IF TG_TABLE_NAME = 'opd_prescriptions' THEN
            v_mutable := v_mutable || ARRAY['delivery_status', 'whatsapp_message_id', 'last_sent_at', 'send_count'];
        END IF;
        IF NEW.status = 'superseded' THEN
            v_mutable := v_mutable || ARRAY['status', 'superseded_at'];
        ELSIF NEW.status <> 'signed' THEN
            RAISE EXCEPTION 'opd_bad_transition:signed->%', NEW.status USING ERRCODE = 'P0001';
        END IF;
        IF (to_jsonb(NEW) - v_mutable) = (to_jsonb(OLD) - v_mutable) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'opd_record_locked:%:%', TG_TABLE_NAME, OLD.id USING ERRCODE = 'P0001';
END $$;

-- 10.2 Prescription lines follow their parent's lock.
CREATE OR REPLACE FUNCTION opd_guard_rx_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_status TEXT; v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT status INTO v_status FROM opd_prescriptions
     WHERE id = COALESCE(NEW.prescription_id, OLD.prescription_id) AND clinic_id = v_clinic;
    -- NULL status = parent already gone (draft delete cascading) → allow
    IF v_status IS NOT NULL AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_prescription_items' USING ERRCODE = 'P0001';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

-- 10.3 Invoice lines: draft-only; keep the parent subtotal in step.
CREATE OR REPLACE FUNCTION opd_guard_invoice_items() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_status TEXT; v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT status INTO v_status FROM opd_invoices
     WHERE id = COALESCE(NEW.invoice_id, OLD.invoice_id) AND clinic_id = v_clinic;
    IF v_status IS NOT NULL AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoice_items' USING ERRCODE = 'P0001';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

CREATE OR REPLACE FUNCTION opd_recalc_invoice_subtotal() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_invoice UUID := COALESCE(NEW.invoice_id, OLD.invoice_id);
        v_clinic  UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    UPDATE opd_invoices i
       SET subtotal_paise = (SELECT COALESCE(SUM(line_total_paise), 0) FROM opd_invoice_items
                              WHERE invoice_id = v_invoice AND clinic_id = v_clinic)
     WHERE i.id = v_invoice AND i.clinic_id = v_clinic AND i.status = 'draft';
    RETURN NULL;
END $$;

-- 10.4 Invoice header: numbering at issue, lock after issue, paid only via receipts.
-- total_paise / invoice_number are generated → not yet computed in NEW → excluded.
CREATE OR REPLACE FUNCTION opd_guard_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_items   INTEGER;
    v_total   INTEGER;
    v_mutable TEXT[] := ARRAY['updated_at', 'total_paise', 'invoice_number',
                              'payment_link_gateway', 'payment_link_id', 'payment_link_url',
                              'payment_link_amount_paise', 'payment_link_created_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' AND NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.patient_id <> OLD.patient_id
       OR NEW.appointment_id IS DISTINCT FROM OLD.appointment_id THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    -- paid_paise moves only inside the receipt trigger, for this invoice
    IF NEW.paid_paise <> OLD.paid_paise
       AND current_setting('kriya.opd_receipt_invoice', true) IS DISTINCT FROM OLD.id::TEXT THEN
        RAISE EXCEPTION 'opd_paid_is_derived' USING ERRCODE = 'P0001';
    END IF;

    IF OLD.status = 'draft' THEN
        IF NEW.status IN ('issued', 'paid') THEN
            SELECT count(*), COALESCE(SUM(line_total_paise), 0) INTO v_items, v_total
              FROM opd_invoice_items WHERE invoice_id = OLD.id AND clinic_id = OLD.clinic_id;
            IF v_items = 0 THEN RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001'; END IF;
            IF v_total <> NEW.subtotal_paise THEN RAISE EXCEPTION 'opd_invoice_subtotal_mismatch' USING ERRCODE = 'P0001'; END IF;
            NEW.invoice_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
            NEW.invoice_seq  := opd_next_counter(OLD.clinic_id, 'inv:' || NEW.invoice_year);
            NEW.issued_at    := now();
            NEW.status       := CASE WHEN NEW.subtotal_paise - NEW.discount_paise = 0 THEN 'paid' ELSE 'issued' END;
        ELSIF NEW.status NOT IN ('draft', 'void') THEN
            RAISE EXCEPTION 'opd_bad_transition:draft->%', NEW.status USING ERRCODE = 'P0001';
        ELSIF NEW.invoice_seq IS NOT NULL OR NEW.invoice_year IS NOT NULL THEN
            RAISE EXCEPTION 'opd_number_is_assigned_at_issue' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'void' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;

    -- issued / partially_paid / paid
    IF NEW.status = 'void' THEN
        IF OLD.paid_paise <> 0 THEN RAISE EXCEPTION 'opd_void_requires_refund_first' USING ERRCODE = 'P0001'; END IF;
        v_mutable := v_mutable || ARRAY['status', 'voided_at', 'voided_by', 'void_reason'];
    ELSIF NEW.status = 'draft' THEN
        RAISE EXCEPTION 'opd_bad_transition:%->draft', OLD.status USING ERRCODE = 'P0001';
    ELSE
        v_mutable := v_mutable || ARRAY['status', 'paid_paise'];
    END IF;
    IF (to_jsonb(NEW) - v_mutable) <> (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- 10.5 Receipts: number + shift check before insert; apply to invoice after;
-- never updated or deleted.
CREATE OR REPLACE FUNCTION opd_receipt_before_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_shift_status TEXT; v_cashier UUID;
BEGIN
    IF NEW.shift_id IS NOT NULL THEN
        -- FOR SHARE waits for a concurrent opd_close_shift and re-reads its result
        SELECT status, cashier_admin_id INTO v_shift_status, v_cashier
          FROM opd_cashier_shifts WHERE id = NEW.shift_id AND clinic_id = NEW.clinic_id FOR SHARE;
        IF v_shift_status IS DISTINCT FROM 'open' THEN
            RAISE EXCEPTION 'opd_shift_not_open' USING ERRCODE = 'P0001';
        END IF;
        IF NEW.received_by_admin_id IS DISTINCT FROM v_cashier THEN
            RAISE EXCEPTION 'opd_shift_not_yours' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    NEW.receipt_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
    NEW.receipt_seq  := opd_next_counter(NEW.clinic_id, 'rct:' || NEW.receipt_year);
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION opd_receipt_after_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_new_paid INTEGER; v_total INTEGER; v_status TEXT;
BEGIN
    SELECT status, subtotal_paise - discount_paise,
           paid_paise + CASE WHEN NEW.kind = 'payment' THEN NEW.amount_paise ELSE -NEW.amount_paise END
      INTO v_status, v_total, v_new_paid
      FROM opd_invoices WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id FOR UPDATE;
    IF v_status IS NULL OR v_status NOT IN ('issued', 'partially_paid', 'paid') THEN
        RAISE EXCEPTION 'opd_invoice_not_payable:%', COALESCE(v_status, 'missing') USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('kriya.opd_receipt_invoice', NEW.invoice_id::TEXT, true);
    -- over-payment / over-refund violate opd_invoices_paid_consistent and abort the receipt
    UPDATE opd_invoices
       SET paid_paise = v_new_paid,
           status = CASE WHEN v_new_paid = 0 THEN 'issued'
                         WHEN v_new_paid < v_total THEN 'partially_paid'
                         ELSE 'paid' END
     WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id;
    PERFORM set_config('kriya.opd_receipt_invoice', '', true);
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION opd_guard_append_only() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND opd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
    RAISE EXCEPTION 'opd_append_only:%', TG_TABLE_NAME USING ERRCODE = 'P0001';
END $$;

-- 10.6 Closed shifts are immutable.
CREATE OR REPLACE FUNCTION opd_guard_shift() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_append_only:opd_cashier_shifts' USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'closed' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_cashier_shifts:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.cashier_admin_id <> OLD.cashier_admin_id
       OR NEW.opening_float_paise <> OLD.opening_float_paise OR NEW.opened_at <> OLD.opened_at THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_cashier_shifts' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- Trigger wiring (DROP first: idempotent re-run). BEFORE triggers fire in name
-- order, so *_guard runs before *_touch on the same table.
DROP TRIGGER IF EXISTS trg_opd_encounters_guard    ON opd_encounters;
CREATE TRIGGER trg_opd_encounters_guard    BEFORE UPDATE OR DELETE ON opd_encounters
    FOR EACH ROW EXECUTE FUNCTION opd_guard_signed_record();
DROP TRIGGER IF EXISTS trg_opd_encounters_touch    ON opd_encounters;
CREATE TRIGGER trg_opd_encounters_touch    BEFORE UPDATE ON opd_encounters
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_prescriptions_guard ON opd_prescriptions;
CREATE TRIGGER trg_opd_prescriptions_guard BEFORE UPDATE OR DELETE ON opd_prescriptions
    FOR EACH ROW EXECUTE FUNCTION opd_guard_signed_record();
DROP TRIGGER IF EXISTS trg_opd_prescriptions_touch ON opd_prescriptions;
CREATE TRIGGER trg_opd_prescriptions_touch BEFORE UPDATE ON opd_prescriptions
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_rx_items_guard      ON opd_prescription_items;
CREATE TRIGGER trg_opd_rx_items_guard      BEFORE INSERT OR UPDATE OR DELETE ON opd_prescription_items
    FOR EACH ROW EXECUTE FUNCTION opd_guard_rx_items();
DROP TRIGGER IF EXISTS trg_opd_invoices_guard      ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_guard      BEFORE UPDATE OR DELETE ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION opd_guard_invoice();
DROP TRIGGER IF EXISTS trg_opd_invoices_touch      ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_touch      BEFORE UPDATE ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_opd_inv_items_guard     ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_guard     BEFORE INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_guard_invoice_items();
DROP TRIGGER IF EXISTS trg_opd_inv_items_recalc    ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_recalc    AFTER INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_recalc_invoice_subtotal();
DROP TRIGGER IF EXISTS trg_opd_receipts_before     ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_before     BEFORE INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_receipt_before_insert();
DROP TRIGGER IF EXISTS trg_opd_receipts_after      ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_after      AFTER INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_receipt_after_insert();
DROP TRIGGER IF EXISTS trg_opd_receipts_immutable  ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_immutable  BEFORE UPDATE OR DELETE ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION opd_guard_append_only();
DROP TRIGGER IF EXISTS trg_opd_shifts_guard        ON opd_cashier_shifts;
CREATE TRIGGER trg_opd_shifts_guard        BEFORE UPDATE OR DELETE ON opd_cashier_shifts
    FOR EACH ROW EXECUTE FUNCTION opd_guard_shift();

-- ── 11. Atomic RPCs (every one takes p_clinic_id and filters on it) ────────

-- Sign (and, for an amendment, supersede the prior signed version) atomically.
-- Supersede runs first so uq_opd_encounters_one_signed never sees two rows.
CREATE OR REPLACE FUNCTION opd_sign_encounter(
    p_clinic_id UUID, p_encounter_id UUID, p_signer_admin_id UUID,
    p_signer_doctor_id UUID, p_signer_snapshot JSONB)
RETURNS SETOF opd_encounters LANGUAGE plpgsql AS $$
DECLARE e opd_encounters;
BEGIN
    SELECT * INTO e FROM opd_encounters WHERE id = p_encounter_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF e.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft' USING ERRCODE = 'P0001'; END IF;
    IF e.doctor_id <> p_signer_doctor_id THEN RAISE EXCEPTION 'opd_not_treating_doctor' USING ERRCODE = 'P0001'; END IF;
    IF e.supersedes_id IS NOT NULL THEN
        UPDATE opd_encounters SET status = 'superseded', superseded_at = now()
         WHERE id = e.supersedes_id AND clinic_id = p_clinic_id AND status = 'signed';
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_supersede_target_not_signed' USING ERRCODE = 'P0001'; END IF;
    END IF;
    RETURN QUERY
    UPDATE opd_encounters
       SET status = 'signed', signed_at = now(), signed_by_admin_id = p_signer_admin_id,
           signer_snapshot = p_signer_snapshot
     WHERE id = p_encounter_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

CREATE OR REPLACE FUNCTION opd_sign_prescription(
    p_clinic_id UUID, p_rx_id UUID, p_signer_admin_id UUID, p_signer_doctor_id UUID,
    p_signer_snapshot JSONB, p_letterhead_snapshot JSONB, p_patient_snapshot JSONB, p_allergy_review JSONB)
RETURNS SETOF opd_prescriptions LANGUAGE plpgsql AS $$
DECLARE r opd_prescriptions; v_enc_status TEXT;
BEGIN
    SELECT * INTO r FROM opd_prescriptions WHERE id = p_rx_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF r.status <> 'draft' THEN RAISE EXCEPTION 'opd_not_draft' USING ERRCODE = 'P0001'; END IF;
    IF r.doctor_id <> p_signer_doctor_id THEN RAISE EXCEPTION 'opd_not_treating_doctor' USING ERRCODE = 'P0001'; END IF;
    SELECT status INTO v_enc_status FROM opd_encounters WHERE id = r.encounter_id AND clinic_id = p_clinic_id;
    IF v_enc_status IS NULL OR v_enc_status NOT IN ('signed', 'superseded') THEN
        RAISE EXCEPTION 'opd_encounter_not_signed' USING ERRCODE = 'P0001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM opd_prescription_items WHERE prescription_id = p_rx_id AND clinic_id = p_clinic_id) THEN
        RAISE EXCEPTION 'opd_prescription_empty' USING ERRCODE = 'P0001';
    END IF;
    IF r.supersedes_id IS NOT NULL THEN
        UPDATE opd_prescriptions SET status = 'superseded', superseded_at = now()
         WHERE id = r.supersedes_id AND clinic_id = p_clinic_id AND status = 'signed';
        IF NOT FOUND THEN RAISE EXCEPTION 'opd_supersede_target_not_signed' USING ERRCODE = 'P0001'; END IF;
    END IF;
    RETURN QUERY
    UPDATE opd_prescriptions
       SET status = 'signed', signed_at = now(), signed_by_admin_id = p_signer_admin_id,
           signer_snapshot = p_signer_snapshot, letterhead_snapshot = p_letterhead_snapshot,
           patient_snapshot = p_patient_snapshot, allergy_review = p_allergy_review
     WHERE id = p_rx_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- Close a shift: expected = float + cash in − cash refunded, computed under the shift lock.
CREATE OR REPLACE FUNCTION opd_close_shift(
    p_clinic_id UUID, p_shift_id UUID, p_admin_id UUID, p_declared_paise INTEGER, p_notes TEXT)
RETURNS SETOF opd_cashier_shifts LANGUAGE plpgsql AS $$
DECLARE s opd_cashier_shifts; v_expected INTEGER;
BEGIN
    SELECT * INTO s FROM opd_cashier_shifts WHERE id = p_shift_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    IF s.status <> 'open' THEN RAISE EXCEPTION 'opd_shift_not_open' USING ERRCODE = 'P0001'; END IF;
    IF s.cashier_admin_id <> p_admin_id THEN RAISE EXCEPTION 'opd_shift_not_yours' USING ERRCODE = 'P0001'; END IF;
    SELECT s.opening_float_paise
         + COALESCE(SUM(CASE WHEN kind = 'payment' THEN amount_paise ELSE -amount_paise END), 0)
      INTO v_expected
      FROM opd_receipts WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash';
    RETURN QUERY
    UPDATE opd_cashier_shifts
       SET status = 'closed', closed_at = now(), expected_cash_paise = v_expected,
           declared_cash_paise = p_declared_paise, close_notes = p_notes
     WHERE id = p_shift_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- Owner-only clinic deletion path: the ONLY way signed clinical/financial rows
-- can be removed. Self-referencing supersedes FKs are NO ACTION (checked at
-- statement end), so one DELETE per table removes a whole version chain.
CREATE OR REPLACE FUNCTION opd_purge_clinic(p_clinic_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM opd_receipts           WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('receipts', n);
    DELETE FROM opd_invoice_items      WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_invoices           WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('invoices', n);
    DELETE FROM opd_cashier_shifts     WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_prescription_items WHERE clinic_id = p_clinic_id;
    DELETE FROM opd_prescriptions      WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('prescriptions', n);
    DELETE FROM opd_encounters         WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('encounters', n);
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 12. RLS parity (049 pattern) + API-role lockout ────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['opd_encounters', 'opd_prescriptions', 'opd_prescription_items',
                             'opd_invoices', 'opd_invoice_items', 'opd_receipts', 'opd_cashier_shifts']
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

-- Supabase exposes public functions at /rest/v1/rpc to API roles by default.
DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'opd_next_counter(uuid,text)', 'opd_assign_mrn(uuid,uuid,uuid)',
        'opd_sign_encounter(uuid,uuid,uuid,uuid,jsonb)',
        'opd_sign_prescription(uuid,uuid,uuid,uuid,jsonb,jsonb,jsonb,jsonb)',
        'opd_close_shift(uuid,uuid,uuid,integer,text)', 'opd_purge_clinic(uuid)']
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

-- ── 13. Verify (fails the transaction if anything is missing) ──────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN (
          'opd_encounters', 'opd_prescriptions', 'opd_prescription_items', 'opd_invoices',
          'opd_invoice_items', 'opd_receipts', 'opd_cashier_shifts')) <> 7 THEN
        RAISE EXCEPTION '103 verify: OPD tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_indexes WHERE tablename = 'appointments'
          AND indexname IN ('uq_appointment_active_slot', 'uq_appointment_active_slot_unassigned')
          AND indexdef ILIKE '%is_walk_in = false%') <> 2 THEN
        RAISE EXCEPTION '103 verify: slot guards not rebuilt';
    END IF;
END $$;
