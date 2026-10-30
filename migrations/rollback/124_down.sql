-- ============================================================================
-- Rollback 124: Insurance / TPA OS (G6)
-- DESTROYS every pre-authorisation, claim and payer-ledger entry. Refused while
-- any claim or pre-authorisation exists: they are financial records tied to
-- issued tax invoices. Take pg_dump -t insurance_payers -t patient_insurance_policies
-- -t ipd_pre_authorizations -t ipd_insurance_claims -t ipd_claim_transactions FIRST
-- and use the owner purge. Refused too while any 'insurance' / 'tds' receipt or
-- 'insurance_disallowance' credit note exists (the narrowed CHECKs would reject them).
-- G5's ipd_apply_deposits / ipd_running_bill / ipd_discharge_checklist are put back
-- to their 117 definitions by re-running those CREATE OR REPLACE blocks from
-- 117_gst_billing_and_credit_notes.sql after this file.
-- The normal rollback is a code revert: pre-124 code runs unchanged on 124.
-- ============================================================================

DO $$ BEGIN
    IF (to_regclass('public.ipd_insurance_claims') IS NOT NULL AND EXISTS (SELECT 1 FROM ipd_insurance_claims))
       OR (to_regclass('public.ipd_pre_authorizations') IS NOT NULL AND EXISTS (SELECT 1 FROM ipd_pre_authorizations)) THEN
        RAISE EXCEPTION '124_down: claims / pre-authorisations exist (financial records); export them and use the owner purge first';
    END IF;
    IF EXISTS (SELECT 1 FROM opd_receipts WHERE mode IN ('insurance', 'tds'))
       OR EXISTS (SELECT 1 FROM opd_credit_notes WHERE reason = 'insurance_disallowance') THEN
        RAISE EXCEPTION '124_down: insurance receipts / credit notes exist';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_opd_invoices_insurance_void ON opd_invoices;
DROP TRIGGER IF EXISTS trg_opd_receipts_insurance ON opd_receipts;
DROP TABLE IF EXISTS ipd_claim_transactions;
DROP TABLE IF EXISTS ipd_insurance_claims;
DROP TABLE IF EXISTS ipd_pre_authorizations;
DROP TABLE IF EXISTS patient_insurance_policies;
DROP TABLE IF EXISTS insurance_payers;
DROP FUNCTION IF EXISTS ins_claim_transition(uuid, uuid, text, text, uuid, text);
DROP FUNCTION IF EXISTS ins_resolve_disallowance(uuid, uuid, text, text, uuid, text);
DROP FUNCTION IF EXISTS ins_record_settlement(uuid, uuid, jsonb, text, uuid, text);
DROP FUNCTION IF EXISTS ins_file_claim(uuid, uuid, integer, integer, uuid, text);
DROP FUNCTION IF EXISTS ins_pre_auth_transition(uuid, uuid, text, jsonb, uuid, text);
DROP FUNCTION IF EXISTS ins_create_pre_auth(uuid, uuid, jsonb, uuid, text);
DROP FUNCTION IF EXISTS insurance_seed_payers(uuid);
DROP FUNCTION IF EXISTS ins_guard_invoice_void();
DROP FUNCTION IF EXISTS ins_guard_record();
DROP FUNCTION IF EXISTS ins_guard_receipt();

-- G5 functions call ins_payer_hold: give them a pre-124 equivalent before dropping it
CREATE OR REPLACE FUNCTION ins_payer_hold(p_clinic_id UUID, p_admission_id UUID) RETURNS INTEGER
LANGUAGE sql STABLE AS $$ SELECT 0 $$;

ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_mode_check;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_mode_check
    CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online', 'bank_transfer', 'deposit'));
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_reference_required;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_reference_required
    CHECK (mode NOT IN ('upi', 'card', 'bank_transfer') OR reference IS NOT NULL);
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_counter_needs_shift;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_counter_needs_shift
    CHECK (mode IN ('payment_link', 'prepaid_online', 'deposit') OR shift_id IS NOT NULL);
ALTER TABLE opd_credit_notes DROP CONSTRAINT IF EXISTS opd_credit_notes_reason_check;
ALTER TABLE opd_credit_notes ADD  CONSTRAINT opd_credit_notes_reason_check
    CHECK (reason IN ('cancellation', 'discount_post_issue', 'billing_error', 'service_deficiency', 'other'));
-- ins_in_tx and ins_payer_hold stay (the stub returns 0): the G5 functions redefined
-- by 124 reference ins_payer_hold until 117's definitions are re-run.
-- opd_counters 'pre:YYYY' / 'clm:YYYY' keys are left in clinics.opd_counters.
