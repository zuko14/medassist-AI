-- ============================================================================
-- Migration 116: IPD finance (G5) - deposits, running bill, packages,
-- discount approval, credit / corporate bills
-- ============================================================================
-- Spec: docs/hospital-os/G5-IPD-FINANCE-SPEC.md.
--
--  * ipd_deposits: append-only ledger per stay (deposit / refund / applied /
--    unapplied). Counter money goes through the receiver's open cashier shift
--    and counts in its expected cash (opd_close_shift is redefined). Balance is
--    checked under the admission row lock: it can never go negative.
--  * Applying deposits writes an opd_receipts row (new mode 'deposit') against
--    the issued final bill, in the same transaction (ipd_apply_deposits).
--    Voiding a bill first returns them (ipd_unapply_deposits).
--  * ipd_bill_lines(): the single definition of the final bill's lines -
--    posted charges, minus what an active package covers - used by the final
--    bill and the running bill alike.
--  * Packages (master + one active per stay), discounts (one live per stay,
--    requester <> approver unless auto-approved, the bill can only be issued
--    with exactly the approved amount), credit authorisations (discharge treats
--    a covered balance as settled; the bill stays a receivable).
--  * opd_receipts gains mode 'bank_transfer' (corporate / payer settlement).
--
-- Additive for pre-116 code: new tables, functions and modes; the redefined
-- functions keep their signatures and behaviour for stays without deposits,
-- packages, discounts or credit. Idempotent.
-- Rollback: migrations/rollback/116_down.sql.
-- ============================================================================

-- ── 1. Wider enums on existing tables ──────────────────────────────────────
ALTER TABLE ipd_charges DROP CONSTRAINT IF EXISTS ipd_charges_source_check;
ALTER TABLE ipd_charges ADD  CONSTRAINT ipd_charges_source_check
    CHECK (source IN ('manual', 'bed_day', 'order', 'package'));

ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_mode_check;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_mode_check
    CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online', 'bank_transfer', 'deposit'));
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_reference_required;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_reference_required
    CHECK (mode NOT IN ('upi', 'card', 'bank_transfer') OR reference IS NOT NULL);
-- a deposit application is money already in the hospital: no shift of its own
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_counter_needs_shift;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_counter_needs_shift
    CHECK (mode IN ('payment_link', 'prepaid_online', 'deposit') OR shift_id IS NOT NULL);
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_refund_manual;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_refund_manual
    CHECK (kind = 'payment' OR (mode IN ('cash', 'upi', 'deposit') AND reason IS NOT NULL));

CREATE OR REPLACE FUNCTION ipd_guard_deposit_receipt() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.mode = 'deposit' AND NOT ipd_in_tx(NEW.clinic_id) THEN
        PERFORM ipd_raise('ipd_deposit_via_function_only');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_opd_receipts_deposit ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_deposit BEFORE INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_deposit_receipt();

-- ── 2. Deposit ledger ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_deposits (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id         UUID NOT NULL,
    branch_id            UUID NULL,          -- the stay's branch, set by the insert trigger
    kind                 TEXT NOT NULL CHECK (kind IN ('deposit', 'refund', 'applied', 'unapplied')),
    mode                 TEXT NOT NULL CHECK (mode IN ('cash', 'upi', 'card', 'bank_transfer', 'bill')),
    amount_paise         INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    reference            TEXT NULL CHECK (char_length(reference) <= 100),
    reason               TEXT NULL CHECK (char_length(btrim(reason)) BETWEEN 3 AND 300),
    shift_id             UUID NULL,
    invoice_id           UUID NULL,
    receipt_id           UUID NULL,
    doc_year             SMALLINT NULL,
    doc_seq              INTEGER NULL CHECK (doc_seq > 0),
    doc_number           TEXT GENERATED ALWAYS AS (
                             CASE WHEN doc_seq IS NULL THEN NULL
                                  WHEN kind = 'deposit' THEN opd_format_number('DEP-', doc_year, doc_seq, 5)
                                  ELSE opd_format_number('DRF-', doc_year, doc_seq, 5) END) STORED,
    received_by_admin_id UUID NULL,          -- clinic_admins.id; no FK: staff rows can be deleted, the record stays
    received_by_name     TEXT NOT NULL CHECK (char_length(btrim(received_by_name)) BETWEEN 1 AND 100),
    idempotency_key      TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_deposits_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_deposits_mode_shape CHECK ((kind IN ('applied', 'unapplied')) = (mode = 'bill')),
    CONSTRAINT ipd_deposits_bill_link CHECK ((kind IN ('applied', 'unapplied')) = (invoice_id IS NOT NULL AND receipt_id IS NOT NULL)),
    CONSTRAINT ipd_deposits_counter_shift CHECK (kind NOT IN ('deposit', 'refund') OR shift_id IS NOT NULL),
    CONSTRAINT ipd_deposits_reference CHECK (mode NOT IN ('upi', 'card', 'bank_transfer') OR reference IS NOT NULL),
    CONSTRAINT ipd_deposits_refund_shape CHECK (kind <> 'refund' OR (mode IN ('cash', 'upi', 'bank_transfer') AND reason IS NOT NULL)),
    CONSTRAINT ipd_deposits_unapplied_reason CHECK (kind <> 'unapplied' OR reason IS NOT NULL),
    CONSTRAINT ipd_deposits_numbered CHECK ((kind IN ('deposit', 'refund')) = (doc_seq IS NOT NULL)),
    CONSTRAINT ipd_deposits_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_deposits_branch_fk    FOREIGN KEY (clinic_id, branch_id)    REFERENCES branches (clinic_id, id),
    CONSTRAINT ipd_deposits_shift_fk     FOREIGN KEY (clinic_id, shift_id)     REFERENCES opd_cashier_shifts (clinic_id, id),
    CONSTRAINT ipd_deposits_invoice_fk   FOREIGN KEY (clinic_id, invoice_id)   REFERENCES opd_invoices (clinic_id, id),
    CONSTRAINT ipd_deposits_receipt_fk   FOREIGN KEY (clinic_id, receipt_id)   REFERENCES opd_receipts (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_deposits_idempotency
    ON ipd_deposits (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_deposits_number
    ON ipd_deposits (clinic_id, kind, doc_year, doc_seq) WHERE doc_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_deposits_admission ON ipd_deposits (clinic_id, admission_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ipd_deposits_shift     ON ipd_deposits (clinic_id, shift_id) WHERE shift_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_deposits_day       ON ipd_deposits (clinic_id, created_at DESC);

CREATE OR REPLACE FUNCTION ipd_deposit_balance(p_clinic UUID, p_admission UUID) RETURNS INTEGER
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(SUM(CASE WHEN kind IN ('deposit', 'unapplied') THEN amount_paise ELSE -amount_paise END), 0)::INTEGER
      FROM ipd_deposits WHERE clinic_id = p_clinic AND admission_id = p_admission
$$;

-- Number, shift and balance checks under the admission lock: of two concurrent
-- refunds of the same balance, the second sees the first and is refused.
CREATE OR REPLACE FUNCTION ipd_deposit_before_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_shift_status TEXT; v_cashier UUID; v_net INTEGER;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE id = NEW.admission_id AND clinic_id = NEW.clinic_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    IF NEW.kind IN ('applied', 'unapplied') AND NOT ipd_in_tx(NEW.clinic_id) THEN
        PERFORM ipd_raise('ipd_deposit_via_function_only');
    END IF;
    IF NEW.kind = 'deposit' AND a.status NOT IN ('pending', 'admitted', 'discharge_pending') THEN
        PERFORM ipd_raise('ipd_admission_not_active');
    END IF;
    NEW.branch_id := a.branch_id;
    IF NEW.shift_id IS NOT NULL THEN
        SELECT status, cashier_admin_id INTO v_shift_status, v_cashier
          FROM opd_cashier_shifts WHERE id = NEW.shift_id AND clinic_id = NEW.clinic_id FOR SHARE;
        IF v_shift_status IS DISTINCT FROM 'open' THEN PERFORM ipd_raise('ipd_shift_not_open'); END IF;
        IF NEW.received_by_admin_id IS DISTINCT FROM v_cashier THEN PERFORM ipd_raise('ipd_shift_not_yours'); END IF;
    END IF;
    IF NEW.kind IN ('refund', 'applied') AND NEW.amount_paise > ipd_deposit_balance(NEW.clinic_id, NEW.admission_id) THEN
        PERFORM ipd_raise('ipd_deposit_insufficient');
    END IF;
    IF NEW.kind = 'unapplied' THEN
        SELECT COALESCE(SUM(CASE kind WHEN 'applied' THEN amount_paise ELSE -amount_paise END), 0) INTO v_net
          FROM ipd_deposits WHERE clinic_id = NEW.clinic_id AND admission_id = NEW.admission_id
           AND invoice_id = NEW.invoice_id AND kind IN ('applied', 'unapplied');
        IF NEW.amount_paise > v_net THEN PERFORM ipd_raise('ipd_deposit_unapply_exceeds'); END IF;
    END IF;
    IF NEW.kind IN ('deposit', 'refund') THEN
        NEW.doc_year := EXTRACT(YEAR FROM (now() + interval '5 hours 30 minutes'))::SMALLINT;
        NEW.doc_seq  := opd_next_counter(NEW.clinic_id, CASE NEW.kind WHEN 'deposit' THEN 'dep:' ELSE 'drf:' END || NEW.doc_year);
    ELSE
        NEW.doc_year := NULL;
        NEW.doc_seq  := NULL;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_deposits_before ON ipd_deposits;
CREATE TRIGGER trg_ipd_deposits_before BEFORE INSERT ON ipd_deposits
    FOR EACH ROW EXECUTE FUNCTION ipd_deposit_before_insert();
DROP TRIGGER IF EXISTS trg_ipd_deposits_append_only ON ipd_deposits;
CREATE TRIGGER trg_ipd_deposits_append_only BEFORE UPDATE OR DELETE ON ipd_deposits
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_append_only();

-- ── 3. Packages ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_packages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    code                TEXT NOT NULL CHECK (code ~ '^[A-Z0-9_-]{2,40}$'),
    name                TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 200),
    description         TEXT NULL CHECK (char_length(description) <= 1000),
    price_paise         INTEGER NOT NULL CHECK (price_paise BETWEEN 1 AND 100000000),
    included_categories TEXT[] NOT NULL CHECK (
                            cardinality(included_categories) BETWEEN 1 AND 8
                            AND included_categories <@ ARRAY['bed', 'nursing', 'doctor_visit', 'procedure', 'investigation',
                                                             'pharmacy', 'consumable', 'other']),
    max_days            SMALLINT NULL CHECK (max_days BETWEEN 1 AND 365),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_by          UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_packages_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_packages_code_key UNIQUE (clinic_id, code)
);
DROP TRIGGER IF EXISTS trg_ipd_packages_touch ON ipd_packages;
CREATE TRIGGER trg_ipd_packages_touch BEFORE UPDATE ON ipd_packages FOR EACH ROW EXECUTE FUNCTION ipd_touch_updated_at();

-- A stay's package: snapshotted at application, so later master edits never re-price a stay.
CREATE TABLE IF NOT EXISTS ipd_admission_packages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id    UUID NOT NULL,
    package_id      UUID NOT NULL,
    snapshot        JSONB NOT NULL CHECK (snapshot ? 'price_paise' AND snapshot ? 'included_categories' AND snapshot ? 'name'),
    charge_id       UUID NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    applied_by      UUID NULL,
    applied_by_name TEXT NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at      TIMESTAMPTZ NULL,
    removed_by      UUID NULL,
    remove_reason   TEXT NULL CHECK (char_length(btrim(remove_reason)) BETWEEN 3 AND 300),
    CONSTRAINT ipd_admission_packages_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_admission_packages_removed CHECK ((status = 'removed') = (removed_at IS NOT NULL AND remove_reason IS NOT NULL)),
    CONSTRAINT ipd_admission_packages_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id),
    CONSTRAINT ipd_admission_packages_package_fk   FOREIGN KEY (clinic_id, package_id)   REFERENCES ipd_packages (clinic_id, id),
    CONSTRAINT ipd_admission_packages_charge_fk    FOREIGN KEY (clinic_id, charge_id)    REFERENCES ipd_charges (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_admission_packages_active
    ON ipd_admission_packages (clinic_id, admission_id) WHERE status = 'active';

-- ── 4. Discounts ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_discounts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id      UUID NOT NULL,
    amount_paise      INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    reason            TEXT NOT NULL CHECK (char_length(btrim(reason)) BETWEEN 5 AND 300),
    status            TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'approved', 'rejected', 'cancelled')),
    auto_approved     BOOLEAN NOT NULL DEFAULT false,
    requested_by      UUID NULL,
    requested_by_name TEXT NOT NULL,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by        UUID NULL,
    decided_by_name   TEXT NULL,
    decided_at        TIMESTAMPTZ NULL,
    decision_note     TEXT NULL CHECK (char_length(decision_note) <= 300),
    idempotency_key   TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    CONSTRAINT ipd_discounts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_discounts_decided CHECK ((status = 'requested') = (decided_at IS NULL)),
    -- segregation of duties: nobody approves their own request (auto-approval is within the requester's limit)
    CONSTRAINT ipd_discounts_four_eyes CHECK (
        status <> 'approved' OR auto_approved OR decided_by IS DISTINCT FROM requested_by),
    CONSTRAINT ipd_discounts_auto_shape CHECK (NOT auto_approved OR status IN ('approved', 'cancelled')),
    CONSTRAINT ipd_discounts_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_discounts_live
    ON ipd_discounts (clinic_id, admission_id) WHERE status IN ('requested', 'approved');
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_discounts_idempotency
    ON ipd_discounts (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_discounts_pending ON ipd_discounts (clinic_id, status) WHERE status = 'requested';

-- ── 5. Credit / corporate authorisations ───────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_credit_authorizations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id     UUID NOT NULL,
    payer_name       TEXT NOT NULL CHECK (char_length(btrim(payer_name)) BETWEEN 2 AND 120),
    reference        TEXT NOT NULL CHECK (char_length(btrim(reference)) BETWEEN 2 AND 100),
    amount_paise     INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    notes            TEXT NULL CHECK (char_length(notes) <= 500),
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    approved_by      UUID NULL,
    approved_by_name TEXT NOT NULL,
    approved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at       TIMESTAMPTZ NULL,
    revoked_by       UUID NULL,
    revoke_reason    TEXT NULL CHECK (char_length(btrim(revoke_reason)) BETWEEN 3 AND 300),
    CONSTRAINT ipd_credit_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_credit_revoked CHECK ((status = 'revoked') = (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)),
    CONSTRAINT ipd_credit_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_credit_active
    ON ipd_credit_authorizations (clinic_id, admission_id) WHERE status = 'active';

-- ── 6. Guards: written by functions only, content never edited ─────────────
CREATE OR REPLACE FUNCTION ipd_guard_finance_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_mutable TEXT[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM ipd_raise('ipd_append_only:' || TG_TABLE_NAME);
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT ipd_in_tx(NEW.clinic_id) THEN PERFORM ipd_raise('ipd_finance_via_function_only'); END IF;
        RETURN NEW;
    END IF;
    IF NOT ipd_in_tx(OLD.clinic_id) THEN PERFORM ipd_raise('ipd_finance_via_function_only'); END IF;
    v_mutable := CASE TG_TABLE_NAME
        WHEN 'ipd_discounts' THEN ARRAY['status', 'decided_by', 'decided_by_name', 'decided_at', 'decision_note']
        WHEN 'ipd_admission_packages' THEN ARRAY['status', 'removed_at', 'removed_by', 'remove_reason']
        ELSE ARRAY['status', 'revoked_at', 'revoked_by', 'revoke_reason'] END;
    IF (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable) THEN
        PERFORM ipd_raise('ipd_record_locked:' || TG_TABLE_NAME);
    END IF;
    -- the decision columns move only together with the status, and only forward
    IF NEW.status = OLD.status THEN
        PERFORM ipd_raise('ipd_record_locked:' || TG_TABLE_NAME);
    END IF;
    IF TG_TABLE_NAME = 'ipd_discounts' THEN
        IF NOT (OLD.status = 'requested' OR (OLD.status = 'approved' AND NEW.status = 'cancelled')) THEN
            PERFORM ipd_raise('ipd_bad_transition:' || OLD.status || '->' || NEW.status);
        END IF;
    ELSIF OLD.status <> 'active' THEN   -- packages: active -> removed; credit: active -> revoked
        PERFORM ipd_raise('ipd_record_locked:' || TG_TABLE_NAME);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ipd_discounts_guard ON ipd_discounts;
CREATE TRIGGER trg_ipd_discounts_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_discounts
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_finance_record();
DROP TRIGGER IF EXISTS trg_ipd_admission_packages_guard ON ipd_admission_packages;
CREATE TRIGGER trg_ipd_admission_packages_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_admission_packages
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_finance_record();
DROP TRIGGER IF EXISTS trg_ipd_credit_guard ON ipd_credit_authorizations;
CREATE TRIGGER trg_ipd_credit_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_credit_authorizations
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_finance_record();

-- An IPD bill is issued only with exactly the approved discount and none pending.
CREATE OR REPLACE FUNCTION ipd_guard_bill_issue() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_approved INTEGER;
BEGIN
    IF NEW.admission_id IS NULL OR OLD.status <> 'draft' OR NEW.status NOT IN ('issued', 'paid') THEN RETURN NEW; END IF;
    IF EXISTS (SELECT 1 FROM ipd_discounts WHERE clinic_id = NEW.clinic_id AND admission_id = NEW.admission_id
                 AND status = 'requested') THEN
        PERFORM ipd_raise('ipd_discount_pending');
    END IF;
    SELECT COALESCE(SUM(amount_paise), 0) INTO v_approved FROM ipd_discounts
     WHERE clinic_id = NEW.clinic_id AND admission_id = NEW.admission_id AND status = 'approved';
    IF NEW.discount_paise <> v_approved THEN PERFORM ipd_raise('ipd_discount_mismatch'); END IF;
    RETURN NEW;
END $$;
-- named after trg_opd_invoices_guard so it sees the guard's final status (zero bill -> paid)
DROP TRIGGER IF EXISTS trg_opd_invoices_ipd_issue ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_ipd_issue BEFORE UPDATE OF status ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION ipd_guard_bill_issue();

-- ── 7. Bill lines: the one definition used by the final and running bill ───
-- Posted, unbilled charges. With an active package, a charge in an included
-- category dated within the package days is covered: it is summed into one
-- 'package_cover' row at price 0 instead of billed. Excluded categories and
-- days beyond the package are billed normally.
CREATE OR REPLACE FUNCTION ipd_bill_lines(p_clinic_id UUID, p_admission_id UUID)
RETURNS TABLE (category TEXT, description TEXT, unit_price_paise INTEGER, qty INTEGER, first_day DATE,
               covered BOOLEAN, covered_value_paise BIGINT, covered_count INTEGER)
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
           false, 0::BIGINT, count(*)::INTEGER
      FROM c WHERE NOT c.is_covered
     GROUP BY c.category, c.description, c.unit_price_paise
    UNION ALL
    SELECT 'package_cover', left('Covered by package: ' || (SELECT name FROM pkg), 200), 0, 1, min(c.service_date),
           true, SUM(c.total_paise)::BIGINT, count(*)::INTEGER
      FROM c WHERE c.is_covered
    HAVING count(*) > 0
$$;

-- ── 8. Final bill (111, now with package cover and the approved discount) ──
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
        INSERT INTO opd_invoice_items (clinic_id, invoice_id, line_no, item_type, description, quantity, unit_price_paise)
        VALUES (p_clinic_id, v_inv, v_line,
                CASE r.category WHEN 'nursing' THEN 'nursing' WHEN 'doctor_visit' THEN 'consultation'
                                WHEN 'procedure' THEN 'procedure' WHEN 'package' THEN 'procedure'
                                WHEN 'investigation' THEN 'diagnostic' ELSE 'other' END,
                left(CASE WHEN r.covered THEN r.description || ' (' || r.covered_count || ' items)' ELSE r.description END, 200),
                r.qty, r.unit_price_paise);
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

-- Puts the approved discount on the stay's draft bill (none -> 0). An issued
-- bill is never touched (ipd_guard_bill_issue already pinned its discount).
CREATE OR REPLACE FUNCTION ipd_sync_bill_discount(p_clinic_id UUID, p_admission_id UUID) RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE v_inv opd_invoices; v_amount INTEGER; v_reason TEXT;
BEGIN
    SELECT * INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = p_admission_id AND status = 'draft' FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT amount_paise, reason INTO v_amount, v_reason FROM ipd_discounts
     WHERE clinic_id = p_clinic_id AND admission_id = p_admission_id AND status = 'approved';
    v_amount := COALESCE(v_amount, 0);
    IF v_amount > v_inv.subtotal_paise THEN PERFORM ipd_raise('ipd_discount_exceeds_bill'); END IF;
    UPDATE opd_invoices SET discount_paise = v_amount,
                            discount_reason = CASE WHEN v_amount > 0 THEN left('Approved: ' || v_reason, 200) END
     WHERE id = v_inv.id AND clinic_id = p_clinic_id;
END $$;

-- ── 9. Running bill (read-only) ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION ipd_running_bill(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_lines JSONB; v_charged BIGINT; v_covered BIGINT; v_inv RECORD; v_pkg JSONB;
        v_disc JSONB; v_pending JSONB; v_credit JSONB; v_dep JSONB; v_prop RECORD; v_approved INTEGER; v_net BIGINT;
        v_balance INTEGER;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE id = p_admission_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_admission_not_found'); END IF;
    SELECT COALESCE(jsonb_agg(to_jsonb(l) ORDER BY l.covered, l.first_day, l.category, l.description), '[]'::jsonb),
           COALESCE(SUM(l.unit_price_paise::BIGINT * l.qty), 0), COALESCE(SUM(l.covered_value_paise), 0)
      INTO v_lines, v_charged, v_covered
      FROM ipd_bill_lines(p_clinic_id, a.id) l;
    SELECT count(*) AS n, COALESCE(SUM(total_paise), 0) AS total INTO v_prop
      FROM ipd_charges WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'proposed';
    SELECT jsonb_build_object('id', id, 'status', status, 'invoice_number', invoice_number,
                              'subtotal_paise', subtotal_paise, 'discount_paise', discount_paise,
                              'total_paise', total_paise, 'paid_paise', paid_paise) AS j, total_paise, paid_paise
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
    -- before the final bill: estimate from the posted charges; after: the bill itself
    v_net := CASE WHEN v_inv.j IS NULL THEN GREATEST(v_charged - COALESCE(v_approved, 0), 0)
                  ELSE v_inv.total_paise - v_inv.paid_paise END;
    RETURN jsonb_build_object(
        'admission_id', a.id, 'admission_number', a.admission_number, 'status', a.status,
        'as_of', now(), 'lines', v_lines, 'charged_paise', v_charged, 'covered_value_paise', v_covered,
        'proposed', jsonb_build_object('count', v_prop.n, 'total_paise', v_prop.total),
        'package', v_pkg, 'discount', v_disc, 'pending_discount', v_pending, 'credit', v_credit,
        'deposits', v_dep || jsonb_build_object('balance_paise', v_balance),
        'bill', v_inv.j,
        'payable_paise', v_net,
        'due_after_deposits_paise', GREATEST(v_net - v_balance, 0),
        'refundable_paise', GREATEST(v_balance - v_net, 0));
END $$;

-- ── 10. Deposits applied to / returned from the final bill ─────────────────
CREATE OR REPLACE FUNCTION ipd_apply_deposits(p_clinic_id UUID, p_admission_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_inv opd_invoices; v_amount INTEGER; v_rct UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    SELECT * INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status IN ('issued', 'partially_paid') FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('applied_paise', 0); END IF;
    v_amount := LEAST(ipd_deposit_balance(p_clinic_id, a.id), v_inv.total_paise - v_inv.paid_paise);
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

-- Before a bill is voided: every deposit applied to it goes back to the stay's balance.
CREATE OR REPLACE FUNCTION ipd_unapply_deposits(p_clinic_id UUID, p_invoice_id UUID, p_reason TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v_inv opd_invoices; v_net INTEGER; v_rct UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO v_inv FROM opd_invoices WHERE id = p_invoice_id AND clinic_id = p_clinic_id;
    IF NOT FOUND OR v_inv.admission_id IS NULL THEN RETURN jsonb_build_object('returned_paise', 0); END IF;
    PERFORM ipd_lock_admission(p_clinic_id, v_inv.admission_id);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    SELECT COALESCE(SUM(CASE kind WHEN 'applied' THEN amount_paise ELSE -amount_paise END), 0) INTO v_net
      FROM ipd_deposits WHERE clinic_id = p_clinic_id AND invoice_id = p_invoice_id AND kind IN ('applied', 'unapplied');
    IF v_net <= 0 THEN RETURN jsonb_build_object('returned_paise', 0); END IF;
    INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, received_by_admin_id,
                              received_by_name, reason)
    VALUES (p_clinic_id, p_invoice_id, 'refund', 'deposit', v_net, p_actor_id, left(p_actor_name, 100),
            left(btrim(p_reason), 300))
    RETURNING id INTO v_rct;
    INSERT INTO ipd_deposits (clinic_id, admission_id, kind, mode, amount_paise, invoice_id, receipt_id, reason,
                              received_by_admin_id, received_by_name)
    VALUES (p_clinic_id, v_inv.admission_id, 'unapplied', 'bill', v_net, p_invoice_id, v_rct, left(btrim(p_reason), 300),
            p_actor_id, left(p_actor_name, 100));
    PERFORM ipd_log(p_clinic_id, v_inv.admission_id, NULL, 'deposit_unapplied', p_actor_id, p_actor_name,
                    jsonb_build_object('invoice_id', p_invoice_id, 'amount_paise', v_net));
    RETURN jsonb_build_object('returned_paise', v_net, 'receipt_id', v_rct);
END $$;

-- ── 11. Packages on a stay ─────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION ipd_apply_package(p_clinic_id UUID, p_admission_id UUID, p_package_id UUID,
                                             p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; p ipd_packages; v_charge UUID; v_ap ipd_admission_packages;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void') THEN
        PERFORM ipd_raise('ipd_bill_exists_void_first');
    END IF;
    SELECT * INTO v_ap FROM ipd_admission_packages
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active';
    IF FOUND THEN
        IF v_ap.package_id = p_package_id THEN RETURN to_jsonb(v_ap) || jsonb_build_object('replayed', true); END IF;
        PERFORM ipd_raise('ipd_package_exists');
    END IF;
    SELECT * INTO p FROM ipd_packages WHERE id = p_package_id AND clinic_id = p_clinic_id AND is_active;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_package_not_found'); END IF;
    INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity, unit_price_paise,
                             source, source_ref, status, posted_at, posted_by, created_by, created_by_name)
    VALUES (p_clinic_id, a.id, 'package', left('Package: ' || p.name, 200), ipd_ist_date(a.admitted_at), 1, p.price_paise,
            'package', 'package:' || p.id || ':' || gen_random_uuid(), 'posted', now(), p_actor_id, p_actor_id,
            left(p_actor_name, 100))
    RETURNING id INTO v_charge;
    INSERT INTO ipd_admission_packages (clinic_id, admission_id, package_id, snapshot, charge_id, applied_by, applied_by_name)
    VALUES (p_clinic_id, a.id, p.id,
            jsonb_build_object('code', p.code, 'name', p.name, 'price_paise', p.price_paise,
                               'included_categories', to_jsonb(p.included_categories), 'max_days', p.max_days),
            v_charge, p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO v_ap;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'package_applied', p_actor_id, p_actor_name,
                    jsonb_build_object('package_id', p.id, 'code', p.code, 'price_paise', p.price_paise));
    RETURN to_jsonb(v_ap);
END $$;

CREATE OR REPLACE FUNCTION ipd_remove_package(p_clinic_id UUID, p_admission_id UUID, p_reason TEXT,
                                              p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_ap ipd_admission_packages;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void') THEN
        PERFORM ipd_raise('ipd_bill_exists_void_first');
    END IF;
    SELECT * INTO v_ap FROM ipd_admission_packages
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status = 'active' FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_package_not_found'); END IF;
    UPDATE ipd_charges SET status = 'void', voided_at = now(), voided_by = p_actor_id,
                           void_reason = left('Package removed: ' || btrim(p_reason), 300)
     WHERE clinic_id = p_clinic_id AND id = v_ap.charge_id AND status <> 'void';
    UPDATE ipd_admission_packages SET status = 'removed', removed_at = now(), removed_by = p_actor_id,
                                      remove_reason = btrim(p_reason)
     WHERE id = v_ap.id AND clinic_id = p_clinic_id RETURNING * INTO v_ap;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'package_removed', p_actor_id, p_actor_name,
                    jsonb_build_object('package_id', v_ap.package_id, 'reason', p_reason));
    RETURN to_jsonb(v_ap);
END $$;

-- ── 12. Discounts ──────────────────────────────────────────────────────────
-- p_auto_approve: the API decides it from the requester's role limit (spec §2).
CREATE OR REPLACE FUNCTION ipd_request_discount(p_clinic_id UUID, p_admission_id UUID, p_amount_paise INTEGER,
                                                p_reason TEXT, p_auto_approve BOOLEAN, p_idempotency_key TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; d ipd_discounts;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    SELECT * INTO d FROM ipd_discounts WHERE clinic_id = p_clinic_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF d.admission_id <> a.id THEN PERFORM ipd_raise('ipd_key_reused'); END IF;
        RETURN to_jsonb(d) || jsonb_build_object('replayed', true);
    END IF;
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id
                 AND status NOT IN ('draft', 'void')) THEN
        PERFORM ipd_raise('ipd_bill_issued');
    END IF;
    IF EXISTS (SELECT 1 FROM ipd_discounts WHERE clinic_id = p_clinic_id AND admission_id = a.id
                 AND status IN ('requested', 'approved')) THEN
        PERFORM ipd_raise('ipd_discount_exists');
    END IF;
    INSERT INTO ipd_discounts (clinic_id, admission_id, amount_paise, reason, status, auto_approved, requested_by,
                               requested_by_name, decided_by, decided_by_name, decided_at, idempotency_key)
    VALUES (p_clinic_id, a.id, p_amount_paise, btrim(p_reason),
            CASE WHEN p_auto_approve THEN 'approved' ELSE 'requested' END, p_auto_approve, p_actor_id,
            left(p_actor_name, 100),
            CASE WHEN p_auto_approve THEN p_actor_id END, CASE WHEN p_auto_approve THEN left(p_actor_name, 100) END,
            CASE WHEN p_auto_approve THEN now() END, p_idempotency_key)
    RETURNING * INTO d;
    IF p_auto_approve THEN PERFORM ipd_sync_bill_discount(p_clinic_id, a.id); END IF;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'discount_requested', p_actor_id, p_actor_name,
                    jsonb_build_object('discount_id', d.id, 'amount_paise', d.amount_paise, 'auto_approved', p_auto_approve));
    RETURN to_jsonb(d);
END $$;

CREATE OR REPLACE FUNCTION ipd_decide_discount(p_clinic_id UUID, p_discount_id UUID, p_decision TEXT, p_note TEXT,
                                               p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE d ipd_discounts;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO d FROM ipd_discounts WHERE id = p_discount_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_discount_not_found'); END IF;
    PERFORM ipd_lock_admission(p_clinic_id, d.admission_id);
    SELECT * INTO d FROM ipd_discounts WHERE id = p_discount_id AND clinic_id = p_clinic_id FOR UPDATE;
    IF p_decision NOT IN ('approved', 'rejected', 'cancelled') THEN PERFORM ipd_raise('ipd_bad_transition'); END IF;
    IF d.status = p_decision THEN RETURN to_jsonb(d) || jsonb_build_object('replayed', true); END IF;
    IF NOT (d.status = 'requested' OR (d.status = 'approved' AND p_decision = 'cancelled')) THEN
        PERFORM ipd_raise('ipd_bad_transition:' || d.status || '->' || p_decision);
    END IF;
    IF p_decision = 'approved' AND d.requested_by IS NOT DISTINCT FROM p_actor_id THEN
        PERFORM ipd_raise('ipd_discount_own_request');
    END IF;
    IF EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = d.admission_id
                 AND status NOT IN ('draft', 'void')) THEN
        PERFORM ipd_raise('ipd_bill_issued');
    END IF;
    UPDATE ipd_discounts SET status = p_decision, decided_by = p_actor_id, decided_by_name = left(p_actor_name, 100),
                             decided_at = now(), decision_note = NULLIF(left(btrim(COALESCE(p_note, '')), 300), '')
     WHERE id = d.id AND clinic_id = p_clinic_id RETURNING * INTO d;
    PERFORM ipd_sync_bill_discount(p_clinic_id, d.admission_id);
    PERFORM ipd_log(p_clinic_id, d.admission_id, NULL, 'discount_' || p_decision, p_actor_id, p_actor_name,
                    jsonb_build_object('discount_id', d.id, 'amount_paise', d.amount_paise));
    RETURN to_jsonb(d);
END $$;

-- ── 13. Credit authorisation ───────────────────────────────────────────────
CREATE OR REPLACE FUNCTION ipd_authorize_credit(p_clinic_id UUID, p_admission_id UUID, p_payer_name TEXT,
                                                p_reference TEXT, p_amount_paise INTEGER, p_notes TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; c ipd_credit_authorizations;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status NOT IN ('pending', 'admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF EXISTS (SELECT 1 FROM ipd_credit_authorizations WHERE clinic_id = p_clinic_id AND admission_id = a.id
                 AND status = 'active') THEN
        PERFORM ipd_raise('ipd_credit_exists');
    END IF;
    INSERT INTO ipd_credit_authorizations (clinic_id, admission_id, payer_name, reference, amount_paise, notes,
                                           approved_by, approved_by_name)
    VALUES (p_clinic_id, a.id, btrim(p_payer_name), btrim(p_reference), p_amount_paise, NULLIF(btrim(COALESCE(p_notes, '')), ''),
            p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO c;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'credit_authorized', p_actor_id, p_actor_name,
                    jsonb_build_object('credit_id', c.id, 'payer', c.payer_name, 'amount_paise', c.amount_paise));
    RETURN to_jsonb(c);
END $$;

CREATE OR REPLACE FUNCTION ipd_revoke_credit(p_clinic_id UUID, p_credit_id UUID, p_reason TEXT,
                                             p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE c ipd_credit_authorizations; a ipd_admissions;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    SELECT * INTO c FROM ipd_credit_authorizations WHERE id = p_credit_id AND clinic_id = p_clinic_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_credit_not_found'); END IF;
    a := ipd_lock_admission(p_clinic_id, c.admission_id);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM ipd_raise('ipd_reason_required'); END IF;
    -- a discharged stay's credit is what let it leave: it is settled by receipts, not revoked
    IF a.status = 'discharged' THEN PERFORM ipd_raise('ipd_record_locked:ipd_credit_authorizations'); END IF;
    UPDATE ipd_credit_authorizations SET status = 'revoked', revoked_at = now(), revoked_by = p_actor_id,
                                         revoke_reason = btrim(p_reason)
     WHERE id = c.id AND clinic_id = p_clinic_id AND status = 'active' RETURNING * INTO c;
    IF NOT FOUND THEN PERFORM ipd_raise('ipd_record_locked:ipd_credit_authorizations'); END IF;
    PERFORM ipd_log(p_clinic_id, c.admission_id, NULL, 'credit_revoked', p_actor_id, p_actor_name,
                    jsonb_build_object('credit_id', c.id, 'reason', p_reason));
    RETURN to_jsonb(c);
END $$;

-- ── 14. Discharge checklist (111 + discount decided, deposits cleared, credit) ─
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
    SELECT id, status, total_paise, paid_paise, invoice_number INTO v_inv FROM opd_invoices
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

-- ── 15. Shift close counts deposit cash (103 + deposits) ───────────────────
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
      INTO v_expected;
    RETURN QUERY
    UPDATE opd_cashier_shifts
       SET status = 'closed', closed_at = now(), expected_cash_paise = v_expected,
           declared_cash_paise = p_declared_paise, close_notes = p_notes
     WHERE id = p_shift_id AND clinic_id = p_clinic_id
    RETURNING *;
END $$;

-- ── 16. Owner purge (111 + the finance tables, which reference receipts) ───
-- Fix (pre-existing since 103): purging a clinic with a discounted draft bill
-- failed - deleting its lines recalculated the subtotal below the discount and
-- opd_invoices_discount_le_subtotal aborted the purge. Rows being purged need
-- no recalculation. Kept by the 116 rollback (it only removes a failure).
CREATE OR REPLACE FUNCTION opd_recalc_invoice_subtotal() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_invoice UUID := COALESCE(NEW.invoice_id, OLD.invoice_id);
        v_clinic  UUID := COALESCE(NEW.clinic_id, OLD.clinic_id);
BEGIN
    IF opd_purging(v_clinic) THEN RETURN NULL; END IF;
    UPDATE opd_invoices i
       SET subtotal_paise = (SELECT COALESCE(SUM(line_total_paise), 0) FROM opd_invoice_items
                              WHERE invoice_id = v_invoice AND clinic_id = v_clinic)
     WHERE i.id = v_invoice AND i.clinic_id = v_clinic AND i.status = 'draft';
    RETURN NULL;
END $$;
ALTER FUNCTION opd_recalc_invoice_subtotal() SET search_path = public, pg_temp;

CREATE OR REPLACE FUNCTION ipd_purge_clinic(p_clinic_id UUID)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.ipd_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    DELETE FROM ipd_deposits            WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('deposits', n);
    DELETE FROM ipd_discounts           WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_credit_authorizations WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_admission_packages  WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_med_administrations WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_orders              WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_observations        WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_notes               WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_charges             WHERE clinic_id = p_clinic_id;
    DELETE FROM ipd_packages            WHERE clinic_id = p_clinic_id;
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

-- ── 17. RLS parity + API-role lockout + search_path (103/111 pattern) ──────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ipd_deposits', 'ipd_packages', 'ipd_admission_packages', 'ipd_discounts',
                             'ipd_credit_authorizations']
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
        'ipd_deposit_balance(uuid,uuid)', 'ipd_bill_lines(uuid,uuid)', 'ipd_generate_final_bill(uuid,uuid,uuid,text)',
        'ipd_sync_bill_discount(uuid,uuid)', 'ipd_running_bill(uuid,uuid)',
        'ipd_apply_deposits(uuid,uuid,uuid,text)', 'ipd_unapply_deposits(uuid,uuid,text,uuid,text)',
        'ipd_apply_package(uuid,uuid,uuid,uuid,text)', 'ipd_remove_package(uuid,uuid,text,uuid,text)',
        'ipd_request_discount(uuid,uuid,integer,text,boolean,text,uuid,text)',
        'ipd_decide_discount(uuid,uuid,text,text,uuid,text)',
        'ipd_authorize_credit(uuid,uuid,text,text,integer,text,uuid,text)', 'ipd_revoke_credit(uuid,uuid,text,uuid,text)',
        'ipd_discharge_checklist(uuid,uuid)', 'opd_close_shift(uuid,uuid,uuid,integer,text)', 'ipd_purge_clinic(uuid)']
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
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND (p.proname LIKE 'ipd\_%' OR p.proname = 'opd_close_shift')
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 18. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename IN (
          'ipd_deposits', 'ipd_packages', 'ipd_admission_packages', 'ipd_discounts', 'ipd_credit_authorizations')) <> 5 THEN
        RAISE EXCEPTION '116 verify: finance tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_opd_receipts_deposit', 'trg_ipd_deposits_before', 'trg_ipd_deposits_append_only', 'trg_ipd_discounts_guard',
          'trg_ipd_admission_packages_guard', 'trg_ipd_credit_guard', 'trg_opd_invoices_ipd_issue')) <> 7 THEN
        RAISE EXCEPTION '116 verify: triggers missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'opd_receipts_mode_check'
                     AND pg_get_constraintdef(oid) LIKE '%deposit%') THEN
        RAISE EXCEPTION '116 verify: receipt modes not widened';
    END IF;
    IF to_regprocedure('ipd_running_bill(uuid,uuid)') IS NULL OR to_regprocedure('ipd_apply_deposits(uuid,uuid,uuid,text)') IS NULL
       OR to_regprocedure('ipd_request_discount(uuid,uuid,integer,text,boolean,text,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '116 verify: functions missing';
    END IF;
END $$;
