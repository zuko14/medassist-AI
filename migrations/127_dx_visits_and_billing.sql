-- ============================================================================
-- Migration 127: Kriya Diagnostic OS visits, items, samples, orders, billing (DX-M3)
-- Referrers, B2B account identity, visits (accessions), sold items, tubes,
-- the visit timeline; opd_orders gains source = 'dx'; DX bills are OPD
-- invoices whose lines only DX functions write (AUDIT A10).
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §4, ADR-DX-03/04/05/20/21.
-- Additive + backward compatible: every new column is NULL; the opd_orders
-- constraints only gain a 'dx' branch, so OPD / IPD rows validate as before.
-- OPD tables reference DX rows by plain UUID (no FK) so the clinic purge stays
-- one-directional: dx_purge_clinic first, then the OPD/IPD purge.
-- Every state change happens inside a dx_* function (kriya.dx_tx); the guards
-- refuse direct writes, so a future code path cannot bypass pricing or numbering.
-- ============================================================================

-- ── 1. Helpers ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.dx_tx', true), '') = p_clinic_id::TEXT
$$;

-- ── 2. Referrers and B2B accounts (identity; money columns arrive in DX-M8) ─
CREATE TABLE IF NOT EXISTS dx_b2b_accounts (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code                       TEXT NULL CHECK (code ~ '^[A-Z0-9]{2,12}$'),
    name                       TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    kind                       TEXT NOT NULL CHECK (kind IN ('hospital', 'clinic', 'franchise', 'corporate',
                                                             'insurance_tpa', 'camp', 'other')),
    contact_name               TEXT NULL CHECK (char_length(contact_name) <= 100),
    contact_phone              TEXT NULL CHECK (contact_phone ~ '^\+?[0-9]{10,15}$'),
    desk_whatsapp              TEXT NULL CHECK (desk_whatsapp ~ '^\+?[0-9]{10,15}$'),
    report_delivery            TEXT NOT NULL DEFAULT 'patient'
                               CHECK (report_delivery IN ('patient', 'account', 'both', 'none')),
    patient_copy_authorised_at TIMESTAMPTZ NULL,
    patient_copy_authorised_by UUID NULL,
    patient_copy_reason        TEXT NULL CHECK (char_length(btrim(patient_copy_reason)) BETWEEN 10 AND 300),
    holder_patient_id          UUID NULL REFERENCES patients (id),
    default_billing_mode       TEXT NOT NULL DEFAULT 'b2b_credit'
                               CHECK (default_billing_mode IN ('patient', 'b2b_credit', 'b2b_prepaid', 'b2b_split')),
    white_label                BOOLEAN NOT NULL DEFAULT false,
    template_id                UUID NULL,
    status                     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'on_hold', 'closed')),
    hold_reason                TEXT NULL CHECK (char_length(hold_reason) <= 300),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_b2b_accounts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_b2b_accounts_clinic_code_key UNIQUE (clinic_id, code),
    -- ADR-DX-20: payer-routed accounts never default to the patient
    CONSTRAINT dx_b2b_payer_routed CHECK (kind NOT IN ('insurance_tpa', 'corporate')
        OR report_delivery IN ('account', 'none')
        OR (report_delivery = 'both' AND patient_copy_authorised_at IS NOT NULL AND patient_copy_reason IS NOT NULL)),
    CONSTRAINT dx_b2b_authorisation_shape CHECK ((patient_copy_authorised_at IS NULL) = (patient_copy_reason IS NULL))
);

CREATE TABLE IF NOT EXISTS dx_referrers (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('doctor', 'hospital', 'clinic', 'corporate', 'other')),
    name             TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    qualification    TEXT NULL CHECK (char_length(qualification) <= 120),
    registration_no  TEXT NULL CHECK (char_length(registration_no) <= 60),
    phone            TEXT NULL CHECK (phone ~ '^\+?[0-9]{10,15}$'),
    email            TEXT NULL CHECK (char_length(email) <= 120),
    organisation     TEXT NULL CHECK (char_length(organisation) <= 120),
    b2b_account_id   UUID NULL,
    report_copy      TEXT NOT NULL DEFAULT 'none' CHECK (report_copy IN ('none', 'whatsapp', 'email')),
    name_key         TEXT GENERATED ALWAYS AS (upper(regexp_replace(name, '[^A-Za-z0-9]', '', 'g'))) STORED,
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_referrers_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_referrers_account_fk FOREIGN KEY (clinic_id, b2b_account_id) REFERENCES dx_b2b_accounts (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_referrers_key ON dx_referrers (clinic_id, name_key);

DROP TRIGGER IF EXISTS trg_dx_b2b_accounts_updated_at ON dx_b2b_accounts;
CREATE TRIGGER trg_dx_b2b_accounts_updated_at BEFORE UPDATE ON dx_b2b_accounts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ── 3. Visits ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_accessions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id            UUID NOT NULL,
    accession_year       SMALLINT NOT NULL,
    accession_seq        INTEGER NOT NULL CHECK (accession_seq > 0),
    accession_number     TEXT GENERATED ALWAYS AS (opd_format_number('LAB-', accession_year, accession_seq, 6)) STORED,
    patient_id           UUID NOT NULL,
    family_member_id     UUID NULL,
    source               TEXT NOT NULL CHECK (source IN ('walk_in', 'whatsapp_booking', 'voice_booking', 'home_collection',
                                                         'b2b', 'partner_portal', 'opd', 'ipd', 'camp')),
    referrer_id          UUID NULL,
    referrer_snapshot    JSONB NULL,
    b2b_account_id       UUID NULL,
    external_ref         TEXT NULL CHECK (char_length(external_ref) <= 40),
    patient_type         TEXT NOT NULL DEFAULT 'op' CHECK (patient_type IN ('op', 'ip', 'home', 'b2b')),
    billing_mode         TEXT NOT NULL DEFAULT 'patient'
                         CHECK (billing_mode IN ('patient', 'b2b_credit', 'b2b_prepaid', 'b2b_split', 'internal')),
    sample_seq           INTEGER NULL CHECK (sample_seq > 0),
    priority             TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine', 'urgent', 'stat')),
    fasting_status       TEXT NOT NULL DEFAULT 'unknown' CHECK (fasting_status IN ('fasting', 'non_fasting', 'unknown')),
    last_meal_at         TIMESTAMPTZ NULL,
    clinical_notes       TEXT NULL CHECK (char_length(clinical_notes) <= 1000),
    patient_snapshot     JSONB NOT NULL,
    delivery_prefs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    template_override_id UUID NULL,
    status               TEXT NOT NULL DEFAULT 'registered'
                         CHECK (status IN ('registered', 'in_process', 'partially_reported', 'reported', 'cancelled')),
    cancelled_at         TIMESTAMPTZ NULL,
    cancelled_by         UUID NULL,
    cancellation_reason  TEXT NULL CHECK (char_length(btrim(cancellation_reason)) BETWEEN 3 AND 300),
    idempotency_key      TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_by           UUID NULL,
    created_by_name      TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 1 AND 100),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_accessions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_accessions_number_key UNIQUE (clinic_id, accession_year, accession_seq),
    CONSTRAINT dx_accessions_b2b_needs_account CHECK (billing_mode NOT LIKE 'b2b%' OR b2b_account_id IS NOT NULL),
    CONSTRAINT dx_accessions_cancel_consistent CHECK (
        (status = 'cancelled') = (cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)),
    CONSTRAINT dx_accessions_branch_fk   FOREIGN KEY (clinic_id, branch_id)        REFERENCES branches (clinic_id, id),
    CONSTRAINT dx_accessions_patient_fk  FOREIGN KEY (clinic_id, patient_id)       REFERENCES patients (clinic_id, id),
    CONSTRAINT dx_accessions_family_fk   FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT dx_accessions_referrer_fk FOREIGN KEY (clinic_id, referrer_id)      REFERENCES dx_referrers (clinic_id, id),
    CONSTRAINT dx_accessions_account_fk  FOREIGN KEY (clinic_id, b2b_account_id)   REFERENCES dx_b2b_accounts (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_accessions_idem ON dx_accessions (clinic_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_accessions_sample_seq ON dx_accessions (clinic_id, accession_year, sample_seq)
    WHERE sample_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dx_accessions_day ON dx_accessions (clinic_id, branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_accessions_patient ON dx_accessions (clinic_id, patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_accessions_account ON dx_accessions (clinic_id, b2b_account_id, created_at DESC)
    WHERE b2b_account_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS dx_accession_bookings (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id       UUID NOT NULL,
    appointment_id     UUID NOT NULL,
    prepaid_applied_at TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_accession_bookings_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_accession_bookings_appointment_key UNIQUE (clinic_id, appointment_id),   -- one visit per booking
    CONSTRAINT dx_accession_bookings_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_accession_bookings_appointment_fk FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS dx_accession_items (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id          UUID NOT NULL,
    lab_test_id           UUID NULL,     -- reference (snapshotted); the lab-test delete refuses a test used here
    kind                  TEXT NOT NULL CHECK (kind IN ('test', 'panel', 'package', 'fee')),
    fee_code              TEXT NULL CHECK (fee_code IN ('home_collection', 'stat_surcharge', 'report_courier', 'other')),
    source_appointment_id UUID NULL,
    item_name             TEXT NOT NULL CHECK (char_length(btrim(item_name)) BETWEEN 1 AND 200),
    test_code             TEXT NULL,
    mrp_paise             INTEGER NOT NULL CHECK (mrp_paise BETWEEN 0 AND 100000000),
    price_paise           INTEGER NOT NULL CHECK (price_paise BETWEEN 0 AND 100000000),
    discount_paise        INTEGER NOT NULL DEFAULT 0 CHECK (discount_paise BETWEEN 0 AND price_paise),
    patient_share_paise   INTEGER NOT NULL,
    payer_share_paise     INTEGER NOT NULL,
    price_source          TEXT NOT NULL CHECK (price_source IN ('mrp', 'rate_card', 'manual', 'booking', 'fee')),
    rate_card_id          UUID NULL,
    status                TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    cancelled_at          TIMESTAMPTZ NULL,
    cancelled_by          UUID NULL,
    cancellation_reason   TEXT NULL CHECK (char_length(btrim(cancellation_reason)) BETWEEN 3 AND 300),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),   -- lines keep their entry order
    CONSTRAINT dx_accession_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_accession_items_fee_shape CHECK ((kind = 'fee') = (lab_test_id IS NULL)),
    CONSTRAINT dx_accession_items_fee_code CHECK ((kind = 'fee') = (fee_code IS NOT NULL)),
    CONSTRAINT dx_accession_items_shares CHECK (patient_share_paise >= 0 AND payer_share_paise >= 0
        AND patient_share_paise + payer_share_paise = price_paise - discount_paise),
    CONSTRAINT dx_accession_items_cancel_consistent CHECK (
        (status = 'cancelled') = (cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL)),
    CONSTRAINT dx_accession_items_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_items_accession ON dx_accession_items (clinic_id, accession_id);
CREATE INDEX IF NOT EXISTS idx_dx_items_test ON dx_accession_items (clinic_id, lab_test_id) WHERE lab_test_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS dx_samples (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id          UUID NOT NULL,
    container_id          UUID NOT NULL,
    sample_type           TEXT NOT NULL,
    barcode               TEXT NULL CHECK (barcode ~ '^[A-Za-z0-9._/-]{3,64}$'),
    barcode_source        TEXT NOT NULL CHECK (barcode_source IN ('generated', 'preprinted', 'external')),
    tube_seq              INTEGER NULL,
    collection_site_id    UUID NOT NULL,
    processing_site_id    UUID NULL,
    status                TEXT NOT NULL DEFAULT 'pending_collection'
                          CHECK (status IN ('pending_collection', 'collected', 'in_transit', 'received', 'rejected',
                                            'stored', 'discarded')),
    collected_at          TIMESTAMPTZ NULL,
    collected_by          UUID NULL,
    collected_by_name     TEXT NULL,
    collection_mode       TEXT NULL CHECK (collection_mode IN ('centre', 'home', 'ward', 'partner')),
    received_at           TIMESTAMPTZ NULL,
    received_by           UUID NULL,
    received_by_name      TEXT NULL,
    received_site_id      UUID NULL,
    rejection_reason_code TEXT NULL,
    rejection_notes       TEXT NULL,
    rejected_at           TIMESTAMPTZ NULL,
    rejected_by           UUID NULL,
    recollect_of          UUID NULL,
    storage_location      TEXT NULL,
    volume_ml             NUMERIC(5,1) NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_samples_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_samples_barcode_shape CHECK (barcode IS NOT NULL
        OR (barcode_source IN ('preprinted', 'external') AND status = 'pending_collection')),
    CONSTRAINT dx_samples_generated_seq CHECK ((barcode_source = 'generated') = (tube_seq IS NOT NULL)),
    CONSTRAINT dx_samples_rejected_consistent CHECK (
        (status = 'rejected') = (rejected_at IS NOT NULL AND rejection_reason_code IS NOT NULL)),
    CONSTRAINT dx_samples_accession_fk  FOREIGN KEY (clinic_id, accession_id)       REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_samples_container_fk  FOREIGN KEY (clinic_id, container_id)       REFERENCES dx_containers (clinic_id, id),
    CONSTRAINT dx_samples_collection_fk FOREIGN KEY (clinic_id, collection_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_samples_processing_fk FOREIGN KEY (clinic_id, processing_site_id) REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_samples_received_fk   FOREIGN KEY (clinic_id, received_site_id)   REFERENCES dx_sites (clinic_id, id),
    CONSTRAINT dx_samples_recollect_fk  FOREIGN KEY (clinic_id, recollect_of)       REFERENCES dx_samples (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_samples_barcode ON dx_samples (clinic_id, barcode) WHERE barcode IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dx_samples_queue ON dx_samples (clinic_id, status, processing_site_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dx_samples_accession ON dx_samples (clinic_id, accession_id);

CREATE TABLE IF NOT EXISTS dx_events (
    id           BIGSERIAL PRIMARY KEY,
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    accession_id UUID NOT NULL,
    sample_id    UUID NULL,
    order_id     UUID NULL,
    kind         TEXT NOT NULL CHECK (char_length(kind) BETWEEN 2 AND 40),
    actor_id     UUID NULL,
    actor_name   TEXT NULL,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_events_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_events_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_events_accession ON dx_events (clinic_id, accession_id, id);

-- ── 4. opd_orders: source = 'dx' (ADR-DX-03) ───────────────────────────────
ALTER TABLE opd_orders
    ADD COLUMN IF NOT EXISTS accession_id      UUID NULL,
    ADD COLUMN IF NOT EXISTS accession_item_id UUID NULL,
    ADD COLUMN IF NOT EXISTS dx_sample_id      UUID NULL,
    ADD COLUMN IF NOT EXISTS replaces_order_id UUID NULL;
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_source_check;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_source_check CHECK (source IN ('opd', 'ipd', 'dx'));
ALTER TABLE opd_orders ALTER COLUMN doctor_id DROP NOT NULL;
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_doctor_required;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_doctor_required CHECK (source = 'dx' OR doctor_id IS NOT NULL);
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_source_shape;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_source_shape CHECK (
    (source = 'opd' AND encounter_id IS NOT NULL AND appointment_id IS NOT NULL
                    AND admission_id IS NULL AND ipd_order_id IS NULL)
 OR (source = 'ipd' AND admission_id IS NOT NULL AND ipd_order_id IS NOT NULL
                    AND encounter_id IS NULL AND appointment_id IS NULL)
 OR (source = 'dx'  AND accession_id IS NOT NULL AND accession_item_id IS NOT NULL
                    AND encounter_id IS NULL AND admission_id IS NULL AND ipd_order_id IS NULL
                    AND appointment_id IS NULL));
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_accession_fk;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_dx_accession_fk FOREIGN KEY (clinic_id, accession_id)
    REFERENCES dx_accessions (clinic_id, id);
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_item_fk;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_dx_item_fk FOREIGN KEY (clinic_id, accession_item_id)
    REFERENCES dx_accession_items (clinic_id, id);
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_dx_sample_fk;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_dx_sample_fk FOREIGN KEY (clinic_id, dx_sample_id)
    REFERENCES dx_samples (clinic_id, id);
ALTER TABLE opd_orders DROP CONSTRAINT IF EXISTS opd_orders_replaces_fk;
ALTER TABLE opd_orders ADD CONSTRAINT opd_orders_replaces_fk FOREIGN KEY (clinic_id, replaces_order_id)
    REFERENCES opd_orders (clinic_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_orders_dx_item_test ON opd_orders (clinic_id, accession_item_id, catalog_reference_id)
    WHERE source = 'dx' AND status <> 'cancelled' AND replaces_order_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_opd_orders_dx_accession ON opd_orders (clinic_id, accession_id) WHERE accession_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_orders_dx_sample ON opd_orders (clinic_id, dx_sample_id) WHERE dx_sample_id IS NOT NULL;

-- ── 5. Billing link, B2B subjects ──────────────────────────────────────────
ALTER TABLE opd_invoices ADD COLUMN IF NOT EXISTS accession_id UUID NULL;       -- reference only (purge order)
CREATE INDEX IF NOT EXISTS idx_opd_invoices_accession ON opd_invoices (clinic_id, accession_id) WHERE accession_id IS NOT NULL;
ALTER TABLE opd_invoice_items ADD COLUMN IF NOT EXISTS dx_accession_item_id UUID NULL;
CREATE INDEX IF NOT EXISTS idx_opd_invoice_items_dx_item ON opd_invoice_items (clinic_id, dx_accession_item_id)
    WHERE dx_accession_item_id IS NOT NULL;
ALTER TABLE family_members ADD COLUMN IF NOT EXISTS dx_b2b_account_id UUID NULL;
ALTER TABLE family_members DROP CONSTRAINT IF EXISTS family_members_dx_b2b_fk;
ALTER TABLE family_members ADD CONSTRAINT family_members_dx_b2b_fk FOREIGN KEY (clinic_id, dx_b2b_account_id)
    REFERENCES dx_b2b_accounts (clinic_id, id);
CREATE INDEX IF NOT EXISTS idx_family_members_dx_b2b ON family_members (clinic_id, dx_b2b_account_id)
    WHERE dx_b2b_account_id IS NOT NULL;

-- ── 6. Guards ──────────────────────────────────────────────────────────────
-- Visit tables change only inside dx_* functions; rows go only on the owner purge.
-- One exception: a booking link's prepaid_applied_at is stamped once by the service.
CREATE OR REPLACE FUNCTION dx_guard_tx() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF TG_OP = 'DELETE' AND dx_purging(v_clinic) THEN RETURN OLD; END IF;
    IF TG_TABLE_NAME = 'dx_events' THEN
        IF TG_OP = 'INSERT' AND dx_in_tx(v_clinic) THEN RETURN NEW; END IF;
        PERFORM dx_raise('dx_append_only:dx_events');
    END IF;
    IF TG_TABLE_NAME = 'dx_accession_bookings' AND TG_OP = 'UPDATE' THEN    -- jsonb: OLD's fields differ per table
        IF to_jsonb(OLD) ->> 'prepaid_applied_at' IS NULL
           AND (to_jsonb(NEW) - 'prepaid_applied_at') = (to_jsonb(OLD) - 'prepaid_applied_at') THEN
            RETURN NEW;
        END IF;
    END IF;
    IF dx_in_tx(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    PERFORM dx_raise('dx_managed:' || TG_TABLE_NAME);
    RETURN NULL;
END $$;
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_accessions', 'dx_accession_bookings', 'dx_accession_items', 'dx_samples', 'dx_events']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION dx_guard_tx()',
                       'trg_' || t || '_guard', t);
    END LOOP;
END $$;
DROP TRIGGER IF EXISTS trg_dx_accessions_touch ON dx_accessions;
CREATE TRIGGER trg_dx_accessions_touch BEFORE UPDATE ON dx_accessions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
DROP TRIGGER IF EXISTS trg_dx_samples_touch ON dx_samples;
CREATE TRIGGER trg_dx_samples_touch BEFORE UPDATE ON dx_samples FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- AUDIT A10: lines of a DX bill are written only by DX functions; a DX line is the
-- item's patient share on its own visit's bill, once. The purge paths are exempt.
CREATE OR REPLACE FUNCTION dx_guard_invoice_item() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_clinic UUID := COALESCE(NEW.clinic_id, OLD.clinic_id); v_acc UUID; it dx_accession_items;
BEGIN
    IF dx_purging(v_clinic) OR opd_purging(v_clinic) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT accession_id INTO v_acc FROM opd_invoices
     WHERE clinic_id = v_clinic AND id = COALESCE(NEW.invoice_id, OLD.invoice_id);
    IF v_acc IS NULL AND (TG_OP = 'DELETE' OR NEW.dx_accession_item_id IS NULL) THEN RETURN COALESCE(NEW, OLD); END IF;
    IF NOT dx_in_tx(v_clinic) THEN PERFORM dx_raise('dx_bill_lines_managed'); END IF;
    IF TG_OP <> 'DELETE' AND NEW.dx_accession_item_id IS NOT NULL THEN
        SELECT * INTO it FROM dx_accession_items
         WHERE clinic_id = v_clinic AND id = NEW.dx_accession_item_id FOR UPDATE;
        IF NOT FOUND OR it.accession_id IS DISTINCT FROM v_acc OR it.status <> 'active'
           OR NEW.unit_price_paise <> it.patient_share_paise OR NEW.quantity <> 1 OR NEW.discount_paise <> 0
           OR NEW.catalog_code IS NOT NULL OR NEW.item_type <> 'diagnostic' THEN
            PERFORM dx_raise('dx_bill_line_invalid');
        END IF;
        IF EXISTS (SELECT 1 FROM opd_invoice_items l JOIN opd_invoices i ON i.clinic_id = l.clinic_id AND i.id = l.invoice_id
                    WHERE l.clinic_id = v_clinic AND l.dx_accession_item_id = NEW.dx_accession_item_id
                      AND l.id <> NEW.id AND i.status <> 'void') THEN
            PERFORM dx_raise('dx_item_already_billed');
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
DROP TRIGGER IF EXISTS trg_dx_guard_invoice_item ON opd_invoice_items;
CREATE TRIGGER trg_dx_guard_invoice_item BEFORE INSERT OR UPDATE OR DELETE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION dx_guard_invoice_item();

-- a bill's visit link is part of its identity
CREATE OR REPLACE FUNCTION dx_guard_invoice_accession() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.accession_id IS DISTINCT FROM OLD.accession_id AND NOT dx_purging(OLD.clinic_id) THEN
        PERFORM dx_raise('dx_identity_immutable:opd_invoices');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_invoice_accession ON opd_invoices;
CREATE TRIGGER trg_dx_invoice_accession BEFORE UPDATE OF accession_id ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION dx_guard_invoice_accession();

-- AUDIT A22: a test ordered in DX keeps its row (signed history points at it); deactivate instead
CREATE OR REPLACE FUNCTION dx_guard_lab_test_delete() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT dx_purging(OLD.clinic_id) AND EXISTS (
        SELECT 1 FROM dx_accession_items WHERE clinic_id = OLD.clinic_id AND lab_test_id = OLD.id) THEN
        PERFORM dx_raise('dx_test_ordered');
    END IF;
    RETURN OLD;
END $$;
DROP TRIGGER IF EXISTS trg_dx_lab_test_delete ON lab_tests;
CREATE TRIGGER trg_dx_lab_test_delete BEFORE DELETE ON lab_tests FOR EACH ROW EXECUTE FUNCTION dx_guard_lab_test_delete();

-- ── 7. Pricing and explosion (shared by register, add items and the quote) ──
-- Member tests of a sold item: a test is itself; a panel its tests; a package
-- its tests and its panels' tests (kinds make depth <= 2, migration 126).
CREATE OR REPLACE FUNCTION dx_explode(p_clinic_id UUID, p_test_id UUID) RETURNS SETOF UUID LANGUAGE sql STABLE AS $$
    SELECT t.id FROM lab_tests t WHERE t.clinic_id = p_clinic_id AND t.id = p_test_id AND t.kind = 'test'
    UNION
    SELECT c.child_test_id FROM dx_test_components c JOIN lab_tests k ON k.clinic_id = c.clinic_id AND k.id = c.child_test_id
     WHERE c.clinic_id = p_clinic_id AND c.parent_test_id = p_test_id AND k.kind = 'test'
    UNION
    SELECT c2.child_test_id FROM dx_test_components c1
      JOIN dx_test_components c2 ON c2.clinic_id = c1.clinic_id AND c2.parent_test_id = c1.child_test_id
     WHERE c1.clinic_id = p_clinic_id AND c1.parent_test_id = p_test_id
$$;

CREATE OR REPLACE FUNCTION dx_test_ready(p_clinic_id UUID, p_test_id UUID, p_gender TEXT) RETURNS lab_tests
LANGUAGE plpgsql STABLE AS $$
DECLARE t lab_tests;
BEGIN
    SELECT * INTO t FROM lab_tests WHERE clinic_id = p_clinic_id AND id = p_test_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_test_not_found'); END IF;
    IF NOT t.is_active THEN PERFORM dx_raise('dx_test_inactive:' || t.name); END IF;
    IF t.approved_at IS NULL THEN PERFORM dx_raise('dx_test_not_approved:' || t.name); END IF;
    IF t.gender_restriction <> 'all' AND p_gender IN ('male', 'female') AND t.gender_restriction <> p_gender THEN
        PERFORM dx_raise('dx_gender_restricted:' || t.name);
    END IF;
    RETURN t;
END $$;

-- p_ctx: {clinic_id, branch_id, gender, billing_mode, discount_limit_percent}
-- p_items: [{lab_test_id, discount_paise?, source_appointment_id?} | {fee_code, amount_paise, item_name?, source_appointment_id?}]
-- returns one priced line per item with its member tests (container, sample type, result mode).
CREATE OR REPLACE FUNCTION dx_price_items(p_ctx JSONB, p_items JSONB) RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_clinic UUID := (p_ctx ->> 'clinic_id')::UUID;
    v_branch UUID := NULLIF(p_ctx ->> 'branch_id', '')::UUID;
    v_gender TEXT := p_ctx ->> 'gender';
    v_mode   TEXT := COALESCE(p_ctx ->> 'billing_mode', 'patient');
    v_limit  NUMERIC := COALESCE((p_ctx ->> 'discount_limit_percent')::NUMERIC, 0);
    r JSONB; t lab_tests; m lab_tests; v_out JSONB := '[]'::jsonb; v_tests JSONB; v_price INTEGER; v_disc INTEGER;
    v_net INTEGER; v_member UUID; v_cont dx_containers;
BEGIN
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        PERFORM dx_raise('dx_no_items');
    END IF;
    IF v_mode LIKE 'b2b%' THEN PERFORM dx_raise('dx_b2b_not_enabled'); END IF;   -- B2B ledger arrives with DX-M8
    FOR r IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_disc := COALESCE((r ->> 'discount_paise')::INTEGER, 0);
        IF r ? 'fee_code' THEN
            v_price := (r ->> 'amount_paise')::INTEGER;
            IF v_price IS NULL OR v_price < 0 THEN PERFORM dx_raise('dx_fee_invalid'); END IF;
            v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price END;
            v_out := v_out || jsonb_build_object(
                'kind', 'fee', 'fee_code', r ->> 'fee_code', 'lab_test_id', NULL,
                'item_name', COALESCE(NULLIF(btrim(r ->> 'item_name'), ''), initcap(replace(r ->> 'fee_code', '_', ' '))),
                'test_code', NULL, 'mrp_paise', v_price, 'price_paise', v_price, 'discount_paise', 0,
                'patient_share_paise', v_net, 'payer_share_paise', 0, 'price_source', 'fee',
                'source_appointment_id', r -> 'source_appointment_id', 'tests', '[]'::jsonb);
            CONTINUE;
        END IF;
        t := dx_test_ready(v_clinic, (r ->> 'lab_test_id')::UUID, v_gender);
        -- branch row -> clinic row price (rate cards join in DX-M8)
        SELECT COALESCE((SELECT b.price_paise FROM lab_tests b
                          WHERE b.clinic_id = v_clinic AND b.branch_id = v_branch AND b.is_active
                            AND t.test_code IS NOT NULL AND upper(b.test_code) = upper(t.test_code) LIMIT 1),
                        t.price_paise) INTO v_price;
        IF v_disc < 0 OR v_disc > v_price THEN PERFORM dx_raise('dx_discount_invalid'); END IF;
        IF v_disc > floor(v_price * v_limit / 100) THEN PERFORM dx_raise('dx_discount_over_limit'); END IF;
        v_tests := '[]'::jsonb;
        FOR v_member IN SELECT dx_explode(v_clinic, t.id) LOOP
            m := dx_test_ready(v_clinic, v_member, v_gender);
            v_cont := NULL;
            IF m.dx_result_mode = 'parameters' AND m.container_id IS NOT NULL THEN
                SELECT * INTO v_cont FROM dx_containers WHERE clinic_id = v_clinic AND id = m.container_id;
            END IF;
            v_tests := v_tests || jsonb_build_object('lab_test_id', m.id, 'name', m.name, 'test_code', m.test_code,
                'container_id', v_cont.id, 'container_code', v_cont.code, 'sample_type', v_cont.sample_type,
                'result_mode', m.dx_result_mode);
        END LOOP;
        IF jsonb_array_length(v_tests) = 0 THEN PERFORM dx_raise('dx_group_empty:' || t.name); END IF;
        v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price - v_disc END;
        v_out := v_out || jsonb_build_object(
            'kind', t.kind, 'fee_code', NULL, 'lab_test_id', t.id, 'item_name', t.name, 'test_code', t.test_code,
            'mrp_paise', t.price_paise, 'price_paise', v_price, 'discount_paise', CASE WHEN v_mode = 'internal' THEN v_price ELSE v_disc END,
            'patient_share_paise', v_net, 'payer_share_paise', 0,
            'price_source', CASE WHEN r ? 'source_appointment_id' AND r ->> 'source_appointment_id' IS NOT NULL THEN 'booking'
                                 ELSE 'mrp' END,
            'source_appointment_id', r -> 'source_appointment_id', 'tests', v_tests);
    END LOOP;
    RETURN v_out;
END $$;

-- Sample sequence: the counter, raised once to the go-live floor (never lowered, AUDIT A8).
CREATE OR REPLACE FUNCTION dx_next_sample_seq(p_clinic_id UUID, p_year INTEGER) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE v INTEGER; v_floor INTEGER;
BEGIN
    v := opd_next_counter(p_clinic_id, 'dxs:' || p_year);
    SELECT COALESCE((dx_settings ->> 'sample_seq_floor')::INTEGER, 1) INTO v_floor FROM clinics WHERE id = p_clinic_id;
    IF v < v_floor THEN
        UPDATE clinics SET opd_counters = jsonb_set(opd_counters, ARRAY['dxs:' || p_year], to_jsonb(v_floor), true)
         WHERE id = p_clinic_id;
        v := v_floor;
    END IF;
    RETURN v;
END $$;

CREATE OR REPLACE FUNCTION dx_log(p_clinic_id UUID, p_accession_id UUID, p_kind TEXT, p_actor_id UUID,
                                  p_actor_name TEXT, p_details JSONB) RETURNS VOID LANGUAGE sql AS $$
    INSERT INTO dx_events (clinic_id, accession_id, kind, actor_id, actor_name, details)
    VALUES (p_clinic_id, p_accession_id, p_kind, p_actor_id, left(p_actor_name, 100), COALESCE(p_details, '{}'::jsonb))
$$;

-- Insert priced lines, their orders, and the tubes they need. Specimen orders join a
-- pending tube of the same container and processing site, else a new tube: the visit's
-- shared sequence for the first tube of a container, a fresh sequence for a repeat (ADR-DX-05).
CREATE OR REPLACE FUNCTION dx_insert_lines(p_clinic_id UUID, p_accession_id UUID, p_lines JSONB, p_actor_id UUID,
                                           p_actor_name TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    a dx_accessions; l JSONB; tst JSONB; v_item UUID; v_sample UUID; v_seq INTEGER; v_collect UUID; v_process UUID;
    v_n INTEGER := 0; v_cont_code TEXT;
BEGIN
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id;
    SELECT id INTO v_collect FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = a.branch_id AND is_active;
    IF v_collect IS NULL THEN PERFORM dx_raise('dx_no_collection_site'); END IF;
    -- processing site: the registration site if it processes, else the clinic's only processing site
    SELECT id INTO v_process FROM dx_sites WHERE clinic_id = p_clinic_id AND id = v_collect AND kind <> 'collection';
    IF v_process IS NULL THEN
        SELECT CASE WHEN count(*) = 1 THEN min(id::TEXT)::UUID END INTO v_process
          FROM dx_sites WHERE clinic_id = p_clinic_id AND is_active AND kind <> 'collection';
    END IF;
    FOR l IN SELECT * FROM jsonb_array_elements(p_lines) LOOP
        INSERT INTO dx_accession_items (clinic_id, accession_id, lab_test_id, kind, fee_code, source_appointment_id,
                                        item_name, test_code, mrp_paise, price_paise, discount_paise,
                                        patient_share_paise, payer_share_paise, price_source)
        VALUES (p_clinic_id, p_accession_id, NULLIF(l ->> 'lab_test_id', '')::UUID, l ->> 'kind', l ->> 'fee_code',
                NULLIF(l ->> 'source_appointment_id', '')::UUID, left(l ->> 'item_name', 200), l ->> 'test_code',
                (l ->> 'mrp_paise')::INTEGER, (l ->> 'price_paise')::INTEGER, (l ->> 'discount_paise')::INTEGER,
                (l ->> 'patient_share_paise')::INTEGER, (l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source')
        RETURNING id INTO v_item;
        v_n := v_n + 1;
        FOR tst IN SELECT * FROM jsonb_array_elements(l -> 'tests') LOOP
            v_sample := NULL;
            IF tst ->> 'container_id' IS NOT NULL THEN
                IF v_process IS NULL THEN PERFORM dx_raise('dx_no_processing_site'); END IF;
                SELECT id INTO v_sample FROM dx_samples
                 WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id AND status = 'pending_collection'
                   AND container_id = (tst ->> 'container_id')::UUID AND processing_site_id = v_process
                 ORDER BY created_at LIMIT 1;
                IF v_sample IS NULL THEN
                    IF NOT EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND accession_id = p_accession_id
                                                              AND container_id = (tst ->> 'container_id')::UUID)
                       AND a.sample_seq IS NOT NULL THEN
                        v_seq := a.sample_seq;
                    ELSE
                        v_seq := dx_next_sample_seq(p_clinic_id, a.accession_year);
                    END IF;
                    v_cont_code := tst ->> 'container_code';
                    INSERT INTO dx_samples (clinic_id, accession_id, container_id, sample_type, barcode, barcode_source,
                                            tube_seq, collection_site_id, processing_site_id)
                    VALUES (p_clinic_id, p_accession_id, (tst ->> 'container_id')::UUID, tst ->> 'sample_type',
                            lpad((a.accession_year % 100)::TEXT, 2, '0') || v_cont_code || lpad(v_seq::TEXT, 8, '0'),
                            'generated', v_seq, v_collect, v_process)
                    RETURNING id INTO v_sample;
                END IF;
            END IF;
            INSERT INTO opd_orders (clinic_id, branch_id, source, accession_id, accession_item_id, dx_sample_id,
                                    patient_id, family_member_id, order_type, catalog_type, catalog_reference_id,
                                    item_name, sample_type, urgency, clinical_notes, unit_price_paise, ordered_by,
                                    ordered_by_name)
            VALUES (p_clinic_id, a.branch_id, 'dx', p_accession_id, v_item, v_sample, a.patient_id, a.family_member_id,
                    'investigation', 'lab_test', (tst ->> 'lab_test_id')::UUID, left(tst ->> 'name', 200),
                    CASE WHEN v_sample IS NULL THEN NULL ELSE left(tst ->> 'sample_type', 60) END,
                    a.priority, a.clinical_notes, 0, p_actor_id, left(p_actor_name, 100))
            ON CONFLICT DO NOTHING;     -- a package listing a test twice explodes it once
        END LOOP;
    END LOOP;
    RETURN v_n;
END $$;

-- ── 8. Read model ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_visit_json(p_clinic_id UUID, p_accession_id UUID) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object(
        'visit', to_jsonb(a),
        'items', COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.created_at, i.id) FROM dx_accession_items i
                            WHERE i.clinic_id = a.clinic_id AND i.accession_id = a.id), '[]'::jsonb),
        'samples', COALESCE((SELECT jsonb_agg(to_jsonb(s) || jsonb_build_object('container_code', c.code,
                                    'container_name', c.name, 'colour_hex', c.colour_hex) ORDER BY s.created_at, s.id)
                              FROM dx_samples s JOIN dx_containers c ON c.clinic_id = s.clinic_id AND c.id = s.container_id
                             WHERE s.clinic_id = a.clinic_id AND s.accession_id = a.id), '[]'::jsonb),
        'orders', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', o.id, 'accession_item_id', o.accession_item_id,
                                   'dx_sample_id', o.dx_sample_id, 'lab_test_id', o.catalog_reference_id,
                                   'item_name', o.item_name, 'sample_type', o.sample_type, 'status', o.status,
                                   'urgency', o.urgency) ORDER BY o.created_at, o.id)
                             FROM opd_orders o WHERE o.clinic_id = a.clinic_id AND o.accession_id = a.id), '[]'::jsonb),
        'bookings', COALESCE((SELECT jsonb_agg(to_jsonb(b)) FROM dx_accession_bookings b
                               WHERE b.clinic_id = a.clinic_id AND b.accession_id = a.id), '[]'::jsonb),
        'invoices', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', v.id, 'status', v.status,
                                     'invoice_number', v.invoice_number, 'total_paise', v.total_paise,
                                     'paid_paise', v.paid_paise, 'credited_paise', v.credited_paise) ORDER BY v.created_at)
                               FROM opd_invoices v WHERE v.clinic_id = a.clinic_id AND v.accession_id = a.id), '[]'::jsonb))
      FROM dx_accessions a WHERE a.clinic_id = p_clinic_id AND a.id = p_accession_id
$$;

-- ── 9. Register a visit ────────────────────────────────────────────────────
-- p_payload: {branch_id, patient_id, family_member_id?, source, referrer_id?, b2b_account_id?, external_ref?,
--   patient_type?, billing_mode?, priority?, fasting_status?, last_meal_at?, clinical_notes?, delivery_prefs?,
--   idempotency_key, items: [...], fees?: [...], booking_ids?: [...], discount_limit_percent (set by the service)}
CREATE OR REPLACE FUNCTION dx_register_visit(p_clinic_id UUID, p_payload JSONB, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    v_key TEXT := NULLIF(p_payload ->> 'idempotency_key', '');
    v_existing UUID; v_branch UUID := (p_payload ->> 'branch_id')::UUID;
    v_patient patients; v_member family_members; v_gender TEXT; v_snapshot JSONB;
    v_ref dx_referrers; v_acc dx_b2b_accounts; v_mode TEXT := COALESCE(p_payload ->> 'billing_mode', 'patient');
    v_items JSONB := COALESCE(p_payload -> 'items', '[]'::jsonb); v_appt appointments; v_bid TEXT;
    v_lines JSONB; v_year INTEGER; v_seq INTEGER; v_sseq INTEGER; v_id UUID; v_needs_tube BOOLEAN;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF v_key IS NULL THEN PERFORM dx_raise('dx_idempotency_required'); END IF;
    -- same key, same visit: retries and double clicks serialise here
    PERFORM pg_advisory_xact_lock(hashtextextended(p_clinic_id::TEXT || ':dxreg:' || v_key, 0));
    SELECT id INTO v_existing FROM dx_accessions WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
    IF v_existing IS NOT NULL THEN
        RETURN dx_visit_json(p_clinic_id, v_existing) || jsonb_build_object('replayed', true);
    END IF;

    SELECT * INTO v_patient FROM patients WHERE clinic_id = p_clinic_id AND id = (p_payload ->> 'patient_id')::UUID;
    IF NOT FOUND THEN PERFORM dx_raise('dx_patient_not_found'); END IF;
    v_gender := v_patient.gender;
    v_snapshot := jsonb_build_object('name', v_patient.name, 'mrn', v_patient.mrn, 'gender', v_patient.gender,
                                     'age_years', v_patient.age_years, 'date_of_birth', v_patient.date_of_birth);
    IF NULLIF(p_payload ->> 'family_member_id', '') IS NOT NULL THEN
        SELECT * INTO v_member FROM family_members
         WHERE clinic_id = p_clinic_id AND id = (p_payload ->> 'family_member_id')::UUID
           AND primary_phone = v_patient.phone;
        IF NOT FOUND THEN PERFORM dx_raise('dx_patient_not_found'); END IF;
        v_gender := v_member.gender;
        v_snapshot := jsonb_build_object('name', v_member.full_name, 'mrn', v_member.mrn, 'gender', v_member.gender,
                                         'age_years', v_member.age_years, 'date_of_birth', v_member.date_of_birth,
                                         'relationship', v_member.relationship);
    END IF;
    IF NULLIF(p_payload ->> 'referrer_id', '') IS NOT NULL THEN
        SELECT * INTO v_ref FROM dx_referrers WHERE clinic_id = p_clinic_id AND id = (p_payload ->> 'referrer_id')::UUID
                                               AND is_active;
        IF NOT FOUND THEN PERFORM dx_raise('dx_referrer_not_found'); END IF;
    END IF;
    IF NULLIF(p_payload ->> 'b2b_account_id', '') IS NOT NULL THEN
        SELECT * INTO v_acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = (p_payload ->> 'b2b_account_id')::UUID;
        IF NOT FOUND THEN PERFORM dx_raise('dx_account_not_found'); END IF;
        IF v_acc.status <> 'active' THEN PERFORM dx_raise('dx_b2b_on_hold'); END IF;
    END IF;

    -- bookings: confirmed lab-test bookings of this person, not linked to another visit; one line each
    FOR v_bid IN SELECT jsonb_array_elements_text(COALESCE(p_payload -> 'booking_ids', '[]'::jsonb)) LOOP
        SELECT * INTO v_appt FROM appointments WHERE clinic_id = p_clinic_id AND id = v_bid::UUID FOR UPDATE;
        IF NOT FOUND OR v_appt.booking_type <> 'lab_test' OR v_appt.status <> 'confirmed' OR v_appt.lab_test_id IS NULL
           OR (v_appt.patient_id IS NOT NULL AND v_appt.patient_id <> v_patient.id)
           OR (v_appt.patient_id IS NULL AND v_appt.patient_phone <> v_patient.phone)
           OR v_appt.family_member_id IS DISTINCT FROM v_member.id THEN
            PERFORM dx_raise('dx_booking_invalid');
        END IF;
        IF EXISTS (SELECT 1 FROM dx_accession_bookings WHERE clinic_id = p_clinic_id AND appointment_id = v_appt.id) THEN
            PERFORM dx_raise('dx_booking_linked');
        END IF;
        v_items := v_items || jsonb_build_object('lab_test_id', v_appt.lab_test_id, 'source_appointment_id', v_appt.id);
        IF v_appt.collection_mode = 'home' AND COALESCE(v_appt.home_collection_fee_paise, 0) > 0 THEN
            v_items := v_items || jsonb_build_object('fee_code', 'home_collection', 'amount_paise',
                                                     v_appt.home_collection_fee_paise, 'source_appointment_id', v_appt.id);
        END IF;
    END LOOP;
    v_items := v_items || COALESCE(p_payload -> 'fees', '[]'::jsonb);

    v_lines := dx_price_items(jsonb_build_object('clinic_id', p_clinic_id, 'branch_id', v_branch, 'gender', v_gender,
                                                 'billing_mode', v_mode,
                                                 'discount_limit_percent', p_payload -> 'discount_limit_percent'),
                              v_items);
    v_needs_tube := EXISTS (SELECT 1 FROM jsonb_array_elements(v_lines) l, jsonb_array_elements(l -> 'tests') t
                             WHERE t ->> 'container_id' IS NOT NULL);
    IF NOT EXISTS (SELECT 1 FROM dx_sites WHERE clinic_id = p_clinic_id AND branch_id = v_branch AND is_active) THEN
        PERFORM dx_raise('dx_no_collection_site');
    END IF;

    -- counters last: the clinics row lock they take is held only for the inserts below (AUDIT A13)
    v_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::INTEGER;
    v_seq := opd_next_counter(p_clinic_id, 'dx:' || v_year);
    IF v_needs_tube THEN v_sseq := dx_next_sample_seq(p_clinic_id, v_year); END IF;

    INSERT INTO dx_accessions (clinic_id, branch_id, accession_year, accession_seq, patient_id, family_member_id, source,
                               referrer_id, referrer_snapshot, b2b_account_id, external_ref, patient_type, billing_mode,
                               sample_seq, priority, fasting_status, last_meal_at, clinical_notes, patient_snapshot,
                               delivery_prefs, idempotency_key, created_by, created_by_name)
    VALUES (p_clinic_id, v_branch, v_year, v_seq, v_patient.id, v_member.id, COALESCE(p_payload ->> 'source', 'walk_in'),
            v_ref.id, CASE WHEN v_ref.id IS NULL THEN NULL ELSE jsonb_build_object('name', v_ref.name,
                       'qualification', v_ref.qualification, 'kind', v_ref.kind) END,
            v_acc.id, NULLIF(p_payload ->> 'external_ref', ''), COALESCE(p_payload ->> 'patient_type', 'op'), v_mode,
            v_sseq, COALESCE(p_payload ->> 'priority', 'routine'), COALESCE(p_payload ->> 'fasting_status', 'unknown'),
            NULLIF(p_payload ->> 'last_meal_at', '')::TIMESTAMPTZ, NULLIF(p_payload ->> 'clinical_notes', ''), v_snapshot,
            COALESCE(p_payload -> 'delivery_prefs', '{}'::jsonb), v_key, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING id INTO v_id;

    PERFORM dx_insert_lines(p_clinic_id, v_id, v_lines, p_actor_id, p_actor_name);
    FOR v_bid IN SELECT jsonb_array_elements_text(COALESCE(p_payload -> 'booking_ids', '[]'::jsonb)) LOOP
        INSERT INTO dx_accession_bookings (clinic_id, accession_id, appointment_id) VALUES (p_clinic_id, v_id, v_bid::UUID);
        UPDATE appointments SET checked_in_at = COALESCE(checked_in_at, now())
         WHERE clinic_id = p_clinic_id AND id = v_bid::UUID;
    END LOOP;
    PERFORM dx_log(p_clinic_id, v_id, 'registered', p_actor_id, p_actor_name,
                   jsonb_build_object('items', jsonb_array_length(v_lines),
                                      'bookings', COALESCE(p_payload -> 'booking_ids', '[]'::jsonb)));
    RETURN dx_visit_json(p_clinic_id, v_id) || jsonb_build_object('replayed', false);
END $$;

-- Price without writing (cart preview at the desk; the same resolver as registration).
CREATE OR REPLACE FUNCTION dx_quote(p_clinic_id UUID, p_payload JSONB) RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE v_gender TEXT;
BEGIN
    SELECT COALESCE(f.gender, p.gender) INTO v_gender FROM patients p
      LEFT JOIN family_members f ON f.clinic_id = p.clinic_id AND f.id = NULLIF(p_payload ->> 'family_member_id', '')::UUID
     WHERE p.clinic_id = p_clinic_id AND p.id = NULLIF(p_payload ->> 'patient_id', '')::UUID;
    RETURN dx_price_items(jsonb_build_object('clinic_id', p_clinic_id, 'branch_id', p_payload ->> 'branch_id',
                                             'gender', v_gender, 'billing_mode', p_payload ->> 'billing_mode',
                                             'discount_limit_percent', p_payload -> 'discount_limit_percent'),
                          COALESCE(p_payload -> 'items', '[]'::jsonb) || COALESCE(p_payload -> 'fees', '[]'::jsonb));
END $$;

-- ── 10. Add items, cancel an item, cancel a visit ──────────────────────────
CREATE OR REPLACE FUNCTION dx_add_items(p_clinic_id UUID, p_accession_id UUID, p_items JSONB, p_idempotency_key TEXT,
                                        p_discount_limit_percent NUMERIC, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; v_gender TEXT; v_lines JSONB;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    IF p_idempotency_key IS NOT NULL AND EXISTS (
        SELECT 1 FROM dx_events WHERE clinic_id = p_clinic_id AND accession_id = a.id AND kind = 'items_added'
                                  AND details ->> 'idempotency_key' = p_idempotency_key) THEN
        RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', true);
    END IF;
    IF a.status IN ('reported', 'cancelled') THEN PERFORM dx_raise('dx_visit_closed'); END IF;
    v_gender := COALESCE(a.patient_snapshot ->> 'gender', '');
    v_lines := dx_price_items(jsonb_build_object('clinic_id', p_clinic_id, 'branch_id', a.branch_id, 'gender', v_gender,
                                                 'billing_mode', a.billing_mode,
                                                 'discount_limit_percent', p_discount_limit_percent), p_items);
    IF a.sample_seq IS NULL AND EXISTS (SELECT 1 FROM jsonb_array_elements(v_lines) l, jsonb_array_elements(l -> 'tests') t
                                         WHERE t ->> 'container_id' IS NOT NULL) THEN
        UPDATE dx_accessions SET sample_seq = dx_next_sample_seq(p_clinic_id, a.accession_year)
         WHERE clinic_id = p_clinic_id AND id = a.id;
    END IF;
    PERFORM dx_insert_lines(p_clinic_id, a.id, v_lines, p_actor_id, p_actor_name);
    PERFORM dx_log(p_clinic_id, a.id, 'items_added', p_actor_id, p_actor_name,
                   jsonb_build_object('idempotency_key', p_idempotency_key, 'items', jsonb_array_length(v_lines)));
    RETURN dx_visit_json(p_clinic_id, a.id) || jsonb_build_object('replayed', false);
END $$;

CREATE OR REPLACE FUNCTION dx_cancel_item(p_clinic_id UUID, p_item_id UUID, p_reason TEXT, p_actor_id UUID,
                                          p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE it dx_accession_items; a dx_accessions;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO it FROM dx_accession_items WHERE clinic_id = p_clinic_id AND id = p_item_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_item_not_found'); END IF;
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = it.accession_id FOR UPDATE;   -- visit -> item order
    SELECT * INTO it FROM dx_accession_items WHERE clinic_id = p_clinic_id AND id = p_item_id FOR UPDATE;
    IF it.status = 'cancelled' THEN RETURN dx_visit_json(p_clinic_id, a.id); END IF;
    IF EXISTS (SELECT 1 FROM opd_orders WHERE clinic_id = p_clinic_id AND accession_item_id = it.id
                                         AND status IN ('in_progress', 'resulted', 'completed')) THEN
        PERFORM dx_raise('dx_item_in_progress');
    END IF;
    -- money taken on an issued bill comes back first (refund / void through OPD billing)
    IF EXISTS (SELECT 1 FROM opd_invoice_items l JOIN opd_invoices i ON i.clinic_id = l.clinic_id AND i.id = l.invoice_id
                WHERE l.clinic_id = p_clinic_id AND l.dx_accession_item_id = it.id AND i.status NOT IN ('draft', 'void')) THEN
        PERFORM dx_raise('dx_refund_first');
    END IF;
    DELETE FROM opd_invoice_items l USING opd_invoices i
     WHERE l.clinic_id = p_clinic_id AND l.dx_accession_item_id = it.id
       AND i.clinic_id = l.clinic_id AND i.id = l.invoice_id AND i.status = 'draft';
    UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                          cancellation_reason = left(p_reason, 300)
     WHERE clinic_id = p_clinic_id AND accession_item_id = it.id AND status <> 'cancelled';
    UPDATE dx_accession_items SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                                  cancellation_reason = left(p_reason, 300)
     WHERE clinic_id = p_clinic_id AND id = it.id;
    PERFORM dx_log(p_clinic_id, a.id, 'item_cancelled', p_actor_id, p_actor_name,
                   jsonb_build_object('item_id', it.id, 'name', it.item_name, 'reason', p_reason));
    RETURN dx_visit_json(p_clinic_id, a.id);
END $$;

CREATE OR REPLACE FUNCTION dx_cancel_visit(p_clinic_id UUID, p_accession_id UUID, p_reason TEXT, p_actor_id UUID,
                                           p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    IF a.status = 'cancelled' THEN RETURN dx_visit_json(p_clinic_id, a.id); END IF;
    IF EXISTS (SELECT 1 FROM dx_samples WHERE clinic_id = p_clinic_id AND accession_id = a.id
                                         AND status <> 'pending_collection') THEN
        PERFORM dx_raise('dx_visit_has_samples');
    END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND accession_id = a.id
                                           AND status NOT IN ('draft', 'void')) THEN
        PERFORM dx_raise('dx_refund_first');
    END IF;
    DELETE FROM opd_invoice_items l USING opd_invoices i
     WHERE i.clinic_id = p_clinic_id AND i.accession_id = a.id AND i.status = 'draft'
       AND l.clinic_id = i.clinic_id AND l.invoice_id = i.id;
    DELETE FROM opd_invoices WHERE clinic_id = p_clinic_id AND accession_id = a.id AND status = 'draft';
    UPDATE opd_orders SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                          cancellation_reason = left(p_reason, 300)
     WHERE clinic_id = p_clinic_id AND accession_id = a.id AND status <> 'cancelled';
    UPDATE dx_accession_items SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                                  cancellation_reason = left(p_reason, 300)
     WHERE clinic_id = p_clinic_id AND accession_id = a.id AND status = 'active';
    UPDATE dx_samples SET status = 'discarded' WHERE clinic_id = p_clinic_id AND accession_id = a.id;
    -- the bookings are released: the patient can be registered again on the same bookings
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id AND accession_id = a.id;
    UPDATE dx_accessions SET status = 'cancelled', cancelled_at = now(), cancelled_by = p_actor_id,
                             cancellation_reason = left(p_reason, 300)
     WHERE clinic_id = p_clinic_id AND id = a.id;
    PERFORM dx_log(p_clinic_id, a.id, 'cancelled', p_actor_id, p_actor_name, jsonb_build_object('reason', p_reason));
    RETURN dx_visit_json(p_clinic_id, a.id);
END $$;

-- ── 11. Bill a visit (draft lines only; issue / receipts / refunds stay OPD) ─
-- One line per active item with a patient share and no live line yet, on the visit's
-- draft bill; once a bill is issued, later items go on a supplementary draft bill.
-- appointment_id stays NULL, so OPD's one-bill-per-appointment index is never involved.
CREATE OR REPLACE FUNCTION dx_bill_visit(p_clinic_id UUID, p_accession_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; v_inv UUID; v_line INTEGER; it dx_accession_items; v_added INTEGER := 0;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_visit_not_found'); END IF;
    IF a.billing_mode <> 'patient' THEN PERFORM dx_raise('dx_not_patient_billing'); END IF;
    IF a.status = 'cancelled' THEN PERFORM dx_raise('dx_visit_closed'); END IF;
    SELECT id INTO v_inv FROM opd_invoices WHERE clinic_id = p_clinic_id AND accession_id = a.id AND status = 'draft'
     ORDER BY created_at LIMIT 1;
    FOR it IN SELECT i.* FROM dx_accession_items i
               WHERE i.clinic_id = p_clinic_id AND i.accession_id = a.id AND i.status = 'active' AND i.patient_share_paise > 0
                 AND NOT EXISTS (SELECT 1 FROM opd_invoice_items l JOIN opd_invoices v ON v.clinic_id = l.clinic_id AND v.id = l.invoice_id
                                  WHERE l.clinic_id = i.clinic_id AND l.dx_accession_item_id = i.id AND v.status <> 'void')
               ORDER BY i.created_at, i.id LOOP
        IF v_inv IS NULL THEN
            INSERT INTO opd_invoices (clinic_id, branch_id, appointment_id, patient_id, family_member_id, patient_snapshot,
                                      accession_id, created_by)
            VALUES (p_clinic_id, a.branch_id, NULL, a.patient_id, a.family_member_id, a.patient_snapshot, a.id, p_actor_id)
            RETURNING id INTO v_inv;
        END IF;
        SELECT COALESCE(max(line_no), 0) + 1 INTO v_line FROM opd_invoice_items WHERE clinic_id = p_clinic_id AND invoice_id = v_inv;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, catalog_code, lab_test_id, description,
                                       quantity, unit_price_paise, discount_paise, dx_accession_item_id)
        VALUES (p_clinic_id, v_inv, v_line, 'diagnostic', NULL, it.lab_test_id, left(it.item_name, 200), 1,
                it.patient_share_paise, 0, it.id);
        v_added := v_added + 1;
    END LOOP;
    IF v_added > 0 THEN
        PERFORM dx_log(p_clinic_id, a.id, 'billed', p_actor_id, p_actor_name,
                       jsonb_build_object('invoice_id', v_inv, 'lines', v_added));
    END IF;
    RETURN jsonb_build_object('invoice_id', v_inv, 'lines_added', v_added);
END $$;

-- DPDP erasure (data_retention): the visit keeps its clinical and money rows, the identity goes.
CREATE OR REPLACE FUNCTION dx_redact_patient(p_clinic_id UUID, p_patient_id UUID) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    UPDATE dx_accessions SET patient_snapshot = (patient_snapshot - 'name' - 'date_of_birth')
                                                || jsonb_build_object('name', '[REDACTED]', 'redacted_at', now()),
                             external_ref = NULL, delivery_prefs = '{}'::jsonb
     WHERE clinic_id = p_clinic_id AND patient_id = p_patient_id AND patient_snapshot ->> 'name' <> '[REDACTED]';
    GET DIAGNOSTICS n = ROW_COUNT;
    PERFORM set_config('kriya.dx_tx', '', true);
    RETURN n;
END $$;

-- ── 12. Purge v3 (children first: DX orders, events, tubes, items, visits, then catalogue) ─
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
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
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
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

-- ── 13. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_referrers', 'dx_b2b_accounts', 'dx_accessions', 'dx_accession_bookings',
                             'dx_accession_items', 'dx_samples', 'dx_events']
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

-- ── 14. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN ('dx_referrers', 'dx_b2b_accounts',
          'dx_accessions', 'dx_accession_bookings', 'dx_accession_items', 'dx_samples', 'dx_events')) <> 7 THEN
        RAISE EXCEPTION '127 verify: DX visit tables missing';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND (
          (table_name = 'opd_orders' AND column_name IN ('accession_id', 'accession_item_id', 'dx_sample_id', 'replaces_order_id'))
       OR (table_name = 'opd_invoices' AND column_name = 'accession_id')
       OR (table_name = 'opd_invoice_items' AND column_name = 'dx_accession_item_id')
       OR (table_name = 'family_members' AND column_name = 'dx_b2b_account_id'))) <> 7 THEN
        RAISE EXCEPTION '127 verify: DX columns on OPD tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_guard_invoice_item', 'trg_dx_accessions_guard',
          'trg_dx_accession_items_guard', 'trg_dx_samples_guard', 'trg_dx_events_guard', 'trg_dx_accession_bookings_guard',
          'trg_dx_lab_test_delete', 'trg_dx_invoice_accession')) <> 8 THEN
        RAISE EXCEPTION '127 verify: DX guards missing';
    END IF;
    IF to_regprocedure('dx_register_visit(uuid,jsonb,uuid,text)') IS NULL
       OR to_regprocedure('dx_bill_visit(uuid,uuid,uuid,text)') IS NULL
       OR to_regprocedure('dx_cancel_visit(uuid,uuid,text,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '127 verify: DX visit functions missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname IN ('dx_referrers', 'dx_b2b_accounts', 'dx_accessions',
                 'dx_accession_bookings', 'dx_accession_items', 'dx_samples', 'dx_events')
                 AND relkind = 'r' AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '127 verify: RLS not forced on DX visit tables';
    END IF;
END $$;
