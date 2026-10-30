-- ============================================================================
-- Migration 117: GST-aware billing, credit notes, room-rent GST (G4)
-- ============================================================================
-- Health care services by a clinical establishment are exempt (SAC 9993,
-- Notification 12/2017-CT(R)), so for almost every bill nothing changes: tax
-- is 0 and the bill stays a bill of supply. Tax is charged ONLY when the clinic
-- has a GSTIN (clinics.opd_settings ->> 'gstin', validated by the API) AND
--   * an OPD line's catalog entry (opd_settings.service_catalog[].tax_rate_bps)
--     carries a rate (cosmetic procedures etc.), or
--   * an IPD bed-day exceeds Rs 5,000/day outside an ICU / NICU / PICU / CCU
--     (Notification 04/2022-CT(R): 5%, SAC 999311).
-- A clinic without a GSTIN never charges GST, whatever the catalog says.
--
-- Money model (integer paise, tax EXCLUSIVE of the line price):
--   line tax     = round_half_up(line_total * rate_bps / 10000), CGST = tax/2,
--                  SGST = tax - CGST (place of supply for on-site health care is
--                  the clinic, so always intra-state; IGST stays 0).
--   subtotal     = SUM(line_total + line tax)    (opd_recalc_invoice_subtotal)
--   total        = subtotal - discount           (unchanged generated column)
--   header tax   = line tax pro-rated by the header discount
--                  (a discount on the bill reduces taxable value proportionally).
-- So every existing money path (receipts, links, deposits) collects the right
-- amount with no change.
--
-- Credit notes (CGST Act s.34): an issued invoice is never edited. A reduction
-- after issue is a numbered, append-only credit note CRN-YYYY-NNNNN from the
-- clinic's gapless counter (opd_next_counter row lock, like INV-/RCT-). The
-- invoice keeps its printed total; credited_paise lowers what is due:
--   due = total - credited - paid.
-- When the patient has already paid more than the reduced amount, the same
-- transaction refunds the excess (cash / UPI) into the cashier's shift.
-- Only opd_issue_credit_note() writes credit notes or credited_paise.
--
-- Additive for pre-117 code: new columns default to 0 / NULL and the redefined
-- functions keep their signatures (ipd_bill_lines gains two output columns).
-- Idempotent. Rollback: migrations/rollback/117_down.sql.
-- ============================================================================

-- ── 1. Helpers ─────────────────────────────────────────────────────────────
-- Half-up on non-negative paise (numeric round() and Python's round() differ;
-- integer arithmetic is the one definition both sides use).
CREATE OR REPLACE FUNCTION opd_gst_amount(p_value BIGINT, p_rate_bps INTEGER)
RETURNS INTEGER LANGUAGE sql IMMUTABLE AS $$
    SELECT ((GREATEST(p_value, 0) * p_rate_bps + 5000) / 10000)::INTEGER
$$;

CREATE OR REPLACE FUNCTION opd_clinic_gstin(p_clinic_id UUID)
RETURNS TEXT LANGUAGE sql STABLE AS $$
    SELECT NULLIF(btrim(opd_settings ->> 'gstin'), '') FROM clinics WHERE id = p_clinic_id
$$;

-- ── 2. Columns ─────────────────────────────────────────────────────────────
ALTER TABLE opd_invoice_items
    ADD COLUMN IF NOT EXISTS hsn_sac TEXT NULL CHECK (hsn_sac ~ '^[0-9]{4,8}$'),
    ADD COLUMN IF NOT EXISTS tax_rate_bps SMALLINT NOT NULL DEFAULT 0
        CHECK (tax_rate_bps IN (0, 500, 1200, 1800, 2800, 4000)),
    ADD COLUMN IF NOT EXISTS cgst_paise INTEGER NOT NULL DEFAULT 0 CHECK (cgst_paise >= 0),
    ADD COLUMN IF NOT EXISTS sgst_paise INTEGER NOT NULL DEFAULT 0 CHECK (sgst_paise >= 0),
    ADD COLUMN IF NOT EXISTS igst_paise INTEGER NOT NULL DEFAULT 0 CHECK (igst_paise >= 0),
    ADD COLUMN IF NOT EXISTS total_tax_paise INTEGER GENERATED ALWAYS AS (cgst_paise + sgst_paise + igst_paise) STORED;
ALTER TABLE opd_invoice_items DROP CONSTRAINT IF EXISTS opd_invoice_items_tax_consistent;
ALTER TABLE opd_invoice_items ADD  CONSTRAINT opd_invoice_items_tax_consistent CHECK (
    cgst_paise + sgst_paise + igst_paise
        = opd_gst_amount(quantity::BIGINT * unit_price_paise - discount_paise, tax_rate_bps)
    AND (igst_paise = 0 OR cgst_paise + sgst_paise = 0));

ALTER TABLE opd_invoices
    ADD COLUMN IF NOT EXISTS is_tax_invoice BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS clinic_gstin TEXT NULL
        CHECK (clinic_gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    ADD COLUMN IF NOT EXISTS patient_gstin TEXT NULL
        CHECK (patient_gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    ADD COLUMN IF NOT EXISTS total_tax_paise INTEGER NOT NULL DEFAULT 0 CHECK (total_tax_paise >= 0),
    ADD COLUMN IF NOT EXISTS credited_paise INTEGER NOT NULL DEFAULT 0 CHECK (credited_paise >= 0);
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_credit_le_total;
ALTER TABLE opd_invoices ADD  CONSTRAINT opd_invoices_credit_le_total
    CHECK (credited_paise <= subtotal_paise - discount_paise AND (credited_paise = 0 OR status <> 'draft'));
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_tax_le_total;
ALTER TABLE opd_invoices ADD  CONSTRAINT opd_invoices_tax_le_total
    CHECK (total_tax_paise <= subtotal_paise - discount_paise);
-- status stays a pure function of paid vs what is still due after credit notes
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_paid_consistent;
ALTER TABLE opd_invoices ADD  CONSTRAINT opd_invoices_paid_consistent CHECK (
    (status IN ('draft', 'issued', 'void') AND paid_paise = 0)
 OR (status = 'partially_paid' AND paid_paise > 0 AND paid_paise < subtotal_paise - discount_paise - credited_paise)
 OR (status = 'paid' AND paid_paise = subtotal_paise - discount_paise - credited_paise));

ALTER TABLE ipd_charges
    ADD COLUMN IF NOT EXISTS hsn_sac TEXT NULL CHECK (hsn_sac ~ '^[0-9]{4,8}$'),
    ADD COLUMN IF NOT EXISTS tax_rate_bps SMALLINT NOT NULL DEFAULT 0
        CHECK (tax_rate_bps IN (0, 500, 1200, 1800, 2800, 4000)),
    ADD COLUMN IF NOT EXISTS tax_paise INTEGER GENERATED ALWAYS AS
        (opd_gst_amount(quantity::BIGINT * unit_price_paise, tax_rate_bps)) STORED;

-- ── 3. Line tax: catalog rate, GSTIN gate, CGST/SGST split ─────────────────
CREATE OR REPLACE FUNCTION opd_invoice_item_tax() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_settings JSONB; v_cat JSONB; v_tax INTEGER;
BEGIN
    SELECT opd_settings INTO v_settings FROM clinics WHERE id = NEW.clinic_id;
    IF NEW.catalog_code IS NOT NULL AND jsonb_typeof(v_settings -> 'service_catalog') = 'array' THEN
        SELECT c INTO v_cat FROM jsonb_array_elements(v_settings -> 'service_catalog') c
         WHERE c ->> 'code' = NEW.catalog_code LIMIT 1;
        IF v_cat IS NOT NULL THEN
            NEW.hsn_sac := NULLIF(v_cat ->> 'hsn_sac', '');
            NEW.tax_rate_bps := COALESCE(NULLIF(v_cat ->> 'tax_rate_bps', '')::SMALLINT, 0);
        END IF;
    END IF;
    IF NULLIF(btrim(v_settings ->> 'gstin'), '') IS NULL THEN
        NEW.tax_rate_bps := 0;   -- an unregistered clinic cannot charge GST
    END IF;
    v_tax := opd_gst_amount(NEW.quantity::BIGINT * NEW.unit_price_paise - NEW.discount_paise, NEW.tax_rate_bps);
    NEW.igst_paise := 0;
    NEW.cgst_paise := v_tax / 2;
    NEW.sgst_paise := v_tax - v_tax / 2;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_opd_inv_items_tax ON opd_invoice_items;
CREATE TRIGGER trg_opd_inv_items_tax BEFORE INSERT OR UPDATE ON opd_invoice_items
    FOR EACH ROW EXECUTE FUNCTION opd_invoice_item_tax();

-- 116 definition + line tax in the subtotal
CREATE OR REPLACE FUNCTION opd_recalc_invoice_subtotal() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_invoice UUID := COALESCE(NEW.invoice_id, OLD.invoice_id);
        v_clinic  UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN NULL; END IF;
    UPDATE opd_invoices i
       SET subtotal_paise = (SELECT COALESCE(SUM(line_total_paise + total_tax_paise), 0) FROM opd_invoice_items
                              WHERE invoice_id = v_invoice AND clinic_id = v_clinic)
     WHERE i.id = v_invoice AND i.clinic_id = v_clinic AND i.status = 'draft';
    RETURN NULL;
END $$;

-- ── 4. Invoice header guard (103 + tax + credit notes) ─────────────────────
-- total_paise / invoice_number are generated → not yet computed in NEW → excluded.
CREATE OR REPLACE FUNCTION opd_guard_invoice() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_items   INTEGER;
    v_total   INTEGER;
    v_tax     INTEGER;
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
    -- credited_paise moves only inside opd_issue_credit_note, for this invoice
    IF NEW.credited_paise <> OLD.credited_paise
       AND current_setting('kriya.opd_credit_invoice', true) IS DISTINCT FROM OLD.id::TEXT THEN
        RAISE EXCEPTION 'opd_credited_is_derived' USING ERRCODE = 'P0001';
    END IF;

    IF OLD.status = 'draft' THEN
        SELECT count(*), COALESCE(SUM(line_total_paise + total_tax_paise), 0), COALESCE(SUM(total_tax_paise), 0)
          INTO v_items, v_total, v_tax
          FROM opd_invoice_items WHERE invoice_id = OLD.id AND clinic_id = OLD.clinic_id;
        -- the header discount lowers taxable value pro rata
        NEW.total_tax_paise := CASE WHEN NEW.subtotal_paise > 0 AND v_total = NEW.subtotal_paise
                                    THEN v_tax - round(v_tax::NUMERIC * NEW.discount_paise / NEW.subtotal_paise)::INTEGER
                                    ELSE 0 END;
        IF NEW.status IN ('issued', 'paid') THEN
            IF v_items = 0 THEN RAISE EXCEPTION 'opd_invoice_empty' USING ERRCODE = 'P0001'; END IF;
            IF v_total <> NEW.subtotal_paise THEN RAISE EXCEPTION 'opd_invoice_subtotal_mismatch' USING ERRCODE = 'P0001'; END IF;
            NEW.invoice_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
            NEW.invoice_seq  := opd_next_counter(OLD.clinic_id, 'inv:' || NEW.invoice_year);
            NEW.issued_at    := now();
            NEW.clinic_gstin := opd_clinic_gstin(OLD.clinic_id);
            NEW.is_tax_invoice := NEW.total_tax_paise > 0;
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
        -- a credit note already reported against this invoice: it can only be credited further
        IF OLD.credited_paise <> 0 THEN RAISE EXCEPTION 'opd_void_has_credit_notes' USING ERRCODE = 'P0001'; END IF;
        v_mutable := v_mutable || ARRAY['status', 'voided_at', 'voided_by', 'void_reason'];
    ELSIF NEW.status = 'draft' THEN
        RAISE EXCEPTION 'opd_bad_transition:%->draft', OLD.status USING ERRCODE = 'P0001';
    ELSE
        v_mutable := v_mutable || ARRAY['status', 'paid_paise', 'credited_paise'];
    END IF;
    IF (to_jsonb(NEW) - v_mutable) <> (to_jsonb(OLD) - v_mutable) THEN
        RAISE EXCEPTION 'opd_record_locked:opd_invoices:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

-- 103 definition, due = total - credited
CREATE OR REPLACE FUNCTION opd_receipt_after_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_new_paid INTEGER; v_total INTEGER; v_status TEXT;
BEGIN
    SELECT status, subtotal_paise - discount_paise - credited_paise,
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

-- ── 5. Credit notes ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS opd_credit_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id           UUID NULL,
    invoice_id          UUID NOT NULL,
    credit_note_year    SMALLINT NOT NULL,
    credit_note_seq     INTEGER NOT NULL CHECK (credit_note_seq > 0),
    credit_note_number  TEXT GENERATED ALWAYS AS (
                            opd_format_number('CRN-', credit_note_year, credit_note_seq, 5)) STORED,
    amount_paise        INTEGER NOT NULL CHECK (amount_paise >= 0),          -- taxable value credited
    tax_paise           INTEGER NOT NULL DEFAULT 0 CHECK (tax_paise >= 0),   -- GST reversed
    total_credit_paise  INTEGER GENERATED ALWAYS AS (amount_paise + tax_paise) STORED,
    reason              TEXT NOT NULL CHECK (reason IN ('cancellation', 'discount_post_issue', 'billing_error',
                                                        'service_deficiency', 'other')),
    reason_notes        TEXT NULL CHECK (char_length(reason_notes) <= 300),
    status              TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'void')),
    refund_receipt_id   UUID NULL,
    issued_by           UUID NULL,   -- NULL = platform login without a staff id
    issued_by_name      TEXT NOT NULL CHECK (char_length(issued_by_name) BETWEEN 1 AND 100),
    idempotency_key     TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_credit_notes_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_credit_notes_seq_key UNIQUE (clinic_id, credit_note_year, credit_note_seq),
    CONSTRAINT opd_credit_notes_positive CHECK (amount_paise + tax_paise > 0),
    -- invoices and receipts are deleted only by the owner purge; their credit notes go with them
    CONSTRAINT opd_credit_notes_invoice_fk FOREIGN KEY (clinic_id, invoice_id)
        REFERENCES opd_invoices (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_credit_notes_refund_fk FOREIGN KEY (clinic_id, refund_receipt_id)
        REFERENCES opd_receipts (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT opd_credit_notes_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_credit_notes_idempotency
    ON opd_credit_notes (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opd_credit_notes_invoice ON opd_credit_notes (clinic_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_opd_credit_notes_day ON opd_credit_notes (clinic_id, created_at DESC);

CREATE OR REPLACE FUNCTION opd_guard_credit_note_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('kriya.opd_credit_invoice', true) IS DISTINCT FROM NEW.invoice_id::TEXT THEN
        RAISE EXCEPTION 'opd_credit_note_via_function_only' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_opd_credit_notes_insert ON opd_credit_notes;
CREATE TRIGGER trg_opd_credit_notes_insert BEFORE INSERT ON opd_credit_notes
    FOR EACH ROW EXECUTE FUNCTION opd_guard_credit_note_insert();
DROP TRIGGER IF EXISTS trg_opd_credit_notes_immutable ON opd_credit_notes;
CREATE TRIGGER trg_opd_credit_notes_immutable BEFORE UPDATE OR DELETE ON opd_credit_notes
    FOR EACH ROW EXECUTE FUNCTION opd_guard_append_only();

-- Issues one credit note under the invoice row lock. p_amount_paise is the
-- gross (tax-inclusive) amount credited; its GST share is the invoice's tax
-- ratio, and the note that exhausts the invoice takes the exact remainder, so
-- notes never reverse more tax than was charged. Money the patient paid beyond
-- what is now due is refunded in the same transaction (p_refund_mode, into
-- p_shift_id, which must be the actor's open shift).
CREATE OR REPLACE FUNCTION opd_issue_credit_note(
    p_clinic_id UUID, p_invoice_id UUID, p_amount_paise INTEGER, p_reason TEXT, p_reason_notes TEXT,
    p_idempotency_key TEXT, p_refund_mode TEXT, p_refund_reference TEXT, p_shift_id UUID,
    p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v_inv opd_invoices; v_cn opd_credit_notes; v_total INTEGER; v_due INTEGER; v_excess INTEGER;
        v_tax INTEGER; v_tax_left INTEGER; v_year SMALLINT; v_seq INTEGER; v_number TEXT; v_rct UUID;
        v_paid_after INTEGER;
BEGIN
    SELECT * INTO v_inv FROM opd_invoices WHERE id = p_invoice_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'opd_not_found' USING ERRCODE = 'P0002'; END IF;
    -- checked under the lock: a concurrent retry with the same key waits, then replays
    IF p_idempotency_key IS NOT NULL THEN
        SELECT * INTO v_cn FROM opd_credit_notes WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key;
        IF FOUND THEN
            IF v_cn.invoice_id <> p_invoice_id THEN
                RAISE EXCEPTION 'opd_credit_note_key_reused' USING ERRCODE = 'P0001';
            END IF;
            RETURN to_jsonb(v_cn) || jsonb_build_object('replayed', true);
        END IF;
    END IF;
    IF v_inv.status NOT IN ('issued', 'partially_paid', 'paid') THEN
        RAISE EXCEPTION 'opd_credit_note_invoice_state:%', v_inv.status USING ERRCODE = 'P0001';
    END IF;
    v_total := v_inv.subtotal_paise - v_inv.discount_paise;
    v_due := v_total - v_inv.credited_paise;
    IF p_amount_paise IS NULL OR p_amount_paise <= 0 THEN
        RAISE EXCEPTION 'opd_credit_note_amount' USING ERRCODE = 'P0001';
    END IF;
    IF p_amount_paise > v_due THEN
        RAISE EXCEPTION 'opd_credit_note_exceeds_invoice:%', v_due USING ERRCODE = 'P0001';
    END IF;

    SELECT v_inv.total_tax_paise - COALESCE(SUM(tax_paise), 0) INTO v_tax_left FROM opd_credit_notes
     WHERE clinic_id = p_clinic_id AND invoice_id = p_invoice_id AND status = 'issued';
    v_tax := CASE WHEN p_amount_paise = v_due THEN v_tax_left
                  ELSE LEAST(round(p_amount_paise::NUMERIC * v_inv.total_tax_paise / v_total)::INTEGER, v_tax_left) END;
    v_tax := LEAST(GREATEST(v_tax, 0), p_amount_paise);

    v_paid_after := LEAST(v_inv.paid_paise, v_due - p_amount_paise);
    v_excess := v_inv.paid_paise - v_paid_after;
    v_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
    v_seq := opd_next_counter(p_clinic_id, 'crn:' || v_year);
    v_number := opd_format_number('CRN-', v_year, v_seq, 5);

    IF v_excess > 0 THEN
        IF p_refund_mode IS NULL OR p_refund_mode NOT IN ('cash', 'upi') THEN
            RAISE EXCEPTION 'opd_credit_note_refund_required:%', v_excess USING ERRCODE = 'P0001';
        END IF;
        -- shift ownership / openness: opd_receipt_before_insert
        INSERT INTO opd_receipts (clinic_id, invoice_id, shift_id, kind, mode, amount_paise, reference,
                                  received_by_admin_id, received_by_name, reason)
        VALUES (p_clinic_id, p_invoice_id, p_shift_id, 'refund', p_refund_mode, v_excess,
                NULLIF(btrim(p_refund_reference), ''), p_actor_id, left(p_actor_name, 100),
                left('Refund against credit note ' || v_number, 300))
        RETURNING id INTO v_rct;
    END IF;

    PERFORM set_config('kriya.opd_credit_invoice', p_invoice_id::TEXT, true);
    INSERT INTO opd_credit_notes (clinic_id, branch_id, invoice_id, credit_note_year, credit_note_seq,
                                  amount_paise, tax_paise, reason, reason_notes, refund_receipt_id,
                                  issued_by, issued_by_name, idempotency_key)
    VALUES (p_clinic_id, v_inv.branch_id, p_invoice_id, v_year, v_seq, p_amount_paise - v_tax, v_tax,
            p_reason, NULLIF(btrim(p_reason_notes), ''), v_rct, p_actor_id, left(p_actor_name, 100),
            p_idempotency_key)
    RETURNING * INTO v_cn;
    UPDATE opd_invoices
       SET credited_paise = credited_paise + p_amount_paise,
           status = CASE WHEN paid_paise = v_due - p_amount_paise THEN 'paid'
                         WHEN paid_paise = 0 THEN 'issued' ELSE 'partially_paid' END
     WHERE id = p_invoice_id AND clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_credit_invoice', '', true);
    RETURN to_jsonb(v_cn) || jsonb_build_object('replayed', false, 'refund_paise', v_excess);
END $$;

-- ── 6. Room rent (Notification 04/2022-CT(R)) ─────────────────────────────
-- A bed-day above Rs 5,000 is taxed at 5% unless the bed held that day is in
-- an ICU-type ward or of an ICU-type category. The day's bed is the one
-- ipd_accrue_bed_charges prices (last assignment started on or before it).
CREATE OR REPLACE FUNCTION ipd_apply_room_rent_gst() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_ward TEXT; v_cat TEXT; v_taxable BOOLEAN := false;
BEGIN
    IF NEW.category <> 'bed' THEN RETURN NEW; END IF;
    IF NEW.unit_price_paise > 500000 AND opd_clinic_gstin(NEW.clinic_id) IS NOT NULL THEN
        SELECT w.ward_type, ba.bed_category INTO v_ward, v_cat
          FROM ipd_bed_assignments ba
          JOIN ipd_wards w ON w.id = ba.ward_id AND w.clinic_id = ba.clinic_id
         WHERE ba.clinic_id = NEW.clinic_id AND ba.admission_id = NEW.admission_id
           AND ba.started_at < ((NEW.service_date + 1)::TIMESTAMP AT TIME ZONE INTERVAL '+05:30')
           AND (ba.ended_at IS NULL OR ba.ended_at >= (NEW.service_date::TIMESTAMP AT TIME ZONE INTERVAL '+05:30'))
         ORDER BY ba.started_at DESC LIMIT 1;
        v_taxable := COALESCE(v_ward, '') NOT IN ('icu', 'nicu', 'picu')
                 AND lower(COALESCE(v_cat, '')) NOT IN ('icu', 'nicu', 'picu', 'ccu', 'iccu');
    END IF;
    NEW.tax_rate_bps := CASE WHEN v_taxable THEN 500 ELSE 0 END;
    NEW.hsn_sac := CASE WHEN v_taxable THEN '999311' ELSE NEW.hsn_sac END;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_ipd_charge_room_gst ON ipd_charges;
CREATE TRIGGER trg_ipd_charge_room_gst BEFORE INSERT OR UPDATE OF category, unit_price_paise, service_date
    ON ipd_charges FOR EACH ROW EXECUTE FUNCTION ipd_apply_room_rent_gst();

-- ── 7. IPD bill: lines carry their rate (116 + tax_rate_bps, hsn_sac) ──────
DROP FUNCTION IF EXISTS ipd_bill_lines(uuid, uuid);
CREATE FUNCTION ipd_bill_lines(p_clinic_id UUID, p_admission_id UUID)
RETURNS TABLE (category TEXT, description TEXT, unit_price_paise INTEGER, qty INTEGER, first_day DATE,
               covered BOOLEAN, covered_value_paise BIGINT, covered_count INTEGER,
               tax_rate_bps SMALLINT, hsn_sac TEXT)
LANGUAGE sql STABLE AS $$
    WITH a AS (
        SELECT admitted_at FROM ipd_admissions WHERE id = p_admission_id AND clinic_id = p_clinic_id
    ), pkg AS (
        SELECT snapshot ->> 'name' AS name,
               ARRAY(SELECT jsonb_array_elements_text(snapshot -> 'included_categories')) AS cats,
               NULLIF(snapshot ->> 'max_days', '')::INTEGER AS max_days
          FROM ipd_admission_packages
         WHERE clinic_id = p_clinic_id AND admission_id = p_admission_id AND status = 'active'
    ), c AS (
        SELECT ch.*,
               (pkg.name IS NOT NULL AND ch.category <> 'package' AND ch.category = ANY (pkg.cats)
                AND (pkg.max_days IS NULL OR ch.service_date < ipd_ist_date(a.admitted_at) + pkg.max_days)) AS is_covered
          FROM ipd_charges ch
          CROSS JOIN a
          LEFT JOIN pkg ON true
         WHERE ch.clinic_id = p_clinic_id AND ch.admission_id = p_admission_id
           AND ch.status = 'posted' AND ch.invoice_id IS NULL
    )
    SELECT c.category, c.description, c.unit_price_paise, SUM(c.quantity)::INTEGER, min(c.service_date),
           false, 0::BIGINT, count(*)::INTEGER, c.tax_rate_bps, c.hsn_sac
      FROM c WHERE NOT c.is_covered
     GROUP BY c.category, c.description, c.unit_price_paise, c.tax_rate_bps, c.hsn_sac
    UNION ALL
    SELECT 'package_cover', left('Covered by package: ' || (SELECT name FROM pkg), 200), 0, 1, min(c.service_date),
           true, SUM(c.total_paise)::BIGINT, count(*)::INTEGER, 0::SMALLINT, NULL::TEXT
      FROM c WHERE c.is_covered
    HAVING count(*) > 0
$$;

-- 116 definition; lines carry tax_rate_bps / hsn_sac
CREATE OR REPLACE FUNCTION ipd_generate_final_bill(p_clinic_id UUID, p_admission_id UUID,
                                                   p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_inv UUID; v_line INTEGER := 0; r RECORD; v_patient JSONB; v_discount INTEGER;
        v_subtotal INTEGER;
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
    FOR r IN SELECT * FROM ipd_bill_lines(p_clinic_id, a.id) l ORDER BY l.covered, l.first_day, l.category, l.description LOOP
        v_line := v_line + 1;
        IF v_line > 100 THEN PERFORM ipd_raise('ipd_bill_too_many_lines'); END IF;
        IF r.qty > 999 THEN PERFORM ipd_raise('ipd_bill_line_quantity'); END IF;
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise,
                                       tax_rate_bps, hsn_sac)
        VALUES (p_clinic_id, v_inv, v_line,
                CASE r.category WHEN 'nursing' THEN 'nursing' WHEN 'doctor_visit' THEN 'consultation'
                                WHEN 'procedure' THEN 'procedure' WHEN 'package' THEN 'procedure'
                                WHEN 'investigation' THEN 'diagnostic' ELSE 'other' END,
                left(CASE WHEN r.covered THEN r.description || ' (' || r.covered_count || ' items)' ELSE r.description END, 200),
                r.qty, r.unit_price_paise, r.tax_rate_bps, r.hsn_sac);
    END LOOP;
    UPDATE ipd_charges SET invoice_id = v_inv
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'posted' AND invoice_id IS NULL;
    PERFORM ipd_sync_bill_discount(p_clinic_id, a.id);
    SELECT discount_paise, subtotal_paise INTO v_discount, v_subtotal FROM opd_invoices WHERE id = v_inv;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'final_bill_created', p_actor_id, p_actor_name,
                    jsonb_build_object('invoice_id', v_inv, 'lines', v_line, 'subtotal_paise', v_subtotal,
                                       'discount_paise', v_discount));
    RETURN jsonb_build_object('invoice_id', v_inv, 'lines', v_line);
END $$;

-- 116 definition + GST in the estimate, credit notes in what is due
CREATE OR REPLACE FUNCTION ipd_running_bill(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_lines JSONB; v_charged BIGINT; v_covered BIGINT; v_inv RECORD; v_pkg JSONB;
        v_disc JSONB; v_pending JSONB; v_credit JSONB; v_dep JSONB; v_prop RECORD; v_approved INTEGER; v_net BIGINT;
        v_balance INTEGER; v_tax BIGINT;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE id = p_admission_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    SELECT COALESCE(jsonb_agg(to_jsonb(l) || jsonb_build_object('tax_paise',
                                  opd_gst_amount(l.unit_price_paise::BIGINT * l.qty, l.tax_rate_bps))
                              ORDER BY l.covered, l.first_day, l.category, l.description), '[]'::jsonb),
           COALESCE(SUM(l.unit_price_paise::BIGINT * l.qty), 0), COALESCE(SUM(l.covered_value_paise), 0),
           COALESCE(SUM(opd_gst_amount(l.unit_price_paise::BIGINT * l.qty, l.tax_rate_bps)), 0)
      INTO v_lines, v_charged, v_covered, v_tax
      FROM ipd_bill_lines(p_clinic_id, a.id) l;
    SELECT count(*) AS n, COALESCE(SUM(total_paise), 0) AS total INTO v_prop
      FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'proposed';
    SELECT jsonb_build_object('id', id, 'status', status, 'invoice_number', invoice_number,
                              'subtotal_paise', subtotal_paise, 'discount_paise', discount_paise,
                              'total_paise', total_paise, 'paid_paise', paid_paise,
                              'total_tax_paise', total_tax_paise, 'credited_paise', credited_paise) AS j,
           total_paise - credited_paise AS due_total, paid_paise
      INTO v_inv FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void';
    SELECT to_jsonb(p) - 'clinic_id' INTO v_pkg FROM ipd_admission_packages p
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active';
    SELECT to_jsonb(d) - 'clinic_id', d.amount_paise INTO v_disc, v_approved FROM ipd_discounts d
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'approved';
    SELECT to_jsonb(d) - 'clinic_id' INTO v_pending FROM ipd_discounts d
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'requested';
    SELECT to_jsonb(c) - 'clinic_id' INTO v_credit FROM ipd_credit_authorizations c
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active';
    SELECT jsonb_build_object(
               'received_paise', COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'deposit'), 0),
               'refunded_paise', COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'refund'), 0),
               'applied_paise', COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'applied'), 0)
                              - COALESCE(SUM(amount_paise) FILTER (WHERE kind = 'unapplied'), 0))
      INTO v_dep FROM ipd_deposits WHERE clinic_id = p_clinic_id AND admission_id = a.id;
    v_balance := ipd_deposit_balance(p_clinic_id, a.id);
    -- before the final bill: estimate from the posted charges (+ GST); after: the bill itself
    v_net := CASE WHEN v_inv.j IS NULL THEN GREATEST(v_charged + v_tax - COALESCE(v_approved, 0), 0)
                  ELSE v_inv.due_total - v_inv.paid_paise END;
    RETURN jsonb_build_object(
        'admission_id', a.id, 'admission_number', a.admission_number, 'status', a.status,
        'as_of', now(), 'lines', v_lines, 'charged_paise', v_charged, 'covered_value_paise', v_covered,
        'tax_paise', v_tax,
        'proposed', jsonb_build_object('count', v_prop.n, 'total_paise', v_prop.total),
        'package', v_pkg, 'discount', v_disc, 'pending_discount', v_pending, 'credit', v_credit,
        'deposits', v_dep || jsonb_build_object('balance_paise', v_balance),
        'bill', v_inv.j,
        'payable_paise', v_net,
        'due_after_deposits_paise', GREATEST(v_net - v_balance, 0),
        'refundable_paise', GREATEST(v_balance - v_net, 0));
END $$;

-- 116 definition, due = total - credited - paid
CREATE OR REPLACE FUNCTION ipd_apply_deposits(p_clinic_id UUID, p_admission_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_inv opd_invoices; v_amount INTEGER; v_rct UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    SELECT * INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status IN ('issued', 'partially_paid') FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('applied_paise', 0); END IF;
    v_amount := LEAST(ipd_deposit_balance(p_clinic_id, a.id), v_inv.total_paise - v_inv.credited_paise - v_inv.paid_paise);
    IF v_amount <= 0 THEN RETURN jsonb_build_object('applied_paise', 0, 'invoice_id', v_inv.id); END IF;
    INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, reference, received_by_admin_id,
                              received_by_name, reason)
    VALUES (p_clinic_id, v_inv.id, 'payment', 'deposit', v_amount, left('Deposit ' || a.admission_number, 100),
            p_actor_id, left(p_actor_name, 100), 'Deposit applied to the final bill')
    RETURNING id INTO v_rct;
    INSERT INTO ipd_deposits (clinic_id, admission_id, kind, mode, amount_paise, invoice_id, receipt_id,
                              received_by_admin_id, received_by_name)
    VALUES (p_clinic_id, a.id, 'applied', 'bill', v_amount, v_inv.id, v_rct, p_actor_id, left(p_actor_name, 100));
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'deposit_applied', p_actor_id, p_actor_name,
                    jsonb_build_object('invoice_id', v_inv.id, 'amount_paise', v_amount));
    RETURN jsonb_build_object('applied_paise', v_amount, 'invoice_id', v_inv.id, 'receipt_id', v_rct);
END $$;

-- 116 definition, due = total - credited - paid
CREATE OR REPLACE FUNCTION ipd_discharge_checklist(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_items JSONB := '[]'::jsonb; v_inv RECORD; v_n INTEGER; v_credit INTEGER;
        v_balance INTEGER; v_due INTEGER;
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
    SELECT count(*) INTO v_n FROM ipd_discounts WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'requested';
    v_items := v_items || jsonb_build_object('key', 'discount_decided', 'done', v_n = 0, 'count', v_n, 'waivable', false);
    SELECT id, status, total_paise - credited_paise AS total_paise, paid_paise, invoice_number INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void' LIMIT 1;
    v_items := v_items || jsonb_build_object('key', 'final_bill_issued', 'waivable', false,
        'done', v_inv.id IS NOT NULL AND v_inv.status <> 'draft', 'invoice_id', v_inv.id,
        'invoice_number', v_inv.invoice_number);
    SELECT count(*) INTO v_n FROM ipd_charges
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'posted' AND invoice_id IS NULL;
    v_items := v_items || jsonb_build_object('key', 'all_charges_billed', 'done', v_n = 0, 'count', v_n, 'waivable', false);
    v_balance := ipd_deposit_balance(p_clinic_id, a.id);
    v_items := v_items || jsonb_build_object('key', 'deposits_cleared', 'done', v_balance = 0, 'waivable', false,
        'balance_paise', v_balance);
    v_due := CASE WHEN v_inv.id IS NULL THEN NULL ELSE v_inv.total_paise - v_inv.paid_paise END;
    SELECT amount_paise INTO v_credit FROM ipd_credit_authorizations
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active';
    v_items := v_items || jsonb_build_object('key', 'bill_settled', 'waivable', true,
        'done', v_inv.id IS NOT NULL AND v_inv.status <> 'draft'
                AND (v_inv.status = 'paid' OR (v_credit IS NOT NULL AND v_due <= v_credit)),
        'balance_paise', v_due, 'credit_paise', v_credit);
    RETURN jsonb_build_object('admission_id', a.id, 'status', a.status, 'items', v_items,
        'ready', NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                              WHERE NOT (i ->> 'done')::BOOLEAN AND NOT (i ->> 'waivable')::BOOLEAN),
        'needs_waiver', EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                                 WHERE NOT (i ->> 'done')::BOOLEAN AND (i ->> 'waivable')::BOOLEAN));
END $$;

-- ── 8. RLS parity + API-role lockout + search_path (103/116 pattern) ───────
DO $$
BEGIN
    ALTER TABLE opd_credit_notes ENABLE ROW LEVEL SECURITY;
    ALTER TABLE opd_credit_notes FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS service_role_all_opd_credit_notes ON opd_credit_notes;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_opd_credit_notes ON opd_credit_notes
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    DROP POLICY IF EXISTS tenant_isolation_opd_credit_notes ON opd_credit_notes;
    CREATE POLICY tenant_isolation_opd_credit_notes ON opd_credit_notes FOR ALL
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE opd_credit_notes FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE opd_credit_notes FROM authenticated;
    END IF;
END $$;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'opd_gst_amount(bigint,integer)', 'opd_clinic_gstin(uuid)', 'ipd_bill_lines(uuid,uuid)',
        'opd_issue_credit_note(uuid,uuid,integer,text,text,text,text,text,uuid,uuid,text)']
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
    FOREACH f IN ARRAY ARRAY[
        'opd_gst_amount(bigint,integer)', 'opd_clinic_gstin(uuid)', 'opd_invoice_item_tax()',
        'opd_recalc_invoice_subtotal()', 'opd_guard_invoice()', 'opd_receipt_after_insert()',
        'opd_guard_credit_note_insert()', 'opd_issue_credit_note(uuid,uuid,integer,text,text,text,text,text,uuid,uuid,text)',
        'ipd_apply_room_rent_gst()', 'ipd_bill_lines(uuid,uuid)', 'ipd_generate_final_bill(uuid,uuid,uuid,text)',
        'ipd_running_bill(uuid,uuid)', 'ipd_apply_deposits(uuid,uuid,uuid,text)', 'ipd_discharge_checklist(uuid,uuid)']
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', f);
    END LOOP;
END $$;

-- ── 9. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.opd_credit_notes') IS NULL THEN
        RAISE EXCEPTION '117 verify: opd_credit_notes missing';
    END IF;
    IF (SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND (table_name, column_name) IN (
          ('opd_invoice_items', 'tax_rate_bps'), ('opd_invoice_items', 'total_tax_paise'),
          ('opd_invoices', 'total_tax_paise'), ('opd_invoices', 'credited_paise'), ('opd_invoices', 'clinic_gstin'),
          ('ipd_charges', 'tax_rate_bps'), ('ipd_charges', 'tax_paise'))) <> 7 THEN
        RAISE EXCEPTION '117 verify: tax columns missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_opd_inv_items_tax', 'trg_opd_credit_notes_insert', 'trg_opd_credit_notes_immutable',
          'trg_ipd_charge_room_gst')) <> 4 THEN
        RAISE EXCEPTION '117 verify: triggers missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'opd_invoices_paid_consistent'
                     AND pg_get_constraintdef(oid) LIKE '%credited_paise%') THEN
        RAISE EXCEPTION '117 verify: paid_consistent not credit-aware';
    END IF;
END $$;
