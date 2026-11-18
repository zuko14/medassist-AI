-- ============================================================================
-- Migration 142: Kriya Pharmacy OS refills, alerts, integrity checks, item metrics (RX-M9, RX-55, RX-56, RX-76)
-- The last Pharmacy OS migration. Chronic refill plans (pharmacist opt-in, closed frequency map — computed in
-- app/services/rx_refills.py) and the requests patients raise from the reminder button; daily deduped stock /
-- integrity alerts (low stock, near expiry, expired on hand, ledger ≠ balance, register chain break, STAT
-- indent); the expiry-return planner (A8) read; rx_item_metrics prepared for A7; rx_purge_clinic v9.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §9 / §14, WORKFLOWS.md W10, ADVANCED-FEATURES.md A8.
-- Existing objects touched: one AFTER UPDATE OF name trigger on patients (fires only on DPDP redaction), and
-- rx_purge_clinic redefined as every RX migration does.
-- Rollback: rollback/142_rollback_rx_refills_alerts.sql.
-- ============================================================================

-- ── 1. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_refill_plans (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    patient_id               UUID NOT NULL,
    family_member_id         UUID NULL,
    item_id                  UUID NOT NULL,
    sale_item_id             UUID NOT NULL,
    opd_prescription_item_id UUID NULL,
    units_per_day            NUMERIC(6, 2) NOT NULL CHECK (units_per_day > 0),
    covered_until            DATE NOT NULL,
    remind_on                DATE NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'scheduled'
                                 CHECK (status IN ('scheduled', 'sent', 'requested', 'fulfilled', 'expired', 'opted_out',
                                                   'cancelled')),
    opt_in_by                UUID NOT NULL,                      -- pharmacist ticked "wants refill reminders" (X16)
    opt_in_at                TIMESTAMPTZ NOT NULL,
    last_message_id          TEXT NULL CHECK (char_length(last_message_id) <= 200),   -- delivery tracking only
    last_sent_at             TIMESTAMPTZ NULL,
    send_failures            SMALLINT NOT NULL DEFAULT 0 CHECK (send_failures BETWEEN 0 AND 3),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rx_refill_plans_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_refill_plans_sale_item_key UNIQUE (clinic_id, sale_item_id),      -- one plan per sold line
    CONSTRAINT rx_refill_plans_dates CHECK (remind_on <= covered_until),
    CONSTRAINT rx_refill_plans_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_refill_plans_sale_item_fk FOREIGN KEY (clinic_id, sale_item_id) REFERENCES rx_sale_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_refill_plans_due ON rx_refill_plans (clinic_id, status, remind_on);
CREATE INDEX IF NOT EXISTS idx_rx_refill_plans_patient ON rx_refill_plans (clinic_id, patient_id, item_id);

CREATE TABLE IF NOT EXISTS rx_refill_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    plan_id         UUID NOT NULL,
    patient_id      UUID NOT NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('whatsapp', 'counter', 'voice')),
    fulfilment      TEXT NOT NULL DEFAULT 'pickup' CHECK (fulfilment IN ('pickup', 'home_delivery')),
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'verified', 'packed', 'completed', 'rejected')),
    rejected_reason TEXT NULL CHECK (char_length(btrim(rejected_reason)) BETWEEN 3 AND 300),
    sale_id         UUID NULL,
    verified_by     UUID NULL,
    verified_at     TIMESTAMPTZ NULL,
    packed_by       UUID NULL,
    packed_at       TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rx_refill_requests_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_refill_requests_reject_reason CHECK ((status = 'rejected') = (rejected_reason IS NOT NULL)),
    CONSTRAINT rx_refill_requests_packed_sale CHECK (status NOT IN ('packed', 'completed') OR sale_id IS NOT NULL),
    CONSTRAINT rx_refill_requests_plan_fk FOREIGN KEY (clinic_id, plan_id) REFERENCES rx_refill_plans (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_refill_requests_sale_fk FOREIGN KEY (clinic_id, sale_id) REFERENCES rx_sales (clinic_id, id)
);
-- one open request per plan: a second tap of "Request refill" never queues twice
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_refill_requests_open ON rx_refill_requests (clinic_id, plan_id)
    WHERE status IN ('new', 'verified', 'packed');
CREATE INDEX IF NOT EXISTS idx_rx_refill_requests_queue ON rx_refill_requests (clinic_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_rx_refill_requests_sale ON rx_refill_requests (clinic_id, sale_id) WHERE sale_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rx_alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id        UUID NULL,
    item_id         UUID NULL,
    batch_id        UUID NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('low_stock', 'near_expiry', 'expired_on_hand', 'ledger_mismatch',
                                                  'register_chain_break', 'cold_chain', 'stat_indent')),
    alert_date      DATE NOT NULL,                                -- IST
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    acknowledged_by UUID NULL,
    acknowledged_at TIMESTAMPTZ NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rx_alerts_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_alerts_ack_shape CHECK ((acknowledged_at IS NULL) = (acknowledged_by IS NULL)),
    CONSTRAINT rx_alerts_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_alerts_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_alerts_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id)
);
-- one alert per entity per day. A STAT indent is an event, not a state: every one of them alerts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_alerts_dedupe ON rx_alerts (clinic_id, kind,
    COALESCE(store_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(item_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(batch_id, '00000000-0000-0000-0000-000000000000'::uuid), alert_date)
    WHERE kind <> 'stat_indent';
CREATE INDEX IF NOT EXISTS idx_rx_alerts_open ON rx_alerts (clinic_id, alert_date DESC) WHERE acknowledged_at IS NULL;

CREATE TABLE IF NOT EXISTS rx_item_metrics (                  -- A7, replaced nightly (job lands with RX-75)
    clinic_id               UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id                UUID NOT NULL,
    item_id                 UUID NOT NULL,
    adu_x1000               INTEGER NOT NULL DEFAULT 0 CHECK (adu_x1000 >= 0),
    abc                     CHAR(1) NULL CHECK (abc IN ('A', 'B', 'C')),
    fsn                     CHAR(1) NULL CHECK (fsn IN ('F', 'S', 'N')),
    suggested_reorder_units INTEGER NULL CHECK (suggested_reorder_units >= 0),
    suggested_max_units     INTEGER NULL CHECK (suggested_max_units >= 0),
    computed_on             DATE NOT NULL,
    PRIMARY KEY (clinic_id, store_id, item_id),
    CONSTRAINT rx_item_metrics_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_item_metrics_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

-- DPDP erasure (RX-68 part 2): when data_retention redacts a patient, their refill reminder state goes with it.
-- Fires only on the redaction itself (name becomes '[REDACTED]'); statutory registers are never touched.
CREATE OR REPLACE FUNCTION rx_patient_erased() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    DELETE FROM rx_refill_requests WHERE clinic_id = NEW.clinic_id AND patient_id = NEW.id;
    DELETE FROM rx_refill_plans WHERE clinic_id = NEW.clinic_id AND patient_id = NEW.id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_patient_erased ON patients;
CREATE TRIGGER trg_rx_patient_erased AFTER UPDATE OF name ON patients
    FOR EACH ROW WHEN (NEW.name = '[REDACTED]' AND OLD.name IS DISTINCT FROM NEW.name)
    EXECUTE FUNCTION rx_patient_erased();

-- ── 2. Integrity checks ────────────────────────────────────────────────────
-- The projection must equal the ledger, per store × batch. Any row here is a P1 incident.
CREATE OR REPLACE FUNCTION rx_ledger_check(p_clinic_id UUID)
RETURNS TABLE (store_id UUID, batch_id UUID, balance_units BIGINT, ledger_units BIGINT)
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT COALESCE(b.store_id, m.store_id), COALESCE(b.batch_id, m.batch_id),
           COALESCE(b.qty_units, 0)::BIGINT, COALESCE(m.units, 0)::BIGINT
      FROM (SELECT store_id, batch_id, qty_units FROM rx_stock_balances WHERE clinic_id = p_clinic_id) b
      FULL JOIN (SELECT store_id, batch_id, SUM(qty_units) AS units FROM rx_stock_movements
                  WHERE clinic_id = p_clinic_id GROUP BY store_id, batch_id) m
        ON m.store_id = b.store_id AND m.batch_id = b.batch_id
     WHERE COALESCE(b.qty_units, 0) <> COALESCE(m.units, 0)
$$;

-- Daily scan (RX-56): inserts the day's deduped alerts and returns what was found. The caller (rx_alerts.py)
-- raises the admin notifications for integrity failures.
CREATE OR REPLACE FUNCTION rx_alerts_scan(p_clinic_id UUID, p_today DATE, p_near_days INTEGER)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER; v_mis JSONB; v_breaks JSONB := '[]'::jsonb; r TEXT; c RECORD;
BEGIN
    INSERT INTO rx_alerts (clinic_id, store_id, item_id, kind, alert_date, payload)
    SELECT p_clinic_id, s.store_id, s.item_id, 'low_stock', p_today,
           jsonb_build_object('sellable_units', s.sellable, 'reorder_level_units', s.reorder_level_units,
                              'brand_name', s.brand_name)
      FROM (SELECT b.store_id, b.item_id, i.reorder_level_units, i.brand_name,
                   COALESCE(SUM(b.qty_units) FILTER (WHERE NOT bt.is_blocked AND bt.expiry_date >= p_today), 0) AS sellable
              FROM rx_stock_balances b
              JOIN rx_batches bt ON bt.clinic_id = b.clinic_id AND bt.id = b.batch_id
              JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
              JOIN rx_stores st ON st.clinic_id = b.clinic_id AND st.id = b.store_id AND st.is_active
             WHERE b.clinic_id = p_clinic_id AND i.is_active AND i.reorder_level_units > 0
             GROUP BY b.store_id, b.item_id, i.reorder_level_units, i.brand_name) s
     WHERE s.sellable < s.reorder_level_units
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('low_stock', n);

    INSERT INTO rx_alerts (clinic_id, store_id, item_id, batch_id, kind, alert_date, payload)
    SELECT p_clinic_id, b.store_id, b.item_id, b.batch_id,
           CASE WHEN bt.expiry_date < p_today THEN 'expired_on_hand' ELSE 'near_expiry' END, p_today,
           jsonb_build_object('qty_units', b.qty_units, 'expiry_date', bt.expiry_date, 'batch_no', bt.batch_no,
                              'brand_name', i.brand_name, 'days_left', bt.expiry_date - p_today)
      FROM rx_stock_balances b
      JOIN rx_batches bt ON bt.clinic_id = b.clinic_id AND bt.id = b.batch_id
      JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
     WHERE b.clinic_id = p_clinic_id AND b.qty_units > 0 AND bt.expiry_date <= p_today + p_near_days
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; v := v || jsonb_build_object('expiry', n);   -- near_expiry + expired_on_hand

    SELECT COALESCE(jsonb_agg(to_jsonb(x)), '[]'::jsonb) INTO v_mis FROM rx_ledger_check(p_clinic_id) x;
    INSERT INTO rx_alerts (clinic_id, store_id, batch_id, kind, alert_date, payload)
    SELECT p_clinic_id, x.store_id, x.batch_id, 'ledger_mismatch', p_today,
           jsonb_build_object('balance_units', x.balance_units, 'ledger_units', x.ledger_units)
      FROM rx_ledger_check(p_clinic_id) x
    ON CONFLICT DO NOTHING;
    v := v || jsonb_build_object('ledger_mismatch', v_mis);

    FOREACH r IN ARRAY ARRAY['H', 'H1', 'X', 'narcotic', 'psychotropic'] LOOP
        SELECT * INTO c FROM rx_register_verify(p_clinic_id, r);
        IF NOT c.ok THEN
            v_breaks := v_breaks || jsonb_build_array(jsonb_build_object('register', r, 'first_bad_seq', c.first_bad_seq,
                                                                         'verified_rows', c.verified_rows));
            INSERT INTO rx_alerts (clinic_id, kind, alert_date, payload)
            VALUES (p_clinic_id, 'register_chain_break', p_today,
                    jsonb_build_object('register', r, 'first_bad_seq', c.first_bad_seq))
            ON CONFLICT DO NOTHING;
        END IF;
    END LOOP;
    RETURN v || jsonb_build_object('register_chain_break', v_breaks);
END $$;

-- ── 3. Expiry-return planner (A8 / RX-76): read-only suggestion, grouped by the batch's original supplier ──
CREATE OR REPLACE FUNCTION rx_expiry_return_plan(p_clinic_id UUID, p_store_id UUID, p_today DATE) RETURNS JSONB
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT COALESCE(jsonb_agg(g ORDER BY g ->> 'supplier_name'), '[]'::jsonb) FROM (
        SELECT jsonb_build_object('supplier_id', s.id, 'supplier_name', s.name, 'return_window_days', s.return_window_days,
                   'lines', jsonb_agg(jsonb_build_object(
                       'batch_id', b.id, 'batch_no', b.batch_no, 'item_id', i.id, 'brand_name', i.brand_name,
                       'expiry_date', b.expiry_date, 'days_left', b.expiry_date - p_today, 'expired', b.expiry_date < p_today,
                       'qty_units', bal.qty_units,
                       'drafted_units', COALESCE((SELECT SUM(rl.qty_units) FROM rx_supplier_return_lines rl
                                                    JOIN rx_supplier_returns sr ON sr.clinic_id = rl.clinic_id
                                                     AND sr.id = rl.return_id AND sr.status = 'draft'
                                                   WHERE rl.clinic_id = p_clinic_id AND rl.batch_id = b.id
                                                     AND sr.store_id = p_store_id), 0))
                       ORDER BY b.expiry_date, i.brand_name)) AS g
          FROM rx_stock_balances bal
          JOIN rx_batches b ON b.clinic_id = bal.clinic_id AND b.id = bal.batch_id
          JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
          JOIN rx_grn_lines gl ON gl.clinic_id = b.clinic_id AND gl.id = b.first_grn_line_id
          JOIN rx_grns gr ON gr.clinic_id = gl.clinic_id AND gr.id = gl.grn_id
          JOIN rx_suppliers s ON s.clinic_id = gr.clinic_id AND s.id = gr.supplier_id
         WHERE bal.clinic_id = p_clinic_id AND bal.store_id = p_store_id AND bal.qty_units > 0
           AND (b.expiry_date < p_today
                OR (s.return_window_days IS NOT NULL AND b.expiry_date - p_today <= s.return_window_days))
         GROUP BY s.id, s.name, s.return_window_days) x
$$;

-- ── 4. Owner purge v9 ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
    DELETE FROM rx_refill_requests WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_refill_plans WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('refill_plans', n);
    DELETE FROM rx_alerts WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_item_metrics WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_register_entries WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('register_entries', n);
    DELETE FROM rx_par_levels WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_stock_audits WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('audits', n);
    DELETE FROM rx_transfer_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_transfers WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('transfers', n);
    DELETE FROM rx_mar_links WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_empty_returns WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_dispense_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_dispenses WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('dispenses', n);
    DELETE FROM rx_indent_lines WHERE clinic_id = p_clinic_id;
    DELETE FROM rx_ipd_indents WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('indents', n);
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

-- ── 5. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT; f TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_refill_plans', 'rx_refill_requests', 'rx_alerts', 'rx_item_metrics'] LOOP
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
    FOREACH f IN ARRAY ARRAY['rx_ledger_check(uuid)', 'rx_alerts_scan(uuid,date,integer)',
                             'rx_expiry_return_plan(uuid,uuid,date)', 'rx_purge_clinic(uuid)'] LOOP
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

-- ── 6. Verify ──────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_refill_plans', 'rx_refill_requests', 'rx_alerts', 'rx_item_metrics'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '142 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '142 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF to_regclass('public.uq_rx_alerts_dedupe') IS NULL THEN RAISE EXCEPTION '142 verify: alert dedupe index missing'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_rx_patient_erased') THEN
        RAISE EXCEPTION '142 verify: erasure trigger on patients missing';
    END IF;
END $$;
