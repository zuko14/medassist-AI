-- ============================================================================
-- Migration 138: Kriya Pharmacy OS counter sales (RX-M5, RX-28)
-- Sales (OPD Rx / OTC), lines priced only from the batch snapshot, payments into the
-- OPD cashier shift, Rx fulfilment ledger, patient returns + credit notes, manager
-- overrides; the inclusive GST split and loose-unit price; rx_complete_sale,
-- rx_record_return, rx_void_sale; opd_close_shift gains pharmacy cash (VERIFICATION D1);
-- rx_purge_clinic v5.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §5 / §14, WORKFLOWS.md W2 / W3 / W5a,
-- SECURITY.md §4, ADR-RX-08 (inclusive GST), TEST-PLAN §4.1 / §4.2.
-- Existing objects touched: opd_close_shift only (116 body + one term; rollback restores 116).
-- Rollback: rollback/138_rollback_rx_pos.sql.
-- ============================================================================

-- ── 1. Money helpers (Python mirror: app/services/rx_tax.py) ───────────────
-- Inclusive GST: taxable = round_half_up(gross × 10000 / (10000 + rate)); CGST = tax / 2,
-- SGST = the rest. Without a GSTIN the bill is a bill of supply: no tax.
CREATE OR REPLACE FUNCTION rx_gst_split_inclusive(p_gross BIGINT, p_rate_bps INTEGER, p_has_gstin BOOLEAN)
RETURNS TABLE (taxable BIGINT, cgst BIGINT, sgst BIGINT)
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT t, (p_gross - t) / 2, (p_gross - t) - (p_gross - t) / 2
      FROM (SELECT CASE WHEN NOT p_has_gstin OR p_rate_bps = 0 THEN p_gross
                        ELSE (p_gross * 10000 + (10000 + p_rate_bps) / 2) / (10000 + p_rate_bps) END AS t) x
$$;

-- Loose units at MRP: round_half_up(mrp_pack × units / pack_size). Never above MRP + 1 paisa.
CREATE OR REPLACE FUNCTION rx_unit_price(p_mrp_pack INTEGER, p_pack_size INTEGER, p_units INTEGER) RETURNS BIGINT
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT (p_mrp_pack::BIGINT * p_units + p_pack_size / 2) / p_pack_size
$$;

-- Units an OPD Rx line prescribes, from the CLOSED frequency map only (W2 step 2); NULL when not
-- computable (SOS / STAT / free text / liquids). Python mirror: rx_tax.prescribed_units.
CREATE OR REPLACE FUNCTION rx_rx_units(p_dosage TEXT, p_frequency TEXT, p_duration_days INTEGER, p_formulation TEXT)
RETURNS INTEGER LANGUAGE plpgsql IMMUTABLE SET search_path = public, pg_temp AS $$
DECLARE v_freq INTEGER; v_dose NUMERIC; m TEXT[];
BEGIN
    IF p_duration_days IS NULL OR p_formulation NOT IN ('tablet', 'capsule') THEN RETURN NULL; END IF;
    v_freq := CASE upper(btrim(p_frequency))
        WHEN 'OD' THEN 1 WHEN 'QD' THEN 1 WHEN 'HS' THEN 1 WHEN 'BD' THEN 2 WHEN 'BID' THEN 2
        WHEN 'TDS' THEN 3 WHEN 'TID' THEN 3 WHEN 'QID' THEN 4
        WHEN '1-0-0' THEN 1 WHEN '0-1-0' THEN 1 WHEN '0-0-1' THEN 1 WHEN '1-0-1' THEN 2 WHEN '1-1-0' THEN 2
        WHEN '0-1-1' THEN 2 WHEN '1-1-1' THEN 3 WHEN '1-1-1-1' THEN 4 ELSE NULL END;
    IF v_freq IS NULL THEN RETURN NULL; END IF;
    m := regexp_match(btrim(COALESCE(p_dosage, '')), '^(\d+)\s*/\s*(\d+)');
    IF m IS NOT NULL AND m[2]::INTEGER > 0 THEN
        v_dose := m[1]::NUMERIC / m[2]::INTEGER;
    ELSE
        m := regexp_match(btrim(COALESCE(p_dosage, '')), '^(\d+(\.\d+)?)');
        IF m IS NULL THEN RETURN NULL; END IF;
        v_dose := m[1]::NUMERIC;
    END IF;
    IF v_dose <= 0 OR v_dose > 20 THEN RETURN NULL; END IF;
    RETURN ceil(v_dose * v_freq * p_duration_days)::INTEGER;
END $$;

-- ── 2. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_sales (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id               UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id                UUID NOT NULL,
    branch_id               UUID NULL,
    sale_kind               TEXT NOT NULL CHECK (sale_kind IN ('opd_rx', 'otc', 'ipd_discharge_meds', 'staff')),
    patient_id              UUID NULL,
    family_member_id        UUID NULL,
    walkin_name             TEXT NULL CHECK (char_length(btrim(walkin_name)) BETWEEN 2 AND 120),
    walkin_phone_masked     TEXT NULL CHECK (char_length(walkin_phone_masked) <= 20),
    opd_prescription_id     UUID NULL,
    prescriber_snapshot     JSONB NULL CHECK (prescriber_snapshot IS NULL OR jsonb_typeof(prescriber_snapshot) = 'object'),
    external_rx_ref         TEXT NULL CHECK (char_length(external_rx_ref) <= 120),
    external_rx_fingerprint BYTEA NULL,              -- set by trigger: sha256(reg_no ‖ rx_date ‖ normalised name) (X2)
    parked_label            TEXT NULL CHECK (char_length(parked_label) <= 60),
    parked_at               TIMESTAMPTZ NULL,
    picked_at               TIMESTAMPTZ NULL,
    picked_by               UUID NULL,
    status                  TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'completed', 'void')),
    sale_year               SMALLINT NULL,
    sale_seq                INTEGER NULL,
    sale_number             TEXT GENERATED ALWAYS AS (CASE WHEN sale_seq IS NULL THEN NULL
                                ELSE opd_format_number('PHS-', sale_year, sale_seq, 6) END) STORED,
    is_tax_invoice          BOOLEAN NOT NULL DEFAULT false,
    clinic_gstin            TEXT NULL,
    buyer_gstin             TEXT NULL CHECK (buyer_gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    gross_paise             BIGINT NOT NULL DEFAULT 0,
    discount_paise          BIGINT NOT NULL DEFAULT 0 CHECK (discount_paise >= 0),
    round_off_paise         SMALLINT NOT NULL DEFAULT 0 CHECK (round_off_paise BETWEEN -99 AND 99),
    net_paise               BIGINT GENERATED ALWAYS AS (gross_paise - discount_paise + round_off_paise) STORED,
    taxable_paise           BIGINT NOT NULL DEFAULT 0,
    cgst_paise              BIGINT NOT NULL DEFAULT 0,
    sgst_paise              BIGINT NOT NULL DEFAULT 0,
    paid_paise              BIGINT NOT NULL DEFAULT 0,
    refunded_paise          BIGINT NOT NULL DEFAULT 0 CHECK (refunded_paise >= 0),
    credited_paise          BIGINT NOT NULL DEFAULT 0 CHECK (credited_paise >= 0),
    idempotency_key         TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    completed_at            TIMESTAMPTZ NULL,
    completed_by            UUID NULL,
    completed_by_name       TEXT NULL,
    voided_at               TIMESTAMPTZ NULL,
    voided_by               UUID NULL,
    void_reason             TEXT NULL CHECK (char_length(void_reason) <= 300),
    created_by              UUID NULL,
    created_by_name         TEXT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_sales_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_sales_number_key UNIQUE (clinic_id, sale_year, sale_seq),
    CONSTRAINT rx_sales_numbered CHECK (status = 'draft' OR sale_seq IS NOT NULL),
    CONSTRAINT rx_sales_paid_in_full CHECK (status = 'draft' OR paid_paise = net_paise),
    CONSTRAINT rx_sales_giveback_le_net CHECK (refunded_paise + credited_paise <= net_paise),
    CONSTRAINT rx_sales_discount_le_gross CHECK (discount_paise <= gross_paise),
    CONSTRAINT rx_sales_has_person CHECK (patient_id IS NOT NULL OR walkin_name IS NOT NULL OR sale_kind = 'otc'),
    CONSTRAINT rx_sales_void_reason CHECK (status <> 'void' OR char_length(btrim(COALESCE(void_reason, ''))) >= 3),
    CONSTRAINT rx_sales_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_sales_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    CONSTRAINT rx_sales_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT rx_sales_family_fk FOREIGN KEY (clinic_id, family_member_id) REFERENCES family_members (clinic_id, id),
    CONSTRAINT rx_sales_rx_fk FOREIGN KEY (clinic_id, opd_prescription_id) REFERENCES opd_prescriptions (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_sales_idem ON rx_sales (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_sales_one_live_per_rx ON rx_sales (clinic_id, opd_prescription_id, store_id)
    WHERE opd_prescription_id IS NOT NULL AND status = 'draft';
CREATE INDEX IF NOT EXISTS idx_rx_sales_day ON rx_sales (clinic_id, store_id, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_rx_sales_patient ON rx_sales (clinic_id, patient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rx_sales_rx ON rx_sales (clinic_id, opd_prescription_id);
CREATE INDEX IF NOT EXISTS idx_rx_sales_fingerprint ON rx_sales (clinic_id, external_rx_fingerprint)
    WHERE external_rx_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS rx_sale_items (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sale_id                  UUID NOT NULL,
    line_no                  SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 200),
    item_id                  UUID NOT NULL,
    batch_id                 UUID NOT NULL,
    qty_units                INTEGER NOT NULL CHECK (qty_units > 0),
    opd_prescription_item_id UUID NULL,
    substituted_from_item_id UUID NULL,
    substitution_reason      TEXT NULL CHECK (char_length(substitution_reason) <= 120),
    fefo_override_reason     TEXT NULL CHECK (char_length(fefo_override_reason) <= 120),
    -- snapshots, written by rx_sale_line_calc (client prices are never trusted)
    mrp_pack_paise           INTEGER NOT NULL DEFAULT 0,
    pack_size                INTEGER NOT NULL DEFAULT 1,
    line_gross_paise         BIGINT NOT NULL DEFAULT 0,
    line_discount_paise      BIGINT NOT NULL DEFAULT 0 CHECK (line_discount_paise >= 0),
    tax_rate_bps             SMALLINT NOT NULL DEFAULT 0,
    hsn_code                 TEXT NOT NULL DEFAULT '',
    schedule                 TEXT NOT NULL DEFAULT 'none',
    taxable_paise            BIGINT NOT NULL DEFAULT 0,
    cgst_paise               BIGINT NOT NULL DEFAULT 0,
    sgst_paise               BIGINT NOT NULL DEFAULT 0,
    returned_units           INTEGER NOT NULL DEFAULT 0 CHECK (returned_units >= 0),
    CONSTRAINT rx_sale_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_sale_items_line_key UNIQUE (sale_id, line_no),
    CONSTRAINT rx_sale_items_returned_le_qty CHECK (returned_units <= qty_units),
    CONSTRAINT rx_sale_items_discount_le_gross CHECK (line_discount_paise <= line_gross_paise),
    CONSTRAINT rx_sale_items_substitution_shape CHECK ((substituted_from_item_id IS NULL) = (substitution_reason IS NULL)),
    CONSTRAINT rx_sale_items_le_mrp CHECK (line_gross_paise <= mrp_pack_paise::BIGINT * qty_units / pack_size + 1),
    CONSTRAINT rx_sale_items_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT rx_sale_items_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_sale_items_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_sale_items_sub_fk FOREIGN KEY (clinic_id, substituted_from_item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_sale_items_rx_item_fk FOREIGN KEY (clinic_id, opd_prescription_item_id)
        REFERENCES opd_prescription_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_sale_items_rx_item ON rx_sale_items (clinic_id, opd_prescription_item_id)
    WHERE opd_prescription_item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rx_sale_payments (           -- [A][F]
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sale_id       UUID NOT NULL,
    shift_id      UUID NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('cash', 'upi', 'card', 'wallet', 'ipd_bill')),
    amount_paise  BIGINT NOT NULL CHECK (amount_paise <> 0),    -- negative = refund
    return_id     UUID NULL,
    ipd_charge_id UUID NULL,
    reference     TEXT NULL CHECK (char_length(reference) <= 60),
    created_by    UUID NULL,
    created_by_name TEXT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_sale_payments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_sale_payments_cash_needs_shift CHECK (mode <> 'cash' OR shift_id IS NOT NULL),
    CONSTRAINT rx_sale_payments_ipd_shape CHECK ((mode = 'ipd_bill') = (ipd_charge_id IS NOT NULL)),
    CONSTRAINT rx_sale_payments_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id),
    CONSTRAINT rx_sale_payments_shift_fk FOREIGN KEY (clinic_id, shift_id) REFERENCES opd_cashier_shifts (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_sale_payments_shift ON rx_sale_payments (clinic_id, shift_id) WHERE shift_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rx_sale_payments_sale ON rx_sale_payments (clinic_id, sale_id);

CREATE TABLE IF NOT EXISTS rx_rx_fulfilments (          -- [A][F]
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    opd_prescription_item_id UUID NOT NULL,
    sale_item_id             UUID NOT NULL,
    units                    INTEGER NOT NULL CHECK (units > 0),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_rx_fulfilments_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_rx_fulfilments_sale_item_key UNIQUE (clinic_id, sale_item_id),
    CONSTRAINT rx_rx_fulfilments_rx_item_fk FOREIGN KEY (clinic_id, opd_prescription_item_id)
        REFERENCES opd_prescription_items (clinic_id, id),
    CONSTRAINT rx_rx_fulfilments_sale_item_fk FOREIGN KEY (clinic_id, sale_item_id) REFERENCES rx_sale_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_rx_fulfilments_rx_item ON rx_rx_fulfilments (clinic_id, opd_prescription_item_id);

CREATE TABLE IF NOT EXISTS rx_credit_notes (            -- [A][F]
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id      UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sale_id        UUID NOT NULL,
    cn_year        SMALLINT NOT NULL,
    cn_seq         INTEGER NOT NULL,
    cn_number      TEXT GENERATED ALWAYS AS (opd_format_number('PCN-', cn_year, cn_seq, 5)) STORED,
    amount_paise   BIGINT NOT NULL CHECK (amount_paise > 0),
    tax_paise      BIGINT NOT NULL CHECK (tax_paise >= 0),
    reason         TEXT NOT NULL,
    issued_by      UUID NULL,
    issued_by_name TEXT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_credit_notes_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_credit_notes_number_key UNIQUE (clinic_id, cn_year, cn_seq),
    CONSTRAINT rx_credit_notes_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS rx_returns (                 -- [A][F]
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    sale_id         UUID NOT NULL,
    store_id        UUID NOT NULL,
    return_year     SMALLINT NOT NULL,
    return_seq      INTEGER NOT NULL,
    return_number   TEXT GENERATED ALWAYS AS (opd_format_number('PRT-', return_year, return_seq, 5)) STORED,
    reason          TEXT NOT NULL CHECK (reason IN ('unopened_unused', 'wrong_item', 'adverse_reaction_doctor_advised',
                                                    'therapy_changed', 'other')),
    refund_mode     TEXT NOT NULL CHECK (refund_mode IN ('cash', 'upi', 'card', 'credit_note')),
    amount_paise    BIGINT NOT NULL CHECK (amount_paise > 0),
    credit_note_id  UUID NULL,
    idempotency_key TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_by      UUID NULL,
    created_by_name TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_returns_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_returns_number_key UNIQUE (clinic_id, return_year, return_seq),
    CONSTRAINT rx_returns_cn_shape CHECK ((refund_mode = 'credit_note') = (credit_note_id IS NOT NULL)),
    CONSTRAINT rx_returns_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id),
    CONSTRAINT rx_returns_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_returns_cn_fk FOREIGN KEY (clinic_id, credit_note_id) REFERENCES rx_credit_notes (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_returns_idem ON rx_returns (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS rx_return_lines (            -- [A][F]
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    return_id    UUID NOT NULL,
    sale_item_id UUID NOT NULL,
    qty_units    INTEGER NOT NULL CHECK (qty_units > 0),
    restock      BOOLEAN NOT NULL,
    amount_paise BIGINT NOT NULL CHECK (amount_paise >= 0),
    CONSTRAINT rx_return_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    -- deferred: rx_record_return writes the lines first and the header (with its final amount and number) last
    CONSTRAINT rx_return_lines_return_fk FOREIGN KEY (clinic_id, return_id) REFERENCES rx_returns (clinic_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT rx_return_lines_item_fk FOREIGN KEY (clinic_id, sale_item_id) REFERENCES rx_sale_items (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS rx_overrides (               -- A9: the row IS the single-use, 60-second token
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    action             TEXT NOT NULL CHECK (action IN ('discount_over_cap', 'return_past_window', 'restock_opened',
                                                       'fefo_override', 'rx_over_fulfil', 'staff_sale', 'controlled_empties')),
    sale_id            UUID NULL,
    return_id          UUID NULL,
    amount_paise       BIGINT NULL CHECK (amount_paise >= 0),
    reason             TEXT NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 3 AND 300),
    approver_admin_id  UUID NOT NULL,
    approver_name      TEXT NOT NULL,
    requester_admin_id UUID NOT NULL,
    expires_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() + interval '60 seconds',
    consumed_at        TIMESTAMPTZ NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_overrides_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_overrides_two_people CHECK (approver_admin_id <> requester_admin_id),
    CONSTRAINT rx_overrides_short_lived CHECK (expires_at <= created_at + interval '61 seconds'),
    CONSTRAINT rx_overrides_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id)
);

-- ── 3. Guards ──────────────────────────────────────────────────────────────
-- Rows only an rx_* function may insert (payments, fulfilments, returns, credit notes).
CREATE OR REPLACE FUNCTION rx_require_tx() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF NOT rx_in_tx(NEW.clinic_id) THEN PERFORM rx_raise('rx_write_via_function_only'); END IF;
    RETURN NEW;
END $$;
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_sale_payments', 'rx_rx_fulfilments', 'rx_credit_notes', 'rx_returns', 'rx_return_lines']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_require_tx', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION rx_require_tx()',
                       'trg_' || t || '_require_tx', t);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_append_only', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION rx_guard_append_only()',
                       'trg_' || t || '_append_only', t);
    END LOOP;
END $$;

-- A sale is edited only while draft; it is completed / voided / refunded only inside an rx_* function.
-- The external Rx fingerprint (loophole X2) is computed here from the captured prescriber snapshot.
CREATE OR REPLACE FUNCTION rx_guard_sale() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE p JSONB;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) OR OLD.status = 'draft' THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_document_locked');
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id THEN PERFORM rx_raise('rx_document_locked'); END IF;
        IF (OLD.status <> 'draft' OR NEW.status <> 'draft') AND NOT rx_in_tx(OLD.clinic_id) THEN
            PERFORM rx_raise('rx_document_locked');
        END IF;
    END IF;
    IF TG_OP = 'INSERT' OR OLD.status = 'draft' THEN
        IF TG_OP = 'INSERT' THEN
            NEW.clinic_gstin := opd_clinic_gstin(NEW.clinic_id);
            NEW.is_tax_invoice := NEW.clinic_gstin IS NOT NULL;
        END IF;
        p := NEW.prescriber_snapshot;
        NEW.external_rx_fingerprint := CASE WHEN p ->> 'external' = 'true' AND NULLIF(btrim(p ->> 'registration_number'), '') IS NOT NULL
            THEN sha256(convert_to(upper(btrim(p ->> 'registration_number')) || '|' || COALESCE(p ->> 'rx_date', '') || '|'
                                   || rx_normalize(COALESCE(p ->> 'patient_name', NEW.walkin_name, '')), 'UTF8'))
            END;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sales_guard ON rx_sales;
CREATE TRIGGER trg_rx_sales_guard BEFORE INSERT OR UPDATE OR DELETE ON rx_sales
    FOR EACH ROW EXECUTE FUNCTION rx_guard_sale();

-- Line pricing: snapshot MRP / pack / GST / HSN / schedule from the batch and item; gross at
-- MRP (loose units half-up); GST split on (gross − allocated discount). After completion a
-- line changes only its returned_units, inside rx_record_return.
CREATE OR REPLACE FUNCTION rx_sale_line_calc() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s rx_sales; b rx_batches; i rx_items; sp RECORD;
BEGIN
    IF rx_purging(COALESCE(NEW.clinic_id, OLD.clinic_id)) THEN RETURN COALESCE(NEW, OLD); END IF;
    SELECT * INTO s FROM rx_sales WHERE clinic_id = COALESCE(NEW.clinic_id, OLD.clinic_id)
                                    AND id = COALESCE(NEW.sale_id, OLD.sale_id);
    IF TG_OP = 'DELETE' THEN
        IF NOT FOUND OR s.status = 'draft' THEN RETURN OLD; END IF;     -- NOT FOUND: the draft's cascade
        PERFORM rx_raise('rx_document_locked');
    END IF;
    IF s.status <> 'draft' THEN
        IF NOT rx_in_tx(NEW.clinic_id) OR TG_OP = 'INSERT'
           OR (to_jsonb(NEW) - 'returned_units') IS DISTINCT FROM (to_jsonb(OLD) - 'returned_units') THEN
            PERFORM rx_raise('rx_document_locked');
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO b FROM rx_batches WHERE clinic_id = NEW.clinic_id AND id = NEW.batch_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_batch_not_found'); END IF;
    IF b.item_id <> NEW.item_id THEN PERFORM rx_raise('rx_batch_item_mismatch'); END IF;
    SELECT * INTO i FROM rx_items WHERE clinic_id = NEW.clinic_id AND id = NEW.item_id;
    NEW.mrp_pack_paise := b.mrp_pack_paise;
    NEW.pack_size := i.pack_size;
    NEW.tax_rate_bps := i.tax_rate_bps;
    NEW.hsn_code := i.hsn_code;
    NEW.schedule := i.schedule;
    IF NOT i.allow_loose AND NEW.qty_units % i.pack_size <> 0 THEN PERFORM rx_raise('rx_whole_packs_only'); END IF;
    NEW.line_gross_paise := rx_unit_price(b.mrp_pack_paise, i.pack_size, NEW.qty_units);
    NEW.line_discount_paise := LEAST(NEW.line_discount_paise, NEW.line_gross_paise);
    SELECT * INTO sp FROM rx_gst_split_inclusive(NEW.line_gross_paise - NEW.line_discount_paise, i.tax_rate_bps,
                                                 s.is_tax_invoice);
    NEW.taxable_paise := sp.taxable; NEW.cgst_paise := sp.cgst; NEW.sgst_paise := sp.sgst;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sale_line_calc ON rx_sale_items;
CREATE TRIGGER trg_rx_sale_line_calc BEFORE INSERT OR UPDATE OR DELETE ON rx_sale_items
    FOR EACH ROW EXECUTE FUNCTION rx_sale_line_calc();

-- Header totals: the header discount is spread over lines by gross (largest remainder), so every
-- line's GST is computed on what the patient actually pays; round-off to the rupee when set.
CREATE OR REPLACE FUNCTION rx_sale_refresh(p_clinic_id UUID, p_sale_id UUID) RETURNS VOID
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s rx_sales; v_gross BIGINT; v_disc BIGINT; v_x BIGINT; v_ro INTEGER := 0;
BEGIN
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    IF NOT FOUND OR s.status <> 'draft' THEN RETURN; END IF;
    SELECT COALESCE(SUM(line_gross_paise), 0) INTO v_gross FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id;
    v_disc := LEAST(s.discount_paise, v_gross);
    WITH l AS (SELECT id, line_no, line_gross_paise g FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id),
         b AS (SELECT id, line_no, CASE WHEN v_gross = 0 THEN 0 ELSE v_disc * g / v_gross END AS base,
                      CASE WHEN v_gross = 0 THEN 0 ELSE v_disc * g % v_gross END AS rem FROM l),
         r AS (SELECT id, base + CASE WHEN row_number() OVER (ORDER BY rem DESC, line_no)
                                   <= (SELECT v_disc - COALESCE(SUM(base), 0) FROM b) THEN 1 ELSE 0 END AS share FROM b)
    UPDATE rx_sale_items x SET line_discount_paise = r.share
      FROM r WHERE x.clinic_id = p_clinic_id AND x.id = r.id AND x.line_discount_paise IS DISTINCT FROM r.share;
    v_x := v_gross - v_disc;
    IF COALESCE((SELECT (rx_settings ->> 'round_off_to_rupee')::BOOLEAN FROM clinics WHERE id = p_clinic_id), true) THEN
        v_ro := ((v_x + 50) / 100) * 100 - v_x;
    END IF;
    UPDATE rx_sales x SET gross_paise = v_gross, discount_paise = v_disc, round_off_paise = v_ro,
           taxable_paise = t.tx, cgst_paise = t.cg, sgst_paise = t.sg
      FROM (SELECT COALESCE(SUM(taxable_paise), 0) tx, COALESCE(SUM(cgst_paise), 0) cg, COALESCE(SUM(sgst_paise), 0) sg
              FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id) t
     WHERE x.clinic_id = p_clinic_id AND x.id = p_sale_id
       AND (x.gross_paise, x.discount_paise, x.round_off_paise, x.taxable_paise, x.cgst_paise, x.sgst_paise)
           IS DISTINCT FROM (v_gross, v_disc, v_ro::SMALLINT, t.tx, t.cg, t.sg);
END $$;

CREATE OR REPLACE FUNCTION rx_sale_lines_after() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r RECORD;
BEGIN
    IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;
    FOR r IN SELECT DISTINCT clinic_id, sale_id FROM changed_lines LOOP
        PERFORM rx_sale_refresh(r.clinic_id, r.sale_id);
    END LOOP;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sale_lines_after_ins ON rx_sale_items;
CREATE TRIGGER trg_rx_sale_lines_after_ins AFTER INSERT ON rx_sale_items
    REFERENCING NEW TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_sale_lines_after();
DROP TRIGGER IF EXISTS trg_rx_sale_lines_after_upd ON rx_sale_items;
CREATE TRIGGER trg_rx_sale_lines_after_upd AFTER UPDATE ON rx_sale_items
    REFERENCING NEW TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_sale_lines_after();
DROP TRIGGER IF EXISTS trg_rx_sale_lines_after_del ON rx_sale_items;
CREATE TRIGGER trg_rx_sale_lines_after_del AFTER DELETE ON rx_sale_items
    REFERENCING OLD TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_sale_lines_after();

CREATE OR REPLACE FUNCTION rx_sale_header_after() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF pg_trigger_depth() = 1 AND NEW.status = 'draft' AND NEW.discount_paise IS DISTINCT FROM OLD.discount_paise THEN
        PERFORM rx_sale_refresh(NEW.clinic_id, NEW.id);
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sales_after ON rx_sales;
CREATE TRIGGER trg_rx_sales_after AFTER UPDATE ON rx_sales FOR EACH ROW EXECUTE FUNCTION rx_sale_header_after();

-- Overrides: inserted (approved) by the API after re-authenticating the approver; consumed only here.
CREATE OR REPLACE FUNCTION rx_guard_override() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_append_only:rx_overrides');
    END IF;
    IF NOT rx_in_tx(OLD.clinic_id)
       OR (to_jsonb(NEW) - 'consumed_at') IS DISTINCT FROM (to_jsonb(OLD) - 'consumed_at') THEN
        PERFORM rx_raise('rx_append_only:rx_overrides');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_overrides_guard ON rx_overrides;
CREATE TRIGGER trg_rx_overrides_guard BEFORE UPDATE OR DELETE ON rx_overrides
    FOR EACH ROW EXECUTE FUNCTION rx_guard_override();

-- Single use, unexpired, bound to this sale (or unbound), action, requester and amount.
CREATE OR REPLACE FUNCTION rx_consume_override(p_clinic_id UUID, p_override_id UUID, p_action TEXT, p_sale_id UUID,
                                               p_amount BIGINT, p_actor_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v UUID;
BEGIN
    IF p_override_id IS NULL OR p_actor_id IS NULL THEN RETURN false; END IF;
    UPDATE rx_overrides SET consumed_at = clock_timestamp()
     WHERE clinic_id = p_clinic_id AND id = p_override_id AND action = p_action AND consumed_at IS NULL
       AND expires_at > clock_timestamp() AND requester_admin_id = p_actor_id
       AND (sale_id IS NULL OR sale_id = p_sale_id)
       AND (p_amount IS NULL OR amount_paise IS NULL OR amount_paise >= p_amount)
    RETURNING id INTO v;
    RETURN v IS NOT NULL;
END $$;

-- ── 4. Completion [F] (DATA-MODEL §5, in this order) ───────────────────────
CREATE OR REPLACE FUNCTION rx_complete_sale(p_clinic_id UUID, p_sale_id UUID, p_payments JSONB, p_shift_id UUID,
                                            p_idem TEXT, p_actor_id UUID, p_actor_name TEXT, p_override_id UUID,
                                            p_is_admin BOOLEAN, p_can_controlled BOOLEAN)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s rx_sales; l RECORD; p JSONB; v_rx_status TEXT; v_max_bps INTEGER; v_prescribed INTEGER; v_prior BIGINT;
    v_paid BIGINT := 0; v_cash BOOLEAN := false; v_override_used BOOLEAN := false;
    v_year INTEGER := extract(year FROM rx_ist_date(now()));
    v_controlled TEXT[] := ARRAY['X', 'narcotic', 'psychotropic'];
BEGIN
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_sale_not_found'); END IF;
    -- 1. per-Rx lock (loophole X1) before the sale row lock (VERIFICATION §6 order)
    IF s.opd_prescription_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtext('rx_rx:' || s.opd_prescription_id::TEXT));
        SELECT status INTO v_rx_status FROM opd_prescriptions WHERE clinic_id = p_clinic_id AND id = s.opd_prescription_id;
        IF v_rx_status IS DISTINCT FROM 'signed' THEN PERFORM rx_raise('rx_prescription_superseded'); END IF;
    END IF;
    -- 2. the sale itself; a replay returns the completed sale
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id FOR UPDATE;
    IF s.status = 'completed' AND p_idem IS NOT NULL AND s.idempotency_key = p_idem THEN
        RETURN jsonb_build_object('sale_number', s.sale_number, 'net_paise', s.net_paise, 'replayed', true);
    END IF;
    IF s.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id) THEN
        PERFORM rx_raise('rx_sale_empty');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = s.store_id AND is_active AND can_sell) THEN
        PERFORM rx_raise('rx_store_cannot_sell');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    -- 3. re-price from the batch snapshot (today's GSTIN decides tax invoice vs bill of supply)
    UPDATE rx_sales SET clinic_gstin = opd_clinic_gstin(p_clinic_id), is_tax_invoice = opd_clinic_gstin(p_clinic_id) IS NOT NULL
     WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    UPDATE rx_sale_items SET qty_units = qty_units WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id;
    PERFORM rx_sale_refresh(p_clinic_id, p_sale_id);
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    v_max_bps := COALESCE((SELECT (rx_settings ->> 'max_discount_bps')::INTEGER FROM clinics WHERE id = p_clinic_id), 1000);
    IF s.discount_paise * 10000 > s.gross_paise * v_max_bps AND NOT p_is_admin THEN
        IF NOT rx_consume_override(p_clinic_id, p_override_id, 'discount_over_cap', p_sale_id, s.discount_paise, p_actor_id) THEN
            PERFORM rx_raise('rx_discount_over_limit');
        END IF;
        v_override_used := true;
    END IF;
    -- 4. fulfilment caps per Rx line, across counters and stores (completed sales, net of returns)
    FOR l IN SELECT si.opd_prescription_item_id AS rx_item, SUM(si.qty_units) AS units, bool_or(si.schedule = ANY (v_controlled || ARRAY['H1'])) AS hard,
                    pi.dosage, pi.frequency, pi.duration_days, pi.formulation
               FROM rx_sale_items si JOIN opd_prescription_items pi ON pi.clinic_id = si.clinic_id AND pi.id = si.opd_prescription_item_id
              WHERE si.clinic_id = p_clinic_id AND si.sale_id = p_sale_id AND si.opd_prescription_item_id IS NOT NULL
              GROUP BY 1, 4, 5, 6, 7 LOOP
        v_prescribed := rx_rx_units(l.dosage, l.frequency, l.duration_days, l.formulation);
        SELECT COALESCE(SUM(f.units - si.returned_units), 0) INTO v_prior
          FROM rx_rx_fulfilments f JOIN rx_sale_items si ON si.clinic_id = f.clinic_id AND si.id = f.sale_item_id
          JOIN rx_sales x ON x.clinic_id = si.clinic_id AND x.id = si.sale_id AND x.status = 'completed'
         WHERE f.clinic_id = p_clinic_id AND f.opd_prescription_item_id = l.rx_item;
        IF (v_prescribed IS NOT NULL AND v_prior + l.units > v_prescribed) OR (v_prescribed IS NULL AND l.hard AND v_prior > 0) THEN
            IF l.hard OR NOT rx_consume_override(p_clinic_id, p_override_id, 'rx_over_fulfil', p_sale_id, NULL, p_actor_id) THEN
                PERFORM rx_raise('rx_fulfilment_exceeds_rx');
            END IF;
            v_override_used := true;
        END IF;
    END LOOP;
    -- 5. schedule preconditions (SECURITY §4)
    IF EXISTS (SELECT 1 FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id
                 AND schedule IN ('H', 'H1', 'X', 'narcotic', 'psychotropic')) THEN
        IF NULLIF(btrim(s.prescriber_snapshot ->> 'registration_number'), '') IS NULL THEN
            PERFORM rx_raise('rx_schedule_needs_rx');
        END IF;
        IF s.patient_id IS NULL AND NULLIF(btrim(COALESCE(s.walkin_name, s.prescriber_snapshot ->> 'patient_name')), '') IS NULL THEN
            PERFORM rx_raise('rx_schedule_needs_patient');
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id
                 AND schedule = ANY (v_controlled)) THEN
        IF NOT p_can_controlled THEN PERFORM rx_raise('rx_controlled_permission'); END IF;
        IF s.opd_prescription_id IS NULL AND s.external_rx_fingerprint IS NULL THEN
            PERFORM rx_raise('rx_controlled_requires_inhouse_rx');
        END IF;
        IF s.external_rx_fingerprint IS NOT NULL AND EXISTS (
               SELECT 1 FROM rx_sales x WHERE x.clinic_id = p_clinic_id AND x.status = 'completed' AND x.id <> p_sale_id
                  AND x.external_rx_fingerprint = s.external_rx_fingerprint) THEN
            PERFORM rx_raise('rx_external_rx_reused');
        END IF;
    END IF;
    -- 6. stock leaves, one movement per line, in (store, batch) order; the ledger refuses expired / blocked / short
    FOR l IN SELECT si.*, b.landed_unit_cost_paise_x100 FROM rx_sale_items si
               JOIN rx_batches b ON b.clinic_id = si.clinic_id AND b.id = si.batch_id
              WHERE si.clinic_id = p_clinic_id AND si.sale_id = p_sale_id ORDER BY si.batch_id, si.line_no LOOP
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name)
        VALUES (p_clinic_id, s.store_id, l.batch_id, l.item_id, -l.qty_units, 'sale_out', 'rx_sale_items', l.id,
                l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name);
        IF l.opd_prescription_item_id IS NOT NULL THEN
            INSERT INTO rx_rx_fulfilments (clinic_id, opd_prescription_item_id, sale_item_id, units)
            VALUES (p_clinic_id, l.opd_prescription_item_id, l.id, l.qty_units);
        END IF;
    END LOOP;
    -- 7. payments: Σ = net; cash only into an open shift
    FOR p IN SELECT value FROM jsonb_array_elements(COALESCE(p_payments, '[]'::jsonb)) LOOP
        IF p ->> 'mode' NOT IN ('cash', 'upi', 'card', 'wallet') THEN PERFORM rx_raise('rx_payment_mode_invalid'); END IF;
        IF COALESCE((p ->> 'amount_paise')::BIGINT, 0) <= 0 THEN PERFORM rx_raise('rx_payment_invalid'); END IF;
        IF p ->> 'mode' = 'cash' THEN v_cash := true; END IF;
        INSERT INTO rx_sale_payments (clinic_id, sale_id, shift_id, mode, amount_paise, reference, created_by, created_by_name)
        VALUES (p_clinic_id, p_sale_id, CASE WHEN p ->> 'mode' = 'cash' THEN p_shift_id END, p ->> 'mode',
                (p ->> 'amount_paise')::BIGINT, left(p ->> 'reference', 60), p_actor_id, p_actor_name);
        v_paid := v_paid + (p ->> 'amount_paise')::BIGINT;
    END LOOP;
    IF v_cash AND NOT EXISTS (SELECT 1 FROM opd_cashier_shifts WHERE clinic_id = p_clinic_id AND id = p_shift_id
                                AND status = 'open') THEN
        PERFORM rx_raise('rx_shift_not_open');
    END IF;
    IF v_paid <> s.net_paise THEN PERFORM rx_raise('rx_payment_mismatch:' || (s.net_paise - v_paid)); END IF;
    -- 8. number last (the counter locks the clinics row, VERIFICATION D12)
    UPDATE rx_sales SET status = 'completed', paid_paise = v_paid, completed_at = clock_timestamp(),
           completed_by = p_actor_id, completed_by_name = p_actor_name, idempotency_key = p_idem,
           parked_label = NULL, parked_at = NULL, sale_year = v_year,
           sale_seq = opd_next_counter(p_clinic_id, 'rx_sale:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = p_sale_id RETURNING * INTO s;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('sale_number', s.sale_number, 'net_paise', s.net_paise, 'replayed', false,
                              'override_used', v_override_used);
END $$;

-- ── 5. Patient return (W5a) [F] ────────────────────────────────────────────
-- Refund value = the SOLD net per unit (after the line's share of the discount) × units, half-up;
-- the last return of a line takes the exact remainder, so returns can never exceed what was paid (X4).
CREATE OR REPLACE FUNCTION rx_record_return(p_clinic_id UUID, p_sale_id UUID, p_lines JSONB, p_reason TEXT,
                                            p_refund_mode TEXT, p_shift_id UUID, p_idem TEXT, p_actor_id UUID,
                                            p_actor_name TEXT, p_is_admin BOOLEAN, p_override_id UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s rx_sales; r rx_returns; e JSONB; si rx_sale_items; b rx_batches; v_ret UUID := gen_random_uuid();
    v_rl UUID; v_amt BIGINT; v_total BIGINT := 0; v_tax BIGINT := 0; v_tender BIGINT; v_cn UUID; v_return_days INTEGER;
    v_today DATE := rx_ist_date(now()); v_year INTEGER := extract(year FROM rx_ist_date(now())); sp RECORD;
BEGIN
    IF p_idem IS NOT NULL THEN
        SELECT * INTO r FROM rx_returns WHERE clinic_id = p_clinic_id AND idempotency_key = p_idem;
        IF FOUND THEN RETURN jsonb_build_object('return_id', r.id, 'return_number', r.return_number,
                                                'amount_paise', r.amount_paise, 'replayed', true); END IF;
    END IF;
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_sale_not_found'); END IF;
    IF s.status <> 'completed' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    v_return_days := COALESCE((SELECT (rx_settings ->> 'return_days')::INTEGER FROM clinics WHERE id = p_clinic_id), 7);
    IF rx_ist_date(s.completed_at) + v_return_days < v_today AND NOT p_is_admin
       AND NOT rx_consume_override(p_clinic_id, p_override_id, 'return_past_window', p_sale_id, NULL, p_actor_id) THEN
        PERFORM rx_raise('rx_return_window_closed');
    END IF;
    IF jsonb_array_length(COALESCE(p_lines, '[]'::jsonb)) = 0 THEN PERFORM rx_raise('rx_return_empty'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR e IN SELECT value FROM jsonb_array_elements(p_lines) ORDER BY value ->> 'sale_item_id' LOOP
        SELECT * INTO si FROM rx_sale_items WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id
                                              AND id = (e ->> 'sale_item_id')::UUID FOR UPDATE;
        IF NOT FOUND THEN PERFORM rx_raise('rx_sale_item_not_found'); END IF;
        IF COALESCE((e ->> 'qty_units')::INTEGER, 0) <= 0
           OR si.returned_units + (e ->> 'qty_units')::INTEGER > si.qty_units THEN
            PERFORM rx_raise('rx_return_exceeds_sold');
        END IF;
        IF (e ->> 'restock')::BOOLEAN AND si.schedule IN ('H1', 'X', 'narcotic', 'psychotropic') AND NOT p_is_admin THEN
            PERFORM rx_raise('rx_restock_needs_admin');
        END IF;
        IF si.returned_units + (e ->> 'qty_units')::INTEGER = si.qty_units THEN     -- last units: exact remainder
            v_amt := (si.line_gross_paise - si.line_discount_paise)
                     - COALESCE((SELECT SUM(rl.amount_paise) FROM rx_return_lines rl
                                  WHERE rl.clinic_id = p_clinic_id AND rl.sale_item_id = si.id), 0);
        ELSE
            v_amt := ((si.line_gross_paise - si.line_discount_paise) * (e ->> 'qty_units')::INTEGER * 2 + si.qty_units)
                     / (2 * si.qty_units);
        END IF;
        v_rl := gen_random_uuid();
        INSERT INTO rx_return_lines (id, clinic_id, return_id, sale_item_id, qty_units, restock, amount_paise)
        VALUES (v_rl, p_clinic_id, v_ret, si.id, (e ->> 'qty_units')::INTEGER, (e ->> 'restock')::BOOLEAN, v_amt);
        UPDATE rx_sale_items SET returned_units = returned_units + (e ->> 'qty_units')::INTEGER
         WHERE clinic_id = p_clinic_id AND id = si.id;
        SELECT * INTO b FROM rx_batches WHERE clinic_id = p_clinic_id AND id = si.batch_id;
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, s.store_id, si.batch_id, si.item_id, (e ->> 'qty_units')::INTEGER, 'sale_return_in',
                'rx_return_lines', v_rl, b.landed_unit_cost_paise_x100, p_actor_id, p_actor_name, 'Patient return: ' || p_reason);
        IF NOT (e ->> 'restock')::BOOLEAN OR b.expiry_date < v_today THEN       -- opened / expired: straight out again
            INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                            ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
            VALUES (p_clinic_id, s.store_id, si.batch_id, si.item_id, -(e ->> 'qty_units')::INTEGER,
                    CASE WHEN b.expiry_date < v_today THEN 'expiry_writeoff_out' ELSE 'damage_writeoff_out' END,
                    'rx_return_lines', v_rl, b.landed_unit_cost_paise_x100, p_actor_id, p_actor_name,
                    CASE WHEN b.expiry_date < v_today THEN 'Returned after expiry' ELSE 'Returned opened / not resaleable' END);
        END IF;
        SELECT * INTO sp FROM rx_gst_split_inclusive(v_amt, si.tax_rate_bps, s.is_tax_invoice);
        v_total := v_total + v_amt; v_tax := v_tax + sp.cgst + sp.sgst;
    END LOOP;
    IF v_total <= 0 THEN PERFORM rx_raise('rx_return_empty'); END IF;
    IF p_refund_mode = 'credit_note' THEN
        v_cn := gen_random_uuid();
        INSERT INTO rx_credit_notes (id, clinic_id, sale_id, cn_year, cn_seq, amount_paise, tax_paise, reason, issued_by,
                                     issued_by_name)
        VALUES (v_cn, p_clinic_id, p_sale_id, v_year, opd_next_counter(p_clinic_id, 'rx_cn:' || v_year), v_total, v_tax,
                p_reason, p_actor_id, p_actor_name);
        UPDATE rx_sales SET credited_paise = credited_paise + v_total WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    ELSE
        -- refund follows the original tender (X3): never more back in a mode than was paid in it
        SELECT COALESCE(SUM(amount_paise), 0) INTO v_tender FROM rx_sale_payments
         WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id AND mode = p_refund_mode;
        IF v_tender < v_total AND NOT p_is_admin THEN PERFORM rx_raise('rx_refund_exceeds_tender'); END IF;
        IF p_refund_mode = 'cash' AND NOT EXISTS (SELECT 1 FROM opd_cashier_shifts WHERE clinic_id = p_clinic_id
                                                    AND id = p_shift_id AND status = 'open') THEN
            PERFORM rx_raise('rx_shift_not_open');
        END IF;
        INSERT INTO rx_sale_payments (clinic_id, sale_id, shift_id, mode, amount_paise, return_id, created_by, created_by_name)
        VALUES (p_clinic_id, p_sale_id, CASE WHEN p_refund_mode = 'cash' THEN p_shift_id END, p_refund_mode, -v_total,
                v_ret, p_actor_id, p_actor_name);
        UPDATE rx_sales SET refunded_paise = refunded_paise + v_total WHERE clinic_id = p_clinic_id AND id = p_sale_id;
    END IF;
    -- header last, with its final amount; the PRT number is the last statement (VERIFICATION D12)
    INSERT INTO rx_returns (id, clinic_id, sale_id, store_id, return_year, return_seq, reason, refund_mode, amount_paise,
                            credit_note_id, idempotency_key, created_by, created_by_name)
    VALUES (v_ret, p_clinic_id, p_sale_id, s.store_id, v_year, opd_next_counter(p_clinic_id, 'rx_ret:' || v_year), p_reason,
            p_refund_mode, v_total, v_cn, p_idem, p_actor_id, p_actor_name)
    RETURNING * INTO r;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('return_id', r.id, 'return_number', r.return_number, 'amount_paise', v_total,
                              'tax_paise', v_tax, 'credit_note_id', v_cn, 'replayed', false);
END $$;

-- ── 6. Same-day void (RX_ADMIN) [F] ────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_void_sale(p_clinic_id UUID, p_sale_id UUID, p_reason TEXT, p_shift_id UUID,
                                        p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE s rx_sales; l RECORD; pay RECORD;
BEGIN
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    SELECT * INTO s FROM rx_sales WHERE clinic_id = p_clinic_id AND id = p_sale_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_sale_not_found'); END IF;
    IF s.status = 'void' THEN RETURN jsonb_build_object('sale_number', s.sale_number, 'replayed', true); END IF;
    IF s.status <> 'completed' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF rx_ist_date(s.completed_at) <> rx_ist_date(now()) THEN PERFORM rx_raise('rx_void_not_same_day'); END IF;
    IF s.refunded_paise + s.credited_paise > 0 OR EXISTS (SELECT 1 FROM rx_sale_items WHERE clinic_id = p_clinic_id
                                                            AND sale_id = p_sale_id AND returned_units > 0) THEN
        PERFORM rx_raise('rx_void_has_returns');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT si.*, b.landed_unit_cost_paise_x100 FROM rx_sale_items si
               JOIN rx_batches b ON b.clinic_id = si.clinic_id AND b.id = si.batch_id
              WHERE si.clinic_id = p_clinic_id AND si.sale_id = p_sale_id ORDER BY si.batch_id, si.line_no LOOP
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, s.store_id, l.batch_id, l.item_id, l.qty_units, 'sale_return_in', 'rx_sale_void', l.id,
                l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name, left('Sale void: ' || btrim(p_reason), 300));
    END LOOP;
    FOR pay IN SELECT mode, SUM(amount_paise) AS amt FROM rx_sale_payments
                WHERE clinic_id = p_clinic_id AND sale_id = p_sale_id GROUP BY mode HAVING SUM(amount_paise) > 0 LOOP
        IF pay.mode = 'cash' AND NOT EXISTS (SELECT 1 FROM opd_cashier_shifts WHERE clinic_id = p_clinic_id
                                               AND id = p_shift_id AND status = 'open') THEN
            PERFORM rx_raise('rx_shift_not_open');
        END IF;
        INSERT INTO rx_sale_payments (clinic_id, sale_id, shift_id, mode, amount_paise, reference, created_by, created_by_name)
        VALUES (p_clinic_id, p_sale_id, CASE WHEN pay.mode = 'cash' THEN p_shift_id END, pay.mode, -pay.amt, 'void',
                p_actor_id, p_actor_name);
    END LOOP;
    UPDATE rx_sales SET status = 'void', voided_at = clock_timestamp(), voided_by = p_actor_id,
           void_reason = btrim(p_reason), refunded_paise = net_paise
     WHERE clinic_id = p_clinic_id AND id = p_sale_id RETURNING * INTO s;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('sale_number', s.sale_number, 'replayed', false);
END $$;

-- ── 7. Shift expected cash includes pharmacy cash (VERIFICATION D1) ────────
-- The 116 body verbatim plus one term; refunds are negative rows, so they subtract.
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
         + COALESCE((SELECT SUM(CASE WHEN kind = 'payment' THEN amount_paise ELSE -amount_paise END)
                       FROM opd_receipts WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
         + COALESCE((SELECT SUM(CASE WHEN kind = 'deposit' THEN amount_paise ELSE -amount_paise END)
                       FROM ipd_deposits WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
         + COALESCE((SELECT SUM(amount_paise) FROM rx_sale_payments
                      WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
      INTO v_expected;
    RETURN QUERY
    UPDATE opd_cashier_shifts
       SET status = 'closed', closed_at = now(), expected_cash_paise = v_expected,
           declared_cash_paise = p_declared_paise, close_notes = p_notes
     WHERE id = p_shift_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;
ALTER FUNCTION opd_close_shift(uuid, uuid, uuid, integer, text) SET search_path = public, pg_temp;

-- Z-report (A13): the cash a shift should hold right now — the same expression opd_close_shift uses.
CREATE OR REPLACE FUNCTION rx_shift_expected_cash(p_clinic_id UUID, p_shift_id UUID) RETURNS BIGINT
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT s.opening_float_paise
         + COALESCE((SELECT SUM(CASE WHEN kind = 'payment' THEN amount_paise ELSE -amount_paise END)
                       FROM opd_receipts WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
         + COALESCE((SELECT SUM(CASE WHEN kind = 'deposit' THEN amount_paise ELSE -amount_paise END)
                       FROM ipd_deposits WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
         + COALESCE((SELECT SUM(amount_paise) FROM rx_sale_payments
                      WHERE clinic_id = p_clinic_id AND shift_id = p_shift_id AND mode = 'cash'), 0)
      FROM opd_cashier_shifts s WHERE s.clinic_id = p_clinic_id AND s.id = p_shift_id
$$;

-- ── 8. Owner purge v5 ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_overrides WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_return_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_credit_notes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_rx_fulfilments WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sale_payments WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sale_items WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_sales WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sales', n);
    DELETE FROM rx_supplier_return_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_supplier_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_grn_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_grns WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('grns', n);
    DELETE FROM rx_po_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_purchase_orders WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_suppliers WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_balances WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_movements WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('movements', n);
    DELETE FROM rx_batches WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_changes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_aliases WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_barcodes WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_items WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('items', n);
    DELETE FROM rx_generics WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_manufacturers WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stores WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('stores', n);
    UPDATE clinics SET rx_state = 'OFF', rx_settings = '{}'::jsonb WHERE id = p_clinic_id;
    PERFORM set_config('kriya.rx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 9. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_sales', 'rx_sale_items', 'rx_sale_payments', 'rx_rx_fulfilments', 'rx_returns',
                             'rx_return_lines', 'rx_credit_notes', 'rx_overrides']
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
    FOREACH f IN ARRAY ARRAY['rx_gst_split_inclusive(bigint,integer,boolean)', 'rx_unit_price(integer,integer,integer)',
                             'rx_rx_units(text,text,integer,text)', 'rx_sale_refresh(uuid,uuid)',
                             'rx_consume_override(uuid,uuid,text,uuid,bigint,uuid)',
                             'rx_complete_sale(uuid,uuid,jsonb,uuid,text,uuid,text,uuid,boolean,boolean)',
                             'rx_record_return(uuid,uuid,jsonb,text,text,uuid,text,uuid,text,boolean,uuid)',
                             'rx_void_sale(uuid,uuid,text,uuid,uuid,text)', 'rx_purge_clinic(uuid)',
                             'rx_shift_expected_cash(uuid,uuid)',
                             'opd_close_shift(uuid,uuid,uuid,integer,text)']
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

-- ── 10. Verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_sales', 'rx_sale_items', 'rx_sale_payments', 'rx_rx_fulfilments', 'rx_returns',
                             'rx_return_lines', 'rx_credit_notes', 'rx_overrides'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '138 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '138 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF to_regprocedure('rx_complete_sale(uuid,uuid,jsonb,uuid,text,uuid,text,uuid,boolean,boolean)') IS NULL
       OR to_regprocedure('rx_record_return(uuid,uuid,jsonb,text,text,uuid,text,uuid,text,boolean,uuid)') IS NULL
       OR to_regprocedure('rx_void_sale(uuid,uuid,text,uuid,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '138 verify: POS functions missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'opd_close_shift' AND prosrc LIKE '%rx_sale_payments%') THEN
        RAISE EXCEPTION '138 verify: opd_close_shift does not count pharmacy cash';
    END IF;
END $$;
