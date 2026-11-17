-- ============================================================================
-- Migration 137: Kriya Pharmacy OS purchase (RX-M4, RX-22)
-- Suppliers, purchase orders, goods receipt (GRN) and supplier returns; the GRN
-- line calculation (taxable, tax, freight share, landed unit cost), the posting,
-- void, PO-send and supplier-return functions; rx_purge_clinic v4.
--
-- Design: docs/pharmacy-os/DATA-MODEL.md §4, WORKFLOWS.md W1 / W6, TEST-PLAN §4.3.
-- Stock moves only through rx_post_grn / rx_void_grn / rx_supplier_return (ADR-RX-01).
-- Additive: new rx_* objects only. Rollback: rollback/137_rollback_rx_purchase.sql.
-- ============================================================================

-- ── 1. Suppliers ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_suppliers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id          UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name               TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 120),
    gstin              TEXT NULL CHECK (gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    state_code         CHAR(2) NULL CHECK (state_code ~ '^[0-9]{2}$'),
    drug_licence_no    TEXT NULL CHECK (char_length(drug_licence_no) <= 60),
    phone              TEXT NULL CHECK (char_length(phone) <= 20),
    email              TEXT NULL CHECK (char_length(email) <= 120),
    address            TEXT NULL CHECK (char_length(address) <= 300),
    payment_terms_days SMALLINT NOT NULL DEFAULT 30 CHECK (payment_terms_days BETWEEN 0 AND 365),
    lead_time_days     SMALLINT NOT NULL DEFAULT 3 CHECK (lead_time_days BETWEEN 0 AND 90),
    return_window_days SMALLINT NULL CHECK (return_window_days BETWEEN 0 AND 365),
    is_active          BOOLEAN NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_suppliers_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_suppliers_state_matches_gstin CHECK (gstin IS NULL OR state_code = left(gstin, 2))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_rx_suppliers_gstin ON rx_suppliers (clinic_id, gstin) WHERE gstin IS NOT NULL;

-- ── 2. Purchase orders ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_purchase_orders (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id     UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id      UUID NOT NULL,
    supplier_id   UUID NOT NULL,
    po_year       SMALLINT NULL,
    po_seq        INTEGER NULL,
    po_number     TEXT GENERATED ALWAYS AS (CASE WHEN po_seq IS NULL THEN NULL
                      ELSE opd_format_number('PO-', po_year, po_seq, 5) END) STORED,
    status        TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'sent', 'partially_received', 'received', 'cancelled')),
    expected_on   DATE NULL,
    notes         TEXT NULL CHECK (char_length(notes) <= 500),
    created_by    UUID NULL,
    created_by_name TEXT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    sent_at       TIMESTAMPTZ NULL,
    cancelled_at  TIMESTAMPTZ NULL,
    cancel_reason TEXT NULL CHECK (char_length(cancel_reason) <= 300),
    CONSTRAINT rx_po_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_po_number_key UNIQUE (clinic_id, po_year, po_seq),
    CONSTRAINT rx_po_numbered CHECK (status IN ('draft', 'cancelled') OR po_seq IS NOT NULL),
    CONSTRAINT rx_po_cancel_reason CHECK (status <> 'cancelled' OR char_length(btrim(COALESCE(cancel_reason, ''))) >= 3),
    CONSTRAINT rx_po_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_po_supplier_fk FOREIGN KEY (clinic_id, supplier_id) REFERENCES rx_suppliers (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_po_status ON rx_purchase_orders (clinic_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS rx_po_lines (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    po_id                    UUID NOT NULL,
    line_no                  SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 500),
    item_id                  UUID NOT NULL,
    ordered_packs            INTEGER NOT NULL CHECK (ordered_packs > 0),
    free_packs               INTEGER NOT NULL DEFAULT 0 CHECK (free_packs >= 0),
    expected_rate_pack_paise INTEGER NULL CHECK (expected_rate_pack_paise >= 0),
    received_packs           INTEGER NOT NULL DEFAULT 0 CHECK (received_packs >= 0),
    CONSTRAINT rx_po_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_po_lines_line_key UNIQUE (po_id, line_no),
    CONSTRAINT rx_po_lines_po_fk FOREIGN KEY (clinic_id, po_id) REFERENCES rx_purchase_orders (clinic_id, id),
    CONSTRAINT rx_po_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

-- ── 3. GRN ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_grns (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id              UUID NOT NULL,
    supplier_id           UUID NOT NULL,
    po_id                 UUID NULL,
    grn_year              SMALLINT NULL,
    grn_seq               INTEGER NULL,
    grn_number            TEXT GENERATED ALWAYS AS (CASE WHEN grn_seq IS NULL THEN NULL
                              ELSE opd_format_number('GRN-', grn_year, grn_seq, 5) END) STORED,
    supplier_invoice_no   TEXT NOT NULL CHECK (char_length(btrim(supplier_invoice_no)) BETWEEN 1 AND 40),
    supplier_invoice_date DATE NOT NULL,
    is_interstate         BOOLEAN NOT NULL DEFAULT false,
    status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'posted', 'void')),
    taxable_paise         BIGINT NOT NULL DEFAULT 0,
    cgst_paise            BIGINT NOT NULL DEFAULT 0,
    sgst_paise            BIGINT NOT NULL DEFAULT 0,
    igst_paise            BIGINT NOT NULL DEFAULT 0,
    -- shortcut: freight is added to landed cost without GST on freight; add a freight tax rate if a supplier bills it
    freight_paise         INTEGER NOT NULL DEFAULT 0 CHECK (freight_paise BETWEEN 0 AND 100000000),
    round_off_paise       SMALLINT NOT NULL DEFAULT 0 CHECK (round_off_paise BETWEEN -99 AND 99),
    invoice_total_paise   BIGINT NOT NULL DEFAULT 0 CHECK (invoice_total_paise >= 0),
    posted_at             TIMESTAMPTZ NULL,
    posted_by             UUID NULL,
    posted_by_name        TEXT NULL,
    voided_at             TIMESTAMPTZ NULL,
    void_reason           TEXT NULL CHECK (char_length(void_reason) <= 300),
    created_by            UUID NULL,
    created_by_name       TEXT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_grns_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_grns_number_key UNIQUE (clinic_id, grn_year, grn_seq),
    CONSTRAINT rx_grns_invoice_key UNIQUE (clinic_id, supplier_id, supplier_invoice_no),
    CONSTRAINT rx_grns_numbered CHECK (status = 'draft' OR grn_seq IS NOT NULL),
    CONSTRAINT rx_grns_void_reason CHECK (status <> 'void' OR char_length(btrim(COALESCE(void_reason, ''))) >= 3),
    CONSTRAINT rx_grns_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_grns_supplier_fk FOREIGN KEY (clinic_id, supplier_id) REFERENCES rx_suppliers (clinic_id, id),
    CONSTRAINT rx_grns_po_fk FOREIGN KEY (clinic_id, po_id) REFERENCES rx_purchase_orders (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_grns_status ON rx_grns (clinic_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS rx_grn_lines (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id                   UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    grn_id                      UUID NOT NULL,
    line_no                     SMALLINT NOT NULL CHECK (line_no BETWEEN 1 AND 500),
    item_id                     UUID NOT NULL,
    po_line_id                  UUID NULL,
    batch_no                    TEXT NOT NULL CHECK (batch_no ~ '^[A-Za-z0-9/._-]{1,30}$'),
    expiry_date                 DATE NOT NULL,
    mfg_date                    DATE NULL,
    gs1_serial                  TEXT NULL CHECK (char_length(gs1_serial) <= 40),
    packs                       INTEGER NOT NULL CHECK (packs > 0),
    free_packs                  INTEGER NOT NULL DEFAULT 0 CHECK (free_packs >= 0),
    rate_pack_paise             INTEGER NOT NULL CHECK (rate_pack_paise >= 0),
    scheme_discount_bps         SMALLINT NOT NULL DEFAULT 0 CHECK (scheme_discount_bps BETWEEN 0 AND 10000),
    cash_discount_bps           SMALLINT NOT NULL DEFAULT 0 CHECK (cash_discount_bps BETWEEN 0 AND 10000),
    tax_rate_bps                SMALLINT NOT NULL CHECK (tax_rate_bps IN (0, 500, 1200, 1800, 2800, 4000)),
    mrp_pack_paise              INTEGER NOT NULL CHECK (mrp_pack_paise BETWEEN 1 AND 100000000),
    taxable_paise               BIGINT NOT NULL DEFAULT 0,        -- computed by rx_grn_line_calc
    tax_paise                   BIGINT NOT NULL DEFAULT 0,        -- computed
    freight_share_paise         BIGINT NOT NULL DEFAULT 0,        -- computed by rx_grn_refresh
    landed_unit_cost_paise_x100 BIGINT NOT NULL DEFAULT 0,        -- computed
    batch_id                    UUID NULL,                         -- set when posted
    CONSTRAINT rx_grn_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_grn_lines_line_key UNIQUE (grn_id, line_no),
    CONSTRAINT rx_grn_lines_dates CHECK (mfg_date IS NULL OR mfg_date < expiry_date),
    CONSTRAINT rx_grn_lines_grn_fk FOREIGN KEY (clinic_id, grn_id) REFERENCES rx_grns (clinic_id, id),
    CONSTRAINT rx_grn_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id),
    CONSTRAINT rx_grn_lines_po_line_fk FOREIGN KEY (clinic_id, po_line_id) REFERENCES rx_po_lines (clinic_id, id),
    CONSTRAINT rx_grn_lines_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rx_grn_lines_item ON rx_grn_lines (clinic_id, item_id);

-- ── 4. Supplier returns ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rx_supplier_returns (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    store_id              UUID NOT NULL,
    supplier_id           UUID NOT NULL,
    srn_year              SMALLINT NULL,
    srn_seq               INTEGER NULL,
    srn_number            TEXT GENERATED ALWAYS AS (CASE WHEN srn_seq IS NULL THEN NULL
                              ELSE opd_format_number('SRN-', srn_year, srn_seq, 5) END) STORED,
    status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'dispatched', 'cancelled')),
    reason                TEXT NOT NULL CHECK (reason IN ('expired', 'near_expiry', 'damaged', 'recall', 'excess', 'other')),
    notes                 TEXT NULL CHECK (char_length(notes) <= 300),
    credit_received_paise BIGINT NULL CHECK (credit_received_paise >= 0),
    credit_ref            TEXT NULL CHECK (char_length(credit_ref) <= 60),
    created_by            UUID NULL,
    created_by_name       TEXT NULL,
    dispatched_by         UUID NULL,
    dispatched_by_name    TEXT NULL,
    dispatched_at         TIMESTAMPTZ NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT rx_srn_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_srn_number_key UNIQUE (clinic_id, srn_year, srn_seq),
    CONSTRAINT rx_srn_numbered CHECK (status <> 'dispatched' OR srn_seq IS NOT NULL),
    CONSTRAINT rx_srn_credit_after_dispatch CHECK (status = 'dispatched' OR (credit_received_paise IS NULL AND credit_ref IS NULL)),
    CONSTRAINT rx_srn_store_fk FOREIGN KEY (clinic_id, store_id) REFERENCES rx_stores (clinic_id, id),
    CONSTRAINT rx_srn_supplier_fk FOREIGN KEY (clinic_id, supplier_id) REFERENCES rx_suppliers (clinic_id, id)
);

CREATE TABLE IF NOT EXISTS rx_supplier_return_lines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id       UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    return_id       UUID NOT NULL,
    batch_id        UUID NOT NULL,
    item_id         UUID NOT NULL,                  -- filled from the batch
    qty_units       INTEGER NOT NULL CHECK (qty_units > 0),
    rate_pack_paise INTEGER NOT NULL,               -- filled from the batch's first GRN line (debit note)
    tax_rate_bps    SMALLINT NOT NULL,
    CONSTRAINT rx_srn_lines_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT rx_srn_lines_batch_key UNIQUE (return_id, batch_id),
    CONSTRAINT rx_srn_lines_return_fk FOREIGN KEY (clinic_id, return_id) REFERENCES rx_supplier_returns (clinic_id, id),
    CONSTRAINT rx_srn_lines_batch_fk FOREIGN KEY (clinic_id, batch_id) REFERENCES rx_batches (clinic_id, id),
    CONSTRAINT rx_srn_lines_item_fk FOREIGN KEY (clinic_id, item_id) REFERENCES rx_items (clinic_id, id)
);

-- ── 5. Guards: documents change only while draft (functions use kriya.rx_tx) ──
CREATE OR REPLACE FUNCTION rx_guard_purchase_doc() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE o JSONB; n JSONB;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF rx_purging(OLD.clinic_id) OR OLD.status = 'draft' THEN RETURN OLD; END IF;
        PERFORM rx_raise('rx_document_locked');
    END IF;
    IF NEW.clinic_id IS DISTINCT FROM OLD.clinic_id THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF OLD.status = 'draft' OR rx_in_tx(OLD.clinic_id) THEN RETURN NEW; END IF;
    -- generated numbers are NULL in a BEFORE row: compare everything else
    o := to_jsonb(OLD) - 'po_number' - 'grn_number' - 'srn_number';
    n := to_jsonb(NEW) - 'po_number' - 'grn_number' - 'srn_number';
    -- outside the posting functions only these moves are allowed after draft
    IF TG_TABLE_NAME = 'rx_purchase_orders' AND OLD.status = 'sent' AND NEW.status = 'cancelled'
       AND (n - 'status' - 'cancelled_at' - 'cancel_reason') = (o - 'status' - 'cancelled_at' - 'cancel_reason') THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'rx_supplier_returns' AND OLD.status = 'dispatched' AND NEW.status = 'dispatched'
       AND (n - 'credit_received_paise' - 'credit_ref') = (o - 'credit_received_paise' - 'credit_ref') THEN
        RETURN NEW;
    END IF;
    PERFORM rx_raise('rx_document_locked');
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_po_guard ON rx_purchase_orders;
CREATE TRIGGER trg_rx_po_guard BEFORE UPDATE OR DELETE ON rx_purchase_orders
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_doc();
DROP TRIGGER IF EXISTS trg_rx_grns_guard ON rx_grns;
CREATE TRIGGER trg_rx_grns_guard BEFORE UPDATE OR DELETE ON rx_grns
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_doc();
DROP TRIGGER IF EXISTS trg_rx_srn_guard ON rx_supplier_returns;
CREATE TRIGGER trg_rx_srn_guard BEFORE UPDATE OR DELETE ON rx_supplier_returns
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_doc();

-- Lines follow their header: editable only while it is draft (or inside an rx_* function).
CREATE OR REPLACE FUNCTION rx_guard_purchase_line() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r RECORD; v_status TEXT;
BEGIN
    r := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    IF rx_purging(r.clinic_id) OR rx_in_tx(r.clinic_id) THEN RETURN r; END IF;
    IF TG_TABLE_NAME = 'rx_po_lines' THEN
        SELECT status INTO v_status FROM rx_purchase_orders WHERE clinic_id = r.clinic_id AND id = r.po_id;
    ELSIF TG_TABLE_NAME = 'rx_grn_lines' THEN
        SELECT status INTO v_status FROM rx_grns WHERE clinic_id = r.clinic_id AND id = r.grn_id;
    ELSE
        SELECT status INTO v_status FROM rx_supplier_returns WHERE clinic_id = r.clinic_id AND id = r.return_id;
    END IF;
    IF v_status IS DISTINCT FROM 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    RETURN r;
END $$;
DROP TRIGGER IF EXISTS trg_rx_po_lines_guard ON rx_po_lines;
CREATE TRIGGER trg_rx_po_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON rx_po_lines
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_line();
DROP TRIGGER IF EXISTS trg_rx_grn_lines_guard ON rx_grn_lines;
CREATE TRIGGER trg_rx_grn_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON rx_grn_lines
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_line();
DROP TRIGGER IF EXISTS trg_rx_srn_lines_guard ON rx_supplier_return_lines;
CREATE TRIGGER trg_rx_srn_lines_guard BEFORE INSERT OR UPDATE OR DELETE ON rx_supplier_return_lines
    FOR EACH ROW EXECUTE FUNCTION rx_guard_purchase_line();

-- ── 6. GRN line calculation (TEST-PLAN §4.3) ───────────────────────────────
-- gross = packs × rate; scheme off gross, cash discount off the post-scheme value (half-up each);
-- tax = opd_gst_amount(taxable, rate). Landed unit cost ×100 = (taxable + tax when the clinic
-- has no GSTIN (no input credit) + freight share) × 100 / ((packs + free) × pack size), half-up.
-- Free packs dilute cost; they never change MRP. An expired batch is refused.
CREATE OR REPLACE FUNCTION rx_grn_line_calc() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v_gross BIGINT; v_scheme BIGINT; v_cash BIGINT; v_pack INTEGER; v_cost BIGINT; v_units BIGINT;
    g rx_grns;
BEGIN
    IF rx_purging(NEW.clinic_id) THEN RETURN NEW; END IF;
    SELECT * INTO g FROM rx_grns WHERE clinic_id = NEW.clinic_id AND id = NEW.grn_id;
    IF g.status = 'draft' THEN
        IF NEW.expiry_date < rx_ist_date(now()) THEN PERFORM rx_raise('rx_grn_expired_batch'); END IF;
        IF NEW.po_line_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM rx_po_lines WHERE clinic_id = NEW.clinic_id AND id = NEW.po_line_id
                  AND po_id IS NOT DISTINCT FROM g.po_id AND item_id = NEW.item_id) THEN
            PERFORM rx_raise('rx_grn_po_line_mismatch');
        END IF;
    END IF;
    SELECT pack_size INTO v_pack FROM rx_items WHERE clinic_id = NEW.clinic_id AND id = NEW.item_id;
    v_gross := NEW.packs::BIGINT * NEW.rate_pack_paise;
    v_scheme := (v_gross * NEW.scheme_discount_bps + 5000) / 10000;
    v_cash := ((v_gross - v_scheme) * NEW.cash_discount_bps + 5000) / 10000;
    NEW.taxable_paise := v_gross - v_scheme - v_cash;
    NEW.tax_paise := opd_gst_amount(NEW.taxable_paise, NEW.tax_rate_bps);
    v_cost := NEW.taxable_paise + NEW.freight_share_paise
              + CASE WHEN opd_clinic_gstin(NEW.clinic_id) IS NULL THEN NEW.tax_paise ELSE 0 END;
    v_units := (NEW.packs + NEW.free_packs)::BIGINT * COALESCE(v_pack, 1);
    NEW.landed_unit_cost_paise_x100 := (v_cost * 100 + v_units / 2) / v_units;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_grn_line_calc ON rx_grn_lines;
CREATE TRIGGER trg_rx_grn_line_calc BEFORE INSERT OR UPDATE ON rx_grn_lines
    FOR EACH ROW EXECUTE FUNCTION rx_grn_line_calc();

-- Header totals and the freight split (largest remainder by taxable value, ties by line number).
CREATE OR REPLACE FUNCTION rx_grn_refresh(p_clinic_id UUID, p_grn_id UUID) RETURNS VOID
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE g rx_grns; v_tax BIGINT; v_taxable BIGINT; v_cgst BIGINT;
BEGIN
    SELECT * INTO g FROM rx_grns WHERE clinic_id = p_clinic_id AND id = p_grn_id;
    IF NOT FOUND OR g.status <> 'draft' THEN RETURN; END IF;
    WITH l AS (SELECT id, line_no, taxable_paise t FROM rx_grn_lines WHERE clinic_id = p_clinic_id AND grn_id = p_grn_id),
         tot AS (SELECT NULLIF(SUM(t), 0) AS s FROM l),
         b AS (SELECT l.id, l.line_no, COALESCE(g.freight_paise::BIGINT * l.t / tot.s, 0) AS base,
                      COALESCE(g.freight_paise::BIGINT * l.t % tot.s, 0) AS rem FROM l, tot),
         r AS (SELECT id, base + CASE WHEN row_number() OVER (ORDER BY rem DESC, line_no)
                                   <= (SELECT COALESCE(g.freight_paise - SUM(base), 0) FROM b) THEN 1 ELSE 0 END AS share
                 FROM b WHERE (SELECT s FROM tot) IS NOT NULL)
    UPDATE rx_grn_lines x SET freight_share_paise = r.share
      FROM r WHERE x.clinic_id = p_clinic_id AND x.id = r.id AND x.freight_share_paise IS DISTINCT FROM r.share;
    SELECT COALESCE(SUM(taxable_paise), 0), COALESCE(SUM(tax_paise), 0),
           COALESCE(SUM(tax_paise / 2), 0)
      INTO v_taxable, v_tax, v_cgst FROM rx_grn_lines WHERE clinic_id = p_clinic_id AND grn_id = p_grn_id;
    UPDATE rx_grns SET taxable_paise = v_taxable,
           cgst_paise = CASE WHEN is_interstate THEN 0 ELSE v_cgst END,
           sgst_paise = CASE WHEN is_interstate THEN 0 ELSE v_tax - v_cgst END,
           igst_paise = CASE WHEN is_interstate THEN v_tax ELSE 0 END
     WHERE clinic_id = p_clinic_id AND id = p_grn_id
       AND (taxable_paise, cgst_paise, sgst_paise, igst_paise) IS DISTINCT FROM
           (v_taxable, CASE WHEN is_interstate THEN 0 ELSE v_cgst END,
            CASE WHEN is_interstate THEN 0 ELSE v_tax - v_cgst END, CASE WHEN is_interstate THEN v_tax ELSE 0 END);
END $$;

CREATE OR REPLACE FUNCTION rx_grn_lines_after() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r RECORD;
BEGIN
    IF pg_trigger_depth() > 1 THEN RETURN NULL; END IF;      -- the refresh's own line updates
    FOR r IN SELECT DISTINCT clinic_id, grn_id FROM changed_lines LOOP
        PERFORM rx_grn_refresh(r.clinic_id, r.grn_id);
    END LOOP;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_grn_lines_after_ins ON rx_grn_lines;
CREATE TRIGGER trg_rx_grn_lines_after_ins AFTER INSERT ON rx_grn_lines
    REFERENCING NEW TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_grn_lines_after();
DROP TRIGGER IF EXISTS trg_rx_grn_lines_after_upd ON rx_grn_lines;
CREATE TRIGGER trg_rx_grn_lines_after_upd AFTER UPDATE ON rx_grn_lines
    REFERENCING NEW TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_grn_lines_after();
DROP TRIGGER IF EXISTS trg_rx_grn_lines_after_del ON rx_grn_lines;
CREATE TRIGGER trg_rx_grn_lines_after_del AFTER DELETE ON rx_grn_lines
    REFERENCING OLD TABLE AS changed_lines FOR EACH STATEMENT EXECUTE FUNCTION rx_grn_lines_after();

CREATE OR REPLACE FUNCTION rx_grn_header_after() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
    IF pg_trigger_depth() = 1 AND NEW.status = 'draft'
       AND (NEW.freight_paise, NEW.is_interstate) IS DISTINCT FROM (OLD.freight_paise, OLD.is_interstate) THEN
        PERFORM rx_grn_refresh(NEW.clinic_id, NEW.id);
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS trg_rx_grns_after ON rx_grns;
CREATE TRIGGER trg_rx_grns_after AFTER UPDATE ON rx_grns
    FOR EACH ROW EXECUTE FUNCTION rx_grn_header_after();

-- Supplier return lines: item, rate and GST come from the batch (its first GRN line), never typed.
CREATE OR REPLACE FUNCTION rx_srn_line_fill() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE b rx_batches; gl rx_grn_lines; v_tax SMALLINT;
BEGIN
    SELECT * INTO b FROM rx_batches WHERE clinic_id = NEW.clinic_id AND id = NEW.batch_id;
    IF NOT FOUND THEN PERFORM rx_raise('rx_batch_not_found'); END IF;
    SELECT * INTO gl FROM rx_grn_lines WHERE clinic_id = NEW.clinic_id AND batch_id = b.id
     ORDER BY (id = b.first_grn_line_id) DESC, line_no LIMIT 1;
    SELECT tax_rate_bps INTO v_tax FROM rx_items WHERE clinic_id = NEW.clinic_id AND id = b.item_id;
    NEW.item_id := b.item_id;
    NEW.rate_pack_paise := COALESCE(gl.rate_pack_paise, b.ptr_pack_paise, 0);
    NEW.tax_rate_bps := COALESCE(gl.tax_rate_bps, v_tax, 0);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_rx_srn_line_fill ON rx_supplier_return_lines;
CREATE TRIGGER trg_rx_srn_line_fill BEFORE INSERT OR UPDATE OF batch_id ON rx_supplier_return_lines
    FOR EACH ROW EXECUTE FUNCTION rx_srn_line_fill();

-- ── 7. Posting functions ───────────────────────────────────────────────────
-- Send a draft PO: numbered last (opd_next_counter locks the clinics row, VERIFICATION D12).
CREATE OR REPLACE FUNCTION rx_send_po(p_clinic_id UUID, p_po_id UUID) RETURNS JSONB
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE p rx_purchase_orders; v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    SELECT * INTO p FROM rx_purchase_orders WHERE clinic_id = p_clinic_id AND id = p_po_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_po_not_found'); END IF;
    IF p.status <> 'draft' THEN
        IF p.status IN ('sent', 'partially_received', 'received') THEN
            RETURN jsonb_build_object('po_number', p.po_number, 'replayed', true);
        END IF;
        PERFORM rx_raise('rx_document_locked');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_po_lines WHERE clinic_id = p_clinic_id AND po_id = p_po_id) THEN
        PERFORM rx_raise('rx_po_empty');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    UPDATE rx_purchase_orders SET status = 'sent', sent_at = clock_timestamp(), po_year = v_year,
           po_seq = opd_next_counter(p_clinic_id, 'rx_po:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = p_po_id RETURNING * INTO p;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('po_number', p.po_number, 'replayed', false);
END $$;

CREATE OR REPLACE FUNCTION rx_po_refresh_status(p_clinic_id UUID, p_po_id UUID) RETURNS VOID
LANGUAGE sql SET search_path = public, pg_temp AS $$
    UPDATE rx_purchase_orders p SET status = CASE
             WHEN NOT EXISTS (SELECT 1 FROM rx_po_lines l WHERE l.clinic_id = p.clinic_id AND l.po_id = p.id
                                AND l.received_packs < l.ordered_packs) THEN 'received'
             WHEN EXISTS (SELECT 1 FROM rx_po_lines l WHERE l.clinic_id = p.clinic_id AND l.po_id = p.id
                            AND l.received_packs > 0) THEN 'partially_received'
             ELSE 'sent' END
     WHERE p.clinic_id = p_clinic_id AND p.id = p_po_id AND p.status IN ('sent', 'partially_received', 'received');
$$;

-- Post a draft GRN [F]. Lock order: GRN row → movements in (batch) order → number last.
CREATE OR REPLACE FUNCTION rx_post_grn(p_clinic_id UUID, p_grn_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE g rx_grns; l rx_grn_lines; v_pack INTEGER; v_batch UUID; v_diff BIGINT; v_n INTEGER := 0; v_units BIGINT := 0;
    v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    SELECT * INTO g FROM rx_grns WHERE clinic_id = p_clinic_id AND id = p_grn_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_grn_not_found'); END IF;
    IF g.status = 'posted' THEN
        RETURN jsonb_build_object('grn_number', g.grn_number, 'replayed', true);
    END IF;
    IF g.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_grn_lines WHERE clinic_id = p_clinic_id AND grn_id = p_grn_id) THEN
        PERFORM rx_raise('rx_grn_empty');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_stores WHERE clinic_id = p_clinic_id AND id = g.store_id AND is_active
                     AND can_receive_grn) THEN
        PERFORM rx_raise('rx_store_cannot_receive');
    END IF;
    PERFORM rx_grn_refresh(p_clinic_id, p_grn_id);
    SELECT * INTO g FROM rx_grns WHERE clinic_id = p_clinic_id AND id = p_grn_id;
    v_diff := g.invoice_total_paise - (g.taxable_paise + g.cgst_paise + g.sgst_paise + g.igst_paise + g.freight_paise);
    IF abs(v_diff) > 99 THEN PERFORM rx_raise('rx_grn_total_mismatch:' || v_diff); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT * FROM rx_grn_lines WHERE clinic_id = p_clinic_id AND grn_id = p_grn_id ORDER BY line_no LOOP
        IF l.expiry_date < rx_ist_date(now()) THEN PERFORM rx_raise('rx_grn_expired_batch'); END IF;
        SELECT pack_size INTO v_pack FROM rx_items WHERE clinic_id = p_clinic_id AND id = l.item_id;
        v_batch := NULL;
        INSERT INTO rx_batches (clinic_id, item_id, batch_no, expiry_date, mfg_date, mrp_pack_paise, ptr_pack_paise,
                                landed_unit_cost_paise_x100, first_grn_line_id)
        VALUES (p_clinic_id, l.item_id, l.batch_no, l.expiry_date, l.mfg_date, l.mrp_pack_paise, l.rate_pack_paise,
                l.landed_unit_cost_paise_x100, l.id)
        ON CONFLICT ON CONSTRAINT rx_batches_identity_key DO NOTHING
        RETURNING id INTO v_batch;
        IF v_batch IS NULL THEN
            SELECT id INTO v_batch FROM rx_batches
             WHERE clinic_id = p_clinic_id AND item_id = l.item_id AND batch_no = l.batch_no
               AND expiry_date = l.expiry_date AND mrp_pack_paise = l.mrp_pack_paise;
        END IF;
        UPDATE rx_grn_lines SET batch_id = v_batch WHERE clinic_id = p_clinic_id AND id = l.id;
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                        ref_id, unit_cost_paise_x100, actor_id, actor_name)
        VALUES (p_clinic_id, g.store_id, v_batch, l.item_id, (l.packs + l.free_packs) * v_pack, 'grn_in',
                'rx_grn_lines', l.id, l.landed_unit_cost_paise_x100, p_actor_id, p_actor_name);
        IF l.po_line_id IS NOT NULL THEN
            UPDATE rx_po_lines SET received_packs = received_packs + l.packs
             WHERE clinic_id = p_clinic_id AND id = l.po_line_id;
        END IF;
        v_n := v_n + 1; v_units := v_units + (l.packs + l.free_packs) * v_pack;
    END LOOP;
    IF g.po_id IS NOT NULL THEN PERFORM rx_po_refresh_status(p_clinic_id, g.po_id); END IF;
    UPDATE rx_grns SET status = 'posted', round_off_paise = v_diff, posted_at = clock_timestamp(),
           posted_by = p_actor_id, posted_by_name = p_actor_name, grn_year = v_year,
           grn_seq = opd_next_counter(p_clinic_id, 'rx_grn:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = p_grn_id RETURNING * INTO g;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('grn_number', g.grn_number, 'lines', v_n, 'units', v_units, 'round_off_paise', v_diff,
                              'replayed', false);
END $$;

-- Void a posted GRN: only while every unit it brought in is still on hand in that store
-- (else 409 rx_grn_stock_consumed: use a supplier return). Reverses with adjust_out.
CREATE OR REPLACE FUNCTION rx_void_grn(p_clinic_id UUID, p_grn_id UUID, p_reason TEXT, p_actor_id UUID,
                                       p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE g rx_grns; l RECORD; v_have INTEGER;
BEGIN
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM rx_raise('rx_reason_required'); END IF;
    SELECT * INTO g FROM rx_grns WHERE clinic_id = p_clinic_id AND id = p_grn_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_grn_not_found'); END IF;
    IF g.status = 'void' THEN RETURN jsonb_build_object('grn_number', g.grn_number, 'replayed', true); END IF;
    IF g.status <> 'posted' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT gl.id, gl.batch_id, gl.item_id, gl.po_line_id, gl.packs,
                    m.qty_units, m.unit_cost_paise_x100
               FROM rx_grn_lines gl JOIN rx_stock_movements m
                 ON m.clinic_id = gl.clinic_id AND m.ref_table = 'rx_grn_lines' AND m.ref_id = gl.id
                AND m.movement_type = 'grn_in'
              WHERE gl.clinic_id = p_clinic_id AND gl.grn_id = p_grn_id ORDER BY gl.batch_id, gl.line_no LOOP
        SELECT qty_units INTO v_have FROM rx_stock_balances
         WHERE clinic_id = p_clinic_id AND store_id = g.store_id AND batch_id = l.batch_id FOR UPDATE;
        IF COALESCE(v_have, 0) < l.qty_units THEN PERFORM rx_raise('rx_grn_stock_consumed'); END IF;
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                        ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, g.store_id, l.batch_id, l.item_id, -l.qty_units, 'adjust_out', 'rx_grn_void', l.id,
                l.unit_cost_paise_x100, p_actor_id, p_actor_name, left('GRN void: ' || btrim(p_reason), 300));
        IF l.po_line_id IS NOT NULL THEN
            UPDATE rx_po_lines SET received_packs = GREATEST(received_packs - l.packs, 0)
             WHERE clinic_id = p_clinic_id AND id = l.po_line_id;
        END IF;
    END LOOP;
    IF g.po_id IS NOT NULL THEN PERFORM rx_po_refresh_status(p_clinic_id, g.po_id); END IF;
    UPDATE rx_grns SET status = 'void', void_reason = btrim(p_reason), voided_at = clock_timestamp()
     WHERE clinic_id = p_clinic_id AND id = p_grn_id;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('grn_number', g.grn_number, 'replayed', false);
END $$;

-- Dispatch a draft supplier return [F]: one supplier_return_out per line (expired / blocked
-- batches allowed, ADR-RX-02); SRN number last.
CREATE OR REPLACE FUNCTION rx_supplier_return(p_clinic_id UUID, p_return_id UUID, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE r rx_supplier_returns; l RECORD; v_cost BIGINT; v_units BIGINT := 0;
    v_year INTEGER := extract(year FROM rx_ist_date(now()));
BEGIN
    SELECT * INTO r FROM rx_supplier_returns WHERE clinic_id = p_clinic_id AND id = p_return_id FOR UPDATE;
    IF NOT FOUND THEN PERFORM rx_raise('rx_return_not_found'); END IF;
    IF r.status = 'dispatched' THEN RETURN jsonb_build_object('srn_number', r.srn_number, 'replayed', true); END IF;
    IF r.status <> 'draft' THEN PERFORM rx_raise('rx_document_locked'); END IF;
    IF NOT EXISTS (SELECT 1 FROM rx_supplier_return_lines WHERE clinic_id = p_clinic_id AND return_id = p_return_id) THEN
        PERFORM rx_raise('rx_return_empty');
    END IF;
    PERFORM set_config('kriya.rx_tx', p_clinic_id::TEXT, true);
    FOR l IN SELECT * FROM rx_supplier_return_lines WHERE clinic_id = p_clinic_id AND return_id = p_return_id
              ORDER BY batch_id LOOP
        SELECT landed_unit_cost_paise_x100 INTO v_cost FROM rx_batches WHERE clinic_id = p_clinic_id AND id = l.batch_id;
        INSERT INTO rx_stock_movements (clinic_id, store_id, batch_id, item_id, qty_units, movement_type, ref_table,
                                        ref_id, unit_cost_paise_x100, actor_id, actor_name, reason)
        VALUES (p_clinic_id, r.store_id, l.batch_id, l.item_id, -l.qty_units, 'supplier_return_out',
                'rx_supplier_return_lines', l.id, v_cost, p_actor_id, p_actor_name, 'Supplier return: ' || r.reason);
        v_units := v_units + l.qty_units;
    END LOOP;
    UPDATE rx_supplier_returns SET status = 'dispatched', dispatched_at = clock_timestamp(), dispatched_by = p_actor_id,
           dispatched_by_name = p_actor_name, srn_year = v_year,
           srn_seq = opd_next_counter(p_clinic_id, 'rx_srn:' || v_year)
     WHERE clinic_id = p_clinic_id AND id = p_return_id RETURNING * INTO r;
    PERFORM set_config('kriya.rx_tx', '', true);
    RETURN jsonb_build_object('srn_number', r.srn_number, 'units', v_units, 'replayed', false);
END $$;

-- Last supplier and rate per item (W1 step 1: reorder grouped by last supplier).
CREATE OR REPLACE VIEW rx_v_item_last_supplier WITH (security_invoker = true) AS
    SELECT DISTINCT ON (gl.clinic_id, gl.item_id) gl.clinic_id, gl.item_id, g.supplier_id, s.name AS supplier_name,
           gl.rate_pack_paise AS last_rate_pack_paise, g.posted_at AS last_received_at
      FROM rx_grn_lines gl
      JOIN rx_grns g ON g.clinic_id = gl.clinic_id AND g.id = gl.grn_id AND g.status = 'posted'
      JOIN rx_suppliers s ON s.clinic_id = g.clinic_id AND s.id = g.supplier_id
     ORDER BY gl.clinic_id, gl.item_id, g.posted_at DESC, gl.line_no;

-- ── 8. Owner purge v4 ──────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION rx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.rx_purge_clinic', p_clinic_id::TEXT, true);
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

-- ── 9. RLS parity + API-role lockout ───────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_suppliers', 'rx_purchase_orders', 'rx_po_lines', 'rx_grns', 'rx_grn_lines',
                             'rx_supplier_returns', 'rx_supplier_return_lines']
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
    FOREACH t IN ARRAY ARRAY['rx_suppliers', 'rx_purchase_orders', 'rx_po_lines', 'rx_grns', 'rx_grn_lines',
                             'rx_supplier_returns', 'rx_supplier_return_lines', 'rx_v_item_last_supplier']
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
    FOREACH f IN ARRAY ARRAY['rx_grn_refresh(uuid,uuid)', 'rx_send_po(uuid,uuid)', 'rx_po_refresh_status(uuid,uuid)',
                             'rx_post_grn(uuid,uuid,uuid,text)', 'rx_void_grn(uuid,uuid,text,uuid,text)',
                             'rx_supplier_return(uuid,uuid,uuid,text)', 'rx_purge_clinic(uuid)']
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

-- ── 10. Verify ─────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['rx_suppliers', 'rx_purchase_orders', 'rx_po_lines', 'rx_grns', 'rx_grn_lines',
                             'rx_supplier_returns', 'rx_supplier_return_lines'] LOOP
        IF to_regclass('public.' || t) IS NULL THEN RAISE EXCEPTION '137 verify: % missing', t; END IF;
        IF NOT (SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = ('public.' || t)::regclass) THEN
            RAISE EXCEPTION '137 verify: % RLS not enabled + forced', t;
        END IF;
    END LOOP;
    IF to_regprocedure('rx_post_grn(uuid,uuid,uuid,text)') IS NULL OR to_regprocedure('rx_void_grn(uuid,uuid,text,uuid,text)') IS NULL
       OR to_regprocedure('rx_send_po(uuid,uuid)') IS NULL OR to_regprocedure('rx_supplier_return(uuid,uuid,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '137 verify: purchase functions missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_rx_grn_line_calc') THEN
        RAISE EXCEPTION '137 verify: trg_rx_grn_line_calc missing';
    END IF;
END $$;
