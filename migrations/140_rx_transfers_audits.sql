-- ============================================================================
-- Migration 140: Kriya Pharmacy OS transfers, physical stock audits, ward par levels (RX-M7, RX-41, RX-78)
-- Inter-store transfers (draft → dispatch → receive; short units written off against the source;
-- controlled lines two-person), stock audits (snapshot → count → review → post as adjust movements,
-- concurrent-movement delta, optional sales freeze, second approver over the value threshold),
-- ward par levels for replenishment drafts; rx_purge_clinic v7.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §7, WORKFLOWS.md W7 / W8, ADVANCED-FEATURES.md A10.
-- Existing objects touched: two triggers added on rx_stock_movements (RX's own ledger). No OPD / IPD object.
-- Rollback: rollback/140_rollback_rx_transfers_audits.sql.
-- ============================================================================

-- ── 1. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_transfers (               -- [F]
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    from_store_id     UUID NOT NULL,
    to_store_id       UUID NOT NULL,
    transfer_year     SMALLINT NULL,                     -- assigned at dispatch, last (VERIFICATION D12)
    transfer_seq      INTEGER NULL,
    transfer_number   TEXT GENERATED ALWAYS AS (opd_format_number('STR-', transfer_year, transfer_seq, 5)) STORED,
    status            TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'dispatched', 'received', 'partially_received', 'cancelled')),
    source            TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'par')),
    has_controlled    BOOLEAN NOT NULL DEFAULT false,   -- X / narcotic / psychotropic line present (set at dispatch)
    notes             TEXT NULL CHECK (char_length(notes) <= 300),
    created_by        UUID NULL,
    created_by_name   TEXT NULL CHECK (char_length(created_by_name) <= 100),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    dispatched_by     UUID NULL,
    dispatched_by_name TEXT NULL CHECK (char_length(dispatched_by_name) <= 100),
    dispatched_at     TIMESTAMPTZ NULL,
    received_by       UUID NULL,
    received_by_name  TEXT NULL CHECK (char_length(received_by_name) <= 100),
    received_at       TIMESTAMPTZ NULL,
    cancelled_reason  TEXT NULL CHECK (char_length(btrim(cancelled_reason)) BETWEEN 3 AND 300),
    CONSTRAINT rx_transfers_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_transfers_number_key UNIQUE (clinic_id, transfer_year, transfer_seq),
    CONSTRAINT rx_transfers_distinct_stores CHECK (from_store_id <> to_store_id),
    CONSTRAINT rx_transfers_dispatched_shape CHECK (status IN ('draft', 'cancelled') OR (dispatched_at IS NOT NULL
                                                                                         AND transfer_seq IS NOT NULL)),
    CONSTRAINT rx_transfers_received_shape CHECK (status NOT IN ('received', 'partially_received') OR received_at IS NOT NULL),
    -- W7 step 3: a controlled transfer is dispatched and received by two different, identified people
    CONSTRAINT rx_transfers_controlled_two_person CHECK (NOT has_controlled OR (dispatched_by IS NOT NULL
                                                         AND (received_by IS NULL OR received_by <> dispatched_by))),
    CONSTRAINT rx_transfers_cancel_reason CHECK (status <> 'cancelled' OR cancelled_reason IS NOT NULL),
    CONSTRAINT rx_transfers_from_fk FOREIGN KEY (clinic_id, from_store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_transfers_to_fk FOREIGN KEY (clinic_id, to_store_id) REFERENCES rx_stores (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_transfers_to ON rx_transfers (clinic_id, to_store_id, status);
CREATE INDEX IF NOT EXISTS idx_rx_transfers_from ON rx_transfers (clinic_id, from_store_id, status);

CREATE TABLE IF NOT EXISTS rx_transfer_lines (          -- [F]
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    transfer_id        UUID NOT NULL,
    item_id            UUID NOT NULL,
    batch_id           UUID NOT NULL,
    sent_units         INTEGER NOT NULL CHECK (sent_units > 0 AND sent_units <= 100000),
    received_units     INTEGER NULL,
    discrepancy_reason TEXT NULL CHECK (char_length(btrim(discrepancy_reason)) BETWEEN 3 AND 300),
    CONSTRAINT rx_transfer_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_transfer_lines_batch_key UNIQUE (transfer_id, batch_id),
    CONSTRAINT rx_transfer_lines_received_range CHECK (received_units BETWEEN 0 AND sent_units),
    CONSTRAINT rx_transfer_lines_short_reason CHECK (received_units IS NULL OR received_units = sent_units
                                                     OR discrepancy_reason IS NOT NULL),
    CONSTRAINT rx_transfer_lines_transfer_fk FOREIGN KEY (clinic_id, transfer_id) REFERENCES rx_transfers (clinic_id, id),
    CONSTRAINT rx_transfer_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_transfer_lines_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_transfer_lines_transfer ON rx_transfer_lines (clinic_id, transfer_id);

CREATE TABLE IF NOT EXISTS rx_stock_audits (            -- [F]
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id        UUID NOT NULL,
    audit_year      SMALLINT NULL,                       -- assigned last
    audit_seq       INTEGER NULL,
    audit_number    TEXT GENERATED ALWAYS AS (opd_format_number('AUD-', audit_year, audit_seq, 5)) STORED,
    scope           TEXT NOT NULL CHECK (scope IN ('full', 'rack', 'item_list', 'controlled')),
    rack            TEXT NULL CHECK (char_length(rack) <= 40),
    item_ids        UUID[] NULL,
    status          TEXT NOT NULL DEFAULT 'counting' CHECK (status IN ('counting', 'review', 'posted', 'cancelled')),
    freeze_sales    BOOLEAN NOT NULL DEFAULT false,
    started_by      UUID NULL,
    started_by_name TEXT NOT NULL CHECK (char_length(started_by_name) BETWEEN 1 AND 100),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reviewed_at     TIMESTAMPTZ NULL,
    posted_by       UUID NULL,
    posted_by_name  TEXT NULL CHECK (char_length(posted_by_name) <= 100),
    posted_at       TIMESTAMPTZ NULL,
    approved_by     UUID NULL,                           -- second RX_ADMIN over the value threshold
    approved_by_name TEXT NULL CHECK (char_length(approved_by_name) <= 100),
    variance_value_paise BIGINT NULL,                    -- Σ |variance| × landed unit cost, fixed at posting
    cancelled_reason TEXT NULL CHECK (char_length(btrim(cancelled_reason)) BETWEEN 3 AND 300),
    CONSTRAINT rx_stock_audits_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_stock_audits_number_key UNIQUE (clinic_id, audit_year, audit_seq),
    CONSTRAINT rx_stock_audits_scope_shape CHECK ((scope = 'rack') = (rack IS NOT NULL)
                                                  AND (scope = 'item_list') = (item_ids IS NOT NULL)),
    CONSTRAINT rx_stock_audits_second_person CHECK (approved_by IS NULL OR approved_by IS DISTINCT FROM started_by),
    CONSTRAINT rx_stock_audits_posted_shape CHECK (status <> 'posted' OR posted_at IS NOT NULL),
    CONSTRAINT rx_stock_audits_cancel_reason CHECK (status <> 'cancelled' OR cancelled_reason IS NOT NULL),
    CONSTRAINT rx_stock_audits_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id)
);
-- one open audit per store; also the index the movement triggers probe (one probe when nothing is open)
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_stock_audits_open ON rx_stock_audits (clinic_id, store_id)
    WHERE status IN ('counting', 'review');

CREATE TABLE IF NOT EXISTS rx_stock_audit_lines (       -- [F]
    id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    audit_id                   UUID NOT NULL,
    batch_id                   UUID NOT NULL,
    item_id                    UUID NOT NULL,
    system_units_at_start      INTEGER NOT NULL CHECK (system_units_at_start >= 0),
    counted_units              INTEGER NULL CHECK (counted_units >= 0 AND counted_units <= 1000000),
    movement_delta_since_start INTEGER NOT NULL DEFAULT 0,      -- movements of this batch here while open
    variance_units             INTEGER GENERATED ALWAYS AS
                                   (counted_units - system_units_at_start - movement_delta_since_start) STORED,
    variance_reason            TEXT NULL CHECK (variance_reason IN ('breakage', 'theft_suspected', 'billing_error',
                                                                    'found', 'expiry_missed', 'other')),
    counted_at                 TIMESTAMPTZ NULL,
    CONSTRAINT rx_stock_audit_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_stock_audit_lines_batch_key UNIQUE (audit_id, batch_id),
    CONSTRAINT rx_stock_audit_lines_audit_fk FOREIGN KEY (clinic_id, audit_id) REFERENCES rx_stock_audits (clinic_id, id),
    CONSTRAINT rx_stock_audit_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_stock_audit_lines_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_stock_audit_lines_batch ON rx_stock_audit_lines (clinic_id, batch_id);

CREATE TABLE IF NOT EXISTS rx_par_levels (              -- A10
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id   UUID NOT NULL,
    item_id    UUID NOT NULL,
    par_units  INTEGER NOT NULL CHECK (par_units > 0 AND par_units <= 100000),
    updated_by UUID NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clinic_id, store_id, item_id),
    CONSTRAINT rx_par_levels_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_par_levels_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

-- ── 2. Guards ──────────────────────────────────────────────────────────────
-- Transfers and audits are written only inside an rx_* function (inserts: rx_require_tx, 138);
-- they are never changed or deleted outside one, nor outside the owner purge.
CREATE OR REPLACE FUNCTION rx_guard_fn_only() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF rx_purging(OLD.clinic_id) OR rx_in_tx(OLD.clinic_id) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    PERFORM rx_raise('rx_write_via_function_only');
    RETURN NULL;
END $$;
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_transfers', 'rx_transfer_lines', 'rx_stock_audits', 'rx_stock_audit_lines'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_require_tx', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION rx_require_tx()',
                       'trg_' || t || '_require_tx', t);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || t || '_guard', t);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION rx_guard_fn_only()',
                       'trg_' || t || '_guard', t);
    END LOOP;
END $$;

-- W8: while an audit of a store is open, a store frozen for it sells / dispenses / issues nothing.
CREATE OR REPLACE FUNCTION rx_sm_audit_freeze() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.movement_type IN ('sale_out', 'ipd_dispense_out', 'ward_issue_out') AND EXISTS (
           SELECT 1 FROM rx_stock_audits WHERE clinic_id = NEW.clinic_id AND store_id = NEW.store_id
              AND status IN ('counting', 'review') AND freeze_sales) THEN
        PERFORM rx_raise('rx_store_frozen_for_audit');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sm_audit_freeze ON rx_stock_movements;
CREATE TRIGGER trg_rx_sm_audit_freeze BEFORE INSERT ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_sm_audit_freeze();

-- W8 step 2: movements of an audited batch while the audit is open accumulate on its line, so the variance
-- compares the count with the stock at the moment of posting, not at the start.
CREATE OR REPLACE FUNCTION rx_sm_audit_delta() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    UPDATE rx_stock_audit_lines l SET movement_delta_since_start = l.movement_delta_since_start + NEW.qty_units
      FROM rx_stock_audits a
     WHERE a.clinic_id = NEW.clinic_id AND a.store_id = NEW.store_id AND a.status IN ('counting', 'review')
       AND l.clinic_id = a.clinic_id AND l.audit_id = a.id AND l.batch_id = NEW.batch_id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sm_audit_delta ON rx_stock_movements;
CREATE TRIGGER trg_rx_sm_audit_delta AFTER INSERT ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_sm_audit_delta();

-- ── 3. Transfers (W7) [F] ──────────────────────────────────────────────────
-- Create (p_transfer_id NULL) or replace a draft. p_lines: [{item_id, batch_id, sent_units}].
CREATE OR REPLACE FUNCTION rx_save_transfer(p_clinic_id UUID, p_transfer_id UUID, p_from UUID, p_to UUID, p_lines JSONB,
                                            p_notes TEXT, p_source TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE t rx_transfers; e JSONB; v_id UUID := COALESCE(p_transfer_id, gen_random_uuid()); v_n INTEGER := 0;
BEGIN
    IF jsonb_array_length(COALESCE(p_lines, '[]'::jsonb)) NOT BETWEEN 1 AND 200 THEN PERFORM rx_raise('rx_transfer_empty'); END IF;
    IF p_from = p_to THEN PERFORM rx_raise('rx_transfer_same_store'); END IF;
    IF (SELECT count(*) FROM rx_stores WHERE clinic_id = p_clinic_id AND id IN (p_from, p_to) AND is_active) <> 2 THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    IF p_transfer_id IS NOT NULL THEN
        SELECT * INTO t FROM rx_transfers WHERE clinic_id = p_clinic_id AND id = p_transfer_id FOR UPDATE;
        IF NOT FOUND THEN PERFORM rx_raise('rx_transfer_not_found'); END IF;
        IF t.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
        UPDATE rx_transfers SET from_store_id = p_from, to_store_id = p_to, notes = NULLIF(btrim(p_notes), '')
         WHERE clinic_id = p_clinic_id AND id = p_transfer_id;
        DELETE FROM rx_transfer_lines WHERE clinic_id = p_clinic_id AND transfer_id = p_transfer_id;
    ELSE
        INSERT INTO rx_transfers (id, clinic_id, from_store_id, to_store_id, source, notes, created_by, created_by_name)
        VALUES (v_id, p_clinic_id, p_from, p_to, COALESCE(p_source, 'manual'), NULLIF(btrim(p_notes), ''), p_actor_id,
                left(p_actor_name, 100));
    END IF;
    FOR e IN SELECT value FROM jsonb_array_elements(p_lines) LOOP
        IF NOT EXISTS (SELECT 1 FROM rx_batches WHERE clinic_id = p_clinic_id AND id = (e ->> 'batch_id')::UUID
                          AND item_id = (e ->> 'item_id')::UUID) THEN
            PERFORM rx_raise('rx_batch_not_found');
        END IF;
        INSERT INTO rx_transfer_lines (clinic_id, transfer_id, item_id, batch_id, sent_units)
        VALUES (p_clinic_id, v_id, (e ->> 'item_id')::UUID, (e ->> 'batch_id')::UUID, (e ->> 'sent_units')::INTEGER);
        v_n := v_n + 1;
    END LOOP;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('transfer_id', v_id, 'lines', v_n);
END $$;

-- Stock leaves the source now (transfer_out, FEFO / expiry / block walls apply). The number is the last statement.
CREATE OR REPLACE FUNCTION rx_dispatch_transfer(p_clinic_id UUID, p_transfer_id UUID, p_actor_id UUID, p_actor_name TEXT,
                                                p_can_controlled BOOLEAN)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE t rx_transfers; l RECORD; v_controlled BOOLEAN; v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    SELECT * INTO t FROM rx_transfers WHERE clinic_id = p_clinic_id AND id = p_transfer_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_transfer_not_found'); END IF;
    IF t.status = 'dispatched' THEN
        RETURN jsonb_build_object('transfer_number', t.transfer_number, 'replayed', true);
    END IF;
    IF t.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = t.to_store_id AND is_active) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    SELECT bool_or(i.schedule IN ('X', 'narcotic', 'psychotropic')) INTO v_controlled
      FROM rx_transfer_lines tl JOIN rx_items i ON i.clinic_id = tl.clinic_id AND i.id = tl.item_id
     WHERE tl.clinic_id = p_clinic_id AND tl.transfer_id = p_transfer_id;
    IF v_controlled IS NULL THEN PERFORM rx_raise('rx_transfer_empty'); END IF;
    IF v_controlled AND (NOT p_can_controlled OR p_actor_id IS NULL) THEN PERFORM rx_raise('rx_controlled_permission'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT tl.*, b.landed_unit_cost_paise_x100 FROM rx_transfer_lines tl
               JOIN rx_batches b ON b.clinic_id = tl.clinic_id AND b.id = tl.batch_id
              WHERE tl.clinic_id = p_clinic_id AND tl.transfer_id = p_transfer_id ORDER BY tl.batch_id LOOP
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name)
        VALUES (p_clinic_id, t.from_store_id, l.batch_id, l.item_id, -l.sent_units, 'transfer_out', 'rx_transfer_lines', l.id,
                l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name);
    END LOOP;
    UPDATE rx_transfers SET status = 'dispatched', has_controlled = v_controlled, dispatched_by = p_actor_id,
           dispatched_by_name = left(p_actor_name, 100), dispatched_at = clock_timestamp(), transfer_year = v_year,
           transfer_seq = opd_next_counter(p_clinic_id, 'rx_str:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = p_transfer_id RETURNING * INTO t;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('transfer_number', t.transfer_number, 'controlled', v_controlled, 'replayed', false);
END $$;

-- p_lines: every line once, [{line_id, received_units, discrepancy_reason?}]. Received units enter the target;
-- short units return to the source on paper (transfer_in) and leave it at once as damage_writeoff_out with the
-- reason: the source balance is untouched (it was debited at dispatch) and the loss is recorded against it.
CREATE OR REPLACE FUNCTION rx_receive_transfer(p_clinic_id UUID, p_transfer_id UUID, p_lines JSONB, p_actor_id UUID,
                                               p_actor_name TEXT, p_can_controlled BOOLEAN)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE t rx_transfers; l RECORD; e JSONB; v_got INTEGER; v_short INTEGER; v_total_short INTEGER := 0; v_reason TEXT;
BEGIN
    SELECT * INTO t FROM rx_transfers WHERE clinic_id = p_clinic_id AND id = p_transfer_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_transfer_not_found'); END IF;
    IF t.status IN ('received', 'partially_received') THEN
        RETURN jsonb_build_object('status', t.status, 'replayed', true);
    END IF;
    IF t.status <> 'dispatched' THEN PERFORM rx_raise('rx_transfer_not_dispatched'); END IF;
    IF t.has_controlled AND (NOT p_can_controlled OR p_actor_id IS NULL) THEN PERFORM rx_raise('rx_controlled_permission'); END IF;
    IF t.has_controlled AND p_actor_id = t.dispatched_by THEN PERFORM rx_raise('rx_controlled_two_person'); END IF;
    IF (SELECT count(DISTINCT x ->> 'line_id') FROM jsonb_array_elements(COALESCE(p_lines, '[]'::jsonb)) x)
       <> jsonb_array_length(COALESCE(p_lines, '[]'::jsonb))
       OR jsonb_array_length(COALESCE(p_lines, '[]'::jsonb))
          <> (SELECT count(*) FROM rx_transfer_lines WHERE clinic_id = p_clinic_id AND transfer_id = p_transfer_id) THEN
        PERFORM rx_raise('rx_transfer_lines_incomplete');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT tl.*, b.landed_unit_cost_paise_x100 FROM rx_transfer_lines tl
               JOIN rx_batches b ON b.clinic_id = tl.clinic_id AND b.id = tl.batch_id
              WHERE tl.clinic_id = p_clinic_id AND tl.transfer_id = p_transfer_id ORDER BY tl.batch_id LOOP
        SELECT value INTO e FROM jsonb_array_elements(p_lines) WHERE value ->> 'line_id' = l.id::TEXT;
        IF e IS NULL THEN PERFORM rx_raise('rx_transfer_lines_incomplete'); END IF;
        v_got := (e ->> 'received_units')::INTEGER;
        IF v_got IS NULL OR v_got < 0 OR v_got > l.sent_units THEN PERFORM rx_raise('rx_received_out_of_range'); END IF;
        v_short := l.sent_units - v_got;
        v_reason := NULLIF(btrim(e ->> 'discrepancy_reason'), '');
        IF v_short > 0 AND char_length(COALESCE(v_reason, '')) < 3 THEN PERFORM rx_raise('rx_discrepancy_reason_required'); END IF;
        IF v_got > 0 THEN
            INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                            ref_id, unit_cost_paise_x100, actor_id, actor_name)
            VALUES (p_clinic_id, t.to_store_id, l.batch_id, l.item_id, v_got, 'transfer_in', 'rx_transfer_lines', l.id,
                    l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name);
        END IF;
        IF v_short > 0 THEN
            INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                            ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
            VALUES (p_clinic_id, t.from_store_id, l.batch_id, l.item_id, v_short, 'transfer_in', 'rx_transfer_lines', l.id,
                    l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name, left('Short on receipt: ' || v_reason, 300)),
                   (p_clinic_id, t.from_store_id, l.batch_id, l.item_id, -v_short, 'damage_writeoff_out', 'rx_transfer_lines',
                    l.id, l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name,
                    left('Transit loss ' || COALESCE(t.transfer_number, '') || ': ' || v_reason, 300));
        END IF;
        UPDATE rx_transfer_lines SET received_units = v_got, discrepancy_reason = CASE WHEN v_short > 0 THEN v_reason END
         WHERE clinic_id = p_clinic_id AND id = l.id;
        v_total_short := v_total_short + v_short;
    END LOOP;
    UPDATE rx_transfers SET status = CASE WHEN v_total_short > 0 THEN 'partially_received' ELSE 'received' END,
           received_by = p_actor_id, received_by_name = left(p_actor_name, 100), received_at = clock_timestamp()
     WHERE clinic_id = p_clinic_id AND id = p_transfer_id RETURNING * INTO t;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('status', t.status, 'short_units', v_total_short, 'replayed', false);
END $$;

CREATE OR REPLACE FUNCTION rx_cancel_transfer(p_clinic_id UUID, p_transfer_id UUID, p_reason TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE t rx_transfers;
BEGIN
    SELECT * INTO t FROM rx_transfers WHERE clinic_id = p_clinic_id AND id = p_transfer_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_transfer_not_found'); END IF;
    IF t.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_transfers SET status = 'cancelled', cancelled_reason = left(btrim(p_reason), 300)
     WHERE clinic_id = p_clinic_id AND id = p_transfer_id;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('transfer_id', p_transfer_id, 'status', 'cancelled');
END $$;

-- ── 4. Par levels (A10) ────────────────────────────────────────────────────
-- p_rows: [{item_id, par_units}]; par_units 0 removes the item's par.
CREATE OR REPLACE FUNCTION rx_set_par_levels(p_clinic_id UUID, p_store_id UUID, p_rows JSONB, p_actor_id UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE e JSONB; v_set INTEGER := 0; v_removed INTEGER := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = p_store_id AND is_active) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    FOR e IN SELECT value FROM jsonb_array_elements(COALESCE(p_rows, '[]'::jsonb)) LOOP
        IF COALESCE((e ->> 'par_units')::INTEGER, 0) <= 0 THEN
            DELETE FROM rx_par_levels WHERE clinic_id = p_clinic_id AND store_id = p_store_id
                                        AND item_id = (e ->> 'item_id')::UUID;
            v_removed := v_removed + 1;
        ELSE
            INSERT INTO rx_par_levels (clinic_id, store_id, item_id, par_units, updated_by)
            VALUES (p_clinic_id, p_store_id, (e ->> 'item_id')::UUID, (e ->> 'par_units')::INTEGER, p_actor_id)
            ON CONFLICT (clinic_id, store_id, item_id)
            DO UPDATE SET par_units = EXCLUDED.par_units, updated_by = EXCLUDED.updated_by, updated_at = now();
            v_set := v_set + 1;
        END IF;
    END LOOP;
    RETURN jsonb_build_object('set', v_set, 'removed', v_removed);
END $$;

-- ── 5. Stock audits (W8) [F] ───────────────────────────────────────────────
-- Is an item inside an audit's scope?
CREATE OR REPLACE FUNCTION rx_audit_in_scope(a rx_stock_audits, i rx_items) RETURNS BOOLEAN
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT CASE a.scope WHEN 'full' THEN true
                        WHEN 'rack' THEN i.rack_location IS NOT DISTINCT FROM a.rack
                        WHEN 'item_list' THEN i.id = ANY (a.item_ids)
                        ELSE i.schedule IN ('H1', 'X', 'narcotic', 'psychotropic') END
$$;

-- Snapshot every batch the store has ever held of the in-scope items (zero-stock rows too, to catch found stock).
CREATE OR REPLACE FUNCTION rx_start_audit(p_clinic_id UUID, p_store_id UUID, p_scope TEXT, p_rack TEXT, p_item_ids UUID[],
                                          p_freeze BOOLEAN, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a rx_stock_audits; v_id UUID := gen_random_uuid(); v_n INTEGER; v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = p_store_id AND is_active) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    IF EXISTS (SELECT 1 FROM rx_stock_audits WHERE clinic_id = p_clinic_id AND store_id = p_store_id
                  AND status IN ('counting', 'review')) THEN
        PERFORM rx_raise('rx_audit_already_open');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    INSERT INTO rx_stock_audits (id, clinic_id, store_id, scope, rack, item_ids, freeze_sales, started_by, started_by_name)
    VALUES (v_id, p_clinic_id, p_store_id, p_scope, CASE WHEN p_scope = 'rack' THEN NULLIF(btrim(p_rack), '') END,
            CASE WHEN p_scope = 'item_list' THEN p_item_ids END, COALESCE(p_freeze, false), p_actor_id, left(p_actor_name, 100))
    RETURNING * INTO a;
    -- the balance rows are row-locked while copied, so no movement slips between the snapshot and the delta trigger
    INSERT INTO rx_stock_audit_lines (clinic_id, audit_id, batch_id, item_id, system_units_at_start)
    SELECT b.clinic_id, v_id, b.batch_id, b.item_id, b.qty_units
      FROM rx_stock_balances b JOIN rx_items i ON i.clinic_id = b.clinic_id AND i.id = b.item_id
     WHERE b.clinic_id = p_clinic_id AND b.store_id = p_store_id AND rx_audit_in_scope(a, i)
     ORDER BY b.batch_id
       FOR UPDATE OF b;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    UPDATE rx_stock_audits SET audit_year = v_year, audit_seq = opd_next_counter(p_clinic_id, 'rx_aud:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = v_id RETURNING * INTO a;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('audit_id', a.id, 'audit_number', a.audit_number, 'lines', v_n);
END $$;

-- p_counts: [{batch_id, counted_units}] — the count of a batch is SET (a resubmission is idempotent). A batch the
-- snapshot did not hold (found stock) joins the audit when its item is in scope, at its current system quantity.
CREATE OR REPLACE FUNCTION rx_record_counts(p_clinic_id UUID, p_audit_id UUID, p_counts JSONB)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a rx_stock_audits; e JSONB; i rx_items; v_n INTEGER := 0; v_added INTEGER := 0;
BEGIN
    SELECT * INTO a FROM rx_stock_audits WHERE clinic_id = p_clinic_id AND id = p_audit_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_audit_not_found'); END IF;
    IF a.status <> 'counting' THEN PERFORM rx_raise('rx_audit_not_counting'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR e IN SELECT value FROM jsonb_array_elements(COALESCE(p_counts, '[]'::jsonb)) LOOP
        IF COALESCE((e ->> 'counted_units')::INTEGER, -1) < 0 THEN PERFORM rx_raise('rx_qty_invalid'); END IF;
        UPDATE rx_stock_audit_lines SET counted_units = (e ->> 'counted_units')::INTEGER, counted_at = clock_timestamp()
         WHERE clinic_id = p_clinic_id AND audit_id = p_audit_id AND batch_id = (e ->> 'batch_id')::UUID;
        IF NOT FOUND THEN
            SELECT it.* INTO i FROM rx_batches b JOIN rx_items it ON it.clinic_id = b.clinic_id AND it.id = b.item_id
             WHERE b.clinic_id = p_clinic_id AND b.id = (e ->> 'batch_id')::UUID;
            IF NOT FOUND THEN PERFORM rx_raise('rx_batch_not_found'); END IF;
            IF NOT rx_audit_in_scope(a, i) THEN PERFORM rx_raise('rx_audit_out_of_scope'); END IF;
            INSERT INTO rx_stock_audit_lines (clinic_id, audit_id, batch_id, item_id, system_units_at_start, counted_units,
                                              counted_at)
            VALUES (p_clinic_id, p_audit_id, (e ->> 'batch_id')::UUID, i.id,
                    COALESCE((SELECT qty_units FROM rx_stock_balances WHERE clinic_id = p_clinic_id
                                 AND store_id = a.store_id AND batch_id = (e ->> 'batch_id')::UUID), 0),
                    (e ->> 'counted_units')::INTEGER, clock_timestamp());
            v_added := v_added + 1;
        END IF;
        v_n := v_n + 1;
    END LOOP;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('counted', v_n, 'added', v_added,
        'uncounted', (SELECT count(*) FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id AND audit_id = p_audit_id
                         AND counted_units IS NULL));
END $$;

-- Counting → review. Every line needs a count (enter 0 for an empty shelf): an uncounted line is not "zero".
CREATE OR REPLACE FUNCTION rx_review_audit(p_clinic_id UUID, p_audit_id UUID)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a rx_stock_audits; v_left INTEGER;
BEGIN
    SELECT * INTO a FROM rx_stock_audits WHERE clinic_id = p_clinic_id AND id = p_audit_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_audit_not_found'); END IF;
    IF a.status = 'review' THEN RETURN jsonb_build_object('status', 'review', 'replayed', true); END IF;
    IF a.status <> 'counting' THEN PERFORM rx_raise('rx_audit_not_counting'); END IF;
    SELECT count(*) INTO v_left FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id AND audit_id = p_audit_id
                                                            AND counted_units IS NULL;
    IF v_left > 0 THEN PERFORM rx_raise('rx_audit_uncounted:' || v_left); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_stock_audits SET status = 'review', reviewed_at = clock_timestamp()
     WHERE clinic_id = p_clinic_id AND id = p_audit_id;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('status', 'review', 'replayed', false);
END $$;

-- Review → posted. p_reasons: [{line_id, variance_reason}]. Every non-zero variance needs a reason. Variance value
-- (Σ |units| × landed unit cost) above rx_settings.audit_approval_paise needs a second person (the caller verifies
-- the approver's RX_ADMIN credentials; here: present, not the starter, not the poster). Status flips to posted
-- BEFORE the adjust movements so the delta trigger does not count the audit's own correction.
CREATE OR REPLACE FUNCTION rx_post_audit(p_clinic_id UUID, p_audit_id UUID, p_reasons JSONB, p_actor_id UUID,
                                         p_actor_name TEXT, p_approver_id UUID, p_approver_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a rx_stock_audits; e JSONB; l RECORD; v_value BIGINT; v_limit BIGINT; v_in INTEGER := 0; v_out INTEGER := 0;
BEGIN
    SELECT * INTO a FROM rx_stock_audits WHERE clinic_id = p_clinic_id AND id = p_audit_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_audit_not_found'); END IF;
    IF a.status = 'posted' THEN
        RETURN jsonb_build_object('audit_number', a.audit_number, 'variance_value_paise', a.variance_value_paise,
                                  'replayed', true);
    END IF;
    IF a.status <> 'review' THEN PERFORM rx_raise('rx_audit_not_in_review'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR e IN SELECT value FROM jsonb_array_elements(COALESCE(p_reasons, '[]'::jsonb)) LOOP
        UPDATE rx_stock_audit_lines SET variance_reason = NULLIF(e ->> 'variance_reason', '')
         WHERE clinic_id = p_clinic_id AND audit_id = p_audit_id AND id = (e ->> 'line_id')::UUID;
        IF NOT FOUND THEN PERFORM rx_raise('rx_audit_line_not_found'); END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM rx_stock_audit_lines WHERE clinic_id = p_clinic_id AND audit_id = p_audit_id
                  AND variance_units <> 0 AND variance_reason IS NULL) THEN
        PERFORM rx_raise('rx_variance_reason_required');
    END IF;
    SELECT COALESCE(SUM(abs(l2.variance_units)::BIGINT * b.landed_unit_cost_paise_x100), 0) / 100 INTO v_value
      FROM rx_stock_audit_lines l2 JOIN rx_batches b ON b.clinic_id = l2.clinic_id AND b.id = l2.batch_id
     WHERE l2.clinic_id = p_clinic_id AND l2.audit_id = p_audit_id;
    v_limit := COALESCE((SELECT (rx_settings ->> 'audit_approval_paise')::BIGINT FROM clinics WHERE id = p_clinic_id), 500000);
    IF v_value > v_limit AND (p_approver_id IS NULL OR p_approver_id IS NOT DISTINCT FROM a.started_by
                              OR p_approver_id IS NOT DISTINCT FROM p_actor_id) THEN
        PERFORM rx_raise('rx_audit_needs_approver');
    END IF;
    UPDATE rx_stock_audits SET status = 'posted', posted_by = p_actor_id, posted_by_name = left(p_actor_name, 100),
           posted_at = clock_timestamp(), variance_value_paise = v_value,
           approved_by = CASE WHEN v_value > v_limit THEN p_approver_id END,
           approved_by_name = CASE WHEN v_value > v_limit THEN left(p_approver_name, 100) END
     WHERE clinic_id = p_clinic_id AND id = p_audit_id RETURNING * INTO a;
    FOR l IN SELECT l3.*, b.landed_unit_cost_paise_x100 FROM rx_stock_audit_lines l3
               JOIN rx_batches b ON b.clinic_id = l3.clinic_id AND b.id = l3.batch_id
              WHERE l3.clinic_id = p_clinic_id AND l3.audit_id = p_audit_id AND l3.variance_units <> 0
              ORDER BY l3.batch_id LOOP
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table, ref_id,
                                        unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, a.store_id, l.batch_id, l.item_id, l.variance_units,
                CASE WHEN l.variance_units > 0 THEN 'adjust_in' ELSE 'adjust_out' END, 'rx_stock_audit_lines', l.id,
                l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name,
                left('Audit ' || a.audit_number || ': ' || l.variance_reason, 300));
        IF l.variance_units > 0 THEN v_in := v_in + 1; ELSE v_out := v_out + 1; END IF;
    END LOOP;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('audit_number', a.audit_number, 'variance_value_paise', v_value, 'adjust_in', v_in,
                              'adjust_out', v_out, 'approved_by', a.approved_by, 'replayed', false);
END $$;

-- An abandoned count must never leave a store frozen: counting / review → cancelled, nothing moves.
CREATE OR REPLACE FUNCTION rx_cancel_audit(p_clinic_id UUID, p_audit_id UUID, p_reason TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE a rx_stock_audits;
BEGIN
    SELECT * INTO a FROM rx_stock_audits WHERE clinic_id = p_clinic_id AND id = p_audit_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_audit_not_found'); END IF;
    IF a.status NOT IN ('counting', 'review') THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_stock_audits SET status = 'cancelled', cancelled_reason = left(btrim(p_reason), 300)
     WHERE clinic_id = p_clinic_id AND id = p_audit_id;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('audit_id', p_audit_id, 'status', 'cancelled');
END $$;

-- ── 6. Owner purge v7 ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- ── 7. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_transfers', 'rx_transfer_lines', 'rx_stock_audits', 'rx_stock_audit_lines', 'rx_par_levels']
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
    FOREACH f IN ARRAY ARRAY['rx_save_transfer(uuid,uuid,uuid,uuid,jsonb,text,text,uuid,text)',
                             'rx_dispatch_transfer(uuid,uuid,uuid,text,boolean)',
                             'rx_receive_transfer(uuid,uuid,jsonb,uuid,text,boolean)', 'rx_cancel_transfer(uuid,uuid,text)',
                             'rx_set_par_levels(uuid,uuid,jsonb,uuid)', 'rx_audit_in_scope(rx_stock_audits,rx_items)',
                             'rx_start_audit(uuid,uuid,text,text,uuid[],boolean,uuid,text)',
                             'rx_record_counts(uuid,uuid,jsonb)', 'rx_review_audit(uuid,uuid)',
                             'rx_post_audit(uuid,uuid,jsonb,uuid,text,uuid,text)', 'rx_cancel_audit(uuid,uuid,text)',
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

-- ── 8. Verify ──────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_transfers', 'rx_transfer_lines', 'rx_stock_audits', 'rx_stock_audit_lines',
                             'rx_par_levels'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '140 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '140 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_trigger WHERE tgrelid = 'rx_stock_movements'::regclass
          AND tgname IN ('trg_rx_sm_audit_freeze', 'trg_rx_sm_audit_delta')) <> 2 THEN
        RAISE EXCEPTION '140 verify: audit triggers on the ledger missing';
    END IF;
END $$;
