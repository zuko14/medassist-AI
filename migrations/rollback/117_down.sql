-- ============================================================================
-- Rollback 117: GST billing, credit notes, room-rent GST
-- DESTROYS the credit-note register and every line's GST breakdown. Take
-- pg_dump -t opd_credit_notes -t opd_invoice_items -t opd_invoices -t ipd_charges FIRST.
-- Refused while data exists that pre-117 code cannot represent: any credit
-- note, any taxed invoice line, or any taxed IPD charge.
-- The normal rollback is a code revert: pre-117 code runs unchanged on 117
-- (a clinic without a GSTIN never produces tax).
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM opd_credit_notes) OR EXISTS (SELECT 1 FROM opd_invoices WHERE credited_paise <> 0) THEN
        RAISE EXCEPTION '117_down: credit notes exist';
    END IF;
    IF EXISTS (SELECT 1 FROM opd_invoice_items WHERE total_tax_paise <> 0 OR tax_rate_bps <> 0)
       OR EXISTS (SELECT 1 FROM ipd_charges WHERE tax_rate_bps <> 0) THEN
        RAISE EXCEPTION '117_down: taxed bill lines or charges exist';
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_opd_inv_items_tax ON opd_invoice_items;
DROP TRIGGER IF EXISTS trg_ipd_charge_room_gst ON ipd_charges;
DROP TABLE IF EXISTS opd_credit_notes;
DROP FUNCTION IF EXISTS opd_issue_credit_note(uuid, uuid, integer, text, text, text, text, text, uuid, uuid, text);
DROP FUNCTION IF EXISTS opd_guard_credit_note_insert();
DROP FUNCTION IF EXISTS opd_invoice_item_tax();
DROP FUNCTION IF EXISTS ipd_apply_room_rent_gst();

ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_paid_consistent;
ALTER TABLE opd_invoices ADD  CONSTRAINT opd_invoices_paid_consistent CHECK (
    (status IN ('draft', 'issued', 'void') AND paid_paise = 0)
 OR (status = 'partially_paid' AND paid_paise > 0 AND paid_paise < subtotal_paise - discount_paise)
 OR (status = 'paid' AND paid_paise = subtotal_paise - discount_paise));
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_credit_le_total;
ALTER TABLE opd_invoices DROP CONSTRAINT IF EXISTS opd_invoices_tax_le_total;
ALTER TABLE opd_invoices
    DROP COLUMN IF EXISTS is_tax_invoice, DROP COLUMN IF EXISTS clinic_gstin, DROP COLUMN IF EXISTS patient_gstin,
    DROP COLUMN IF EXISTS total_tax_paise, DROP COLUMN IF EXISTS credited_paise;
ALTER TABLE opd_invoice_items DROP CONSTRAINT IF EXISTS opd_invoice_items_tax_consistent;
ALTER TABLE opd_invoice_items
    DROP COLUMN IF EXISTS total_tax_paise, DROP COLUMN IF EXISTS hsn_sac, DROP COLUMN IF EXISTS tax_rate_bps,
    DROP COLUMN IF EXISTS cgst_paise, DROP COLUMN IF EXISTS sgst_paise, DROP COLUMN IF EXISTS igst_paise;
ALTER TABLE ipd_charges
    DROP COLUMN IF EXISTS tax_paise, DROP COLUMN IF EXISTS hsn_sac, DROP COLUMN IF EXISTS tax_rate_bps;

-- pre-117 definitions, copied verbatim from 103 / 116
DROP FUNCTION IF EXISTS ipd_bill_lines(uuid, uuid);
-- opd_guard_invoice: 103_opd_os_core.sql
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


-- opd_receipt_after_insert: 103_opd_os_core.sql
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


-- opd_recalc_invoice_subtotal: 116_ipd_finance.sql
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

-- ipd_bill_lines: 116_ipd_finance.sql
CREATE FUNCTION ipd_bill_lines(p_clinic_id UUID, p_admission_id UUID)
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


-- ipd_generate_final_bill: 116_ipd_finance.sql
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


-- ipd_running_bill: 116_ipd_finance.sql
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


-- ipd_apply_deposits: 116_ipd_finance.sql
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


-- ipd_discharge_checklist: 116_ipd_finance.sql
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


DROP FUNCTION IF EXISTS opd_gst_amount(bigint, integer);
DROP FUNCTION IF EXISTS opd_clinic_gstin(uuid);

DO $$
DECLARE f TEXT;
BEGIN
    REVOKE ALL ON FUNCTION ipd_bill_lines(uuid, uuid) FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION ipd_bill_lines(uuid, uuid) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON FUNCTION ipd_bill_lines(uuid, uuid) FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION ipd_bill_lines(uuid, uuid) TO service_role;
    END IF;
    FOREACH f IN ARRAY ARRAY[
        'opd_recalc_invoice_subtotal()', 'opd_guard_invoice()', 'opd_receipt_after_insert()',
        'ipd_bill_lines(uuid,uuid)', 'ipd_generate_final_bill(uuid,uuid,uuid,text)', 'ipd_running_bill(uuid,uuid)',
        'ipd_apply_deposits(uuid,uuid,uuid,text)', 'ipd_discharge_checklist(uuid,uuid)']
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', f);
    END LOOP;
END $$;
