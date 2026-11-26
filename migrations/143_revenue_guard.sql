-- ============================================================================
-- Migration 143: Kriya Revenue Guard (lane RI, RI-01) — nightly unbilled-charge check
-- Read-only over billing and clinical tables; writes only ri_* tables and clinics.ri_settings.
-- ri_scan(clinic, day, now, settings, modules) is the whole rule set (docs/revenue-integrity/SPEC.md §3): it upserts
-- one open finding per (rule, source row), resolves findings whose condition no longer holds (billed → recovered ₹,
-- voided, else auto_closed) and records the run. Amounts come from snapshots, never recomputed.
--
-- Existing objects touched: clinics gains ri_settings (additive, default '{}'). No trigger on any existing table.
-- Rollback: rollback/143_rollback_revenue_guard.sql.
-- ============================================================================

ALTER TABLE clinics ADD COLUMN IF NOT EXISTS ri_settings JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ── 1. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ri_runs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    run_date     DATE NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ NULL,
    manual_runs  SMALLINT NOT NULL DEFAULT 0 CHECK (manual_runs BETWEEN 0 AND 3),   -- POST /admin/ri/run, 3 a day
    rule_counts  JSONB NOT NULL DEFAULT '{}'::jsonb,
    error        TEXT NULL CHECK (char_length(error) <= 500),
    CONSTRAINT ri_runs_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ri_runs_day_key UNIQUE (clinic_id, run_date)
);

CREATE TABLE IF NOT EXISTS ri_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    rule_code       TEXT NOT NULL CHECK (rule_code IN (
                        'opd_seen_not_billed', 'prepaid_not_invoiced', 'order_done_not_billed', 'order_stale_unbilled',
                        'draft_bill_stale', 'ipd_charge_unposted', 'ipd_discharged_unbilled',
                        'ipd_discharge_pending_open', 'ot_done_no_charge', 'rx_dispense_no_charge',
                        'rx_signed_not_sold', 'dx_b2b_unstatemented')),
    kind            TEXT NOT NULL CHECK (kind IN ('leakage', 'review', 'integrity', 'opportunity')),
    source_table    TEXT NOT NULL CHECK (source_table IN ('appointments', 'opd_orders', 'opd_invoices', 'ipd_charges',
                                                          'ipd_admissions', 'ot_surgeries', 'rx_dispense_lines',
                                                          'opd_prescriptions', 'dx_b2b_ledger')),
    source_id       UUID NOT NULL,
    patient_id      UUID NULL,
    branch_id       UUID NULL,
    service_date    DATE NOT NULL,
    amount_paise    BIGINT NOT NULL DEFAULT 0 CHECK (amount_paise >= 0),
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    first_seen_on   DATE NOT NULL,
    last_seen_on    DATE NOT NULL,
    resolved_on     DATE NULL,
    resolution      TEXT NULL CHECK (resolution IN ('billed', 'voided', 'exempt', 'dismissed', 'auto_closed')),
    recovered_paise BIGINT NULL CHECK (recovered_paise >= 0),
    dismissed_by    UUID NULL,
    dismissed_by_name TEXT NULL CHECK (char_length(dismissed_by_name) <= 100),
    dismiss_reason  TEXT NULL CHECK (char_length(btrim(dismiss_reason)) BETWEEN 5 AND 300),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ri_findings_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT ri_findings_seen CHECK (last_seen_on >= first_seen_on),
    CONSTRAINT ri_findings_state CHECK (
        (status = 'open' AND resolution IS NULL AND resolved_on IS NULL)
     OR (status = 'resolved' AND resolution IN ('billed', 'voided', 'exempt', 'auto_closed') AND resolved_on IS NOT NULL)
     OR (status = 'dismissed' AND resolution = 'dismissed' AND resolved_on IS NOT NULL AND dismiss_reason IS NOT NULL
         AND dismissed_by IS NOT NULL)),
    CONSTRAINT ri_findings_recovered CHECK (recovered_paise IS NULL OR resolution = 'billed')
);
-- one open finding per source row and rule; one exemption record per source row and rule (a re-run never repeats it)
CREATE UNIQUE INDEX IF NOT EXISTS uq_ri_findings_open ON ri_findings (clinic_id, rule_code, source_table, source_id)
    WHERE status = 'open';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ri_findings_exempt ON ri_findings (clinic_id, rule_code, source_table, source_id)
    WHERE resolution = 'exempt';
CREATE INDEX IF NOT EXISTS idx_ri_findings_list ON ri_findings (clinic_id, status, service_date DESC);
CREATE INDEX IF NOT EXISTS idx_ri_findings_resolved ON ri_findings (clinic_id, resolved_on) WHERE resolved_on IS NOT NULL;

-- ── 2. The rule set ───────────────────────────────────────────────────────
-- p_day = the service day scanned (D−1 at night). p_now = "now" for the age rules. p_modules ⊆ {opd, ipd, ot, rx, dx}.
-- Settings (bounded here as well as in the API): min_opd_invoices_30d 0–1000 (20), review_free_days 0–30 (0),
-- exempt_reasons array of words matched against a draft bill's discount reason (staff / vip / charity).
CREATE OR REPLACE FUNCTION ri_scan(p_clinic_id UUID, p_day DATE, p_now TIMESTAMPTZ, p_settings JSONB, p_modules TEXT[])
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
    v_lo TIMESTAMPTZ := (p_day::timestamp AT TIME ZONE 'Asia/Kolkata');
    v_hi TIMESTAMPTZ := ((p_day + 1)::timestamp AT TIME ZONE 'Asia/Kolkata');
    v_today DATE := (p_now AT TIME ZONE 'Asia/Kolkata')::date;
    v_min_inv INT := LEAST(GREATEST(COALESCE((p_settings ->> 'min_opd_invoices_30d')::INT, 20), 0), 1000);
    v_free_days INT := LEAST(GREATEST(COALESCE((p_settings ->> 'review_free_days')::INT, 0), 0), 30);
    v_words TEXT[] := COALESCE((SELECT array_agg(lower(btrim(w))) FROM jsonb_array_elements_text(
                          CASE WHEN jsonb_typeof(p_settings -> 'exempt_reasons') = 'array' THEN p_settings -> 'exempt_reasons'
                               ELSE '["staff", "vip", "charity"]'::jsonb END) w WHERE btrim(w) <> ''), '{}');
    v_opd BOOLEAN := 'opd' = ANY (p_modules);
    v_ipd BOOLEAN := 'ipd' = ANY (p_modules);
    v_inv30 INT := 0;
    v_gate BOOLEAN;
    v_new INT; v_resolved INT; v_exempt INT;
    v_counts JSONB;
BEGIN
    IF p_clinic_id IS NULL OR p_day IS NULL OR p_now IS NULL THEN
        RAISE EXCEPTION 'ri_scan_bad_args' USING ERRCODE = 'P0001';
    END IF;
    IF v_opd THEN
        SELECT count(*) INTO v_inv30 FROM opd_invoices
         WHERE clinic_id = p_clinic_id AND invoice_seq IS NOT NULL AND status <> 'void'
           AND issued_at >= p_now - INTERVAL '30 days';
    END IF;
    v_gate := v_opd AND v_inv30 >= v_min_inv;

    CREATE TEMP TABLE IF NOT EXISTS ri_cand (
        rule_code TEXT, kind TEXT, source_table TEXT, source_id UUID, patient_id UUID, branch_id UUID,
        service_date DATE, amount_paise BIGINT, detail JSONB, exempt TEXT) ON COMMIT DROP;
    TRUNCATE ri_cand;

    IF v_gate THEN
        -- consultations seen on D (check-in or signed encounter) with no live invoice
        INSERT INTO ri_cand
        SELECT CASE WHEN a.payment_id IS NULL THEN 'opd_seen_not_billed' ELSE 'prepaid_not_invoiced' END,
               CASE WHEN a.payment_id IS NULL THEN 'leakage' ELSE 'review' END,
               'appointments', a.id, a.patient_id, a.branch_id, p_day,
               CASE WHEN a.payment_id IS NULL THEN COALESCE(d.consultation_fee, 0)::BIGINT * 100 ELSE 0 END,
               jsonb_build_object('doctor', d.name, 'estimate', a.payment_id IS NULL,
                                  'prepaid_paise', CASE WHEN a.payment_id IS NOT NULL THEN COALESCE(a.amount_paise, 0) END,
                                  'patient_name', a.patient_name),
               CASE WHEN a.payment_id IS NULL AND v_free_days > 0 AND EXISTS (
                        SELECT 1 FROM opd_invoices i JOIN appointments a2 ON a2.clinic_id = i.clinic_id AND a2.id = i.appointment_id
                         WHERE i.clinic_id = p_clinic_id AND i.patient_id = a.patient_id
                           AND i.status IN ('issued', 'partially_paid', 'paid')
                           AND a2.id <> a.id AND a2.appointment_date BETWEEN p_day - v_free_days AND p_day)
                    THEN 'free_review_within_' || v_free_days || '_days' END
          FROM appointments a
          LEFT JOIN doctors d ON d.clinic_id = a.clinic_id AND d.id = a.doctor_id
         WHERE a.clinic_id = p_clinic_id AND COALESCE(a.booking_type, 'consultation') = 'consultation'
           AND a.status <> 'cancelled' AND a.patient_id IS NOT NULL
           AND ((a.checked_in_at >= v_lo AND a.checked_in_at < v_hi)
                OR EXISTS (SELECT 1 FROM opd_encounters e WHERE e.clinic_id = a.clinic_id AND e.appointment_id = a.id
                             AND e.status = 'signed' AND e.signed_at >= v_lo AND e.signed_at < v_hi))
           AND NOT EXISTS (SELECT 1 FROM opd_invoices i WHERE i.clinic_id = a.clinic_id AND i.appointment_id = a.id
                             AND i.status <> 'void')
           AND (a.payment_id IS NOT NULL OR COALESCE(d.consultation_fee, 0) > 0);

        -- OPD orders performed (or older than 48 h) with no live bill line
        INSERT INTO ri_cand
        SELECT CASE WHEN o.status = 'ordered' THEN 'order_stale_unbilled' ELSE 'order_done_not_billed' END,
               CASE WHEN o.status = 'ordered' THEN 'review' ELSE 'leakage' END,
               'opd_orders', o.id, o.patient_id, o.branch_id, (o.created_at AT TIME ZONE 'Asia/Kolkata')::date,
               o.unit_price_paise::BIGINT * o.quantity, jsonb_build_object('item', o.item_name, 'status', o.status,
                                                                          'appointment_id', o.appointment_id), NULL
          FROM opd_orders o
         WHERE o.clinic_id = p_clinic_id AND o.source = 'opd' AND o.created_at >= p_now - INTERVAL '90 days'
           AND (o.status IN ('sample_collected', 'in_progress', 'resulted', 'completed')
                OR (o.status = 'ordered' AND o.created_at < p_now - INTERVAL '48 hours'))
           AND NOT EXISTS (SELECT 1 FROM opd_invoice_items li JOIN opd_invoices i ON i.clinic_id = li.clinic_id AND i.id = li.invoice_id
                            WHERE li.clinic_id = o.clinic_id AND li.order_id = o.id AND i.status <> 'void');

        -- draft bills with lines left for more than 24 h
        INSERT INTO ri_cand
        SELECT 'draft_bill_stale', 'review', 'opd_invoices', i.id, i.patient_id, i.branch_id,
               (i.created_at AT TIME ZONE 'Asia/Kolkata')::date, i.subtotal_paise::BIGINT,
               jsonb_build_object('appointment_id', i.appointment_id, 'discount_reason', i.discount_reason),
               CASE WHEN i.discount_reason IS NOT NULL AND EXISTS (
                        SELECT 1 FROM unnest(v_words) w WHERE position(w IN lower(i.discount_reason)) > 0)
                    THEN 'discount_reason' END
          FROM opd_invoices i
         WHERE i.clinic_id = p_clinic_id AND i.status = 'draft' AND i.created_at < p_now - INTERVAL '24 hours'
           AND i.created_at >= p_now - INTERVAL '90 days'
           AND EXISTS (SELECT 1 FROM opd_invoice_items li WHERE li.clinic_id = i.clinic_id AND li.invoice_id = i.id);
    END IF;

    IF v_ipd THEN
        INSERT INTO ri_cand
        SELECT 'ipd_charge_unposted', 'leakage', 'ipd_charges', c.id, ad.patient_id, ad.branch_id, c.service_date,
               c.total_paise::BIGINT, jsonb_build_object('admission_id', c.admission_id, 'description', c.description), NULL
          FROM ipd_charges c JOIN ipd_admissions ad ON ad.clinic_id = c.clinic_id AND ad.id = c.admission_id
         WHERE c.clinic_id = p_clinic_id AND c.status = 'proposed' AND c.created_at < p_now - INTERVAL '24 hours';

        INSERT INTO ri_cand
        SELECT 'ipd_discharged_unbilled', 'leakage', 'ipd_charges', c.id, ad.patient_id, ad.branch_id, c.service_date,
               c.total_paise::BIGINT, jsonb_build_object('admission_id', c.admission_id, 'description', c.description), NULL
          FROM ipd_charges c JOIN ipd_admissions ad ON ad.clinic_id = c.clinic_id AND ad.id = c.admission_id
         WHERE c.clinic_id = p_clinic_id AND ad.status = 'discharged' AND c.status = 'posted' AND c.invoice_id IS NULL;

        INSERT INTO ri_cand
        SELECT 'ipd_discharge_pending_open', 'review', 'ipd_admissions', ad.id, ad.patient_id, ad.branch_id,
               (ad.updated_at AT TIME ZONE 'Asia/Kolkata')::date, sum(c.total_paise)::BIGINT,
               jsonb_build_object('admission_id', ad.id, 'charges', count(*)), NULL
          FROM ipd_admissions ad JOIN ipd_charges c ON c.clinic_id = ad.clinic_id AND c.admission_id = ad.id
         WHERE ad.clinic_id = p_clinic_id AND ad.status = 'discharge_pending' AND ad.updated_at < p_now - INTERVAL '24 hours'
           AND (c.status = 'proposed' OR (c.status = 'posted' AND c.invoice_id IS NULL))
         GROUP BY ad.id, ad.patient_id, ad.branch_id, ad.updated_at;
    END IF;

    IF v_ipd AND 'ot' = ANY (p_modules) THEN
        INSERT INTO ri_cand
        SELECT 'ot_done_no_charge', 'integrity', 'ot_surgeries', s.id, s.patient_id, s.branch_id,
               (COALESCE(s.actual_end, s.updated_at) AT TIME ZONE 'Asia/Kolkata')::date,
               COALESCE(bs.standard_price_paise, 0)::BIGINT,
               jsonb_build_object('admission_id', s.admission_id, 'procedure', s.procedure_name,
                                  'surgery_number', s.surgery_number, 'tariff_known', bs.id IS NOT NULL), NULL
          FROM ot_surgeries s LEFT JOIN billable_services bs ON bs.clinic_id = s.clinic_id AND bs.id = s.service_id
         WHERE s.clinic_id = p_clinic_id AND s.status = 'completed' AND s.charge_id IS NULL;
    END IF;

    IF v_ipd AND 'rx' = ANY (p_modules) THEN
        INSERT INTO ri_cand
        SELECT 'rx_dispense_no_charge', 'integrity', 'rx_dispense_lines', l.id, NULL, NULL,
               (dp.dispensed_at AT TIME ZONE 'Asia/Kolkata')::date,
               (l.qty_units - l.returned_units)::BIGINT * l.unit_price_paise,
               jsonb_build_object('dispense_id', l.dispense_id), NULL
          FROM rx_dispense_lines l JOIN rx_dispenses dp ON dp.clinic_id = l.clinic_id AND dp.id = l.dispense_id
         WHERE l.clinic_id = p_clinic_id AND l.ipd_charge_id IS NULL AND l.qty_units > l.returned_units
           AND dp.dispensed_at >= p_now - INTERVAL '90 days';
    END IF;

    IF 'rx' = ANY (p_modules) THEN
        -- shortcut: only prescriptions with NO completed sale (partial fulfilment stays on the RX lost-sale list),
        -- amount 0 because a prescription line has no reliable map to an rx_items MRP; upgrade when RX maps lines.
        INSERT INTO ri_cand
        SELECT 'rx_signed_not_sold', 'opportunity', 'opd_prescriptions', p.id, p.patient_id, NULL, p_day, 0,
               jsonb_build_object('signed_at', p.signed_at), NULL
          FROM opd_prescriptions p
         WHERE p.clinic_id = p_clinic_id AND p.status = 'signed' AND p.signed_at >= v_lo AND p.signed_at < v_hi
           AND NOT EXISTS (SELECT 1 FROM rx_sales s WHERE s.clinic_id = p.clinic_id AND s.opd_prescription_id = p.id
                             AND s.status = 'completed');
    END IF;

    IF 'dx' = ANY (p_modules) THEN
        INSERT INTO ri_cand
        SELECT 'dx_b2b_unstatemented', 'review', 'dx_b2b_ledger', g.id, NULL, NULL,
               (g.created_at AT TIME ZONE 'Asia/Kolkata')::date, g.amount_paise,
               jsonb_build_object('account_id', g.account_id), NULL
          FROM dx_b2b_ledger g
         WHERE g.clinic_id = p_clinic_id AND g.entry_type = 'charge' AND g.statement_id IS NULL
           AND (date_trunc('month', g.created_at AT TIME ZONE 'Asia/Kolkata') + INTERVAL '1 month' + INTERVAL '7 days')::date
               <= v_today;
    END IF;

    -- exemptions: one resolved row each, visible in the list (never an open finding)
    INSERT INTO ri_findings (clinic_id, rule_code, kind, source_table, source_id, patient_id, branch_id, service_date,
                             amount_paise, detail, status, first_seen_on, last_seen_on, resolved_on, resolution)
    SELECT p_clinic_id, rule_code, kind, source_table, source_id, patient_id, branch_id, service_date, amount_paise,
           detail || jsonb_build_object('exemption', exempt), 'resolved', v_today, v_today, v_today, 'exempt'
      FROM ri_cand WHERE exempt IS NOT NULL
    ON CONFLICT (clinic_id, rule_code, source_table, source_id) WHERE resolution = 'exempt' DO NOTHING;
    GET DIAGNOSTICS v_exempt = ROW_COUNT;

    -- a dismissed source stays dismissed: the same row and rule never reopen
    DELETE FROM ri_cand c WHERE exempt IS NOT NULL OR EXISTS (
        SELECT 1 FROM ri_findings f WHERE f.clinic_id = p_clinic_id AND f.rule_code = c.rule_code
          AND f.source_table = c.source_table AND f.source_id = c.source_id AND f.status = 'dismissed');

    WITH up AS (
        INSERT INTO ri_findings (clinic_id, rule_code, kind, source_table, source_id, patient_id, branch_id, service_date,
                                 amount_paise, detail, first_seen_on, last_seen_on)
        SELECT p_clinic_id, rule_code, kind, source_table, source_id, patient_id, branch_id, service_date, amount_paise,
               detail, v_today, v_today FROM ri_cand
        ON CONFLICT (clinic_id, rule_code, source_table, source_id) WHERE status = 'open'
        DO UPDATE SET last_seen_on = GREATEST(ri_findings.last_seen_on, EXCLUDED.last_seen_on),
                      amount_paise = EXCLUDED.amount_paise, detail = EXCLUDED.detail, updated_at = now()
        RETURNING (xmax = 0) AS inserted)
    SELECT count(*) FILTER (WHERE inserted) INTO v_new FROM up;

    -- auto-resolve: open findings (any day) whose condition no longer holds. Only rules this run evaluated close.
    WITH gone AS (
        SELECT f.id, f.rule_code, f.source_table, f.source_id FROM ri_findings f
         WHERE f.clinic_id = p_clinic_id AND f.status = 'open'
           AND (f.rule_code NOT IN ('opd_seen_not_billed', 'prepaid_not_invoiced', 'order_done_not_billed',
                                    'order_stale_unbilled', 'draft_bill_stale') OR v_gate)
           AND (f.rule_code NOT LIKE 'ipd_%' OR v_ipd)
           AND (f.rule_code <> 'ot_done_no_charge' OR (v_ipd AND 'ot' = ANY (p_modules)))
           AND (f.rule_code <> 'rx_dispense_no_charge' OR (v_ipd AND 'rx' = ANY (p_modules)))
           AND (f.rule_code <> 'rx_signed_not_sold' OR 'rx' = ANY (p_modules))
           AND (f.rule_code <> 'dx_b2b_unstatemented' OR 'dx' = ANY (p_modules))
           -- rx_signed_not_sold is a one-day rule: a re-scan of its own day re-checks it; otherwise it stays on the
           -- list for 7 days (the patient may still come) and then closes, or closes as billed when the sale is made
           AND (f.rule_code <> 'rx_signed_not_sold' OR f.service_date = p_day OR f.service_date < v_today - 7
                OR EXISTS (SELECT 1 FROM rx_sales s WHERE s.clinic_id = p_clinic_id AND s.opd_prescription_id = f.source_id
                             AND s.status = 'completed'))
           -- a seen-not-billed finding of another day is re-checked only for the invoice it is waiting for
           AND (f.rule_code NOT IN ('opd_seen_not_billed', 'prepaid_not_invoiced') OR f.service_date = p_day
                OR EXISTS (SELECT 1 FROM opd_invoices i WHERE i.clinic_id = p_clinic_id AND i.appointment_id = f.source_id
                             AND i.status <> 'void')
                OR EXISTS (SELECT 1 FROM appointments a WHERE a.clinic_id = p_clinic_id AND a.id = f.source_id
                             AND a.status = 'cancelled'))
           AND NOT EXISTS (SELECT 1 FROM ri_cand c WHERE c.rule_code = f.rule_code AND c.source_table = f.source_table
                             AND c.source_id = f.source_id)),
    outcome AS (
        SELECT g.id,
               CASE
                 WHEN g.source_table = 'appointments' THEN
                   (SELECT CASE WHEN a.status = 'cancelled' THEN 'voided' END FROM appointments a
                     WHERE a.clinic_id = p_clinic_id AND a.id = g.source_id)
                 WHEN g.source_table = 'opd_orders' THEN
                   (SELECT CASE WHEN o.status = 'cancelled' THEN 'voided' END FROM opd_orders o
                     WHERE o.clinic_id = p_clinic_id AND o.id = g.source_id)
                 WHEN g.source_table = 'opd_invoices' THEN
                   (SELECT CASE WHEN i.status = 'void' THEN 'voided' END FROM opd_invoices i
                     WHERE i.clinic_id = p_clinic_id AND i.id = g.source_id)
                 WHEN g.source_table = 'ipd_charges' THEN
                   (SELECT CASE WHEN c.status = 'void' THEN 'voided' END FROM ipd_charges c
                     WHERE c.clinic_id = p_clinic_id AND c.id = g.source_id)
                 WHEN g.source_table = 'ot_surgeries' THEN
                   (SELECT CASE WHEN s.status = 'cancelled' THEN 'voided' END FROM ot_surgeries s
                     WHERE s.clinic_id = p_clinic_id AND s.id = g.source_id)
               END AS voided,
               CASE
                 WHEN g.source_table = 'appointments' THEN
                   (SELECT max(i.total_paise) FROM opd_invoices i WHERE i.clinic_id = p_clinic_id
                       AND i.appointment_id = g.source_id AND i.status IN ('issued', 'partially_paid', 'paid'))
                 WHEN g.source_table = 'opd_orders' THEN
                   (SELECT max(li.line_total_paise) FROM opd_invoice_items li JOIN opd_invoices i
                        ON i.clinic_id = li.clinic_id AND i.id = li.invoice_id
                     WHERE li.clinic_id = p_clinic_id AND li.order_id = g.source_id AND i.status <> 'void')
                 WHEN g.source_table = 'opd_invoices' THEN
                   (SELECT i.total_paise FROM opd_invoices i WHERE i.clinic_id = p_clinic_id AND i.id = g.source_id
                       AND i.status IN ('issued', 'partially_paid', 'paid'))
                 WHEN g.source_table = 'ipd_charges' AND g.rule_code = 'ipd_charge_unposted' THEN
                   (SELECT c.total_paise FROM ipd_charges c WHERE c.clinic_id = p_clinic_id AND c.id = g.source_id
                       AND c.status = 'posted')
                 WHEN g.source_table = 'ipd_charges' THEN
                   (SELECT c.total_paise FROM ipd_charges c WHERE c.clinic_id = p_clinic_id AND c.id = g.source_id
                       AND c.invoice_id IS NOT NULL AND c.status = 'posted')
                 WHEN g.source_table = 'ot_surgeries' THEN
                   (SELECT c.total_paise FROM ot_surgeries s JOIN ipd_charges c ON c.clinic_id = s.clinic_id AND c.id = s.charge_id
                     WHERE s.clinic_id = p_clinic_id AND s.id = g.source_id)
                 WHEN g.source_table = 'rx_dispense_lines' THEN
                   (SELECT c.total_paise FROM rx_dispense_lines l JOIN ipd_charges c ON c.clinic_id = l.clinic_id AND c.id = l.ipd_charge_id
                     WHERE l.clinic_id = p_clinic_id AND l.id = g.source_id)
                 WHEN g.source_table = 'opd_prescriptions' THEN
                   (SELECT max(s.net_paise) FROM rx_sales s WHERE s.clinic_id = p_clinic_id
                       AND s.opd_prescription_id = g.source_id AND s.status = 'completed')
                 WHEN g.source_table = 'dx_b2b_ledger' THEN
                   (SELECT x.amount_paise FROM dx_b2b_ledger x WHERE x.clinic_id = p_clinic_id AND x.id = g.source_id
                       AND x.statement_id IS NOT NULL)
               END AS billed
          FROM gone g),
    upd AS (
        UPDATE ri_findings f SET status = 'resolved', resolved_on = v_today, updated_at = now(),
               resolution = CASE WHEN o.billed IS NOT NULL THEN 'billed' WHEN o.voided IS NOT NULL THEN 'voided'
                                 ELSE 'auto_closed' END,
               -- prepaid money was already collected online: issuing its invoice recovers nothing new
               recovered_paise = CASE WHEN o.billed IS NOT NULL AND f.rule_code <> 'prepaid_not_invoiced'
                                      THEN o.billed::BIGINT WHEN o.billed IS NOT NULL THEN 0 END
          FROM outcome o WHERE f.id = o.id AND f.clinic_id = p_clinic_id
        RETURNING f.id)
    SELECT count(*) INTO v_resolved FROM upd;

    SELECT COALESCE(jsonb_object_agg(rule_code, n), '{}'::jsonb) INTO v_counts
      FROM (SELECT rule_code, count(*) n FROM ri_cand GROUP BY rule_code) x;
    v_counts := jsonb_build_object('rules', v_counts, 'new', v_new, 'resolved', v_resolved, 'exempt', v_exempt,
                                   'opd_gate', v_gate, 'opd_invoices_30d', v_inv30, 'opd_gate_min', v_min_inv,
                                   'day', p_day, 'modules', to_jsonb(p_modules));

    INSERT INTO ri_runs (clinic_id, run_date, started_at, finished_at, rule_counts)
    VALUES (p_clinic_id, v_today, p_now, now(), v_counts)
    ON CONFLICT (clinic_id, run_date) DO UPDATE SET finished_at = now(), rule_counts = EXCLUDED.rule_counts, error = NULL;
    RETURN v_counts;
END $$;

-- ── 3. RLS parity (049/103/111/125/134 pattern) + API-role lockout ─────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['ri_runs', 'ri_findings']
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
    EXECUTE 'REVOKE ALL ON FUNCTION ri_scan(uuid, date, timestamptz, jsonb, text[]) FROM PUBLIC';
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ri_scan(uuid, date, timestamptz, jsonb, text[]) FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON FUNCTION ri_scan(uuid, date, timestamptz, jsonb, text[]) FROM authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION ri_scan(uuid, date, timestamptz, jsonb, text[]) TO service_role';
    END IF;
END $$;

-- ── 4. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'clinics'
                     AND column_name = 'ri_settings') THEN
        RAISE EXCEPTION '143 verify: clinics.ri_settings missing';
    END IF;
    IF to_regclass('public.ri_runs') IS NULL OR to_regclass('public.ri_findings') IS NULL THEN
        RAISE EXCEPTION '143 verify: ri tables missing';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE oid IN ('public.ri_runs'::regclass, 'public.ri_findings'::regclass)
                 AND NOT (relrowsecurity AND relforcerowsecurity)) THEN
        RAISE EXCEPTION '143 verify: ri RLS not enabled + forced';
    END IF;
    IF to_regclass('public.uq_ri_findings_open') IS NULL OR to_regclass('public.uq_ri_findings_exempt') IS NULL THEN
        RAISE EXCEPTION '143 verify: ri_findings unique indexes missing';
    END IF;
    IF to_regprocedure('ri_scan(uuid,date,timestamptz,jsonb,text[])') IS NULL THEN
        RAISE EXCEPTION '143 verify: ri_scan missing';
    END IF;
END $$;
