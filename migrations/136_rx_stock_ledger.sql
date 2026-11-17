-- ============================================================================
-- Migration 136: Kriya Pharmacy OS batches and stock ledger (RX-M3, RX-17)
-- rx_batches, the append-only movement ledger (the ONLY source of stock truth),
-- the balance projection written only by the ledger trigger, the hard walls
-- (no negative stock, no expired / blocked batch leaving for a patient), the
-- write-off and opening-stock functions, FEFO / stock views, rx_purge_clinic v3.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §3, ADR-RX-01 (ledger) / ADR-RX-02 (expiry lock).
-- Python never inserts or updates rx_stock_movements / rx_stock_balances: it calls
-- the rx_* functions below, which set kriya.rx_tx for their transaction.
-- Additive: new rx_* objects only. Rollback: rollback/136_rollback_rx_stock_ledger.sql.
-- ============================================================================

-- ── 1. Helpers ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_in_tx(p_clinic_id UUID) RETURNS BOOLEAN
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    SELECT COALESCE(current_setting('kriya.rx_tx', true), '') = p_clinic_id::TEXT
$$;

CREATE OR REPLACE FUNCTION rx_ist_date(p_ts TIMESTAMPTZ) RETURNS DATE
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT (p_ts AT TIME ZONE 'Asia/Kolkata')::DATE
$$;

-- ── 2. Batches ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_batches (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                   UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_id                     UUID NOT NULL,
    batch_no                    TEXT NOT NULL CHECK (batch_no ~ '^[A-Za-z0-9/._-]{1,30}$'),
    expiry_date                 DATE NOT NULL,          -- last day of the printed expiry month
    mfg_date                    DATE NULL,
    mrp_pack_paise              INTEGER NOT NULL CHECK (mrp_pack_paise BETWEEN 1 AND 100000000),
    ptr_pack_paise              INTEGER NULL CHECK (ptr_pack_paise >= 0),
    landed_unit_cost_paise_x100 BIGINT NOT NULL CHECK (landed_unit_cost_paise_x100 >= 0),
    first_grn_line_id           UUID NULL,
    is_blocked                  BOOLEAN NOT NULL DEFAULT false,   -- recall / quarantine
    block_reason                TEXT NULL CHECK (block_reason IS NULL OR char_length(block_reason) <= 300),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_batches_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_batches_identity_key UNIQUE (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise),
    CONSTRAINT rx_batches_dates CHECK (mfg_date IS NULL OR mfg_date < expiry_date),
    CONSTRAINT rx_batches_block_reason CHECK (NOT is_blocked OR char_length(btrim(COALESCE(block_reason, ''))) >= 3),
    CONSTRAINT rx_batches_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_batches_item ON rx_batches (clinic_id, item_id, expiry_date);

-- A batch's identity, price and cost never change (same number + new MRP = new batch row);
-- only the quarantine flag and its reason move. Never deleted outside the owner purge.
CREATE OR REPLACE FUNCTION rx_guard_batch() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_batch_delete_refused');
    END IF;
    IF (to_jsonb(NEW) - 'is_blocked' - 'block_reason') IS DISTINCT FROM (to_jsonb(OLD) - 'is_blocked' - 'block_reason') THEN
        PERFORM rx_raise('rx_batch_field_locked');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_batches_guard ON rx_batches;
CREATE TRIGGER trg_rx_batches_guard BEFORE UPDATE OR DELETE ON rx_batches
    FOR EACH ROW EXECUTE FUNCTION rx_guard_batch();

-- ── 3. The ledger [A][F] ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_stock_movements (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id             UUID NOT NULL,
    batch_id             UUID NOT NULL,
    item_id              UUID NOT NULL,             -- denormalised; checked equal to the batch's item
    qty_units            INTEGER NOT NULL CHECK (qty_units <> 0),          -- + in, − out
    movement_type        TEXT NOT NULL CHECK (movement_type IN (
                             'opening', 'grn_in', 'sale_out', 'sale_return_in', 'ipd_dispense_out', 'ipd_return_in',
                             'transfer_out', 'transfer_in', 'supplier_return_out', 'adjust_in', 'adjust_out',
                             'expiry_writeoff_out', 'damage_writeoff_out', 'ward_issue_out')),
    ref_table            TEXT NOT NULL,
    ref_id               UUID NOT NULL,
    unit_cost_paise_x100 BIGINT NOT NULL,
    actor_id             UUID NULL,
    actor_name           TEXT NOT NULL,
    reason               TEXT NULL CHECK (char_length(reason) <= 300),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_sm_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_sm_sign CHECK ((movement_type LIKE '%\_in' ESCAPE '\' OR movement_type = 'opening') = (qty_units > 0)),
    CONSTRAINT rx_sm_reason_for_adjust CHECK (movement_type NOT IN ('adjust_in', 'adjust_out', 'damage_writeoff_out',
                                                                   'expiry_writeoff_out')
                                              OR char_length(btrim(COALESCE(reason, ''))) >= 3),
    CONSTRAINT rx_sm_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_sm_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_sm_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_sm_ref
    ON rx_stock_movements (clinic_id, ref_table, ref_id, movement_type, batch_id, store_id);
CREATE INDEX IF NOT EXISTS idx_rx_sm_store_item ON rx_stock_movements (clinic_id, store_id, item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rx_sm_batch ON rx_stock_movements (clinic_id, batch_id);

CREATE TABLE IF NOT EXISTS rx_stock_balances (   -- projection, written only by trg_rx_prevent_negative_stock
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id   UUID NOT NULL,
    batch_id   UUID NOT NULL,
    item_id    UUID NOT NULL,
    qty_units  INTEGER NOT NULL CONSTRAINT rx_bal_non_negative CHECK (qty_units >= 0),   -- the negative-stock wall
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (clinic_id, store_id, batch_id),
    CONSTRAINT rx_bal_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_bal_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_bal_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_bal_item ON rx_stock_balances (clinic_id, store_id, item_id) WHERE qty_units > 0;

-- 3a. Only an rx_* function may write the ledger, and a movement's item is its batch's item.
CREATE OR REPLACE FUNCTION rx_sm_guard_insert() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_item UUID;
BEGIN
    IF NOT rx_in_tx(NEW.clinic_id) THEN PERFORM rx_raise('rx_movement_via_function_only'); END IF;
    SELECT item_id INTO v_item FROM rx_batches WHERE clinic_id = NEW.clinic_id AND id = NEW.batch_id;
    IF v_item IS DISTINCT FROM NEW.item_id THEN PERFORM rx_raise('rx_batch_item_mismatch'); END IF;
    NEW.created_at := clock_timestamp();
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_sm_guard_insert ON rx_stock_movements;
CREATE TRIGGER trg_rx_sm_guard_insert BEFORE INSERT ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_sm_guard_insert();

-- 3b. ADR-RX-02: an expired or quarantined batch never leaves for a patient or a ward.
-- Supplier returns and write-offs are allowed: that is how such stock leaves the shelf.
CREATE OR REPLACE FUNCTION rx_sm_expiry_lock() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE b rx_batches;
BEGIN
    IF NEW.movement_type IN ('sale_out', 'ipd_dispense_out', 'transfer_out', 'ward_issue_out') THEN
        SELECT * INTO b FROM rx_batches WHERE clinic_id = NEW.clinic_id AND id = NEW.batch_id;
        IF b.expiry_date < rx_ist_date(now()) THEN PERFORM rx_raise('rx_batch_expired'); END IF;
        IF b.is_blocked THEN PERFORM rx_raise('rx_batch_blocked'); END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_block_expired_dispense ON rx_stock_movements;
CREATE TRIGGER trg_rx_block_expired_dispense BEFORE INSERT ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_sm_expiry_lock();

-- 3c. Apply the movement to the balance. Both paths row-lock the store × batch balance,
-- serialising concurrent movements; rx_bal_non_negative aborts any movement that would
-- take the batch (and so the store) below zero. Outgoing movements UPDATE (an upsert would
-- CHECK the proposed negative row before seeing the conflict); no row = no stock.
CREATE OR REPLACE FUNCTION rx_sm_apply() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF NEW.qty_units > 0 THEN
        INSERT INTO rx_stock_balances AS b (clinic_id, store_id, batch_id, item_id, qty_units)
        VALUES (NEW.clinic_id, NEW.store_id, NEW.batch_id, NEW.item_id, NEW.qty_units)
        ON CONFLICT (clinic_id, store_id, batch_id)
        DO UPDATE SET qty_units = b.qty_units + EXCLUDED.qty_units, updated_at = clock_timestamp();
        RETURN NULL;
    END IF;
    BEGIN
        UPDATE rx_stock_balances SET qty_units = qty_units + NEW.qty_units, updated_at = clock_timestamp()
         WHERE clinic_id = NEW.clinic_id AND store_id = NEW.store_id AND batch_id = NEW.batch_id;
        IF NOT FOUND THEN PERFORM rx_raise('rx_insufficient_stock'); END IF;
    EXCEPTION WHEN check_violation THEN
        PERFORM rx_raise('rx_insufficient_stock');
    END;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_prevent_negative_stock ON rx_stock_movements;
CREATE TRIGGER trg_rx_prevent_negative_stock AFTER INSERT ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_sm_apply();

DROP TRIGGER IF EXISTS trg_rx_sm_append_only ON rx_stock_movements;
CREATE TRIGGER trg_rx_sm_append_only BEFORE UPDATE OR DELETE ON rx_stock_movements
    FOR EACH ROW EXECUTE FUNCTION rx_guard_append_only();

-- 3d. Balances move only inside the ledger trigger (depth > 1) or the owner purge.
CREATE OR REPLACE FUNCTION rx_guard_balance() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF pg_trigger_depth() > 1 OR rx_purging(CASE WHEN TG_OP = 'DELETE' THEN OLD.clinic_id ELSE NEW.clinic_id END) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    PERFORM rx_raise('rx_balance_via_ledger_only');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_balances_guard ON rx_stock_balances;
CREATE TRIGGER trg_rx_balances_guard BEFORE INSERT OR UPDATE OR DELETE ON rx_stock_balances
    FOR EACH ROW EXECUTE FUNCTION rx_guard_balance();

-- ── 4. Ledger writers ──────────────────────────────────────────────────────
-- Write-off: p_lines [{batch_id, qty_units > 0, kind 'expiry'|'damage', reason}]. Expired and
-- blocked batches may be written off. Replaying p_ref_id returns {replayed: true}.
CREATE OR REPLACE FUNCTION rx_post_writeoff(p_clinic_id UUID, p_store_id UUID, p_ref_id UUID, p_lines JSONB,
                                            p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE l JSONB; b rx_batches; n INTEGER := 0; v_units INTEGER := 0;
BEGIN
    IF EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = p_clinic_id AND ref_table = 'rx_writeoffs'
                 AND ref_id = p_ref_id) THEN
        RETURN jsonb_build_object('ref_id', p_ref_id, 'replayed', true);
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT value FROM jsonb_array_elements(p_lines) ORDER BY value ->> 'batch_id' LOOP   -- lock order
        SELECT * INTO b FROM rx_batches WHERE clinic_id = p_clinic_id AND id = (l ->> 'batch_id')::UUID;
        IF NOT FOUND THEN PERFORM rx_raise('rx_batch_not_found'); END IF;
        IF COALESCE((l ->> 'qty_units')::INTEGER, 0) <= 0 THEN PERFORM rx_raise('rx_qty_invalid'); END IF;
        IF l ->> 'kind' NOT IN ('expiry', 'damage') THEN PERFORM rx_raise('rx_writeoff_kind_invalid'); END IF;
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                        ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, p_store_id, b.id, b.item_id, -(l ->> 'qty_units')::INTEGER,
                (l ->> 'kind') || '_writeoff_out', 'rx_writeoffs', p_ref_id, b.landed_unit_cost_paise_x100,
                p_actor_id, p_actor_name, l ->> 'reason');
        n := n + 1; v_units := v_units + (l ->> 'qty_units')::INTEGER;
    END LOOP;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('ref_id', p_ref_id, 'movements', n, 'units', v_units, 'replayed', false);
END $$;

-- Opening stock (ROLLOUT §4.3): only while the store has no non-opening movement, so it can
-- never hide later shrinkage. Rows: {item_code, batch_no, expiry_date, mrp_pack_paise,
-- ptr_pack_paise, landed_unit_cost_paise_x100, qty_units, _row}. Expired rows are refused.
-- All rows in one transaction, each in a savepoint; undone unless p_apply and no row failed.
-- Idempotent per (item, batch, expiry, MRP, store): a re-upload writes nothing new.
CREATE OR REPLACE FUNCTION rx_opening_stock(p_clinic_id UUID, p_store_id UUID, p_rows JSONB, p_apply BOOLEAN,
                                            p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE e JSONB; i INTEGER := 0; it rx_items; v_batch UUID; n INTEGER; v_today DATE := rx_ist_date(now());
    v_batches INTEGER := 0; v_moves INTEGER := 0; v_units BIGINT := 0; v_skipped INTEGER := 0;
    v_errors JSONB := '[]'::jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = p_store_id) THEN
        PERFORM rx_raise('rx_store_not_found');
    END IF;
    IF EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = p_clinic_id AND store_id = p_store_id
                 AND movement_type <> 'opening') THEN
        PERFORM rx_raise('rx_opening_closed');
    END IF;
    BEGIN
        PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
        FOR e IN SELECT value FROM jsonb_array_elements(p_rows) LOOP
            i := i + 1;
            BEGIN
                SELECT * INTO it FROM rx_items WHERE clinic_id = p_clinic_id AND item_code = e ->> 'item_code';
                IF NOT FOUND THEN PERFORM rx_raise('rx_item_not_found'); END IF;
                IF (e ->> 'expiry_date')::DATE < v_today THEN PERFORM rx_raise('rx_batch_expired'); END IF;
                IF COALESCE((e ->> 'qty_units')::INTEGER, 0) <= 0 THEN PERFORM rx_raise('rx_qty_invalid'); END IF;
                v_batch := NULL;
                INSERT INTO rx_batches (clinic_id, item_id, batch_no, expiry_date, mrp_pack_paise, ptr_pack_paise,
                                        landed_unit_cost_paise_x100)
                VALUES (p_clinic_id, it.id, e ->> 'batch_no', (e ->> 'expiry_date')::DATE,
                        (e ->> 'mrp_pack_paise')::INTEGER, (e ->> 'ptr_pack_paise')::INTEGER,
                        (e ->> 'landed_unit_cost_paise_x100')::BIGINT)
                ON CONFLICT ON CONSTRAINT rx_batches_identity_key DO NOTHING
                RETURNING id INTO v_batch;
                IF v_batch IS NULL THEN
                    SELECT id INTO v_batch FROM rx_batches
                     WHERE clinic_id = p_clinic_id AND item_id = it.id AND batch_no = e ->> 'batch_no'
                       AND expiry_date = (e ->> 'expiry_date')::DATE AND mrp_pack_paise = (e ->> 'mrp_pack_paise')::INTEGER;
                ELSE
                    v_batches := v_batches + 1;
                END IF;
                INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type,
                                                ref_table, ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
                SELECT p_clinic_id, p_store_id, v_batch, it.id, (e ->> 'qty_units')::INTEGER, 'opening', 'rx_opening',
                       p_store_id, landed_unit_cost_paise_x100, p_actor_id, p_actor_name, 'Opening balance'
                  FROM rx_batches WHERE clinic_id = p_clinic_id AND id = v_batch
                ON CONFLICT (clinic_id, ref_table, ref_id, movement_type, batch_id, store_id) DO NOTHING;
                GET DIAGNOSTICS n = ROW_COUNT;
                IF n = 1 THEN
                    v_moves := v_moves + 1; v_units := v_units + (e ->> 'qty_units')::INTEGER;
                ELSE
                    v_skipped := v_skipped + 1;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_errors := v_errors || jsonb_build_object('row', COALESCE((e ->> '_row')::INTEGER, i), 'error', SQLERRM);
            END;
        END LOOP;
        PERFORM set_config('kriya.rx_tx', '', true);
        IF NOT p_apply OR jsonb_array_length(v_errors) > 0 THEN
            RAISE EXCEPTION 'rx_import_rolled_back' USING ERRCODE = 'RXDRY';
        END IF;
    EXCEPTION WHEN SQLSTATE 'RXDRY' THEN
        NULL;   -- dry run, or a row failed: nothing of this file is kept
    END;
    RETURN jsonb_build_object('new_batches', v_batches, 'movements', v_moves, 'units', v_units,
                              'already_loaded', v_skipped, 'errors', v_errors,
                              'applied', p_apply AND jsonb_array_length(v_errors) = 0);
END $$;

-- ── 5. Read views (security_invoker: no privilege escalation through a view) ──
CREATE OR REPLACE VIEW rx_v_batch_stock WITH (security_invoker = true) AS
    SELECT s.clinic_id, s.store_id, s.item_id, s.batch_id, s.qty_units, b.batch_no, b.expiry_date, b.mrp_pack_paise,
           b.landed_unit_cost_paise_x100, b.is_blocked, b.block_reason, b.created_at AS batch_created_at
      FROM rx_stock_balances s JOIN rx_batches b ON b.clinic_id = s.clinic_id AND b.id = s.batch_id
     WHERE s.qty_units > 0;

-- FEFO: sellable batches (not blocked, not expired) first-expiring first.
CREATE OR REPLACE VIEW rx_v_fefo WITH (security_invoker = true) AS
    SELECT v.*, row_number() OVER (PARTITION BY v.clinic_id, v.store_id, v.item_id
                                   ORDER BY v.expiry_date, v.batch_created_at, v.batch_id) AS fefo_rank
      FROM rx_v_batch_stock v
     WHERE NOT v.is_blocked AND v.expiry_date >= rx_ist_date(now());

CREATE OR REPLACE VIEW rx_v_item_stock WITH (security_invoker = true) AS
    SELECT clinic_id, store_id, item_id,
           SUM(qty_units)::BIGINT AS qty_units,
           COALESCE(SUM(qty_units) FILTER (WHERE NOT is_blocked AND expiry_date >= rx_ist_date(now())), 0)::BIGINT
               AS sellable_units,
           MIN(expiry_date) FILTER (WHERE NOT is_blocked AND expiry_date >= rx_ist_date(now())) AS nearest_expiry,
           (SUM(qty_units::BIGINT * landed_unit_cost_paise_x100) / 100)::BIGINT AS value_paise
      FROM rx_v_batch_stock
     GROUP BY clinic_id, store_id, item_id;

-- ── 6. Owner purge v3 (ledger, then catalogue, then stores) ────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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
    FOREACH t IN ARRAY ARRAY['rx_batches', 'rx_stock_movements', 'rx_stock_balances']
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
    END LOOP;
    FOREACH t IN ARRAY ARRAY['rx_batches', 'rx_stock_movements', 'rx_stock_balances', 'rx_v_batch_stock', 'rx_v_fefo',
                             'rx_v_item_stock']
    LOOP
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
    FOREACH f IN ARRAY ARRAY['rx_in_tx(uuid)', 'rx_ist_date(timestamptz)', 'rx_purge_clinic(uuid)',
                             'rx_post_writeoff(uuid,uuid,uuid,jsonb,uuid,text)',
                             'rx_opening_stock(uuid,uuid,jsonb,boolean,uuid,text)']
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
    FOREACH t IN ARRAY ARRAY['rx_batches', 'rx_stock_movements', 'rx_stock_balances'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '136 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '136 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_rx_batches_guard', 'trg_rx_sm_guard_insert',
          'trg_rx_block_expired_dispense', 'trg_rx_prevent_negative_stock', 'trg_rx_sm_append_only',
          'trg_rx_balances_guard')) <> 6 THEN
        RAISE EXCEPTION '136 verify: ledger triggers missing';
    END IF;
    IF to_regclass('public.rx_v_fefo') IS NULL OR to_regclass('public.rx_v_item_stock') IS NULL
       OR to_regclass('public.rx_v_batch_stock') IS NULL THEN
        RAISE EXCEPTION '136 verify: stock views missing';
    END IF;
    IF to_regprocedure('rx_post_writeoff(uuid,uuid,uuid,jsonb,uuid,text)') IS NULL
       OR to_regprocedure('rx_opening_stock(uuid,uuid,jsonb,boolean,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '136 verify: ledger functions missing';
    END IF;
END $$;
