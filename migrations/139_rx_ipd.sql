-- ============================================================================
-- Migration 139: Kriya Pharmacy OS inpatient pharmacy (RX-M6, RX-37)
-- Ward indents against active medication orders, pharmacy dispense that moves stock
-- and writes the IPD charge in one transaction, nurse receipt, MAR batch links
-- (RX-owned), narcotic empties, ward returns (charge void + re-issue, or a credit note
-- on an issued IPD bill); rx_purge_clinic v6.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §6, WORKFLOWS.md W4 / W5b, ADR-RX-04,
-- VERIFICATION D2 / D6 / D9 / D10 / D12.
-- Existing objects touched: ipd_charges_source_check is WIDENED (adds 'pharmacy', keeps
-- 116's 'package'). No trigger is added to any IPD table and no other IPD table is altered (D2, D9).
-- Every FK into an IPD table cascades (or sets null) like OPD orders / consents / OT do, so
-- ipd_purge_clinic and the 111–117 rollbacks never need to know about pharmacy rows. rx_stores gets
-- no ward FK: a ward sub-store would then block the IPD owner purge; the API checks the ward.
-- Rollback: rollback/139_rollback_rx_ipd.sql.
-- ============================================================================

-- ── 1. Widen ipd_charges.source (NOT VALID + VALIDATE: every existing row satisfies it) ──
ALTER TABLE ipd_charges DROP CONSTRAINT IF EXISTS ipd_charges_source_check;
ALTER TABLE ipd_charges ADD CONSTRAINT ipd_charges_source_check
    CHECK (source IN ('manual', 'bed_day', 'order', 'package', 'pharmacy')) NOT VALID;
ALTER TABLE ipd_charges VALIDATE CONSTRAINT ipd_charges_source_check;

-- ── 2. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_ipd_indents (            -- [F]
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    admission_id     UUID NOT NULL,
    ward_id          UUID NULL,                          -- the bed's ward when raised
    from_store_id    UUID NOT NULL,                      -- store asked to supply
    indent_year      SMALLINT NULL,                      -- assigned last (VERIFICATION D12)
    indent_seq       INTEGER NULL,
    indent_number    TEXT GENERATED ALWAYS AS (opd_format_number('IND-', indent_year, indent_seq, 5)) STORED,
    priority         TEXT NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine', 'urgent', 'stat')),
    status           TEXT NOT NULL DEFAULT 'raised'
                         CHECK (status IN ('raised', 'partially_dispensed', 'dispensed', 'cancelled', 'closed')),
    raised_by        UUID NULL,
    raised_by_name   TEXT NOT NULL CHECK (char_length(raised_by_name) BETWEEN 1 AND 100),
    raised_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    cancelled_reason TEXT NULL CHECK (char_length(btrim(cancelled_reason)) BETWEEN 3 AND 300),
    idempotency_key  TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    created_by       UUID NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_ipd_indents_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_ipd_indents_number_key UNIQUE (clinic_id, indent_year, indent_seq),
    CONSTRAINT rx_ipd_indents_cancel_reason CHECK (status <> 'cancelled' OR cancelled_reason IS NOT NULL),
    CONSTRAINT rx_ipd_indents_admission_fk FOREIGN KEY (clinic_id, admission_id) REFERENCES ipd_admissions (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_ipd_indents_ward_fk FOREIGN KEY (clinic_id, ward_id) REFERENCES ipd_wards (clinic_id, id)
        ON DELETE SET NULL (ward_id),
    CONSTRAINT rx_ipd_indents_store_fk FOREIGN KEY (clinic_id, from_store_id) REFERENCES rx_stores (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_ipd_indents_idem ON rx_ipd_indents (clinic_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rx_ipd_indents_queue ON rx_ipd_indents (clinic_id, status, priority, raised_at);
CREATE INDEX IF NOT EXISTS idx_rx_ipd_indents_admission ON rx_ipd_indents (clinic_id, admission_id, raised_at DESC);

CREATE TABLE IF NOT EXISTS rx_indent_lines (           -- [F]
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    indent_id       UUID NOT NULL,
    line_no         SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 100),
    ipd_order_id    UUID NOT NULL,
    item_id         UUID NULL,                          -- nurse's pick or pharmacist's mapping
    requested_units INTEGER NOT NULL CHECK (requested_units BETWEEN 1 AND 10000),
    dispensed_units INTEGER NOT NULL DEFAULT 0 CHECK (dispensed_units >= 0),
    returned_units  INTEGER NOT NULL DEFAULT 0 CHECK (returned_units >= 0),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'partial', 'done', 'cancelled', 'not_available')),
    CONSTRAINT rx_indent_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_indent_lines_line_key UNIQUE (indent_id, line_no),
    CONSTRAINT rx_indent_lines_dispensed_le_requested CHECK (dispensed_units <= requested_units),
    CONSTRAINT rx_indent_lines_returned_le_dispensed CHECK (returned_units <= dispensed_units),
    CONSTRAINT rx_indent_lines_indent_fk FOREIGN KEY (clinic_id, indent_id) REFERENCES rx_ipd_indents (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_indent_lines_order_fk FOREIGN KEY (clinic_id, ipd_order_id) REFERENCES ipd_orders (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_indent_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_indent_lines_order ON rx_indent_lines (clinic_id, ipd_order_id);

CREATE TABLE IF NOT EXISTS rx_dispenses (              -- [F]
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    indent_id         UUID NOT NULL,
    store_id          UUID NOT NULL,
    dispensed_by      UUID NULL,
    dispensed_by_name TEXT NOT NULL CHECK (char_length(dispensed_by_name) BETWEEN 1 AND 100),
    dispensed_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    received_by_nurse UUID NULL,
    received_by_name  TEXT NULL CHECK (char_length(received_by_name) <= 100),
    received_at       TIMESTAMPTZ NULL,
    idempotency_key   TEXT NULL CHECK (char_length(idempotency_key) BETWEEN 8 AND 80),
    CONSTRAINT rx_dispenses_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_dispenses_received_shape CHECK ((received_at IS NULL) = (received_by_name IS NULL)),
    CONSTRAINT rx_dispenses_indent_fk FOREIGN KEY (clinic_id, indent_id) REFERENCES rx_ipd_indents (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_dispenses_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_dispenses_idem ON rx_dispenses (clinic_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rx_dispenses_indent ON rx_dispenses (clinic_id, indent_id);

CREATE TABLE IF NOT EXISTS rx_dispense_lines (         -- [F]; only returned_units / ipd_charge_id move, in an rx_* function
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    dispense_id              UUID NOT NULL,
    indent_line_id           UUID NOT NULL,
    item_id                  UUID NOT NULL,
    batch_id                 UUID NOT NULL,
    qty_units                INTEGER NOT NULL CHECK (qty_units > 0),
    returned_units           INTEGER NOT NULL DEFAULT 0 CHECK (returned_units >= 0),
    substituted_from_item_id UUID NULL,
    substitution_reason      TEXT NULL CHECK (char_length(btrim(substitution_reason)) BETWEEN 3 AND 120),
    fefo_override_reason     TEXT NULL CHECK (char_length(btrim(fefo_override_reason)) BETWEEN 3 AND 120),
    unit_price_paise         INTEGER NOT NULL CHECK (unit_price_paise >= 0),   -- per base unit (ADR-RX-08 §IPD)
    ipd_charge_id            UUID NULL,                                        -- first live charge (source_ref rx:dl:<id>…)
    created_at               TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_dispense_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_dispense_lines_returned_le_qty CHECK (returned_units <= qty_units),
    CONSTRAINT rx_dispense_lines_subst_shape CHECK ((substituted_from_item_id IS NULL) = (substitution_reason IS NULL)),
    CONSTRAINT rx_dispense_lines_dispense_fk FOREIGN KEY (clinic_id, dispense_id) REFERENCES rx_dispenses (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_dispense_lines_indent_line_fk FOREIGN KEY (clinic_id, indent_line_id)
        REFERENCES rx_indent_lines (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT rx_dispense_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_dispense_lines_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_dispense_lines_charge_fk FOREIGN KEY (clinic_id, ipd_charge_id) REFERENCES ipd_charges (clinic_id, id)
        ON DELETE SET NULL (ipd_charge_id)
);
CREATE INDEX IF NOT EXISTS idx_rx_dispense_lines_dispense ON rx_dispense_lines (clinic_id, dispense_id);
CREATE INDEX IF NOT EXISTS idx_rx_dispense_lines_indent_line ON rx_dispense_lines (clinic_id, indent_line_id);
CREATE INDEX IF NOT EXISTS idx_rx_dispense_lines_batch ON rx_dispense_lines (clinic_id, batch_id);

CREATE TABLE IF NOT EXISTS rx_mar_links (              -- [A][F] dose → dispensed batch (replaces the v1 MAR column, D2)
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    mar_id           UUID NOT NULL,
    dispense_line_id UUID NOT NULL,
    created_by       UUID NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_mar_links_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_mar_links_mar_key UNIQUE (clinic_id, mar_id),
    -- ipd_purge_clinic deletes MAR rows: their links go with them, so the IPD purge needs no change
    CONSTRAINT rx_mar_links_mar_fk FOREIGN KEY (clinic_id, mar_id) REFERENCES ipd_med_administrations (clinic_id, id)
        ON DELETE CASCADE,
    CONSTRAINT rx_mar_links_line_fk FOREIGN KEY (clinic_id, dispense_line_id) REFERENCES rx_dispense_lines (clinic_id, id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rx_mar_links_line ON rx_mar_links (clinic_id, dispense_line_id);

CREATE TABLE IF NOT EXISTS rx_empty_returns (          -- [A][F] narcotic empties (ADVANCED-FEATURES A11)
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id        UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    dispense_line_id UUID NOT NULL,
    units            INTEGER NOT NULL CHECK (units > 0),
    returned_by      UUID NOT NULL,
    received_by      UUID NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_empty_returns_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_empty_returns_two_people CHECK (returned_by <> received_by),
    CONSTRAINT rx_empty_returns_line_fk FOREIGN KEY (clinic_id, dispense_line_id) REFERENCES rx_dispense_lines (clinic_id, id)
        ON DELETE CASCADE
);

-- ── 3. Guards ──────────────────────────────────────────────────────────────
-- Inserts only inside an rx_* function (rx_require_tx, 138).
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_ipd_indents', 'rx_indent_lines', 'rx_dispenses', 'rx_dispense_lines',
                             'rx_mar_links', 'rx_empty_returns']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_require_tx', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION rx_require_tx()',
                       'trg_' || t || '_require_tx', t);
    END LOOP;
END $$;

-- Never deleted outside an owner purge (RX's, or IPD's cascading into these rows); links and empties are
-- append-only; indents, lines and dispenses change only inside an rx_* function, and a dispense line's
-- identity (what left which batch, at what price) never changes.
CREATE OR REPLACE FUNCTION rx_guard_ipd_doc() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF rx_purging(OLD.clinic_id) OR ipd_purging(OLD.clinic_id) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'DELETE' OR TG_TABLE_NAME IN ('rx_mar_links', 'rx_empty_returns') THEN
        PERFORM rx_raise('rx_append_only:' || TG_TABLE_NAME);
    END IF;
    IF NOT rx_in_tx(OLD.clinic_id) THEN PERFORM rx_raise('rx_write_via_function_only'); END IF;
    IF TG_TABLE_NAME = 'rx_dispense_lines'
       AND (to_jsonb(NEW) - 'returned_units' - 'ipd_charge_id') IS DISTINCT FROM (to_jsonb(OLD) - 'returned_units' - 'ipd_charge_id') THEN
        PERFORM rx_raise('rx_append_only:rx_dispense_lines');
    END IF;
    IF TG_TABLE_NAME = 'rx_ipd_indents' THEN NEW.updated_at := clock_timestamp(); END IF;
    RETURN NEW;
END $$;
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_ipd_indents', 'rx_indent_lines', 'rx_dispenses', 'rx_dispense_lines', 'rx_mar_links',
                             'rx_empty_returns'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION rx_guard_ipd_doc()',
                       'trg_' || t || '_guard', t);
    END LOOP;
END $$;

-- An indent line names an ACTIVE MEDICATION order of the indent's own, still-admitted stay (111:559 logic).
CREATE OR REPLACE FUNCTION rx_check_indent_line() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE o RECORD; v_adm UUID; v_adm_status TEXT;
BEGIN
    SELECT admission_id INTO v_adm FROM rx_ipd_indents WHERE clinic_id = NEW.clinic_id AND id = NEW.indent_id;
    SELECT order_type, status, admission_id INTO o FROM ipd_orders WHERE clinic_id = NEW.clinic_id AND id = NEW.ipd_order_id;
    IF NOT FOUND OR o.admission_id IS DISTINCT FROM v_adm THEN PERFORM rx_raise('rx_order_not_found'); END IF;
    IF o.order_type <> 'medication' THEN PERFORM rx_raise('rx_order_not_medication'); END IF;
    IF o.status <> 'active' THEN PERFORM rx_raise('rx_order_not_active'); END IF;
    SELECT status INTO v_adm_status FROM ipd_admissions WHERE clinic_id = NEW.clinic_id AND id = v_adm;
    IF v_adm_status IS DISTINCT FROM 'admitted' AND v_adm_status IS DISTINCT FROM 'discharge_pending' THEN
        PERFORM rx_raise('rx_admission_not_active');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_check_indent_line ON rx_indent_lines;
CREATE TRIGGER trg_rx_check_indent_line BEFORE INSERT ON rx_indent_lines
    FOR EACH ROW EXECUTE FUNCTION rx_check_indent_line();

-- ── 4. Helpers ─────────────────────────────────────────────────────────────
-- The stay must still hold a bed. FOR SHARE: concurrent dispenses on one stay run side by side,
-- but each waits for (and blocks) ipd_generate_final_bill's FOR UPDATE (VERIFICATION D10).
CREATE OR REPLACE FUNCTION rx_lock_admission_active(p_clinic_id UUID, p_admission_id UUID) RETURNS ipd_admissions
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a ipd_admissions;
BEGIN
    SELECT * INTO a FROM ipd_admissions WHERE clinic_id = p_clinic_id AND id = p_admission_id FOR SHARE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_admission_not_found'); END IF;
    IF a.status NOT IN ('admitted', 'discharge_pending') THEN PERFORM rx_raise('rx_admission_not_active'); END IF;
    RETURN a;
END $$;

-- IPD price per base unit: MRP per unit (default, O-2) or landed cost + margin, never above MRP (DPCO).
CREATE OR REPLACE FUNCTION rx_ipd_unit_price(p_clinic_id UUID, p_batch_id UUID) RETURNS INTEGER
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN COALESCE(c.rx_settings ->> 'ipd_price_basis', 'mrp') = 'cost_plus'
                THEN LEAST(rx_unit_price(b.mrp_pack_paise, i.pack_size, 1),
                           (b.landed_unit_cost_paise_x100 * (10000 + COALESCE((c.rx_settings ->> 'ipd_cost_plus_bps')::INTEGER, 0))
                            + 500000) / 1000000)
                ELSE rx_unit_price(b.mrp_pack_paise, i.pack_size, 1) END::INTEGER
      FROM rx_batches b JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
      JOIN clinics c ON c.id = b.clinic_id
     WHERE b.clinic_id = p_clinic_id AND b.id = p_batch_id
$$;

-- Writes the pharmacy charge(s) for `p_units` of a dispense line. ipd_charges.quantity is 1..999, so a
-- larger quantity is split: source_ref <base>, <base>:p2, <base>:p3 … Returns the first charge id.
CREATE OR REPLACE FUNCTION rx_ipd_post_charge(p_clinic_id UUID, p_admission_id UUID, p_base_ref TEXT, p_units INTEGER,
                                              p_unit_price INTEGER, p_description TEXT, p_hsn TEXT, p_actor_id UUID,
                                              p_actor_name TEXT)
RETURNS UUID LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_left INTEGER := p_units; v_part INTEGER := 1; v_take INTEGER; v_id UUID; v_first UUID;
    v_post BOOLEAN := COALESCE((SELECT (rx_settings ->> 'ipd_autopost')::BOOLEAN FROM clinics WHERE id = p_clinic_id), true);
BEGIN
    WHILE v_left > 0 LOOP
        v_take := LEAST(v_left, 999);
        INSERT INTO ipd_charges (clinic_id, admission_id, category, description, service_date, quantity, unit_price_paise,
                                 source, source_ref, status, posted_at, posted_by, created_by, created_by_name, hsn_sac)
        VALUES (p_clinic_id, p_admission_id, 'pharmacy', left(p_description, 200), rx_ist_date(now()), v_take, p_unit_price,
                'pharmacy', p_base_ref || CASE WHEN v_part > 1 THEN ':p' || v_part ELSE '' END,
                CASE WHEN v_post THEN 'posted' ELSE 'proposed' END, CASE WHEN v_post THEN now() END,
                CASE WHEN v_post THEN p_actor_id END, p_actor_id, left(p_actor_name, 100), p_hsn)
        RETURNING id INTO v_id;
        v_first := COALESCE(v_first, v_id);
        v_left := v_left - v_take;
        v_part := v_part + 1;
    END LOOP;
    RETURN v_first;
END $$;

-- Indent status from its lines (never moves a cancelled / closed indent).
CREATE OR REPLACE FUNCTION rx_indent_refresh(p_clinic_id UUID, p_indent_id UUID) RETURNS TEXT
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_open BOOLEAN; v_any BOOLEAN; v_status TEXT;
BEGIN
    SELECT bool_or(status IN ('open', 'partial')), bool_or(dispensed_units > 0) INTO v_open, v_any
      FROM rx_indent_lines WHERE clinic_id = p_clinic_id AND indent_id = p_indent_id;
    UPDATE rx_ipd_indents SET status = CASE WHEN v_open AND v_any THEN 'partially_dispensed'
                                            WHEN v_open THEN 'raised'
                                            WHEN v_any THEN 'dispensed'
                                            ELSE 'cancelled' END,
                              cancelled_reason = CASE WHEN NOT v_open AND NOT v_any
                                                      THEN COALESCE(cancelled_reason, 'Nothing available to dispense')
                                                      ELSE cancelled_reason END
     WHERE clinic_id = p_clinic_id AND id = p_indent_id AND status NOT IN ('cancelled', 'closed')
    RETURNING status INTO v_status;
    RETURN v_status;
END $$;

-- ── 5. Raise an indent (W4 step 2) [F] ─────────────────────────────────────
-- p_lines: [{ipd_order_id, requested_units, item_id?}]. The number is the last statement (D12).
CREATE OR REPLACE FUNCTION rx_raise_indent(p_clinic_id UUID, p_admission_id UUID, p_store_id UUID, p_priority TEXT,
                                           p_lines JSONB, p_idem TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r rx_ipd_indents; e JSONB; v_id UUID := gen_random_uuid(); v_n INTEGER := 0; v_ward UUID;
    v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    IF p_idem IS NOT NULL THEN
        SELECT * INTO r FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND idempotency_key = p_idem;
        IF FOUND THEN RETURN jsonb_build_object('indent_id', r.id, 'indent_number', r.indent_number, 'replayed', true); END IF;
    END IF;
    PERFORM rx_lock_admission_active(p_clinic_id, p_admission_id);
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = p_store_id AND is_active) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    IF jsonb_array_length(COALESCE(p_lines, '[]'::jsonb)) = 0 THEN PERFORM rx_raise('rx_indent_empty'); END IF;
    SELECT ward_id INTO v_ward FROM ipd_bed_assignments
     WHERE clinic_id = p_clinic_id AND admission_id = p_admission_id AND ended_at IS NULL;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    INSERT INTO rx_ipd_indents (id, clinic_id, admission_id, ward_id, from_store_id, priority, raised_by, raised_by_name,
                                idempotency_key, created_by)
    VALUES (v_id, p_clinic_id, p_admission_id, v_ward, p_store_id, COALESCE(p_priority, 'routine'), p_actor_id,
            left(p_actor_name, 100), p_idem, p_actor_id);
    FOR e IN SELECT value FROM jsonb_array_elements(p_lines) LOOP
        v_n := v_n + 1;
        INSERT INTO rx_indent_lines (clinic_id, indent_id, line_no, ipd_order_id, item_id, requested_units)
        VALUES (p_clinic_id, v_id, v_n, (e ->> 'ipd_order_id')::UUID, NULLIF(e ->> 'item_id', '')::UUID,
                (e ->> 'requested_units')::INTEGER);
    END LOOP;
    UPDATE rx_ipd_indents SET indent_year = v_year, indent_seq = opd_next_counter(p_clinic_id, 'rx_ind:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = v_id RETURNING * INTO r;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('indent_id', r.id, 'indent_number', r.indent_number, 'lines', v_n, 'replayed', false);
END $$;

-- ── 6. Dispense (W4 step 4) [F] ────────────────────────────────────────────
-- p_lines: [{indent_line_id, item_id, batch_id, qty_units, substitution_reason?, fefo_override_reason?}] or
-- [{indent_line_id, not_available: true}]. Batches are picked (FEFO) by the caller; the ledger re-checks stock,
-- expiry and block. Lock order (VERIFICATION §6): admission (share) → indent → lines → movements by batch.
CREATE OR REPLACE FUNCTION rx_dispense_indent(p_clinic_id UUID, p_indent_id UUID, p_store_id UUID, p_lines JSONB,
                                              p_actor_id UUID, p_actor_name TEXT, p_idem TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE ind rx_ipd_indents; d rx_dispenses; e JSONB; l rx_indent_lines; it rx_items; b rx_batches; v_order TEXT;
    v_dispense UUID := gen_random_uuid(); v_dl UUID; v_price INTEGER; v_charge UUID; v_total BIGINT := 0; v_n INTEGER := 0;
    v_qty INTEGER;
BEGIN
    IF p_idem IS NOT NULL THEN
        SELECT * INTO d FROM rx_dispenses WHERE clinic_id = p_clinic_id AND idempotency_key = p_idem;
        IF FOUND THEN RETURN jsonb_build_object('dispense_id', d.id, 'replayed', true); END IF;
    END IF;
    SELECT * INTO ind FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = p_indent_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_indent_not_found'); END IF;
    -- 1. the stay must still hold a bed (share lock, D10)
    PERFORM rx_lock_admission_active(p_clinic_id, ind.admission_id);
    -- 2. the indent, then its lines
    SELECT * INTO ind FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = p_indent_id FOR UPDATE;
    IF p_idem IS NOT NULL THEN          -- a concurrent retry waited on the lock above
        SELECT * INTO d FROM rx_dispenses WHERE clinic_id = p_clinic_id AND idempotency_key = p_idem;
        IF FOUND THEN RETURN jsonb_build_object('dispense_id', d.id, 'replayed', true); END IF;
    END IF;
    IF ind.status NOT IN ('raised', 'partially_dispensed') THEN PERFORM rx_raise('rx_indent_not_open'); END IF;
    PERFORM 1 FROM rx_indent_lines WHERE clinic_id = p_clinic_id AND indent_id = p_indent_id ORDER BY id FOR UPDATE;
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = p_store_id AND is_active) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    IF jsonb_array_length(COALESCE(p_lines, '[]'::jsonb)) = 0 THEN PERFORM rx_raise('rx_indent_empty'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    INSERT INTO rx_dispenses (id, clinic_id, indent_id, store_id, dispensed_by, dispensed_by_name, idempotency_key)
    VALUES (v_dispense, p_clinic_id, p_indent_id, p_store_id, p_actor_id, left(p_actor_name, 100), p_idem);
    FOR e IN SELECT value FROM jsonb_array_elements(p_lines) ORDER BY value ->> 'batch_id' NULLS FIRST, value ->> 'indent_line_id' LOOP
        SELECT * INTO l FROM rx_indent_lines WHERE clinic_id = p_clinic_id AND indent_id = p_indent_id
                                               AND id = (e ->> 'indent_line_id')::UUID;
        IF NOT FOUND THEN PERFORM rx_raise('rx_indent_line_not_found'); END IF;
        IF l.status NOT IN ('open', 'partial') THEN PERFORM rx_raise('rx_indent_line_closed'); END IF;
        IF COALESCE((e ->> 'not_available')::BOOLEAN, false) THEN
            UPDATE rx_indent_lines SET status = 'not_available' WHERE clinic_id = p_clinic_id AND id = l.id;
            CONTINUE;
        END IF;
        -- 3. the doctor's order is re-read under lock: no trigger on ipd_orders (D9)
        SELECT status INTO v_order FROM ipd_orders WHERE clinic_id = p_clinic_id AND id = l.ipd_order_id;
        IF v_order IS DISTINCT FROM 'active' THEN PERFORM rx_raise('rx_order_not_active'); END IF;
        v_qty := COALESCE((e ->> 'qty_units')::INTEGER, 0);
        IF v_qty <= 0 THEN PERFORM rx_raise('rx_qty_invalid'); END IF;
        SELECT * INTO it FROM rx_items WHERE clinic_id = p_clinic_id AND id = (e ->> 'item_id')::UUID;
        IF NOT FOUND THEN PERFORM rx_raise('rx_item_not_found'); END IF;
        SELECT * INTO b FROM rx_batches WHERE clinic_id = p_clinic_id AND id = (e ->> 'batch_id')::UUID;
        IF NOT FOUND OR b.item_id <> it.id THEN PERFORM rx_raise('rx_batch_not_found'); END IF;
        -- a different item than the nurse / earlier mapping named is a substitution: same generic + a reason
        IF l.item_id IS NOT NULL AND l.item_id <> it.id THEN
            IF NOT EXISTS (SELECT 1 FROM rx_items o WHERE o.clinic_id = p_clinic_id AND o.id = l.item_id
                              AND o.generic_id IS NOT NULL AND o.generic_id = it.generic_id) THEN
                PERFORM rx_raise('rx_substitute_not_same_generic');
            END IF;
            IF char_length(btrim(COALESCE(e ->> 'substitution_reason', ''))) < 3 THEN
                PERFORM rx_raise('rx_substitution_reason_required');
            END IF;
        END IF;
        -- 4. price from the batch (client prices are never read), stock leaves, the charge is written
        v_price := rx_ipd_unit_price(p_clinic_id, b.id);
        v_dl := gen_random_uuid();
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name)
        VALUES (p_clinic_id, p_store_id, b.id, it.id, -v_qty, 'ipd_dispense_out', 'rx_dispense_lines', v_dl,
                b.landed_unit_cost_paise_x100, p_actor_id, p_actor_name);
        -- 5. IPD charge (source 'pharmacy', posted per ipd_autopost); retries are idempotent by key + source_ref
        v_charge := rx_ipd_post_charge(p_clinic_id, ind.admission_id, 'rx:dl:' || v_dl, v_qty, v_price,
                                       it.brand_name || ' (batch ' || b.batch_no || ')', it.hsn_code, p_actor_id, p_actor_name);
        INSERT INTO rx_dispense_lines (id, clinic_id, dispense_id, indent_line_id, item_id, batch_id, qty_units,
                                       substituted_from_item_id, substitution_reason, fefo_override_reason,
                                       unit_price_paise, ipd_charge_id)
        VALUES (v_dl, p_clinic_id, v_dispense, l.id, it.id, b.id, v_qty,
                CASE WHEN l.item_id IS NOT NULL AND l.item_id <> it.id THEN l.item_id END,
                CASE WHEN l.item_id IS NOT NULL AND l.item_id <> it.id THEN btrim(e ->> 'substitution_reason') END,
                NULLIF(btrim(e ->> 'fefo_override_reason'), ''), v_price, v_charge);
        -- 6. line progress (rx_indent_lines_dispensed_le_requested caps an over-dispense)
        UPDATE rx_indent_lines SET dispensed_units = dispensed_units + v_qty, item_id = COALESCE(item_id, it.id),
               status = CASE WHEN dispensed_units + v_qty >= requested_units THEN 'done' ELSE 'partial' END
         WHERE clinic_id = p_clinic_id AND id = l.id;
        v_total := v_total + v_qty::BIGINT * v_price;
        v_n := v_n + 1;
    END LOOP;
    PERFORM rx_indent_refresh(p_clinic_id, p_indent_id);
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('dispense_id', v_dispense, 'lines', v_n, 'total_paise', v_total, 'replayed', false,
        'final_bill_exists', EXISTS (SELECT 1 FROM opd_invoices WHERE clinic_id = p_clinic_id
                                        AND admission_id = ind.admission_id AND status <> 'void'));
END $$;

-- ── 7. Cancel undispensed lines (nurse or pharmacist) [F] ──────────────────
CREATE OR REPLACE FUNCTION rx_cancel_indent(p_clinic_id UUID, p_indent_id UUID, p_reason TEXT, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE ind rx_ipd_indents; v_n INTEGER;
BEGIN
    SELECT * INTO ind FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = p_indent_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_indent_not_found'); END IF;
    IF ind.status NOT IN ('raised', 'partially_dispensed') THEN PERFORM rx_raise('rx_indent_not_open'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_indent_lines SET status = 'cancelled'
     WHERE clinic_id = p_clinic_id AND indent_id = p_indent_id AND status IN ('open', 'partial');
    GET DIAGNOSTICS v_n = ROW_COUNT;
    UPDATE rx_ipd_indents SET cancelled_reason = left(btrim(p_reason), 300) WHERE clinic_id = p_clinic_id AND id = p_indent_id;
    PERFORM rx_indent_refresh(p_clinic_id, p_indent_id);
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('indent_id', p_indent_id, 'lines_cancelled', v_n,
                              'status', (SELECT status FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = p_indent_id));
END $$;

-- ── 8. Nurse acknowledges receipt (W4 step 5) [F] ──────────────────────────
CREATE OR REPLACE FUNCTION rx_receive_dispense(p_clinic_id UUID, p_dispense_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE d rx_dispenses; ind rx_ipd_indents;
BEGIN
    SELECT * INTO d FROM rx_dispenses WHERE clinic_id = p_clinic_id AND id = p_dispense_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_dispense_not_found'); END IF;
    SELECT * INTO ind FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = d.indent_id FOR UPDATE;
    SELECT * INTO d FROM rx_dispenses WHERE clinic_id = p_clinic_id AND id = p_dispense_id FOR UPDATE;
    IF d.received_at IS NOT NULL THEN
        RETURN jsonb_build_object('dispense_id', d.id, 'received_at', d.received_at, 'indent_status', ind.status,
                                  'replayed', true);
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_dispenses SET received_by_nurse = p_actor_id, received_by_name = left(p_actor_name, 100),
           received_at = clock_timestamp()
     WHERE clinic_id = p_clinic_id AND id = d.id RETURNING * INTO d;
    -- every line settled and every delivery acknowledged → closed
    IF ind.status = 'dispensed' AND NOT EXISTS (SELECT 1 FROM rx_dispenses WHERE clinic_id = p_clinic_id
                                                  AND indent_id = ind.id AND received_at IS NULL) THEN
        UPDATE rx_ipd_indents SET status = 'closed' WHERE clinic_id = p_clinic_id AND id = ind.id RETURNING * INTO ind;
    END IF;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('dispense_id', d.id, 'received_at', d.received_at, 'indent_status', ind.status,
                              'replayed', false);
END $$;

-- ── 9. MAR batch link (W4 step 6) [F] ──────────────────────────────────────
-- Called after the unchanged MAR insert. The dispense line must be for the same order (and so the same stay).
CREATE OR REPLACE FUNCTION rx_link_mar(p_clinic_id UUID, p_mar_id UUID, p_dispense_line_id UUID, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE m RECORD; v_order UUID; v_adm UUID; v_existing UUID;
BEGIN
    SELECT order_id, admission_id INTO m FROM ipd_med_administrations WHERE clinic_id = p_clinic_id AND id = p_mar_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_mar_not_found'); END IF;
    SELECT il.ipd_order_id, i.admission_id INTO v_order, v_adm
      FROM rx_dispense_lines dl
      JOIN rx_indent_lines il ON il.clinic_id = dl.clinic_id AND il.id = dl.indent_line_id
      JOIN rx_ipd_indents i ON i.clinic_id = il.clinic_id AND i.id = il.indent_id
     WHERE dl.clinic_id = p_clinic_id AND dl.id = p_dispense_line_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_dispense_line_not_found'); END IF;
    IF v_order <> m.order_id OR v_adm <> m.admission_id THEN PERFORM rx_raise('rx_mar_line_mismatch'); END IF;
    SELECT dispense_line_id INTO v_existing FROM rx_mar_links WHERE clinic_id = p_clinic_id AND mar_id = p_mar_id;
    IF FOUND THEN
        IF v_existing <> p_dispense_line_id THEN PERFORM rx_raise('rx_mar_already_linked'); END IF;
        RETURN jsonb_build_object('mar_id', p_mar_id, 'dispense_line_id', v_existing, 'replayed', true);
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    INSERT INTO rx_mar_links (clinic_id, mar_id, dispense_line_id, created_by)
    VALUES (p_clinic_id, p_mar_id, p_dispense_line_id, p_actor_id);
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('mar_id', p_mar_id, 'dispense_line_id', p_dispense_line_id, 'replayed', false);
END $$;

-- ── 10. Ward return (W5b) [F] ──────────────────────────────────────────────
-- p_lines: [{dispense_line_id, qty_units, restock}]. Stock comes back (ipd_return_in; opened / expired goes
-- straight out again as a write-off). The bill is never edited: an unbilled charge is voided and re-issued
-- for the units kept (source_ref rx:dl:<id>:r:<return id>); a charge already on an issued IPD bill gets a credit note.
-- p_return_id makes a retry a replay (the same id names every movement of one return).
CREATE OR REPLACE FUNCTION rx_ipd_return(p_clinic_id UUID, p_dispense_id UUID, p_lines JSONB, p_reason TEXT,
                                         p_return_id UUID, p_actor_id UUID, p_actor_name TEXT, p_is_admin BOOLEAN)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE d rx_dispenses; ind rx_ipd_indents; e JSONB; dl rx_dispense_lines; it rx_items; b rx_batches; c RECORD;
    v_qty INTEGER; v_keep INTEGER; v_value BIGINT; v_total BIGINT := 0; v_credit BIGINT := 0; v_inv UUID;
    v_cn JSONB; v_cns JSONB := '[]'::jsonb; v_today DATE := rx_ist_date(now()); v_n INTEGER := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = p_clinic_id AND ref_table = 'rx_ipd_returns:' || p_return_id) THEN
        RETURN jsonb_build_object('return_id', p_return_id, 'replayed', true);
    END IF;
    SELECT * INTO d FROM rx_dispenses WHERE clinic_id = p_clinic_id AND id = p_dispense_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_dispense_not_found'); END IF;
    SELECT * INTO ind FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = d.indent_id;
    PERFORM 1 FROM ipd_admissions WHERE clinic_id = p_clinic_id AND id = ind.admission_id FOR SHARE;
    PERFORM 1 FROM rx_ipd_indents WHERE clinic_id = p_clinic_id AND id = ind.id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = p_clinic_id AND ref_table = 'rx_ipd_returns:' || p_return_id) THEN
        RETURN jsonb_build_object('return_id', p_return_id, 'replayed', true);
    END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    IF jsonb_array_length(COALESCE(p_lines, '[]'::jsonb)) = 0 THEN PERFORM rx_raise('rx_return_empty'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR e IN SELECT value FROM jsonb_array_elements(p_lines) ORDER BY value ->> 'dispense_line_id' LOOP
        SELECT * INTO dl FROM rx_dispense_lines WHERE clinic_id = p_clinic_id AND dispense_id = p_dispense_id
                                                  AND id = (e ->> 'dispense_line_id')::UUID FOR UPDATE;
        IF NOT FOUND THEN PERFORM rx_raise('rx_dispense_line_not_found'); END IF;
        v_qty := COALESCE((e ->> 'qty_units')::INTEGER, 0);
        IF v_qty <= 0 OR dl.returned_units + v_qty > dl.qty_units THEN PERFORM rx_raise('rx_return_exceeds_dispensed'); END IF;
        SELECT * INTO it FROM rx_items WHERE clinic_id = p_clinic_id AND id = dl.item_id;
        SELECT * INTO b FROM rx_batches WHERE clinic_id = p_clinic_id AND id = dl.batch_id;
        IF COALESCE((e ->> 'restock')::BOOLEAN, false) AND it.schedule IN ('H1', 'X', 'narcotic', 'psychotropic')
           AND NOT p_is_admin THEN
            PERFORM rx_raise('rx_restock_needs_admin');
        END IF;
        -- stock back to the dispensing store and batch; opened or expired leaves again as a write-off
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, d.store_id, b.id, it.id, v_qty, 'ipd_return_in', 'rx_ipd_returns:' || p_return_id, dl.id,
                b.landed_unit_cost_paise_x100, p_actor_id, p_actor_name, left('Ward return: ' || btrim(p_reason), 300));
        IF NOT COALESCE((e ->> 'restock')::BOOLEAN, false) OR b.expiry_date < v_today THEN
            INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                            ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
            VALUES (p_clinic_id, d.store_id, b.id, it.id, -v_qty,
                    CASE WHEN b.expiry_date < v_today THEN 'expiry_writeoff_out' ELSE 'damage_writeoff_out' END,
                    'rx_ipd_returns:' || p_return_id, dl.id, b.landed_unit_cost_paise_x100, p_actor_id, p_actor_name,
                    CASE WHEN b.expiry_date < v_today THEN 'Returned after expiry' ELSE 'Returned opened / not resaleable' END);
        END IF;
        UPDATE rx_dispense_lines SET returned_units = returned_units + v_qty WHERE clinic_id = p_clinic_id AND id = dl.id
        RETURNING * INTO dl;
        UPDATE rx_indent_lines SET returned_units = returned_units + v_qty
         WHERE clinic_id = p_clinic_id AND id = dl.indent_line_id;
        v_value := v_qty::BIGINT * dl.unit_price_paise;
        v_total := v_total + v_value;
        -- the bill side: credit note on an issued bill, else void + re-issue for the units kept
        v_inv := NULL;
        SELECT ch.invoice_id INTO v_inv FROM ipd_charges ch
          JOIN opd_invoices inv ON inv.clinic_id = ch.clinic_id AND inv.id = ch.invoice_id AND inv.status <> 'void'
         WHERE ch.clinic_id = p_clinic_id AND ch.admission_id = ind.admission_id AND ch.status <> 'void'
           AND (ch.source_ref = 'rx:dl:' || dl.id OR ch.source_ref LIKE 'rx:dl:' || dl.id || ':%')
         LIMIT 1;
        IF v_inv IS NOT NULL THEN
            IF v_value > 0 THEN
                v_cn := opd_issue_credit_note(p_clinic_id, v_inv, v_value::INTEGER, 'other',
                                              left('Pharmacy ward return: ' || v_qty || ' x ' || it.brand_name, 300),
                                              'rx-ret-' || p_return_id || '-' || dl.id, NULL, NULL, NULL, p_actor_id,
                                              p_actor_name);
                v_cns := v_cns || jsonb_build_array(v_cn ->> 'credit_note_number');
                v_credit := v_credit + v_value;
            END IF;
        ELSE
            FOR c IN SELECT id FROM ipd_charges
                      WHERE clinic_id = p_clinic_id AND admission_id = ind.admission_id AND status <> 'void'
                        AND (source_ref = 'rx:dl:' || dl.id OR source_ref LIKE 'rx:dl:' || dl.id || ':%') ORDER BY id LOOP
                PERFORM ipd_void_charge(p_clinic_id, c.id, left('Returned ' || v_qty || ' units to pharmacy', 300),
                                        p_actor_id, p_actor_name);
            END LOOP;
            v_keep := dl.qty_units - dl.returned_units;
            UPDATE rx_dispense_lines SET ipd_charge_id = CASE WHEN v_keep > 0 THEN
                       rx_ipd_post_charge(p_clinic_id, ind.admission_id, 'rx:dl:' || dl.id || ':r:' || p_return_id, v_keep,
                                          dl.unit_price_paise, it.brand_name || ' (batch ' || b.batch_no || ')',
                                          it.hsn_code, p_actor_id, p_actor_name) END
             WHERE clinic_id = p_clinic_id AND id = dl.id;
        END IF;
        v_n := v_n + 1;
    END LOOP;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('return_id', p_return_id, 'lines', v_n, 'value_paise', v_total,
                              'credited_paise', v_credit, 'credit_notes', v_cns, 'replayed', false);
END $$;

-- ── 11. Owner purge v6 ─────────────────────────────────────────────────────
-- ipd_charges rows with source 'pharmacy' stay: they belong to the patient bill and go with ipd_purge_clinic.
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- ── 12. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_ipd_indents', 'rx_indent_lines', 'rx_dispenses', 'rx_dispense_lines', 'rx_mar_links',
                             'rx_empty_returns']
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
    FOREACH f IN ARRAY ARRAY['rx_lock_admission_active(uuid,uuid)', 'rx_ipd_unit_price(uuid,uuid)',
                             'rx_ipd_post_charge(uuid,uuid,text,integer,integer,text,text,uuid,text)',
                             'rx_indent_refresh(uuid,uuid)',
                             'rx_raise_indent(uuid,uuid,uuid,text,jsonb,text,uuid,text)',
                             'rx_dispense_indent(uuid,uuid,uuid,jsonb,uuid,text,text)',
                             'rx_cancel_indent(uuid,uuid,text,uuid)', 'rx_receive_dispense(uuid,uuid,uuid,text)',
                             'rx_link_mar(uuid,uuid,uuid,uuid)',
                             'rx_ipd_return(uuid,uuid,jsonb,text,uuid,uuid,text,boolean)', 'rx_purge_clinic(uuid)']
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

-- ── 13. Verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_ipd_indents', 'rx_indent_lines', 'rx_dispenses', 'rx_dispense_lines', 'rx_mar_links',
                             'rx_empty_returns'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '139 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '139 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ipd_charges_source_check' AND convalidated
                      AND pg_get_constraintdef(oid) LIKE '%pharmacy%' AND pg_get_constraintdef(oid) LIKE '%package%') THEN
        RAISE EXCEPTION '139 verify: ipd_charges.source not widened';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                WHERE t.tgrelid IN ('ipd_charges'::regclass, 'ipd_orders'::regclass, 'ipd_med_administrations'::regclass,
                                    'ipd_admissions'::regclass) AND p.proname LIKE 'rx\_%') THEN
        RAISE EXCEPTION '139 verify: an rx trigger sits on an IPD table (VERIFICATION D2/D9)';
    END IF;
END $$;
