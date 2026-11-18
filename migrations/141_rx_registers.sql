-- ============================================================================
-- Migration 141: Kriya Pharmacy OS statutory registers (RX-M8, RX-48, RX-49, RX-74, RX-79)
-- Schedule H / H1 / X / narcotic / psychotropic registers as one tamper-evident table, hash-chained per
-- clinic × register with a gapless seq; rx_register_append / rx_register_verify; batch recall trace (A6);
-- narcotic empty-ampoule returns under the two-person rule (A11); rx_purge_clinic v8.
--
-- Wiring: instead of redefining every ledger writer (rx_post_grn, rx_complete_sale, rx_dispense_indent,
-- rx_record_return, rx_ipd_return, rx_supplier_return, rx_post_audit, rx_post_writeoff, rx_void_*,
-- rx_dispatch_transfer, rx_receive_transfer, rx_opening_stock), ONE deferred constraint trigger on the ledger
-- appends a register row for every movement of a scheduled item. Every writer goes through rx_stock_movements
-- (ADR-RX-01), so no current or future writer can skip the register. It is deferred to commit because
-- rx_dispense_indent writes the movement before its dispense line (the patient / prescriber source).
--
-- direction: in (opening, grn_in, transfer_in) · out (sale_out, ipd_dispense_out — a patient issue, so the
-- prescriber is required) · return (sale / IPD returns, supplier returns) · writeoff · adjust (audit variance,
-- GRN void, transfer_out, ward_issue_out). movement_type keeps the exact ledger kind (receipt vs issue).
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §8, WORKFLOWS.md W9, ADVANCED-FEATURES.md A6 / A11.
-- Existing objects touched: one trigger added on rx_stock_movements (RX's own ledger). No OPD / IPD object.
-- Rollback: rollback/141_rollback_rx_registers.sql.
-- ============================================================================

-- ── 1. Table [A][F] ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_register_entries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    register            TEXT NOT NULL CHECK (register IN ('H', 'H1', 'X', 'narcotic', 'psychotropic')),
    seq                 BIGINT NOT NULL CHECK (seq > 0),          -- gapless per (clinic, register)
    entry_date          DATE NOT NULL,                             -- IST
    direction           TEXT NOT NULL CHECK (direction IN ('in', 'out', 'return', 'writeoff', 'adjust')),
    movement_type       TEXT NOT NULL CHECK (char_length(movement_type) BETWEEN 1 AND 40),
    item_id             UUID NOT NULL,
    batch_id            UUID NOT NULL,
    store_id            UUID NOT NULL,
    qty_units           INTEGER NOT NULL CHECK (qty_units > 0),
    balance_after_units INTEGER NOT NULL,                          -- item × store, after this movement
    source_table        TEXT NOT NULL,
    source_id           UUID NOT NULL,
    doc_ref             TEXT NULL CHECK (char_length(doc_ref) <= 60),   -- bill / GRN / SRN / STR / IND / AUD number
    patient_snapshot    JSONB NULL,                                -- name, age, gender, mrn, address — never a phone
    prescriber_snapshot JSONB NULL,                                -- name, registration_number, council, address
    supplier_snapshot   JSONB NULL,                                -- name, licence_no, invoice_no, invoice_date
    dispensed_by_name   TEXT NOT NULL,
    pharmacist_reg_no   TEXT NULL,
    prev_hash           BYTEA NULL,
    row_hash            BYTEA NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT rx_register_entries_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_register_entries_seq_key UNIQUE (clinic_id, register, seq),
    CONSTRAINT rx_register_entries_out_prescriber CHECK (direction <> 'out' OR prescriber_snapshot ? 'registration_number'),
    CONSTRAINT rx_register_entries_out_patient CHECK (register NOT IN ('H1', 'X', 'narcotic') OR direction <> 'out'
                                                      OR patient_snapshot ? 'name'),
    CONSTRAINT rx_register_entries_chain_head CHECK ((seq = 1) = (prev_hash IS NULL)),
    CONSTRAINT rx_register_entries_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_register_entries_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_register_entries_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_register_date ON rx_register_entries (clinic_id, register, entry_date, seq);
CREATE INDEX IF NOT EXISTS idx_rx_register_batch ON rx_register_entries (clinic_id, batch_id);

-- ── 2. Guards: rows come only from rx_register_append; never changed; deleted only by the owner purge ──
CREATE OR REPLACE FUNCTION rx_register_guard_insert() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF COALESCE(current_setting('kriya.rx_reg', true), '') <> NEW.clinic_id::TEXT THEN
        PERFORM rx_raise('rx_register_via_function_only');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_register_entries_insert ON rx_register_entries;
CREATE TRIGGER trg_rx_register_entries_insert BEFORE INSERT ON rx_register_entries
    FOR EACH ROW EXECUTE FUNCTION rx_register_guard_insert();
DROP TRIGGER IF EXISTS trg_rx_register_entries_append_only ON rx_register_entries;
CREATE TRIGGER trg_rx_register_entries_append_only BEFORE UPDATE OR DELETE ON rx_register_entries
    FOR EACH ROW EXECUTE FUNCTION rx_guard_append_only();

-- ── 3. Hash, append, verify ────────────────────────────────────────────────
-- row_hash = sha256(prev_hash ‖ canonical jsonb of the row). jsonb::text is canonical (sorted keys, one
-- spelling per value); created_at is rendered in UTC so the session time zone never changes the hash.
-- A column added later changes the canonical form: exclude it here (or re-anchor) in that migration.
CREATE OR REPLACE FUNCTION rx_register_hash(r rx_register_entries) RETURNS BYTEA
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT sha256(COALESCE(r.prev_hash, ''::BYTEA) || convert_to(
        ((to_jsonb(r) - 'prev_hash' - 'row_hash' - 'created_at')
         || jsonb_build_object('created_at', to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')))::TEXT,
        'UTF8'))
$$;

-- p_balance NULL → the item's current on-hand in the store (rx_stock_balances).
CREATE OR REPLACE FUNCTION rx_register_append(p_clinic_id UUID, p_register TEXT, p_direction TEXT, p_item_id UUID,
        p_batch_id UUID, p_store_id UUID, p_qty INTEGER, p_source_table TEXT, p_source_id UUID, p_patient JSONB,
        p_prescriber JSONB, p_supplier JSONB, p_actor_name TEXT, p_pharmacist_reg TEXT,
        p_movement_type TEXT DEFAULT NULL, p_doc_ref TEXT DEFAULT NULL, p_at TIMESTAMPTZ DEFAULT NULL,
        p_balance INTEGER DEFAULT NULL)
RETURNS UUID LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r rx_register_entries; v_prev rx_register_entries;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('rx_reg:' || p_clinic_id::TEXT || ':' || p_register));
    SELECT * INTO v_prev FROM rx_register_entries WHERE clinic_id = p_clinic_id AND register = p_register
     ORDER BY seq DESC LIMIT 1;
    r.id := gen_random_uuid();
    r.clinic_id := p_clinic_id;
    r.register := p_register;
    r.seq := COALESCE(v_prev.seq, 0) + 1;
    r.entry_date := rx_ist_date(COALESCE(p_at, now()));
    r.direction := p_direction;
    r.movement_type := COALESCE(p_movement_type, p_direction);
    r.item_id := p_item_id;
    r.batch_id := p_batch_id;
    r.store_id := p_store_id;
    r.qty_units := p_qty;
    r.balance_after_units := COALESCE(p_balance, (SELECT COALESCE(SUM(qty_units), 0)::INTEGER FROM rx_stock_balances
                                                  WHERE clinic_id = p_clinic_id AND store_id = p_store_id
                                                    AND item_id = p_item_id));
    r.source_table := p_source_table;
    r.source_id := p_source_id;
    r.doc_ref := left(p_doc_ref, 60);
    r.patient_snapshot := p_patient;
    r.prescriber_snapshot := p_prescriber;
    r.supplier_snapshot := p_supplier;
    r.dispensed_by_name := left(COALESCE(NULLIF(btrim(p_actor_name), ''), 'system'), 100);
    r.pharmacist_reg_no := NULLIF(btrim(p_pharmacist_reg), '');
    r.prev_hash := v_prev.row_hash;
    r.created_at := now();
    r.row_hash := rx_register_hash(r);
    PERFORM set_config('kriya.rx_reg', p_clinic_id::TEXT, true);
    INSERT INTO rx_register_entries SELECT r.*;
    PERFORM set_config('kriya.rx_reg', '', true);
    RETURN r.id;
END $$;

-- Recomputes the chain in seq order. The first bad seq is always verified_rows + 1: a changed row, a broken
-- link or a missing seq all stop the walk there.
-- shortcut: deleting the newest rows leaves a valid (shorter) chain; anchor the head hash off-site (nightly
-- job, P8 alerts) if that must be detected too.
CREATE OR REPLACE FUNCTION rx_register_verify(p_clinic_id UUID, p_register TEXT)
RETURNS TABLE (ok BOOLEAN, verified_rows BIGINT, first_bad_seq BIGINT)
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $$
DECLARE r rx_register_entries; v_prev BYTEA := NULL; n BIGINT := 0;
BEGIN
    FOR r IN SELECT * FROM rx_register_entries WHERE clinic_id = p_clinic_id AND register = p_register ORDER BY seq LOOP
        IF r.seq <> n + 1 OR r.prev_hash IS DISTINCT FROM v_prev OR r.row_hash IS DISTINCT FROM rx_register_hash(r) THEN
            RETURN QUERY SELECT false, n, n + 1;
            RETURN;
        END IF;
        v_prev := r.row_hash;
        n := n + 1;
    END LOOP;
    RETURN QUERY SELECT true, n, NULL::BIGINT;
END $$;

-- ── 4. Ledger → register (deferred to commit) ──────────────────────────────
CREATE OR REPLACE FUNCTION rx_patient_snap(p_clinic_id UUID, p_patient_id UUID, p_fallback_name TEXT) RETURNS JSONB
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT COALESCE(
        (SELECT jsonb_build_object('name', COALESCE(NULLIF(btrim(p.name), ''), p_fallback_name), 'age', p.age_years,
                                   'gender', p.gender, 'mrn', p.mrn, 'address', p.address_line)
           FROM patients p WHERE p.clinic_id = p_clinic_id AND p.id = p_patient_id),
        jsonb_build_object('name', p_fallback_name, 'age', NULL, 'gender', NULL, 'mrn', NULL, 'address', NULL))
$$;

CREATE OR REPLACE FUNCTION rx_sm_register() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_reg TEXT; v_dir TEXT; v_pat JSONB; v_pres JSONB; v_sup JSONB; v_doc TEXT; v_bal INTEGER;
    s rx_sales; v_sale_item UUID; v_dl rx_dispense_lines; v_adm UUID; v_doctor UUID; v_grn rx_grns; v_sup_id UUID;
BEGIN
    SELECT schedule INTO v_reg FROM rx_items WHERE clinic_id = NEW.clinic_id AND id = NEW.item_id;
    IF v_reg IS NULL OR v_reg NOT IN ('H', 'H1', 'X', 'narcotic', 'psychotropic') THEN RETURN NULL; END IF;
    -- purged later in the same transaction: nothing left to register
    IF NOT EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = NEW.clinic_id AND id = NEW.id) THEN RETURN NULL; END IF;

    v_dir := CASE
        WHEN NEW.movement_type IN ('opening', 'grn_in', 'transfer_in') THEN 'in'
        WHEN NEW.movement_type IN ('sale_out', 'ipd_dispense_out') THEN 'out'
        WHEN NEW.movement_type IN ('sale_return_in', 'ipd_return_in', 'supplier_return_out') THEN 'return'
        WHEN NEW.movement_type LIKE '%writeoff_out' THEN 'writeoff'
        ELSE 'adjust' END;

    -- counter sale, its return / void (and a returned strip written off straight away)
    IF NEW.ref_table IN ('rx_sale_items', 'rx_sale_void') THEN
        v_sale_item := NEW.ref_id;
    ELSIF NEW.ref_table = 'rx_return_lines' THEN
        SELECT sale_item_id INTO v_sale_item FROM rx_return_lines WHERE clinic_id = NEW.clinic_id AND id = NEW.ref_id;
    END IF;
    IF v_sale_item IS NOT NULL THEN
        SELECT s2.* INTO s FROM rx_sale_items si JOIN rx_sales s2 ON s2.clinic_id = si.clinic_id AND s2.id = si.sale_id
         WHERE si.clinic_id = NEW.clinic_id AND si.id = v_sale_item;
        v_doc := s.sale_number;
        v_pat := rx_patient_snap(NEW.clinic_id, s.patient_id,
                                 COALESCE(NULLIF(btrim(s.walkin_name), ''), s.prescriber_snapshot ->> 'patient_name'));
        v_pres := jsonb_build_object('name', s.prescriber_snapshot ->> 'name',
                                     'registration_number', s.prescriber_snapshot ->> 'registration_number',
                                     'council', COALESCE(s.prescriber_snapshot ->> 'council',
                                                         s.prescriber_snapshot ->> 'registration_council'),
                                     'address', s.prescriber_snapshot ->> 'address');
    -- inpatient dispense, ward return (and its write-off)
    ELSIF NEW.ref_table = 'rx_dispense_lines' OR NEW.ref_table LIKE 'rx\_ipd\_returns:%' THEN
        SELECT * INTO v_dl FROM rx_dispense_lines WHERE clinic_id = NEW.clinic_id AND id = NEW.ref_id;
        SELECT i.indent_number, i.admission_id, o.ordered_by_doctor_id INTO v_doc, v_adm, v_doctor
          FROM rx_indent_lines il
          JOIN rx_ipd_indents i ON i.clinic_id = il.clinic_id AND i.id = il.indent_id
          JOIN ipd_orders o ON o.clinic_id = il.clinic_id AND o.id = il.ipd_order_id
         WHERE il.clinic_id = NEW.clinic_id AND il.id = v_dl.indent_line_id;
        v_pat := rx_patient_snap(NEW.clinic_id, (SELECT patient_id FROM ipd_admissions
                                                  WHERE clinic_id = NEW.clinic_id AND id = v_adm), NULL)
                 || jsonb_build_object('admission', (SELECT admission_number FROM ipd_admissions
                                                      WHERE clinic_id = NEW.clinic_id AND id = v_adm));
        v_pres := COALESCE((SELECT jsonb_build_object('name', d.name, 'registration_number', d.registration_number,
                                                      'council', d.registration_council, 'address', NULL)
                              FROM doctors d WHERE d.clinic_id = NEW.clinic_id AND d.id = v_doctor),
                           jsonb_build_object('name', NULL, 'registration_number', NULL, 'council', NULL, 'address', NULL));
    -- purchase receipt and its void
    ELSIF NEW.ref_table IN ('rx_grn_lines', 'rx_grn_void') THEN
        SELECT g.* INTO v_grn FROM rx_grn_lines gl JOIN rx_grns g ON g.clinic_id = gl.clinic_id AND g.id = gl.grn_id
         WHERE gl.clinic_id = NEW.clinic_id AND gl.id = NEW.ref_id;
        v_doc := v_grn.grn_number;
        v_sup := (SELECT jsonb_build_object('name', sp.name, 'licence_no', sp.drug_licence_no,
                                            'invoice_no', v_grn.supplier_invoice_no, 'invoice_date', v_grn.supplier_invoice_date)
                    FROM rx_suppliers sp WHERE sp.clinic_id = NEW.clinic_id AND sp.id = v_grn.supplier_id);
    ELSIF NEW.ref_table = 'rx_supplier_return_lines' THEN
        SELECT r.srn_number, r.supplier_id INTO v_doc, v_sup_id
          FROM rx_supplier_return_lines rl JOIN rx_supplier_returns r ON r.clinic_id = rl.clinic_id AND r.id = rl.return_id
         WHERE rl.clinic_id = NEW.clinic_id AND rl.id = NEW.ref_id;
        v_sup := (SELECT jsonb_build_object('name', sp.name, 'licence_no', sp.drug_licence_no, 'invoice_no', NULL,
                                            'invoice_date', NULL)
                    FROM rx_suppliers sp WHERE sp.clinic_id = NEW.clinic_id AND sp.id = v_sup_id);
    ELSIF NEW.ref_table = 'rx_transfer_lines' THEN
        SELECT t.transfer_number INTO v_doc FROM rx_transfer_lines tl
          JOIN rx_transfers t ON t.clinic_id = tl.clinic_id AND t.id = tl.transfer_id
         WHERE tl.clinic_id = NEW.clinic_id AND tl.id = NEW.ref_id;
    ELSIF NEW.ref_table = 'rx_stock_audit_lines' THEN
        SELECT a.audit_number INTO v_doc FROM rx_stock_audit_lines al
          JOIN rx_stock_audits a ON a.clinic_id = al.clinic_id AND a.id = al.audit_id
         WHERE al.clinic_id = NEW.clinic_id AND al.id = NEW.ref_id;
    END IF;

    -- running balance as of this movement (deferred: later movements of this transaction are excluded)
    SELECT COALESCE(SUM(qty_units), 0)::INTEGER INTO v_bal FROM rx_stock_movements
     WHERE clinic_id = NEW.clinic_id AND store_id = NEW.store_id AND item_id = NEW.item_id AND created_at <= NEW.created_at;

    PERFORM rx_register_append(NEW.clinic_id, v_reg, v_dir, NEW.item_id, NEW.batch_id, NEW.store_id, abs(NEW.qty_units),
        NEW.ref_table, NEW.ref_id, v_pat, v_pres, v_sup, NEW.actor_name,
        (SELECT rx_settings ->> 'pharmacist_reg_no' FROM clinics WHERE id = NEW.clinic_id),
        NEW.movement_type, v_doc, NEW.created_at, v_bal);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sm_register ON rx_stock_movements;
CREATE CONSTRAINT TRIGGER trg_rx_sm_register AFTER INSERT ON rx_stock_movements
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rx_sm_register();

-- ── 5. Narcotic empty-ampoule returns (A11 / RX-79) [F] ────────────────────
CREATE OR REPLACE FUNCTION rx_record_empties(p_clinic_id UUID, p_dispense_line_id UUID, p_units INTEGER,
                                             p_returned_by UUID, p_received_by UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE dl rx_dispense_lines; v_schedule TEXT; v_done INTEGER; v_id UUID := gen_random_uuid();
BEGIN
    SELECT * INTO dl FROM rx_dispense_lines WHERE clinic_id = p_clinic_id AND id = p_dispense_line_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_dispense_line_not_found'); END IF;
    SELECT schedule INTO v_schedule FROM rx_items WHERE clinic_id = p_clinic_id AND id = dl.item_id;
    IF v_schedule NOT IN ('X', 'narcotic', 'psychotropic') THEN PERFORM rx_raise('rx_empties_not_controlled'); END IF;
    IF p_units IS NULL OR p_units <= 0 THEN PERFORM rx_raise('rx_qty_invalid'); END IF;
    IF p_returned_by IS NULL OR p_received_by IS NULL OR p_returned_by = p_received_by THEN
        PERFORM rx_raise('rx_empties_two_person');
    END IF;
    SELECT COALESCE(SUM(units), 0) INTO v_done FROM rx_empty_returns
     WHERE clinic_id = p_clinic_id AND dispense_line_id = dl.id;
    IF v_done + p_units > dl.qty_units - dl.returned_units THEN PERFORM rx_raise('rx_empties_exceed_dispensed'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    INSERT INTO rx_empty_returns (id, clinic_id, dispense_line_id, units, returned_by, received_by)
    VALUES (v_id, p_clinic_id, dl.id, p_units, p_returned_by, p_received_by);
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('id', v_id, 'dispense_line_id', dl.id, 'units', p_units,
                              'empties_returned_units', v_done + p_units,
                              'outstanding_units', dl.qty_units - dl.returned_units - v_done - p_units);
END $$;

-- ── 6. Batch recall trace (A6 / RX-74): pure reads by batch ────────────────
CREATE OR REPLACE FUNCTION rx_batch_trace(p_clinic_id UUID, p_batch_id UUID) RETURNS JSONB
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT jsonb_build_object(
        'batch', jsonb_build_object('id', b.id, 'batch_no', b.batch_no, 'expiry_date', b.expiry_date,
                                    'is_blocked', b.is_blocked, 'block_reason', b.block_reason, 'item_id', i.id,
                                    'item_code', i.item_code, 'brand_name', i.brand_name, 'schedule', i.schedule),
        'sales', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'sale_id', s.id, 'sale_number', s.sale_number, 'completed_at', s.completed_at,
                       'patient_id', s.patient_id, 'patient_name', COALESCE(p.name, s.walkin_name),
                       -- masked: never more than the last 4 digits leave this function
                       'phone_masked', COALESCE(s.walkin_phone_masked,
                                                CASE WHEN p.phone IS NOT NULL THEN 'XXXXXX' || right(p.phone, 4) END),
                       'qty_units', si.qty_units, 'returned_units', si.returned_units) ORDER BY s.completed_at)
              FROM rx_sale_items si
              JOIN rx_sales s ON s.clinic_id = si.clinic_id AND s.id = si.sale_id AND s.status = 'completed'
              LEFT JOIN patients p ON p.clinic_id = s.clinic_id AND p.id = s.patient_id
             WHERE si.clinic_id = p_clinic_id AND si.batch_id = p_batch_id), '[]'::jsonb),
        'ipd', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'dispense_line_id', dl.id, 'dispensed_at', d.dispensed_at, 'indent_number', ind.indent_number,
                       'admission_id', a.id, 'admission_number', a.admission_number, 'patient_name', p.name,
                       'ward', w.name, 'bed', bd.code, 'qty_units', dl.qty_units, 'returned_units', dl.returned_units,
                       'mar_doses', (SELECT count(*) FROM rx_mar_links m WHERE m.clinic_id = dl.clinic_id
                                                                         AND m.dispense_line_id = dl.id))
                       ORDER BY d.dispensed_at)
              FROM rx_dispense_lines dl
              JOIN rx_dispenses d ON d.clinic_id = dl.clinic_id AND d.id = dl.dispense_id
              JOIN rx_ipd_indents ind ON ind.clinic_id = d.clinic_id AND ind.id = d.indent_id
              JOIN ipd_admissions a ON a.clinic_id = ind.clinic_id AND a.id = ind.admission_id
              LEFT JOIN patients p ON p.clinic_id = a.clinic_id AND p.id = a.patient_id
              LEFT JOIN ipd_wards w ON w.clinic_id = ind.clinic_id AND w.id = ind.ward_id
              LEFT JOIN ipd_beds bd ON bd.clinic_id = a.clinic_id AND bd.id = a.current_bed_id
             WHERE dl.clinic_id = p_clinic_id AND dl.batch_id = p_batch_id), '[]'::jsonb),
        'transfers', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'transfer_id', t.id, 'transfer_number', t.transfer_number, 'status', t.status,
                       'from_store', fs.code, 'to_store', ts.code, 'sent_units', tl.sent_units,
                       'received_units', tl.received_units) ORDER BY t.created_at)
              FROM rx_transfer_lines tl
              JOIN rx_transfers t ON t.clinic_id = tl.clinic_id AND t.id = tl.transfer_id AND t.status <> 'draft'
              JOIN rx_stores fs ON fs.clinic_id = t.clinic_id AND fs.id = t.from_store_id
              JOIN rx_stores ts ON ts.clinic_id = t.clinic_id AND ts.id = t.to_store_id
             WHERE tl.clinic_id = p_clinic_id AND tl.batch_id = p_batch_id), '[]'::jsonb),
        'on_hand', COALESCE((
            SELECT jsonb_agg(jsonb_build_object('store_id', st.id, 'store_code', st.code, 'store_name', st.name,
                                                'qty_units', bal.qty_units) ORDER BY st.code)
              FROM rx_stock_balances bal JOIN rx_stores st ON st.clinic_id = bal.clinic_id AND st.id = bal.store_id
             WHERE bal.clinic_id = p_clinic_id AND bal.batch_id = p_batch_id AND bal.qty_units > 0), '[]'::jsonb))
      FROM rx_batches b JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
     WHERE b.clinic_id = p_clinic_id AND b.id = p_batch_id
$$;

-- ── 7. Owner purge v8 ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- ── 8. RLS parity + API-role lockout ───────────────────────────────────────
ALTER TABLE rx_register_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE rx_register_entries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_role_all_rx_register_entries ON rx_register_entries;
DROP POLICY IF EXISTS tenant_isolation_rx_register_entries ON rx_register_entries;
CREATE POLICY tenant_isolation_rx_register_entries ON rx_register_entries FOR ALL
    USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
DO $$
DECLARE f TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_rx_register_entries ON rx_register_entries FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE rx_register_entries FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE rx_register_entries FROM authenticated;
    END IF;
    FOREACH f IN ARRAY ARRAY['rx_register_hash(rx_register_entries)',
                             'rx_register_append(uuid,text,text,uuid,uuid,uuid,integer,text,uuid,jsonb,jsonb,jsonb,text,text,text,text,timestamptz,integer)',
                             'rx_register_verify(uuid,text)', 'rx_patient_snap(uuid,uuid,text)',
                             'rx_record_empties(uuid,uuid,integer,uuid,uuid)', 'rx_batch_trace(uuid,uuid)',
                             'rx_purge_clinic(uuid)']
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

-- ── 9. Verify ──────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regclass('public.rx_register_entries') IS NULL THEN RAISE EXCEPTION '141 verify: rx_register_entries missing'; END IF;
    IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'public.rx_register_entries'::regclass) THEN
        RAISE EXCEPTION '141 verify: rx_register_entries RLS not enabled + forced';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'rx_stock_movements'::regclass AND tgname = 'trg_rx_sm_register'
                     AND tgdeferrable AND tginitdeferred) THEN
        RAISE EXCEPTION '141 verify: deferred register trigger on the ledger missing';
    END IF;
    IF to_regprocedure('rx_register_verify(uuid,text)') IS NULL THEN RAISE EXCEPTION '141 verify: rx_register_verify missing'; END IF;
END $$;
