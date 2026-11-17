-- ============================================================================
-- Migration 135: Kriya Pharmacy OS catalogue (RX-M2, RX-12)
-- Manufacturers, generics (the substitution class), items (one sellable SKU),
-- barcodes, learned aliases and the append-only item change log; the item
-- guard (immutables, no schedule downgrade, reason for tax changes), the
-- deterministic resolver, the transactional item import and rx_purge_clinic v2.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §2, ADR-RX-06/07 (zero-LLM matching).
-- Additive: new rx_* tables and functions only; no existing table, trigger or
-- function outside lane RX is touched. Unused until a clinic enables pharmacy.
-- Rollback: migrations/rollback/135_rollback_rx_catalogue.sql (restores purge v1).
-- ============================================================================

-- ── 1. Helpers ─────────────────────────────────────────────────────────────
-- Free text → comparable key: lower case, punctuation to spaces, "500mg" → "500 mg",
-- spaces collapsed. Dots survive only inside numbers ("0.5 mg").
CREATE OR REPLACE FUNCTION rx_normalize(p TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT btrim(regexp_replace(
             regexp_replace(
               regexp_replace(
                 regexp_replace(lower(COALESCE(p, '')), '[^a-z0-9.+ ]', ' ', 'g'),
                 '(?<![0-9])\.|\.(?![0-9])', ' ', 'g'),
               '([0-9])([a-z])', '\1 \2', 'g'),
             '\s+', ' ', 'g'))
$$;

-- Clinical rank of a schedule: an item may move up (stricter), never down or sideways
-- between controlled classes (X / narcotic / psychotropic each have their own register).
CREATE OR REPLACE FUNCTION rx_schedule_rank(p TEXT) RETURNS INTEGER
LANGUAGE sql IMMUTABLE SET search_path = public, pg_temp AS $$
    SELECT CASE p WHEN 'none' THEN 0 WHEN 'G' THEN 1 WHEN 'H' THEN 2 WHEN 'H1' THEN 3 ELSE 4 END
$$;

-- ── 2. Tables ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_manufacturers (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    is_active  BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_manufacturers_clinic_id_id_key UNIQUE (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_manufacturers_name ON rx_manufacturers (clinic_id, lower(name));

CREATE TABLE IF NOT EXISTS rx_generics (       -- molecule(s) + strength + form = substitution class
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id   UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 200),
    strength    TEXT NOT NULL CHECK (char_length(btrim(strength)) BETWEEN 1 AND 60),
    dosage_form TEXT NOT NULL CHECK (dosage_form IN ('tablet', 'capsule', 'syrup', 'suspension', 'injection', 'drops',
                    'ointment', 'cream', 'gel', 'inhaler', 'powder', 'lotion', 'spray', 'patch', 'infusion', 'other')),
    route       TEXT NOT NULL DEFAULT 'oral' CHECK (route IN ('oral', 'topical', 'iv', 'im', 'sc', 'inhalation',
                    'nasal', 'ophthalmic', 'otic', 'rectal', 'vaginal', 'sublingual', 'other')),   -- 103's enum
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_generics_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_generics_class_key UNIQUE (clinic_id, name, strength, dosage_form)
);

CREATE TABLE IF NOT EXISTS rx_items (          -- one sellable SKU (brand + pack)
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_code           TEXT NOT NULL CHECK (item_code ~ '^[A-Z0-9_-]{2,40}$'),
    brand_name          TEXT NOT NULL CHECK (char_length(btrim(brand_name)) BETWEEN 2 AND 200),
    generic_id          UUID NULL,             -- NULL = OTC / consumable without a substitution class
    manufacturer_id     UUID NULL,
    item_kind           TEXT NOT NULL DEFAULT 'drug' CHECK (item_kind IN ('drug', 'consumable', 'surgical', 'otc',
                            'nutrition', 'device')),
    schedule            TEXT NOT NULL DEFAULT 'none' CHECK (schedule IN ('none', 'G', 'H', 'H1', 'X', 'narcotic',
                            'psychotropic')),
    base_unit           TEXT NOT NULL CHECK (base_unit IN ('tablet', 'capsule', 'ml', 'gram', 'vial', 'ampoule',
                            'bottle', 'tube', 'sachet', 'piece', 'pair', 'box', 'inhaler', 'patch')),
    pack_size           INTEGER NOT NULL CHECK (pack_size BETWEEN 1 AND 10000),   -- base units per pack
    pack_label          TEXT NOT NULL DEFAULT 'strip' CHECK (pack_label IN ('strip', 'blister', 'bottle', 'vial',
                            'ampoule', 'tube', 'box', 'sachet', 'piece', 'pack')),
    allow_loose         BOOLEAN NOT NULL DEFAULT true,
    hsn_code            TEXT NOT NULL CHECK (hsn_code ~ '^[0-9]{4,8}$'),
    tax_rate_bps        SMALLINT NOT NULL CHECK (tax_rate_bps IN (0, 500, 1200, 1800, 2800, 4000)),
    reorder_level_units INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level_units >= 0),
    reorder_qty_units   INTEGER NOT NULL DEFAULT 0 CHECK (reorder_qty_units >= 0),
    ved_class           TEXT NULL CHECK (ved_class IN ('V', 'E', 'D')),
    is_chronic          BOOLEAN NOT NULL DEFAULT false,
    is_cold_chain       BOOLEAN NOT NULL DEFAULT false,
    rack_location       TEXT NULL CHECK (char_length(rack_location) <= 40),
    is_active           BOOLEAN NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_items_code_key UNIQUE (clinic_id, item_code),
    CONSTRAINT rx_items_controlled_no_loose CHECK (schedule NOT IN ('X', 'narcotic') OR allow_loose),
    CONSTRAINT rx_items_drug_has_generic CHECK (item_kind <> 'drug' OR generic_id IS NOT NULL),
    CONSTRAINT rx_items_generic_fk FOREIGN KEY (clinic_id, generic_id) REFERENCES rx_generics (clinic_id, id),
    CONSTRAINT rx_items_mfr_fk FOREIGN KEY (clinic_id, manufacturer_id) REFERENCES rx_manufacturers (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_items_generic ON rx_items (clinic_id, generic_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_rx_items_brand ON rx_items (clinic_id, lower(brand_name));

CREATE TABLE IF NOT EXISTS rx_item_barcodes (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_id    UUID NOT NULL,
    barcode    TEXT NOT NULL CHECK (barcode ~ '^[0-9A-Za-z]{6,40}$'),
    symbology  TEXT NOT NULL DEFAULT 'internal' CHECK (symbology IN ('ean13', 'ean8', 'gs1_128', 'gs1_datamatrix',
                   'upc', 'internal')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_item_barcodes_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_item_barcodes_barcode_key UNIQUE (clinic_id, barcode),
    CONSTRAINT rx_item_barcodes_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS rx_item_aliases (   -- learned free text → item (ADR-RX-07), never auto-applied
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_id      UUID NOT NULL,
    alias_norm   TEXT NOT NULL CHECK (char_length(alias_norm) BETWEEN 2 AND 200 AND alias_norm = rx_normalize(alias_norm)),
    source       TEXT NOT NULL CHECK (source IN ('manual', 'pharmacist_confirmed', 'import')),
    confirmed_by UUID NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_item_aliases_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_item_aliases_alias_key UNIQUE (clinic_id, alias_norm, item_id),
    CONSTRAINT rx_item_aliases_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS rx_item_changes (   -- [A] who changed what on an item, and why
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    item_id         UUID NOT NULL,
    field           TEXT NOT NULL,
    old_value       TEXT NULL,
    new_value       TEXT NULL,
    changed_by      UUID NULL,
    changed_by_name TEXT NULL,
    reason          TEXT NULL CHECK (char_length(reason) <= 300),
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_item_changes_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_item_changes_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_item_changes_item ON rx_item_changes (clinic_id, item_id, changed_at DESC);

-- ── 3. Guards ──────────────────────────────────────────────────────────────
-- Items are never deleted (deactivate). item_code, base_unit are immutable; the schedule
-- may only go up; generic / pack size lock once stock exists (136 tables, looked up
-- dynamically so this function never needs redefining). Every changed field is logged;
-- a tax rate / HSN change needs a reason (rx_update_item passes it in kriya.rx_reason).
CREATE OR REPLACE FUNCTION rx_guard_item() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_used BOOLEAN; k TEXT; ov JSONB; nv JSONB;
    v_actor TEXT := NULLIF(current_setting('kriya.rx_actor_id', true), '');
    v_reason TEXT := NULLIF(btrim(COALESCE(current_setting('kriya.rx_reason', true), '')), '');
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_item_delete_refused');
    END IF;
    IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id THEN PERFORM rx_raise('rx_item_field_locked:clinic_id'); END IF;
    IF NEW.item_code IS DISTINCT FROM OLD.item_code THEN PERFORM rx_raise('rx_item_field_locked:item_code'); END IF;
    IF NEW.base_unit IS DISTINCT FROM OLD.base_unit THEN PERFORM rx_raise('rx_item_field_locked:base_unit'); END IF;
    IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN PERFORM rx_raise('rx_item_field_locked:created_at'); END IF;
    IF NEW.schedule IS DISTINCT FROM OLD.schedule
       AND rx_schedule_rank(NEW.schedule) <= rx_schedule_rank(OLD.schedule) THEN
        PERFORM rx_raise('rx_item_field_locked:schedule');
    END IF;
    IF NEW.generic_id IS DISTINCT FROM OLD.generic_id AND to_regclass('public.rx_stock_movements') IS NOT NULL THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM rx_stock_movements WHERE clinic_id = $1 AND item_id = $2)'
           INTO v_used USING OLD.clinic_id, OLD.id;
        IF v_used THEN PERFORM rx_raise('rx_item_field_locked:generic_id'); END IF;
    END IF;
    IF NEW.pack_size IS DISTINCT FROM OLD.pack_size AND to_regclass('public.rx_batches') IS NOT NULL THEN
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM rx_batches WHERE clinic_id = $1 AND item_id = $2)'
           INTO v_used USING OLD.clinic_id, OLD.id;
        IF v_used THEN PERFORM rx_raise('rx_item_field_locked:pack_size'); END IF;
    END IF;
    IF (NEW.tax_rate_bps IS DISTINCT FROM OLD.tax_rate_bps OR NEW.hsn_code IS DISTINCT FROM OLD.hsn_code)
       AND (v_reason IS NULL OR char_length(v_reason) < 3) THEN
        PERFORM rx_raise('rx_item_reason_required');
    END IF;
    NEW.updated_at := clock_timestamp();
    FOR k, ov, nv IN
        SELECT o.key, o.value, n.value FROM jsonb_each(to_jsonb(OLD)) o JOIN jsonb_each(to_jsonb(NEW)) n USING (key)
         WHERE o.value IS DISTINCT FROM n.value AND o.key <> 'updated_at' ORDER BY o.key
    LOOP
        INSERT INTO rx_item_changes (clinic_id, item_id, field, old_value, new_value, changed_by, changed_by_name, reason)
        VALUES (OLD.clinic_id, OLD.id, k, ov #>> '{}', nv #>> '{}',
                CASE WHEN v_actor ~ '^[0-9a-fA-F-]{36}$' THEN v_actor::UUID END,
                NULLIF(current_setting('kriya.rx_actor_name', true), ''), left(v_reason, 300));
    END LOOP;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_items_guard ON rx_items;
CREATE TRIGGER trg_rx_items_guard BEFORE UPDATE OR DELETE ON rx_items
    FOR EACH ROW EXECUTE FUNCTION rx_guard_item();

-- Append-only rows (change log here; the stock ledger in 136).
CREATE OR REPLACE FUNCTION rx_guard_append_only() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF rx_purging(OLD.clinic_id) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    PERFORM rx_raise('rx_append_only:' || TG_TABLE_NAME);
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_item_changes_append_only ON rx_item_changes;
CREATE TRIGGER trg_rx_item_changes_append_only BEFORE UPDATE OR DELETE ON rx_item_changes
    FOR EACH ROW EXECUTE FUNCTION rx_guard_append_only();

-- ── 4. Item update with actor + reason (the only PATCH path) ───────────────
-- p_patch keys are item columns (validated by app/services/rx_catalogue.py); immutables
-- are passed through so the guard, not the caller, decides.
CREATE OR REPLACE FUNCTION rx_update_item(p_clinic_id UUID, p_item_id UUID, p_patch JSONB, p_actor_id UUID,
                                          p_actor_name TEXT, p_reason TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r rx_items; v JSONB;
BEGIN
    SELECT * INTO r FROM rx_items WHERE clinic_id = p_clinic_id AND id = p_item_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_item_not_found'); END IF;
    r := jsonb_populate_record(r, p_patch - 'id' - 'clinic_id');
    PERFORM set_config('kriya.rx_actor_id', COALESCE(p_actor_id::TEXT, ''), true);
    PERFORM set_config('kriya.rx_actor_name', COALESCE(p_actor_name, ''), true);
    PERFORM set_config('kriya.rx_reason', COALESCE(p_reason, ''), true);
    UPDATE rx_items SET item_code = r.item_code, brand_name = r.brand_name, generic_id = r.generic_id,
           manufacturer_id = r.manufacturer_id, item_kind = r.item_kind, schedule = r.schedule,
           base_unit = r.base_unit, pack_size = r.pack_size, pack_label = r.pack_label, allow_loose = r.allow_loose,
           hsn_code = r.hsn_code, tax_rate_bps = r.tax_rate_bps, reorder_level_units = r.reorder_level_units,
           reorder_qty_units = r.reorder_qty_units, ved_class = r.ved_class, is_chronic = r.is_chronic,
           is_cold_chain = r.is_cold_chain, rack_location = r.rack_location, is_active = r.is_active
     WHERE clinic_id = p_clinic_id AND id = p_item_id
    RETURNING to_jsonb(rx_items.*) INTO v;
    PERFORM set_config('kriya.rx_actor_id', '', true);
    PERFORM set_config('kriya.rx_actor_name', '', true);
    PERFORM set_config('kriya.rx_reason', '', true);
    RETURN v;
END $$;

-- ── 5. Item master import (ROLLOUT §4.2) ───────────────────────────────────
-- One transaction: every row is tried in its own savepoint so all row errors are
-- reported; then the whole import is undone unless p_apply and no row failed.
-- Idempotent on item_code (an identical row is "unchanged"). Rows are pre-validated
-- in Python and carry item columns + generic_name/strength/dosage_form/route,
-- manufacturer, barcode, symbology and _row (the file's row number).
CREATE OR REPLACE FUNCTION rx_import_items(p_clinic_id UUID, p_rows JSONB, p_apply BOOLEAN, p_actor_id UUID,
                                           p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE e JSONB; j JSONB; i INTEGER := 0; v_mfr UUID; v_gen UUID; v_owner UUID; v_item rx_items; v_new rx_items;
    v_created INTEGER := 0; v_updated INTEGER := 0; v_unchanged INTEGER := 0; v_errors JSONB := '[]'::jsonb;
BEGIN
    BEGIN
        FOR e IN SELECT value FROM jsonb_array_elements(p_rows) LOOP
            i := i + 1;
            BEGIN
                v_mfr := NULL; v_gen := NULL;
                IF NULLIF(btrim(e ->> 'manufacturer'), '') IS NOT NULL THEN
                    SELECT id INTO v_mfr FROM rx_manufacturers
                     WHERE clinic_id = p_clinic_id AND lower(name) = lower(btrim(e ->> 'manufacturer'));
                    IF v_mfr IS NULL THEN
                        INSERT INTO rx_manufacturers (clinic_id, name) VALUES (p_clinic_id, btrim(e ->> 'manufacturer'))
                        RETURNING id INTO v_mfr;
                    END IF;
                END IF;
                IF NULLIF(btrim(e ->> 'generic_name'), '') IS NOT NULL THEN
                    SELECT id INTO v_gen FROM rx_generics
                     WHERE clinic_id = p_clinic_id AND name = btrim(e ->> 'generic_name')
                       AND strength = btrim(e ->> 'strength') AND dosage_form = e ->> 'dosage_form';
                    IF v_gen IS NULL THEN
                        INSERT INTO rx_generics (clinic_id, name, strength, dosage_form, route)
                        VALUES (p_clinic_id, btrim(e ->> 'generic_name'), btrim(e ->> 'strength'), e ->> 'dosage_form',
                                COALESCE(NULLIF(e ->> 'route', ''), 'oral'))
                        RETURNING id INTO v_gen;
                    END IF;
                END IF;
                j := (e - 'generic_name' - 'strength' - 'dosage_form' - 'route' - 'manufacturer' - 'barcode'
                        - 'symbology' - '_row') || jsonb_build_object('generic_id', v_gen, 'manufacturer_id', v_mfr);
                SELECT * INTO v_item FROM rx_items WHERE clinic_id = p_clinic_id AND item_code = e ->> 'item_code'
                   FOR UPDATE;
                IF NOT FOUND THEN
                    INSERT INTO rx_items SELECT * FROM jsonb_populate_record(NULL::rx_items, jsonb_build_object(
                        'item_kind', 'drug', 'schedule', 'none', 'pack_label', 'strip', 'allow_loose', true,
                        'reorder_level_units', 0, 'reorder_qty_units', 0, 'is_chronic', false, 'is_cold_chain', false,
                        'is_active', true) || j || jsonb_build_object(
                        'id', gen_random_uuid(), 'clinic_id', p_clinic_id, 'created_at', clock_timestamp(),
                        'updated_at', clock_timestamp()))
                    RETURNING * INTO v_item;
                    v_created := v_created + 1;
                ELSE
                    v_new := jsonb_populate_record(v_item, j);
                    IF v_new IS DISTINCT FROM v_item THEN
                        PERFORM rx_update_item(p_clinic_id, v_item.id, j, p_actor_id, p_actor_name, 'item master import');
                        v_updated := v_updated + 1;
                    ELSE
                        v_unchanged := v_unchanged + 1;
                    END IF;
                END IF;
                IF NULLIF(e ->> 'barcode', '') IS NOT NULL THEN
                    SELECT item_id INTO v_owner FROM rx_item_barcodes
                     WHERE clinic_id = p_clinic_id AND barcode = e ->> 'barcode';
                    IF v_owner IS NULL THEN
                        INSERT INTO rx_item_barcodes (clinic_id, item_id, barcode, symbology)
                        VALUES (p_clinic_id, v_item.id, e ->> 'barcode', COALESCE(NULLIF(e ->> 'symbology', ''), 'internal'));
                    ELSIF v_owner <> v_item.id THEN
                        PERFORM rx_raise('rx_barcode_taken');
                    END IF;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                v_errors := v_errors || jsonb_build_object('row', COALESCE((e ->> '_row')::INTEGER, i), 'error', SQLERRM);
            END;
        END LOOP;
        IF NOT p_apply OR jsonb_array_length(v_errors) > 0 THEN
            RAISE EXCEPTION 'rx_import_rolled_back' USING ERRCODE = 'RXDRY';
        END IF;
    EXCEPTION WHEN SQLSTATE 'RXDRY' THEN
        NULL;   -- dry run, or a row failed: every row of this import is undone together
    END;
    RETURN jsonb_build_object('would_create', v_created, 'would_update', v_updated, 'unchanged', v_unchanged,
                              'errors', v_errors, 'applied', p_apply AND jsonb_array_length(v_errors) = 0);
END $$;

-- ── 6. Deterministic resolver (ADR-RX-06/07: no LLM, stable order) ─────────
-- barcode 100 > confirmed alias 95 > item code 90 > brand exact 85 / prefix 75 / contains 60
-- > generic exact 70 / contains 50. Ties: brand name, then id. Active items only.
-- shortcut: substring match, no trigram ranking; add pg_trgm similarity when the catalogue
-- outgrows ILIKE (≈50k items per clinic).
CREATE OR REPLACE FUNCTION rx_resolve_items(p_clinic_id UUID, p_q TEXT, p_limit INTEGER DEFAULT 5)
RETURNS JSONB LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
    WITH q AS (SELECT btrim(COALESCE(p_q, '')) AS raw, rx_normalize(p_q) AS n),
    cand AS (
        SELECT b.item_id, 100 AS score, 'barcode' AS source FROM rx_item_barcodes b, q
         WHERE b.clinic_id = p_clinic_id AND b.barcode = q.raw
        UNION ALL
        SELECT a.item_id, 95, 'alias' FROM rx_item_aliases a, q WHERE a.clinic_id = p_clinic_id AND a.alias_norm = q.n
        UNION ALL
        SELECT i.id, 90, 'code' FROM rx_items i, q WHERE i.clinic_id = p_clinic_id AND i.item_code = upper(q.raw)
        UNION ALL
        SELECT i.id, CASE WHEN rx_normalize(i.brand_name) = q.n THEN 85
                          WHEN rx_normalize(i.brand_name) LIKE q.n || '%' THEN 75 ELSE 60 END, 'brand'
          FROM rx_items i, q WHERE i.clinic_id = p_clinic_id AND char_length(q.n) >= 2
           AND rx_normalize(i.brand_name) LIKE '%' || q.n || '%'
        UNION ALL
        SELECT i.id, CASE WHEN rx_normalize(g.name) = q.n OR rx_normalize(g.name || ' ' || g.strength) = q.n
                          THEN 70 ELSE 50 END, 'generic'
          FROM rx_items i JOIN rx_generics g ON g.clinic_id = i.clinic_id AND g.id = i.generic_id, q
         WHERE i.clinic_id = p_clinic_id AND char_length(q.n) >= 2
           AND rx_normalize(g.name || ' ' || g.strength) LIKE '%' || q.n || '%'
    ),
    best AS (SELECT DISTINCT ON (item_id) item_id, score, source FROM cand ORDER BY item_id, score DESC, source),
    ranked AS (
        SELECT i.id, i.item_code, i.brand_name, i.generic_id, i.schedule, i.pack_size, i.base_unit, b.score, b.source
          FROM best b JOIN rx_items i ON i.clinic_id = p_clinic_id AND i.id = b.item_id
         WHERE i.is_active
         ORDER BY b.score DESC, i.brand_name, i.id
         LIMIT LEAST(GREATEST(COALESCE(p_limit, 5), 1), 50)
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object('item_id', id, 'item_code', item_code, 'brand_name', brand_name,
               'generic_id', generic_id, 'schedule', schedule, 'pack_size', pack_size, 'base_unit', base_unit,
               'score', score, 'source', source) ORDER BY score DESC, brand_name, id), '[]'::jsonb)
      FROM ranked
$$;

-- ── 7. Owner purge v2 (catalogue, then stores) ─────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- ── 8. RLS parity + API-role lockout (134 pattern) ─────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_manufacturers', 'rx_generics', 'rx_items', 'rx_item_barcodes', 'rx_item_aliases',
                             'rx_item_changes']
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
    FOREACH f IN ARRAY ARRAY['rx_normalize(text)', 'rx_schedule_rank(text)', 'rx_purge_clinic(uuid)',
                             'rx_update_item(uuid,uuid,jsonb,uuid,text,text)',
                             'rx_import_items(uuid,jsonb,boolean,uuid,text)', 'rx_resolve_items(uuid,text,integer)']
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
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_manufacturers', 'rx_generics', 'rx_items', 'rx_item_barcodes', 'rx_item_aliases',
                             'rx_item_changes']
    LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '135 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '135 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_rx_items_guard', 'trg_rx_item_changes_append_only')) <> 2 THEN
        RAISE EXCEPTION '135 verify: item guards missing';
    END IF;
    IF to_regprocedure('rx_update_item(uuid,uuid,jsonb,uuid,text,text)') IS NULL
       OR to_regprocedure('rx_import_items(uuid,jsonb,boolean,uuid,text)') IS NULL
       OR to_regprocedure('rx_resolve_items(uuid,text,integer)') IS NULL THEN
        RAISE EXCEPTION '135 verify: catalogue functions missing';
    END IF;
END $$;
