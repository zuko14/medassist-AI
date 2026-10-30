-- ============================================================================
-- Migration 124: Insurance / TPA OS (G6) - payers, policies, cashless
-- pre-authorisation, dual-debtor claims, settlement with deductions
-- ============================================================================
-- insurance_payers             the clinic's TPA / insurer / scheme master (12 seeded)
-- patient_insurance_policies   a patient's card: payer, policy, member id, validity
-- ipd_pre_authorizations       PRE-YYYY-NNNNN, one live per stay:
--     draft -> submitted -> query_raised -> submitted ... -> approved
--     approved -> enhancement_requested -> approved (enhanced or denied)
--     submitted / query_raised -> rejected;   any live state -> cancelled
--     total_approved_paise = the latest approval letter, never above what was requested
-- ipd_insurance_claims         CLM-YYYY-NNNNN, the issued final bill split between two debtors:
--     total_bill_paise = patient_payable_paise + claimed_amount_paise (payer share)
--     claimed = LEAST(total approved, bill - non-payable - co-pay)
-- ipd_claim_transactions       append-only payer ledger per claim: payment (UTR),
--     tds, disallowance (reason code; resolved once: written_off via a GST credit
--     note, or recovered_from_patient). Never deleted.
--
-- Accounting (deliberate deviation from the spec, which put only the patient's
-- share on the bill): the final bill stays the ONE GST tax invoice for every
-- service rendered (G4) - the split lives on the claim. The payer's NEFT and its
-- TDS are opd_receipts (modes 'insurance' / 'tds', no cashier shift, written by
-- the functions here only); a written-off disallowance is a G4 credit note
-- ('insurance_disallowance'). So the invoice, GST, receivables (G5) and the
-- day book always agree, and a payer short-payment is visible, not lost.
--
-- What the patient owes = invoice due - payer hold, where payer hold = payer
-- outstanding + disallowances not yet resolved (ins_payer_hold). G5's deposit
-- application, running bill and discharge checklist are redefined to subtract
-- it: deposits never pay the insurer's share, and a stay can leave once the
-- patient's own portion is paid.
--
-- Other deviations: numbering by opd_next_counter (not MAX+1, which hands two
-- concurrent filings one number); tenant-composite FKs; the pre-auth carries the
-- stay's patient (ON UPDATE CASCADE) so a G7 identity merge moves it; a bill
-- with a live claim cannot be voided (withdraw the claim first); every state
-- change is one function and every row is append-only or function-only.
-- Additive and idempotent. Rollback: migrations/rollback/124_down.sql.
-- ============================================================================

-- ── 0. Shared helpers ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION ins_in_tx(p_clinic_id UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT COALESCE(current_setting('kriya.insurance_tx', true), '') = p_clinic_id::TEXT
$$;

-- ── 1. Wider enums on existing tables ──────────────────────────────────────
-- the payer's NEFT and the TDS it withheld settle the bill without a cashier shift
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_mode_check;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_mode_check
    CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online', 'bank_transfer', 'deposit',
                    'insurance', 'tds'));
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_reference_required;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_reference_required
    CHECK (mode NOT IN ('upi', 'card', 'bank_transfer', 'insurance', 'tds') OR reference IS NOT NULL);
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_counter_needs_shift;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_counter_needs_shift
    CHECK (mode IN ('payment_link', 'prepaid_online', 'deposit', 'insurance', 'tds') OR shift_id IS NOT NULL);

CREATE OR REPLACE FUNCTION ins_guard_receipt() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_adm UUID; v_due INTEGER; v_hold INTEGER;
BEGIN
    IF NEW.mode IN ('insurance', 'tds') THEN
        IF NEW.kind <> 'payment' OR NOT ins_in_tx(NEW.clinic_id) THEN
            RAISE EXCEPTION 'ins_receipt_via_function_only' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    -- the counter (and deposits) collect only the patient's share of a cashless bill
    IF NEW.kind = 'payment' THEN
        SELECT admission_id, total_paise - credited_paise - paid_paise INTO v_adm, v_due
          FROM opd_invoices WHERE id = NEW.invoice_id AND clinic_id = NEW.clinic_id;
        IF v_adm IS NOT NULL THEN
            v_hold := ins_payer_hold(NEW.clinic_id, v_adm);
            IF v_hold > 0 AND NEW.amount_paise > v_due - v_hold THEN
                RAISE EXCEPTION 'ins_receipt_exceeds_patient_share:%', GREATEST(v_due - v_hold, 0) USING ERRCODE = 'P0001';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_opd_receipts_insurance ON opd_receipts;
CREATE TRIGGER trg_opd_receipts_insurance BEFORE INSERT ON opd_receipts
    FOR EACH ROW EXECUTE FUNCTION ins_guard_receipt();

ALTER TABLE opd_credit_notes DROP CONSTRAINT IF EXISTS opd_credit_notes_reason_check;
ALTER TABLE opd_credit_notes ADD  CONSTRAINT opd_credit_notes_reason_check
    CHECK (reason IN ('cancellation', 'discount_post_issue', 'billing_error', 'service_deficiency', 'other',
                      'insurance_disallowance'));

-- ── 2. Payers / TPAs ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS insurance_payers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id     UUID NULL,          -- NULL = every branch
    name          TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 150),
    payer_type    TEXT NOT NULL CHECK (payer_type IN ('tpa', 'private_insurance', 'government_scheme', 'corporate')),
    tpa_code      TEXT NULL CHECK (char_length(tpa_code) <= 50),
    portal_url    TEXT NULL CHECK (portal_url ~* '^https?://[^\s<>"'']+$' AND char_length(portal_url) <= 300),
    contact_email TEXT NULL CHECK (contact_email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' AND char_length(contact_email) <= 120),
    contact_phone TEXT NULL CHECK (contact_phone ~ '^[0-9+() -]{6,30}$'),
    is_active     BOOLEAN NOT NULL DEFAULT true,
    is_seeded     BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT insurance_payers_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT insurance_payers_clinic_name_key UNIQUE (clinic_id, name),
    CONSTRAINT insurance_payers_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_insurance_payers_clinic ON insurance_payers (clinic_id, payer_type, is_active);
DROP TRIGGER IF EXISTS trg_insurance_payers_touch ON insurance_payers;
CREATE TRIGGER trg_insurance_payers_touch BEFORE UPDATE ON insurance_payers
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- Popular Indian TPAs, insurers and schemes; idempotent (an edited or renamed row is left alone).
CREATE OR REPLACE FUNCTION insurance_seed_payers(p_clinic_id UUID) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE n INTEGER;
BEGIN
    INSERT INTO insurance_payers (clinic_id, name, payer_type, tpa_code, portal_url, is_seeded)
    SELECT p_clinic_id, x.name, x.payer_type, x.code, x.url, true
      FROM (VALUES
        ('Medi Assist TPA', 'tpa', 'MEDIASSIST', 'https://www.mediassist.in'),
        ('Vidal Health TPA', 'tpa', 'VIDAL', 'https://www.vidalhealthtpa.com'),
        ('Paramount TPA', 'tpa', 'PARAMOUNT', 'https://www.paramounttpa.com'),
        ('MDIndia Health TPA', 'tpa', 'MDINDIA', 'https://www.mdindiaonline.com'),
        ('Heritage Health TPA', 'tpa', 'HERITAGE', 'https://www.heritagehealthtpa.com'),
        ('Star Health Insurance', 'private_insurance', 'STARHEALTH', 'https://www.starhealth.in'),
        ('Care Health Insurance', 'private_insurance', 'CAREHEALTH', 'https://www.careinsurance.com'),
        ('HDFC ERGO', 'private_insurance', 'HDFCERGO', 'https://www.hdfcergo.com'),
        ('ICICI Lombard', 'private_insurance', 'ICICILOMBARD', 'https://www.icicilombard.com'),
        ('Niva Bupa', 'private_insurance', 'NIVABUPA', 'https://www.nivabupa.com'),
        ('Ayushman Bharat (PM-JAY)', 'government_scheme', 'PMJAY', 'https://pmjay.gov.in'),
        ('CGHS', 'government_scheme', 'CGHS', 'https://cghs.gov.in')) AS x(name, payer_type, code, url)
    ON CONFLICT (clinic_id, name) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- ── 3. Patient policies ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patient_insurance_policies (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id        UUID NOT NULL,
    family_member_id  UUID NULL,      -- the insured person when it is a family member of the account
    payer_id          UUID NOT NULL,
    policy_number     TEXT NOT NULL CHECK (char_length(btrim(policy_number)) BETWEEN 1 AND 80),
    member_id         TEXT NOT NULL CHECK (char_length(btrim(member_id)) BETWEEN 1 AND 80),   -- TPA card / ABHA / scheme id
    corporate_name    TEXT NULL CHECK (char_length(corporate_name) <= 120),
    sum_insured_paise BIGINT NULL CHECK (sum_insured_paise BETWEEN 0 AND 100000000000),
    valid_from        DATE NULL,
    valid_to          DATE NULL,
    is_active         BOOLEAN NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patient_insurance_policies_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT patient_insurance_policies_policy_key UNIQUE (clinic_id, patient_id, payer_id, policy_number),
    CONSTRAINT patient_insurance_policies_patient_fk FOREIGN KEY (clinic_id, patient_id) REFERENCES patients (clinic_id, id),
    CONSTRAINT patient_insurance_policies_family_fk FOREIGN KEY (clinic_id, family_member_id)
        REFERENCES family_members (clinic_id, id),
    CONSTRAINT patient_insurance_policies_payer_fk FOREIGN KEY (clinic_id, payer_id) REFERENCES insurance_payers (clinic_id, id),
    CONSTRAINT patient_insurance_policies_validity CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS idx_patient_policies_patient ON patient_insurance_policies (clinic_id, patient_id, is_active);
DROP TRIGGER IF EXISTS trg_patient_insurance_policies_touch ON patient_insurance_policies;
CREATE TRIGGER trg_patient_insurance_policies_touch BEFORE UPDATE ON patient_insurance_policies
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();

-- ── 4. Cashless pre-authorisation ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ipd_pre_authorizations (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id                UUID NULL,
    admission_id             UUID NOT NULL,
    patient_id               UUID NOT NULL,
    policy_id                UUID NOT NULL,
    payer_id                 UUID NOT NULL,
    pre_auth_year            SMALLINT NOT NULL,
    pre_auth_seq             INTEGER NOT NULL CHECK (pre_auth_seq > 0),
    pre_auth_number          TEXT GENERATED ALWAYS AS (opd_format_number('PRE-', pre_auth_year, pre_auth_seq, 5)) STORED,
    provisional_diagnosis    TEXT NOT NULL CHECK (char_length(btrim(provisional_diagnosis)) BETWEEN 3 AND 500),
    treatment_plan           TEXT NULL CHECK (char_length(treatment_plan) <= 1000),
    expected_stay_days       SMALLINT NULL CHECK (expected_stay_days BETWEEN 1 AND 365),
    initial_requested_paise  INTEGER NOT NULL CHECK (initial_requested_paise BETWEEN 1 AND 100000000),
    initial_approved_paise   INTEGER NOT NULL DEFAULT 0 CHECK (initial_approved_paise >= 0),
    enhanced_requested_paise INTEGER NOT NULL DEFAULT 0 CHECK (enhanced_requested_paise BETWEEN 0 AND 100000000),
    enhanced_approved_paise  INTEGER NOT NULL DEFAULT 0 CHECK (enhanced_approved_paise >= 0),
    total_approved_paise     INTEGER GENERATED ALWAYS AS (initial_approved_paise + enhanced_approved_paise) STORED,
    status                   TEXT NOT NULL DEFAULT 'submitted' CHECK (status IN (
                                 'draft', 'submitted', 'query_raised', 'approved', 'enhancement_requested',
                                 'rejected', 'cancelled')),
    approval_ref_number      TEXT NULL CHECK (char_length(approval_ref_number) <= 100),
    query_details            TEXT NULL CHECK (char_length(query_details) <= 1000),
    denial_reason            TEXT NULL CHECK (char_length(denial_reason) <= 500),
    cancel_reason            TEXT NULL CHECK (char_length(cancel_reason) <= 300),
    approval_letter_path     TEXT NULL CHECK (char_length(approval_letter_path) <= 300),
    notes                    TEXT NULL CHECK (char_length(notes) <= 1000),
    submitted_at             TIMESTAMPTZ NULL,
    decided_at               TIMESTAMPTZ NULL,
    decided_by_name          TEXT NULL CHECK (char_length(decided_by_name) <= 100),
    created_by               UUID NULL,
    created_by_name          TEXT NOT NULL CHECK (char_length(btrim(created_by_name)) BETWEEN 1 AND 100),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_pre_authorizations_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_pre_authorizations_seq_key UNIQUE (clinic_id, pre_auth_year, pre_auth_seq),
    CONSTRAINT ipd_pre_authorizations_admission_fk FOREIGN KEY (clinic_id, admission_id, patient_id)
        REFERENCES ipd_admissions (clinic_id, id, patient_id) ON UPDATE CASCADE ON DELETE CASCADE,
    CONSTRAINT ipd_pre_authorizations_policy_fk FOREIGN KEY (clinic_id, policy_id)
        REFERENCES patient_insurance_policies (clinic_id, id),
    CONSTRAINT ipd_pre_authorizations_payer_fk FOREIGN KEY (clinic_id, payer_id) REFERENCES insurance_payers (clinic_id, id),
    CONSTRAINT ipd_pre_authorizations_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    -- capped by what was asked for; the approved state names its letter
    CONSTRAINT ipd_pre_authorizations_cap CHECK (
        initial_approved_paise + enhanced_approved_paise <= initial_requested_paise + enhanced_requested_paise),
    CONSTRAINT ipd_pre_authorizations_approved_shape CHECK (
        status NOT IN ('approved', 'enhancement_requested')
        OR (initial_approved_paise > 0 AND approval_ref_number IS NOT NULL AND decided_at IS NOT NULL)),
    CONSTRAINT ipd_pre_authorizations_query_shape CHECK (status <> 'query_raised' OR query_details IS NOT NULL),
    CONSTRAINT ipd_pre_authorizations_rejected_shape CHECK (status <> 'rejected' OR denial_reason IS NOT NULL),
    CONSTRAINT ipd_pre_authorizations_cancel_shape CHECK (status <> 'cancelled' OR cancel_reason IS NOT NULL),
    CONSTRAINT ipd_pre_authorizations_submitted_shape CHECK (status = 'draft' OR status = 'cancelled' OR submitted_at IS NOT NULL)
);
-- one live pre-authorisation per stay
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_pre_auth_live ON ipd_pre_authorizations (clinic_id, admission_id)
    WHERE status NOT IN ('rejected', 'cancelled');
CREATE INDEX IF NOT EXISTS idx_pre_auth_admission ON ipd_pre_authorizations (clinic_id, admission_id, status);
CREATE INDEX IF NOT EXISTS idx_pre_auth_pipeline ON ipd_pre_authorizations (clinic_id, status, updated_at);

-- ── 5. Claims (the bill split between two debtors) ─────────────────────────
CREATE TABLE IF NOT EXISTS ipd_insurance_claims (
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    branch_id                  UUID NULL,
    admission_id               UUID NOT NULL,
    pre_auth_id                UUID NOT NULL,
    payer_id                   UUID NOT NULL,
    invoice_id                 UUID NOT NULL,
    claim_year                 SMALLINT NOT NULL,
    claim_seq                  INTEGER NOT NULL CHECK (claim_seq > 0),
    claim_number               TEXT GENERATED ALWAYS AS (opd_format_number('CLM-', claim_year, claim_seq, 5)) STORED,
    total_bill_paise           INTEGER NOT NULL CHECK (total_bill_paise > 0),
    non_payable_paise          INTEGER NOT NULL DEFAULT 0 CHECK (non_payable_paise >= 0),   -- consumables, room-rent excess
    copay_paise                INTEGER NOT NULL DEFAULT 0 CHECK (copay_paise >= 0),
    patient_payable_paise      INTEGER NOT NULL CHECK (patient_payable_paise >= 0),
    claimed_amount_paise       INTEGER NOT NULL CHECK (claimed_amount_paise > 0),           -- payer share
    approved_at_filing_paise   INTEGER NOT NULL CHECK (approved_at_filing_paise > 0),
    settled_amount_paise       INTEGER NOT NULL DEFAULT 0 CHECK (settled_amount_paise >= 0),    -- received by NEFT
    tds_deducted_paise         INTEGER NOT NULL DEFAULT 0 CHECK (tds_deducted_paise >= 0),
    disallowed_amount_paise    INTEGER NOT NULL DEFAULT 0 CHECK (disallowed_amount_paise >= 0),
    written_off_paise          INTEGER NOT NULL DEFAULT 0 CHECK (written_off_paise >= 0),
    recovered_from_patient_paise INTEGER NOT NULL DEFAULT 0 CHECK (recovered_from_patient_paise >= 0),
    outstanding_paise          INTEGER GENERATED ALWAYS AS (
                                   claimed_amount_paise - settled_amount_paise - tds_deducted_paise
                                   - disallowed_amount_paise) STORED,
    settlement_utr_number      TEXT NULL CHECK (char_length(settlement_utr_number) <= 100),   -- latest UTR
    disallowance_reason        TEXT NULL CHECK (char_length(disallowance_reason) <= 300),     -- latest
    query_details              TEXT NULL CHECK (char_length(query_details) <= 1000),
    withdraw_reason            TEXT NULL CHECK (char_length(withdraw_reason) <= 300),
    status                     TEXT NOT NULL DEFAULT 'filed' CHECK (status IN (
                                   'filed', 'in_process', 'query_raised', 'settled', 'partially_settled',
                                   'repudiated', 'withdrawn')),
    filed_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    filed_by                   UUID NULL,
    filed_by_name              TEXT NOT NULL CHECK (char_length(btrim(filed_by_name)) BETWEEN 1 AND 100),
    settled_at                 TIMESTAMPTZ NULL,
    settled_by                 UUID NULL,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_insurance_claims_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_insurance_claims_seq_key UNIQUE (clinic_id, claim_year, claim_seq),
    CONSTRAINT ipd_insurance_claims_admission_fk FOREIGN KEY (clinic_id, admission_id)
        REFERENCES ipd_admissions (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT ipd_insurance_claims_pre_auth_fk FOREIGN KEY (clinic_id, pre_auth_id)
        REFERENCES ipd_pre_authorizations (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT ipd_insurance_claims_payer_fk FOREIGN KEY (clinic_id, payer_id) REFERENCES insurance_payers (clinic_id, id),
    CONSTRAINT ipd_insurance_claims_invoice_fk FOREIGN KEY (clinic_id, invoice_id)
        REFERENCES opd_invoices (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT ipd_insurance_claims_branch_fk FOREIGN KEY (clinic_id, branch_id) REFERENCES branches (clinic_id, id),
    -- the two debtors share the bill exactly; the payer never owes more than approved
    CONSTRAINT ipd_insurance_claims_bill_split_valid CHECK (patient_payable_paise + claimed_amount_paise = total_bill_paise),
    CONSTRAINT ipd_insurance_claims_within_approval CHECK (claimed_amount_paise <= approved_at_filing_paise),
    CONSTRAINT ipd_insurance_claims_ledger_balances CHECK (
        settled_amount_paise + tds_deducted_paise + disallowed_amount_paise <= claimed_amount_paise
        AND written_off_paise + recovered_from_patient_paise <= disallowed_amount_paise),
    CONSTRAINT ipd_insurance_claims_closed_shape CHECK (
        status NOT IN ('settled', 'partially_settled', 'repudiated')
        OR (settled_amount_paise + tds_deducted_paise + disallowed_amount_paise = claimed_amount_paise
            AND settled_at IS NOT NULL)),
    CONSTRAINT ipd_insurance_claims_withdrawn_shape CHECK (
        status <> 'withdrawn' OR (withdraw_reason IS NOT NULL
                                  AND settled_amount_paise + tds_deducted_paise + disallowed_amount_paise = 0))
);
-- one live claim per stay and per bill
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_claims_live ON ipd_insurance_claims (clinic_id, admission_id)
    WHERE status <> 'withdrawn';
CREATE INDEX IF NOT EXISTS idx_insurance_claims_status ON ipd_insurance_claims (clinic_id, status);
CREATE INDEX IF NOT EXISTS idx_insurance_claims_invoice ON ipd_insurance_claims (clinic_id, invoice_id);

-- ── 6. Payer ledger: payments, TDS, disallowances (append-only) ────────────
CREATE TABLE IF NOT EXISTS ipd_claim_transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    claim_id        UUID NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('payment', 'tds', 'disallowance')),
    amount_paise    INTEGER NOT NULL CHECK (amount_paise BETWEEN 1 AND 100000000),
    utr_number      TEXT NULL CHECK (char_length(btrim(utr_number)) BETWEEN 4 AND 100),
    reason_code     TEXT NULL CHECK (reason_code IN (
                        'tds', 'non_payable_item', 'room_rent_proportionate', 'copay', 'policy_limit',
                        'excess_over_tariff', 'documentation_incomplete', 'not_medically_necessary',
                        'repudiated', 'other')),
    note            TEXT NULL CHECK (char_length(note) <= 300),
    settled_on      DATE NULL,
    receipt_id      UUID NULL,      -- opd_receipts row (payment / tds); no FK: the owner purge clears receipts first
    resolution      TEXT NULL CHECK (resolution IN ('pending', 'written_off', 'recovered_from_patient')),
    credit_note_id  UUID NULL,      -- the G4 credit note that wrote it off
    resolved_at     TIMESTAMPTZ NULL,
    resolved_by     UUID NULL,
    resolved_by_name TEXT NULL CHECK (char_length(resolved_by_name) <= 100),
    resolution_note TEXT NULL CHECK (char_length(resolution_note) <= 300),
    idempotency_key TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 90),
    recorded_by     UUID NULL,
    recorded_by_name TEXT NOT NULL CHECK (char_length(btrim(recorded_by_name)) BETWEEN 1 AND 100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ipd_claim_transactions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ipd_claim_transactions_claim_fk FOREIGN KEY (clinic_id, claim_id)
        REFERENCES ipd_insurance_claims (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT ipd_claim_transactions_shape CHECK (
        (kind = 'payment' AND utr_number IS NOT NULL AND reason_code IS NULL AND resolution IS NULL)
     OR (kind = 'tds' AND reason_code = 'tds' AND resolution IS NULL)
     OR (kind = 'disallowance' AND reason_code IS NOT NULL AND reason_code <> 'tds' AND resolution IS NOT NULL)),
    CONSTRAINT ipd_claim_transactions_other_needs_note CHECK (
        reason_code IS DISTINCT FROM 'other' OR char_length(btrim(COALESCE(note, ''))) >= 3),
    CONSTRAINT ipd_claim_transactions_resolved_shape CHECK (
        (resolution IS DISTINCT FROM 'written_off' OR credit_note_id IS NOT NULL)
        AND ((resolution IN ('written_off', 'recovered_from_patient')) = (resolved_at IS NOT NULL)))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ipd_claim_txn_idempotency
    ON ipd_claim_transactions (clinic_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ipd_claim_txn_claim ON ipd_claim_transactions (clinic_id, claim_id, created_at);

-- ── 7. Guards: function-only writes, ledger append-only ────────────────────
CREATE OR REPLACE FUNCTION ins_guard_record() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    -- 121: an emergency identity merge may move a pre-authorisation to the real patient, nothing else
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'ipd_pre_authorizations'
       AND patient_merge_allows(TG_RELID, to_jsonb(OLD), to_jsonb(NEW)) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF ipd_purging(OLD.clinic_id) OR opd_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'ins_append_only:%', TG_TABLE_NAME USING ERRCODE = 'P0001';
    END IF;
    IF NOT ins_in_tx(COALESCE(NEW.clinic_id, OLD.clinic_id)) THEN
        RAISE EXCEPTION 'ins_via_function_only:%', TG_TABLE_NAME USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'ipd_claim_transactions' THEN
        -- a disallowance is resolved once; amounts, kind and reason never change
        IF OLD.resolution IS DISTINCT FROM 'pending' OR NEW.resolution NOT IN ('written_off', 'recovered_from_patient')
           OR (to_jsonb(NEW) - ARRAY['resolution', 'credit_note_id', 'resolved_at', 'resolved_by', 'resolved_by_name',
                                     'resolution_note'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['resolution', 'credit_note_id', 'resolved_at', 'resolved_by',
                                                      'resolved_by_name', 'resolution_note']) THEN
            RAISE EXCEPTION 'ins_record_locked:ipd_claim_transactions' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'ipd_insurance_claims' THEN
        -- the split is fixed at filing; generated outstanding_paise / claim_number are NULL in NEW
        IF (NEW.admission_id, NEW.invoice_id, NEW.pre_auth_id, NEW.total_bill_paise, NEW.patient_payable_paise,
            NEW.claimed_amount_paise, NEW.claim_year, NEW.claim_seq)
           IS DISTINCT FROM (OLD.admission_id, OLD.invoice_id, OLD.pre_auth_id, OLD.total_bill_paise,
                             OLD.patient_payable_paise, OLD.claimed_amount_paise, OLD.claim_year, OLD.claim_seq)
           OR OLD.status = 'withdrawn' THEN
            RAISE EXCEPTION 'ins_record_locked:ipd_insurance_claims' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS trg_ipd_pre_auth_guard ON ipd_pre_authorizations;
CREATE TRIGGER trg_ipd_pre_auth_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_pre_authorizations
    FOR EACH ROW EXECUTE FUNCTION ins_guard_record();
DROP TRIGGER IF EXISTS trg_ipd_pre_auth_touch ON ipd_pre_authorizations;
CREATE TRIGGER trg_ipd_pre_auth_touch BEFORE UPDATE ON ipd_pre_authorizations
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_ipd_claims_guard ON ipd_insurance_claims;
CREATE TRIGGER trg_ipd_claims_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_insurance_claims
    FOR EACH ROW EXECUTE FUNCTION ins_guard_record();
DROP TRIGGER IF EXISTS trg_ipd_claims_touch ON ipd_insurance_claims;
CREATE TRIGGER trg_ipd_claims_touch BEFORE UPDATE ON ipd_insurance_claims
    FOR EACH ROW EXECUTE FUNCTION opd_touch_updated_at();
DROP TRIGGER IF EXISTS trg_ipd_claim_txn_guard ON ipd_claim_transactions;
CREATE TRIGGER trg_ipd_claim_txn_guard BEFORE INSERT OR UPDATE OR DELETE ON ipd_claim_transactions
    FOR EACH ROW EXECUTE FUNCTION ins_guard_record();

-- A bill with a live claim cannot be voided: the payer has (or will have) it. Withdraw the claim first.
CREATE OR REPLACE FUNCTION ins_guard_invoice_void() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'void' AND OLD.status <> 'void' AND EXISTS (
        SELECT 1 FROM ipd_insurance_claims WHERE clinic_id = NEW.clinic_id AND invoice_id = NEW.id
           AND status <> 'withdrawn') THEN
        RAISE EXCEPTION 'ins_claim_open_on_bill' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_opd_invoices_insurance_void ON opd_invoices;
CREATE TRIGGER trg_opd_invoices_insurance_void BEFORE UPDATE OF status ON opd_invoices
    FOR EACH ROW EXECUTE FUNCTION ins_guard_invoice_void();

-- ── 8. What the payer still holds on a stay's bill ─────────────────────────
-- With a claim: payer outstanding + disallowances nobody has resolved yet.
-- Before the claim, an approved pre-authorisation holds its approved amount
-- (provisionally): the bill's issue applies deposits, and they must not pay
-- the insurer's share before the claim fixes the exact split. The patient owes
-- the rest of the invoice's due; 0 when the stay is not cashless.
CREATE OR REPLACE FUNCTION ins_payer_hold(p_clinic_id UUID, p_admission_id UUID) RETURNS INTEGER
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        (SELECT c.outstanding_paise + c.disallowed_amount_paise - c.written_off_paise - c.recovered_from_patient_paise
           FROM ipd_insurance_claims c
          WHERE c.clinic_id = p_clinic_id AND c.admission_id = p_admission_id AND c.status <> 'withdrawn'),
        (SELECT pa.total_approved_paise FROM ipd_pre_authorizations pa
          WHERE pa.clinic_id = p_clinic_id AND pa.admission_id = p_admission_id
            AND pa.status IN ('approved', 'enhancement_requested')),
        0)
$$;

-- ── 9. Pre-authorisation functions ─────────────────────────────────────────
-- p_row: {policy_id, requested_paise, provisional_diagnosis, treatment_plan, expected_stay_days, notes, as_draft}
CREATE OR REPLACE FUNCTION ins_create_pre_auth(p_clinic_id UUID, p_admission_id UUID, p_row JSONB,
                                               p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; pol patient_insurance_policies; pay insurance_payers; r ipd_pre_authorizations;
        v_year SMALLINT; v_draft BOOLEAN := COALESCE((p_row ->> 'as_draft')::BOOLEAN, false);
        v_day DATE;
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    IF a.status NOT IN ('pending', 'admitted', 'discharge_pending') THEN PERFORM ipd_raise('ipd_admission_not_active'); END IF;
    IF EXISTS (SELECT 1 FROM ipd_pre_authorizations WHERE clinic_id = p_clinic_id AND admission_id = a.id
                 AND status NOT IN ('rejected', 'cancelled')) THEN
        PERFORM ipd_raise('ins_pre_auth_exists');
    END IF;
    SELECT * INTO pol FROM patient_insurance_policies
     WHERE clinic_id = p_clinic_id AND id = NULLIF(p_row ->> 'policy_id', '')::UUID;
    IF NOT FOUND THEN PERFORM ipd_raise('ins_policy_not_found'); END IF;
    IF pol.patient_id <> a.patient_id
       OR (pol.family_member_id IS NOT NULL AND pol.family_member_id IS DISTINCT FROM a.family_member_id) THEN
        PERFORM ipd_raise('ins_policy_other_patient');
    END IF;
    v_day := ipd_ist_date(COALESCE(a.admitted_at, now()));
    IF NOT pol.is_active OR (pol.valid_from IS NOT NULL AND v_day < pol.valid_from)
       OR (pol.valid_to IS NOT NULL AND v_day > pol.valid_to) THEN
        PERFORM ipd_raise('ins_policy_not_valid');
    END IF;
    SELECT * INTO pay FROM insurance_payers WHERE clinic_id = p_clinic_id AND id = pol.payer_id;
    IF NOT pay.is_active THEN PERFORM ipd_raise('ins_payer_inactive'); END IF;
    v_year := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
    INSERT INTO ipd_pre_authorizations (clinic_id, branch_id, admission_id, patient_id, policy_id, payer_id,
                                        pre_auth_year, pre_auth_seq, provisional_diagnosis, treatment_plan,
                                        expected_stay_days, initial_requested_paise, status, notes, submitted_at,
                                        created_by, created_by_name)
    VALUES (p_clinic_id, a.branch_id, a.id, a.patient_id, pol.id, pay.id, v_year,
            opd_next_counter(p_clinic_id, 'pre:' || v_year), btrim(p_row ->> 'provisional_diagnosis'),
            NULLIF(btrim(COALESCE(p_row ->> 'treatment_plan', '')), ''), NULLIF(p_row ->> 'expected_stay_days', '')::SMALLINT,
            (p_row ->> 'requested_paise')::INTEGER, CASE WHEN v_draft THEN 'draft' ELSE 'submitted' END,
            NULLIF(btrim(COALESCE(p_row ->> 'notes', '')), ''), CASE WHEN v_draft THEN NULL ELSE now() END,
            p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO r;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'preauth_' || r.status, p_actor_id, p_actor_name,
                    jsonb_build_object('pre_auth_id', r.id, 'number', r.pre_auth_number, 'payer', pay.name,
                                       'requested_paise', r.initial_requested_paise));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(r);
END $$;

-- p_action: submit | query | approve | reject | request_enhancement | deny_enhancement | cancel
-- p_row: {amount_paise (approve: the TOTAL approved per the latest letter; request_enhancement: extra requested),
--         approval_ref, approval_letter_path, note}
CREATE OR REPLACE FUNCTION ins_pre_auth_transition(p_clinic_id UUID, p_pre_auth_id UUID, p_action TEXT, p_row JSONB,
                                                   p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE r ipd_pre_authorizations; v_from TEXT; v_amount INTEGER := NULLIF(p_row ->> 'amount_paise', '')::INTEGER;
        v_note TEXT := NULLIF(btrim(COALESCE(p_row ->> 'note', '')), ''); v_to TEXT;
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    SELECT * INTO r FROM ipd_pre_authorizations WHERE clinic_id = p_clinic_id AND id = p_pre_auth_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ins_pre_auth_not_found'); END IF;
    PERFORM ipd_lock_admission(p_clinic_id, r.admission_id);
    SELECT * INTO r FROM ipd_pre_authorizations WHERE clinic_id = p_clinic_id AND id = p_pre_auth_id FOR UPDATE;
    v_from := r.status;
    IF EXISTS (SELECT 1 FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND pre_auth_id = r.id
                 AND status <> 'withdrawn') THEN
        PERFORM ipd_raise('ins_pre_auth_claimed');
    END IF;
    v_to := CASE p_action
        WHEN 'submit' THEN CASE WHEN v_from IN ('draft', 'query_raised') THEN 'submitted' END
        WHEN 'query' THEN CASE WHEN v_from = 'submitted' THEN 'query_raised' END
        WHEN 'approve' THEN CASE WHEN v_from IN ('submitted', 'query_raised', 'enhancement_requested') THEN 'approved' END
        WHEN 'reject' THEN CASE WHEN v_from IN ('submitted', 'query_raised') THEN 'rejected' END
        WHEN 'request_enhancement' THEN CASE WHEN v_from = 'approved' THEN 'enhancement_requested' END
        WHEN 'deny_enhancement' THEN CASE WHEN v_from = 'enhancement_requested' THEN 'approved' END
        WHEN 'cancel' THEN CASE WHEN v_from NOT IN ('rejected', 'cancelled') THEN 'cancelled' END
        END;
    IF v_to IS NULL THEN PERFORM ipd_raise('ins_bad_transition:' || v_from || ':' || COALESCE(p_action, '')); END IF;

    IF p_action = 'submit' THEN
        UPDATE ipd_pre_authorizations SET status = 'submitted', submitted_at = COALESCE(submitted_at, now()),
               notes = COALESCE(left(v_note, 1000), notes)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    ELSIF p_action = 'query' THEN
        IF v_note IS NULL THEN PERFORM ipd_raise('ins_note_required'); END IF;
        UPDATE ipd_pre_authorizations SET status = 'query_raised', query_details = left(v_note, 1000)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    ELSIF p_action = 'approve' THEN
        IF NULLIF(btrim(COALESCE(p_row ->> 'approval_ref', '')), '') IS NULL THEN PERFORM ipd_raise('ins_approval_ref_required'); END IF;
        IF v_amount IS NULL OR v_amount <= 0 THEN PERFORM ipd_raise('ins_amount_required'); END IF;
        IF v_amount > r.initial_requested_paise + r.enhanced_requested_paise THEN
            PERFORM ipd_raise('ins_approval_exceeds_request');
        END IF;
        IF v_from = 'enhancement_requested' THEN
            -- an enhancement letter states the new total; it never lowers what was already approved
            IF v_amount < r.total_approved_paise THEN PERFORM ipd_raise('ins_enhancement_below_approved'); END IF;
            UPDATE ipd_pre_authorizations SET status = 'approved', enhanced_approved_paise = v_amount - r.initial_approved_paise,
                   approval_ref_number = left(btrim(p_row ->> 'approval_ref'), 100),
                   approval_letter_path = COALESCE(NULLIF(btrim(COALESCE(p_row ->> 'approval_letter_path', '')), ''), approval_letter_path),
                   decided_at = now(), decided_by_name = left(p_actor_name, 100)
             WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
        ELSE
            UPDATE ipd_pre_authorizations SET status = 'approved', initial_approved_paise = v_amount,
                   approval_ref_number = left(btrim(p_row ->> 'approval_ref'), 100),
                   approval_letter_path = NULLIF(btrim(COALESCE(p_row ->> 'approval_letter_path', '')), ''),
                   decided_at = now(), decided_by_name = left(p_actor_name, 100)
             WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
        END IF;
    ELSIF p_action = 'reject' THEN
        IF v_note IS NULL THEN PERFORM ipd_raise('ins_note_required'); END IF;
        UPDATE ipd_pre_authorizations SET status = 'rejected', denial_reason = left(v_note, 500), decided_at = now(),
               decided_by_name = left(p_actor_name, 100)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    ELSIF p_action = 'request_enhancement' THEN
        IF v_amount IS NULL OR v_amount <= 0 THEN PERFORM ipd_raise('ins_amount_required'); END IF;
        IF r.enhanced_requested_paise + v_amount > 100000000 THEN PERFORM ipd_raise('ins_amount_too_large'); END IF;
        UPDATE ipd_pre_authorizations SET status = 'enhancement_requested',
               enhanced_requested_paise = enhanced_requested_paise + v_amount,
               notes = COALESCE(left(v_note, 1000), notes)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    ELSIF p_action = 'deny_enhancement' THEN
        IF v_note IS NULL THEN PERFORM ipd_raise('ins_note_required'); END IF;
        UPDATE ipd_pre_authorizations SET status = 'approved', notes = left(v_note, 1000)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    ELSE   -- cancel
        IF v_note IS NULL OR char_length(v_note) < 3 THEN PERFORM ipd_raise('ins_note_required'); END IF;
        UPDATE ipd_pre_authorizations SET status = 'cancelled', cancel_reason = left(v_note, 300)
         WHERE clinic_id = p_clinic_id AND id = r.id RETURNING * INTO r;
    END IF;
    PERFORM ipd_log(p_clinic_id, r.admission_id, NULL, 'preauth_' || p_action, p_actor_id, p_actor_name,
                    jsonb_build_object('pre_auth_id', r.id, 'number', r.pre_auth_number, 'from', v_from, 'to', r.status,
                                       'amount_paise', v_amount, 'total_approved_paise', r.total_approved_paise,
                                       'approval_ref', r.approval_ref_number, 'note', v_note));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(r) || jsonb_build_object('from', v_from);
END $$;

-- ── 10. File the claim on the issued final bill (the dual split) ───────────
CREATE OR REPLACE FUNCTION ins_file_claim(p_clinic_id UUID, p_admission_id UUID, p_non_payable_paise INTEGER,
                                          p_copay_paise INTEGER, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; pa ipd_pre_authorizations; inv opd_invoices; c ipd_insurance_claims;
        v_total INTEGER; v_claim INTEGER; v_year SMALLINT;
        v_np INTEGER := COALESCE(p_non_payable_paise, 0); v_cp INTEGER := COALESCE(p_copay_paise, 0);
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'withdrawn';
    IF FOUND THEN RETURN to_jsonb(c) || jsonb_build_object('replayed', true); END IF;
    SELECT * INTO pa FROM ipd_pre_authorizations WHERE clinic_id = p_clinic_id AND admission_id = a.id
       AND status NOT IN ('rejected', 'cancelled') FOR UPDATE;
    IF NOT FOUND THEN PERFORM ipd_raise('ins_no_pre_auth'); END IF;
    IF pa.status = 'enhancement_requested' THEN PERFORM ipd_raise('ins_enhancement_pending'); END IF;
    IF pa.status <> 'approved' THEN PERFORM ipd_raise('ins_pre_auth_not_approved:' || pa.status); END IF;
    SELECT * INTO inv FROM opd_invoices WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status <> 'void' FOR UPDATE;
    IF NOT FOUND OR inv.status = 'draft' THEN PERFORM ipd_raise('ins_bill_not_issued'); END IF;
    IF v_np < 0 OR v_cp < 0 THEN PERFORM ipd_raise('ins_amount_required'); END IF;
    v_total := inv.total_paise - inv.credited_paise;
    IF v_np + v_cp >= v_total THEN PERFORM ipd_raise('ins_nothing_to_claim'); END IF;
    v_claim := LEAST(pa.total_approved_paise, v_total - v_np - v_cp);
    -- the patient's own payments must fit their share: the payer is never asked for money already paid
    IF inv.paid_paise > v_total - v_claim THEN PERFORM ipd_raise('ins_patient_overpaid:' || (inv.paid_paise - (v_total - v_claim))); END IF;
    v_year := EXTRACT(YEAR FROM (now() AT TIME ZONE 'Asia/Kolkata'))::SMALLINT;
    INSERT INTO ipd_insurance_claims (clinic_id, branch_id, admission_id, pre_auth_id, payer_id, invoice_id, claim_year,
                                      claim_seq, total_bill_paise, non_payable_paise, copay_paise, patient_payable_paise,
                                      claimed_amount_paise, approved_at_filing_paise, filed_by, filed_by_name)
    VALUES (p_clinic_id, a.branch_id, a.id, pa.id, pa.payer_id, inv.id, v_year,
            opd_next_counter(p_clinic_id, 'clm:' || v_year), v_total, v_np, v_cp, v_total - v_claim, v_claim,
            pa.total_approved_paise, p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO c;
    PERFORM ipd_log(p_clinic_id, a.id, NULL, 'claim_filed', p_actor_id, p_actor_name,
                    jsonb_build_object('claim_id', c.id, 'number', c.claim_number, 'invoice_id', inv.id,
                                       'total_bill_paise', v_total, 'claimed_paise', v_claim,
                                       'patient_payable_paise', v_total - v_claim));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(c) || jsonb_build_object('replayed', false);
END $$;

-- ── 11. Settlement: NEFT receipt, TDS, disallowances in one transaction ────
-- p_row: {amount_received_paise, utr_number, tds_paise, settled_on,
--         disallowances: [{amount_paise, reason_code, note}]}
CREATE OR REPLACE FUNCTION ins_record_settlement(p_clinic_id UUID, p_claim_id UUID, p_row JSONB,
                                                 p_idempotency_key TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE c ipd_insurance_claims; t ipd_claim_transactions; v_rct UUID; d JSONB; n INTEGER := 0;
        v_recv INTEGER := COALESCE(NULLIF(p_row ->> 'amount_received_paise', '')::INTEGER, 0);
        v_tds INTEGER := COALESCE(NULLIF(p_row ->> 'tds_paise', '')::INTEGER, 0);
        v_dis INTEGER := 0; v_utr TEXT := NULLIF(btrim(COALESCE(p_row ->> 'utr_number', '')), '');
        v_on DATE := COALESCE(NULLIF(p_row ->> 'settled_on', '')::DATE, (now() AT TIME ZONE 'Asia/Kolkata')::DATE);
        v_status TEXT; v_last_reason TEXT;
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND id = p_claim_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ins_claim_not_found'); END IF;
    PERFORM ipd_lock_admission(p_clinic_id, c.admission_id);
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND id = p_claim_id FOR UPDATE;
    IF p_idempotency_key IS NOT NULL THEN
        SELECT * INTO t FROM ipd_claim_transactions WHERE clinic_id = p_clinic_id AND idempotency_key LIKE p_idempotency_key || ':%'
         LIMIT 1;
        IF FOUND THEN
            IF t.claim_id <> c.id THEN PERFORM ipd_raise('ins_key_reused'); END IF;
            RETURN to_jsonb(c) || jsonb_build_object('replayed', true);
        END IF;
    END IF;
    IF c.status IN ('settled', 'partially_settled', 'repudiated', 'withdrawn') THEN PERFORM ipd_raise('ins_claim_closed'); END IF;
    IF v_recv < 0 OR v_tds < 0 THEN PERFORM ipd_raise('ins_amount_required'); END IF;
    IF v_recv > 0 AND v_utr IS NULL THEN PERFORM ipd_raise('ins_utr_required'); END IF;
    IF v_on > (now() AT TIME ZONE 'Asia/Kolkata')::DATE THEN PERFORM ipd_raise('ins_future_date'); END IF;
    FOR d IN SELECT value FROM jsonb_array_elements(COALESCE(p_row -> 'disallowances', '[]'::jsonb)) LOOP
        IF COALESCE((d ->> 'amount_paise')::INTEGER, 0) <= 0 OR COALESCE(d ->> 'reason_code', '') IN ('', 'tds') THEN
            PERFORM ipd_raise('ins_disallowance_reason_required');
        END IF;
        v_dis := v_dis + (d ->> 'amount_paise')::INTEGER;
    END LOOP;
    IF v_recv + v_tds + v_dis = 0 THEN PERFORM ipd_raise('ins_amount_required'); END IF;
    IF v_recv + v_tds + v_dis > c.outstanding_paise THEN PERFORM ipd_raise('ins_exceeds_outstanding:' || c.outstanding_paise); END IF;

    IF v_recv > 0 THEN
        INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, reference, received_by_admin_id,
                                  received_by_name, reason)
        VALUES (p_clinic_id, c.invoice_id, 'payment', 'insurance', v_recv, left('UTR ' || v_utr, 100), p_actor_id,
                left(p_actor_name, 100), left('Insurance settlement ' || c.claim_number, 300))
        RETURNING id INTO v_rct;
        INSERT INTO ipd_claim_transactions (clinic_id, claim_id, kind, amount_paise, utr_number, settled_on, receipt_id,
                                            idempotency_key, recorded_by, recorded_by_name)
        VALUES (p_clinic_id, c.id, 'payment', v_recv, v_utr, v_on, v_rct,
                CASE WHEN p_idempotency_key IS NOT NULL THEN p_idempotency_key || ':pay' END, p_actor_id, left(p_actor_name, 100));
    END IF;
    IF v_tds > 0 THEN
        INSERT INTO opd_receipts (clinic_id, invoice_id, kind, mode, amount_paise, reference, received_by_admin_id,
                                  received_by_name, reason)
        VALUES (p_clinic_id, c.invoice_id, 'payment', 'tds', v_tds, left('TDS ' || COALESCE(v_utr, c.claim_number), 100),
                p_actor_id, left(p_actor_name, 100), left('TDS withheld by payer on ' || c.claim_number, 300))
        RETURNING id INTO v_rct;
        INSERT INTO ipd_claim_transactions (clinic_id, claim_id, kind, amount_paise, utr_number, reason_code, settled_on,
                                            receipt_id, idempotency_key, recorded_by, recorded_by_name)
        VALUES (p_clinic_id, c.id, 'tds', v_tds, v_utr, 'tds', v_on, v_rct,
                CASE WHEN p_idempotency_key IS NOT NULL THEN p_idempotency_key || ':tds' END, p_actor_id, left(p_actor_name, 100));
    END IF;
    FOR d IN SELECT value FROM jsonb_array_elements(COALESCE(p_row -> 'disallowances', '[]'::jsonb)) LOOP
        n := n + 1;
        INSERT INTO ipd_claim_transactions (clinic_id, claim_id, kind, amount_paise, utr_number, reason_code, note,
                                            settled_on, resolution, idempotency_key, recorded_by, recorded_by_name)
        VALUES (p_clinic_id, c.id, 'disallowance', (d ->> 'amount_paise')::INTEGER, v_utr, d ->> 'reason_code',
                NULLIF(left(btrim(COALESCE(d ->> 'note', '')), 300), ''), v_on, 'pending',
                CASE WHEN p_idempotency_key IS NOT NULL THEN p_idempotency_key || ':dis' || n END, p_actor_id,
                left(p_actor_name, 100));
        v_last_reason := left(d ->> 'reason_code' || COALESCE(': ' || NULLIF(btrim(COALESCE(d ->> 'note', '')), ''), ''), 300);
    END LOOP;

    v_status := CASE
        WHEN c.outstanding_paise - v_recv - v_tds - v_dis > 0 THEN 'in_process'
        WHEN c.settled_amount_paise + v_recv + c.tds_deducted_paise + v_tds = 0 THEN 'repudiated'
        WHEN c.disallowed_amount_paise + v_dis > 0 THEN 'partially_settled'
        ELSE 'settled' END;
    UPDATE ipd_insurance_claims SET settled_amount_paise = settled_amount_paise + v_recv,
           tds_deducted_paise = tds_deducted_paise + v_tds, disallowed_amount_paise = disallowed_amount_paise + v_dis,
           settlement_utr_number = COALESCE(v_utr, settlement_utr_number),
           disallowance_reason = COALESCE(v_last_reason, disallowance_reason), status = v_status,
           settled_at = CASE WHEN v_status IN ('settled', 'partially_settled', 'repudiated') THEN now() END,
           settled_by = CASE WHEN v_status IN ('settled', 'partially_settled', 'repudiated') THEN p_actor_id END
     WHERE clinic_id = p_clinic_id AND id = c.id RETURNING * INTO c;
    PERFORM ipd_log(p_clinic_id, c.admission_id, NULL, 'claim_settlement', p_actor_id, p_actor_name,
                    jsonb_build_object('claim_id', c.id, 'number', c.claim_number, 'received_paise', v_recv,
                                       'tds_paise', v_tds, 'disallowed_paise', v_dis, 'utr', v_utr, 'status', v_status));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(c) || jsonb_build_object('replayed', false);
END $$;

-- ── 12. Resolve a disallowance: write off (credit note) or recover from the patient ─
CREATE OR REPLACE FUNCTION ins_resolve_disallowance(p_clinic_id UUID, p_txn_id UUID, p_resolution TEXT, p_note TEXT,
                                                    p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE t ipd_claim_transactions; c ipd_insurance_claims; v_cn JSONB; v_cn_id UUID;
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    SELECT * INTO t FROM ipd_claim_transactions WHERE clinic_id = p_clinic_id AND id = p_txn_id;
    IF NOT FOUND OR t.kind <> 'disallowance' THEN PERFORM ipd_raise('ins_disallowance_not_found'); END IF;
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND id = t.claim_id;
    PERFORM ipd_lock_admission(p_clinic_id, c.admission_id);
    SELECT * INTO t FROM ipd_claim_transactions WHERE clinic_id = p_clinic_id AND id = p_txn_id FOR UPDATE;
    IF t.resolution = p_resolution THEN RETURN to_jsonb(t) || jsonb_build_object('replayed', true); END IF;
    IF t.resolution <> 'pending' THEN PERFORM ipd_raise('ins_record_locked:ipd_claim_transactions'); END IF;
    IF p_resolution NOT IN ('written_off', 'recovered_from_patient') THEN PERFORM ipd_raise('ins_bad_transition'); END IF;
    IF char_length(btrim(COALESCE(p_note, ''))) < 3 THEN PERFORM ipd_raise('ins_note_required'); END IF;
    IF p_resolution = 'written_off' THEN
        -- the G4 credit note reduces the tax invoice (and its GST) by exactly the disallowed amount
        v_cn := opd_issue_credit_note(p_clinic_id, c.invoice_id, t.amount_paise, 'insurance_disallowance',
                                      left(c.claim_number || ' ' || t.reason_code || ': ' || btrim(p_note), 300),
                                      'ins-wo-' || t.id, NULL, NULL, NULL, p_actor_id, p_actor_name);
        v_cn_id := (v_cn ->> 'id')::UUID;
    END IF;
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    UPDATE ipd_claim_transactions SET resolution = p_resolution, credit_note_id = v_cn_id, resolved_at = now(),
           resolved_by = p_actor_id, resolved_by_name = left(p_actor_name, 100), resolution_note = left(btrim(p_note), 300)
     WHERE clinic_id = p_clinic_id AND id = t.id RETURNING * INTO t;
    UPDATE ipd_insurance_claims
       SET written_off_paise = written_off_paise + CASE WHEN p_resolution = 'written_off' THEN t.amount_paise ELSE 0 END,
           recovered_from_patient_paise = recovered_from_patient_paise
                                          + CASE WHEN p_resolution = 'recovered_from_patient' THEN t.amount_paise ELSE 0 END
     WHERE clinic_id = p_clinic_id AND id = c.id;
    -- ipd_events.event_type is at most 40 characters
    PERFORM ipd_log(p_clinic_id, c.admission_id, NULL, 'claim_dis_' || p_resolution, p_actor_id, p_actor_name,
                    jsonb_build_object('claim_id', c.id, 'txn_id', t.id, 'amount_paise', t.amount_paise,
                                       'credit_note', v_cn ->> 'credit_note_number'));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(t) || jsonb_build_object('replayed', false, 'credit_note', v_cn);
END $$;

-- ── 13. Claim query / in-process / withdraw ────────────────────────────────
-- p_action: query (note required) | in_process | withdraw (reason; only before any settlement)
CREATE OR REPLACE FUNCTION ins_claim_transition(p_clinic_id UUID, p_claim_id UUID, p_action TEXT, p_note TEXT,
                                                p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE c ipd_insurance_claims; v_from TEXT; v_note TEXT := NULLIF(btrim(COALESCE(p_note, '')), '');
BEGIN
    PERFORM set_config('kriya.insurance_tx', p_clinic_id::TEXT, true);
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND id = p_claim_id;
    IF NOT FOUND THEN PERFORM ipd_raise('ins_claim_not_found'); END IF;
    PERFORM ipd_lock_admission(p_clinic_id, c.admission_id);
    SELECT * INTO c FROM ipd_insurance_claims WHERE clinic_id = p_clinic_id AND id = p_claim_id FOR UPDATE;
    v_from := c.status;
    IF v_from IN ('settled', 'partially_settled', 'repudiated', 'withdrawn') THEN PERFORM ipd_raise('ins_claim_closed'); END IF;
    IF p_action = 'query' THEN
        IF v_note IS NULL THEN PERFORM ipd_raise('ins_note_required'); END IF;
        UPDATE ipd_insurance_claims SET status = 'query_raised', query_details = left(v_note, 1000)
         WHERE clinic_id = p_clinic_id AND id = c.id RETURNING * INTO c;
    ELSIF p_action = 'in_process' THEN
        UPDATE ipd_insurance_claims SET status = 'in_process' WHERE clinic_id = p_clinic_id AND id = c.id RETURNING * INTO c;
    ELSIF p_action = 'withdraw' THEN
        IF v_note IS NULL OR char_length(v_note) < 3 THEN PERFORM ipd_raise('ins_note_required'); END IF;
        IF c.settled_amount_paise + c.tds_deducted_paise + c.disallowed_amount_paise > 0 THEN
            PERFORM ipd_raise('ins_claim_has_settlement');
        END IF;
        UPDATE ipd_insurance_claims SET status = 'withdrawn', withdraw_reason = left(v_note, 300)
         WHERE clinic_id = p_clinic_id AND id = c.id RETURNING * INTO c;
    ELSE
        PERFORM ipd_raise('ins_bad_transition:' || v_from || ':' || COALESCE(p_action, ''));
    END IF;
    PERFORM ipd_log(p_clinic_id, c.admission_id, NULL, 'claim_' || p_action, p_actor_id, p_actor_name,
                    jsonb_build_object('claim_id', c.id, 'number', c.claim_number, 'from', v_from, 'note', v_note));
    PERFORM set_config('kriya.insurance_tx', '', true);
    RETURN to_jsonb(c);
END $$;

-- ── 14. G5 redefinitions: the payer's share is not the patient's ───────────
-- 117 definition; deposits pay only what the patient owes (due - payer hold)
CREATE OR REPLACE FUNCTION ipd_apply_deposits(p_clinic_id UUID, p_admission_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE a ipd_admissions; v_inv opd_invoices; v_amount INTEGER; v_rct UUID;
BEGIN
    PERFORM set_config('kriya.ipd_tx', p_clinic_id::TEXT, true);
    a := ipd_lock_admission(p_clinic_id, p_admission_id);
    SELECT * INTO v_inv FROM opd_invoices
     WHERE clinic_id = p_clinic_id AND admission_id = a.id AND status IN ('issued', 'partially_paid') FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('applied_paise', 0); END IF;
    v_amount := LEAST(ipd_deposit_balance(p_clinic_id, a.id),
                      v_inv.total_paise - v_inv.credited_paise - v_inv.paid_paise - ins_payer_hold(p_clinic_id, a.id));
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

-- 117 definition + 'insurance': the approved cover before the bill, the payer hold after it
CREATE OR REPLACE FUNCTION ipd_running_bill(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_lines JSONB; v_charged BIGINT; v_covered BIGINT; v_inv RECORD; v_pkg JSONB;
        v_disc JSONB; v_pending JSONB; v_credit JSONB; v_dep JSONB; v_prop RECORD; v_approved INTEGER; v_net BIGINT;
        v_balance INTEGER; v_tax BIGINT; v_ins JSONB; v_cover INTEGER; v_hold INTEGER;
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
    SELECT jsonb_build_object('pre_auth_id', pa.id, 'pre_auth_number', pa.pre_auth_number, 'status', pa.status,
                              'payer_name', py.name, 'total_approved_paise', pa.total_approved_paise,
                              'claim_id', c.id, 'claim_number', c.claim_number, 'claim_status', c.status,
                              'claimed_paise', c.claimed_amount_paise, 'patient_payable_paise', c.patient_payable_paise),
           CASE WHEN pa.status IN ('approved', 'enhancement_requested') THEN pa.total_approved_paise ELSE 0 END
      INTO v_ins, v_cover
      FROM ipd_pre_authorizations pa
      JOIN insurance_payers py ON py.id = pa.payer_id AND py.clinic_id = pa.clinic_id
      LEFT JOIN ipd_insurance_claims c ON c.clinic_id = pa.clinic_id AND c.pre_auth_id = pa.id AND c.status <> 'withdrawn'
     WHERE pa.clinic_id = p_clinic_id AND pa.admission_id = a.id AND pa.status NOT IN ('rejected', 'cancelled');
    v_hold := ins_payer_hold(p_clinic_id, a.id);
    -- before the final bill: estimate (+ GST, - discount, - approved insurance cover); after: the bill less the payer's hold
    v_net := CASE WHEN v_inv.j IS NULL THEN GREATEST(v_charged + v_tax - COALESCE(v_approved, 0) - COALESCE(v_cover, 0), 0)
                  WHEN v_ins IS NOT NULL AND v_ins ->> 'claim_id' IS NULL
                      THEN GREATEST(v_inv.due_total - v_inv.paid_paise - COALESCE(v_cover, 0), 0)
                  ELSE GREATEST(v_inv.due_total - v_inv.paid_paise - v_hold, 0) END;
    RETURN jsonb_build_object(
        'admission_id', a.id, 'admission_number', a.admission_number, 'status', a.status,
        'as_of', now(), 'lines', v_lines, 'charged_paise', v_charged, 'covered_value_paise', v_covered,
        'tax_paise', v_tax,
        'proposed', jsonb_build_object('count', v_prop.n, 'total_paise', v_prop.total),
        'package', v_pkg, 'discount', v_disc, 'pending_discount', v_pending, 'credit', v_credit,
        'insurance', CASE WHEN v_ins IS NULL THEN NULL ELSE v_ins || jsonb_build_object('payer_hold_paise', v_hold) END,
        'deposits', v_dep || jsonb_build_object('balance_paise', v_balance),
        'bill', v_inv.j,
        'payable_paise', v_net,
        'due_after_deposits_paise', GREATEST(v_net - v_balance, 0),
        'refundable_paise', GREATEST(v_balance - v_net, 0));
END $$;

-- 117 definition; the bill is settled for discharge when the patient's own share is paid
CREATE OR REPLACE FUNCTION ipd_discharge_checklist(p_clinic_id UUID, p_admission_id UUID)
RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE a ipd_admissions; v_items JSONB := '[]'::jsonb; v_inv RECORD; v_n INTEGER; v_credit INTEGER;
        v_balance INTEGER; v_due INTEGER; v_hold INTEGER;
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
    v_hold := ins_payer_hold(p_clinic_id, a.id);
    v_items := v_items || jsonb_build_object('key', 'bill_settled', 'waivable', true,
        'done', v_inv.id IS NOT NULL AND v_inv.status <> 'draft'
                AND (v_inv.status = 'paid' OR (v_credit IS NOT NULL AND v_due <= v_credit)
                     OR (v_hold > 0 AND v_due <= v_hold)),
        'balance_paise', v_due, 'credit_paise', v_credit, 'insurance_hold_paise', v_hold);
    RETURN jsonb_build_object('admission_id', a.id, 'status', a.status, 'items', v_items,
        'ready', NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                              WHERE NOT (i ->> 'done')::BOOLEAN AND NOT (i ->> 'waivable')::BOOLEAN),
        'needs_waiver', EXISTS (SELECT 1 FROM jsonb_array_elements(v_items) i
                                 WHERE NOT (i ->> 'done')::BOOLEAN AND (i ->> 'waivable')::BOOLEAN));
END $$;

-- ── 15. Seed every existing clinic ─────────────────────────────────────────
SELECT insurance_seed_payers(id) FROM clinics;

-- ── 16. Function hygiene + API-role lockout ────────────────────────────────
ALTER FUNCTION ins_in_tx(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ins_guard_receipt() SET search_path = public, pg_temp;
ALTER FUNCTION ins_guard_record() SET search_path = public, pg_temp;
ALTER FUNCTION ins_guard_invoice_void() SET search_path = public, pg_temp;
ALTER FUNCTION ins_payer_hold(uuid, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION insurance_seed_payers(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ins_create_pre_auth(uuid, uuid, jsonb, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ins_pre_auth_transition(uuid, uuid, text, jsonb, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ins_file_claim(uuid, uuid, integer, integer, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ins_record_settlement(uuid, uuid, jsonb, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ins_resolve_disallowance(uuid, uuid, text, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ins_claim_transition(uuid, uuid, text, text, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_apply_deposits(uuid, uuid, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_running_bill(uuid, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_discharge_checklist(uuid, uuid) SET search_path = public, pg_temp;

DO $$
DECLARE f TEXT;
BEGIN
    FOREACH f IN ARRAY ARRAY[
        'insurance_seed_payers(uuid)', 'ins_payer_hold(uuid,uuid)',
        'ins_create_pre_auth(uuid,uuid,jsonb,uuid,text)', 'ins_pre_auth_transition(uuid,uuid,text,jsonb,uuid,text)',
        'ins_file_claim(uuid,uuid,integer,integer,uuid,text)', 'ins_record_settlement(uuid,uuid,jsonb,text,uuid,text)',
        'ins_resolve_disallowance(uuid,uuid,text,text,uuid,text)', 'ins_claim_transition(uuid,uuid,text,text,uuid,text)']
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

-- RLS parity (defense in depth); the service_role connection bypasses it and
-- the application's clinic predicate is the boundary (app/tenancy.py).
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['insurance_payers', 'patient_insurance_policies', 'ipd_pre_authorizations',
                             'ipd_insurance_claims', 'ipd_claim_transactions'] LOOP
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

-- ── 17. Verify (fails the transaction if anything is missing) ──────────────
DO $$
BEGIN
    IF to_regclass('public.insurance_payers') IS NULL OR to_regclass('public.patient_insurance_policies') IS NULL
       OR to_regclass('public.ipd_pre_authorizations') IS NULL OR to_regclass('public.ipd_insurance_claims') IS NULL
       OR to_regclass('public.ipd_claim_transactions') IS NULL THEN
        RAISE EXCEPTION '124 verify: insurance tables missing';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conname IN (
          'ipd_pre_authorizations_admission_fk', 'ipd_pre_authorizations_cap', 'ipd_insurance_claims_bill_split_valid',
          'ipd_insurance_claims_within_approval', 'ipd_insurance_claims_ledger_balances', 'ipd_insurance_claims_invoice_fk',
          'ipd_claim_transactions_shape', 'patient_insurance_policies_payer_fk')) <> 8 THEN
        RAISE EXCEPTION '124 verify: insurance constraints missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN (
          'trg_ipd_pre_auth_guard', 'trg_ipd_claims_guard', 'trg_ipd_claim_txn_guard', 'trg_opd_invoices_insurance_void',
          'trg_opd_receipts_insurance')) <> 5 THEN
        RAISE EXCEPTION '124 verify: insurance triggers missing';
    END IF;
    IF to_regprocedure('ins_record_settlement(uuid,uuid,jsonb,text,uuid,text)') IS NULL
       OR to_regprocedure('ins_file_claim(uuid,uuid,integer,integer,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '124 verify: insurance functions missing';
    END IF;
END $$;
