-- ============================================================================
-- Rollback 116: IPD finance
-- DESTROYS the deposit ledger, packages, discount decisions and credit
-- authorisations. Take pg_dump -t 'ipd_deposits' -t 'ipd_packages'
-- -t 'ipd_admission_packages' -t 'ipd_discounts' -t 'ipd_credit_authorizations' FIRST.
-- Refused while money or records exist that pre-116 constraints cannot hold:
-- receipts in the new modes, package charges, or any finance row.
-- The normal rollback is a code revert: pre-116 code runs unchanged on 116.
-- opd_recalc_invoice_subtotal keeps its 116 purge fix (it only removes a failure).
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM opd_receipts WHERE mode IN ('deposit', 'bank_transfer')) THEN
        RAISE EXCEPTION '116_down: receipts in mode deposit/bank_transfer exist';
    END IF;
    IF EXISTS (SELECT 1 FROM ipd_charges WHERE source = 'package') THEN
        RAISE EXCEPTION '116_down: package charges exist';
    END IF;
    IF EXISTS (SELECT 1 FROM ipd_deposits) OR EXISTS (SELECT 1 FROM ipd_discounts)
       OR EXISTS (SELECT 1 FROM ipd_credit_authorizations) OR EXISTS (SELECT 1 FROM ipd_admission_packages) THEN
        RAISE EXCEPTION '116_down: finance records exist - export and purge them first';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_opd_receipts_deposit ON opd_receipts;
DROP TRIGGER IF EXISTS trg_opd_invoices_ipd_issue ON opd_invoices;

DROP FUNCTION IF EXISTS ipd_running_bill(uuid, uuid);
DROP FUNCTION IF EXISTS ipd_apply_deposits(uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS ipd_unapply_deposits(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS ipd_apply_package(uuid, uuid, uuid, uuid, text);
DROP FUNCTION IF EXISTS ipd_remove_package(uuid, uuid, text, uuid, text);
DROP FUNCTION IF EXISTS ipd_request_discount(uuid, uuid, integer, text, boolean, text, uuid, text);
DROP FUNCTION IF EXISTS ipd_decide_discount(uuid, uuid, text, text, uuid, text);
DROP FUNCTION IF EXISTS ipd_authorize_credit(uuid, uuid, text, text, integer, text, uuid, text);
DROP FUNCTION IF EXISTS ipd_revoke_credit(uuid, uuid, text, uuid, text);

-- pre-116 definitions, copied verbatim from 111 / 103
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

DROP FUNCTION IF EXISTS ipd_bill_lines(uuid, uuid);
DROP FUNCTION IF EXISTS ipd_sync_bill_discount(uuid, uuid);

DROP TABLE IF EXISTS ipd_deposits, ipd_admission_packages, ipd_discounts, ipd_credit_authorizations, ipd_packages CASCADE;
DROP FUNCTION IF EXISTS ipd_deposit_balance(uuid, uuid);
DROP FUNCTION IF EXISTS ipd_deposit_before_insert();
DROP FUNCTION IF EXISTS ipd_guard_finance_record();
DROP FUNCTION IF EXISTS ipd_guard_bill_issue();
DROP FUNCTION IF EXISTS ipd_guard_deposit_receipt();

ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_mode_check;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_mode_check
    CHECK (mode IN ('cash', 'upi', 'card', 'payment_link', 'prepaid_online'));
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_reference_required;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_reference_required
    CHECK (mode NOT IN ('upi', 'card') OR reference IS NOT NULL);
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_counter_needs_shift;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_counter_needs_shift
    CHECK (mode IN ('payment_link', 'prepaid_online') OR shift_id IS NOT NULL);
ALTER TABLE opd_receipts DROP CONSTRAINT IF EXISTS opd_receipts_refund_manual;
ALTER TABLE opd_receipts ADD  CONSTRAINT opd_receipts_refund_manual
    CHECK (kind = 'payment' OR (mode IN ('cash', 'upi') AND reason IS NOT NULL));
ALTER TABLE ipd_charges DROP CONSTRAINT IF EXISTS ipd_charges_source_check;
ALTER TABLE ipd_charges ADD  CONSTRAINT ipd_charges_source_check CHECK (source IN ('manual', 'bed_day', 'order'));

ALTER FUNCTION ipd_generate_final_bill(uuid, uuid, uuid, text) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_discharge_checklist(uuid, uuid) SET search_path = public, pg_temp;
ALTER FUNCTION ipd_purge_clinic(uuid) SET search_path = public, pg_temp;
ALTER FUNCTION opd_close_shift(uuid, uuid, uuid, integer, text) SET search_path = public, pg_temp;
