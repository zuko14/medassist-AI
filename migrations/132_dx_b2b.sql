-- ============================================================================
-- Migration 132: Kriya Diagnostic OS B2B money (DX-M8) — rate cards, the
-- append-only B2B ledger, statements, holds, partner logins, barcode stock.
--
-- b2b_credit / b2b_prepaid / b2b_split visits post one 'charge' ledger row per
-- item (uq_dx_ledger_charge: never twice); cancelling an item or a visit posts a
-- 'charge_reversal'. Balance, ageing and overdue are derived (dx_b2b_balance,
-- STABLE), never stored. Statements B2B-YYYY-NNNNN take the gapless
-- 'dx_b2b:YYYY' counter and link every unbilled ledger row up to the period end.
-- Zero-LLM: every posting, total and check is SQL (ADR-DX-04, ADR-DX-10).
-- No referral commission of any kind (ADR-DX-11).
--
-- Design: docs/diagnostic-os/DATA-MODEL.md §9, ADR-DX-04/09/11/20, W5, W14, SECURITY §5.
-- Additive: five new tables, new account / login columns, new functions,
-- dx_price_items replaced (rate cards, co-pay), dx_purge_clinic v8, and small
-- guarded text patches (verified below, reversed by the rollback) in
-- dx_register_visit / dx_add_items / dx_quote (account in the pricing context,
-- charges after the lines), dx_cancel_item / dx_cancel_visit (reversals),
-- dx_bill_visit (b2b_split bills the patient share), dx_insert_lines (rate card id)
-- and dx_bind_barcode (stock range check). Patient-billed visits are unchanged.
-- ============================================================================

-- ── 1. Rate cards ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_rate_cards (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
    kind       TEXT NOT NULL CHECK (kind IN ('b2b', 'corporate', 'camp', 'insurance')),
    valid_from DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_to   DATE NULL,
    is_active  BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_rate_cards_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_rate_cards_validity CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS dx_rate_card_items (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id    UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    rate_card_id UUID NOT NULL,
    lab_test_id  UUID NOT NULL,
    price_paise  INTEGER NOT NULL CHECK (price_paise BETWEEN 0 AND 100000000),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_rate_card_items_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_rate_card_items_test_key UNIQUE (clinic_id, rate_card_id, lab_test_id),
    CONSTRAINT dx_rate_card_items_card_fk FOREIGN KEY (clinic_id, rate_card_id)
        REFERENCES dx_rate_cards (clinic_id, id) ON DELETE CASCADE,
    CONSTRAINT dx_rate_card_items_test_fk FOREIGN KEY (clinic_id, lab_test_id)
        REFERENCES lab_tests (clinic_id, id) ON DELETE CASCADE
);

-- ── 2. Account money columns, partner logins ──────────────────────────────
ALTER TABLE dx_b2b_accounts
    ADD COLUMN IF NOT EXISTS rate_card_id       UUID NULL,
    ADD COLUMN IF NOT EXISTS credit_limit_paise BIGINT NULL CHECK (credit_limit_paise >= 0),
    ADD COLUMN IF NOT EXISTS credit_days        SMALLINT NULL CHECK (credit_days BETWEEN 0 AND 365),
    ADD COLUMN IF NOT EXISTS gstin              TEXT NULL CHECK (gstin ~ '^[0-9A-Z]{15}$'),
    ADD COLUMN IF NOT EXISTS billing_address    TEXT NULL CHECK (char_length(billing_address) <= 500),
    ADD COLUMN IF NOT EXISTS statement_cycle    TEXT NOT NULL DEFAULT 'monthly'
                                                CHECK (statement_cycle IN ('weekly', 'fortnightly', 'monthly')),
    ADD COLUMN IF NOT EXISTS auto_hold          BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS hold_kind          TEXT NULL CHECK (hold_kind IN ('manual', 'limit', 'overdue')),
    -- ADR-DX-20 co-pay for b2b_split: basis points of each item's net (percent) or paise per visit (fixed)
    ADD COLUMN IF NOT EXISTS copay_mode         TEXT NOT NULL DEFAULT 'none' CHECK (copay_mode IN ('none', 'percent', 'fixed')),
    ADD COLUMN IF NOT EXISTS copay_value        INTEGER NULL CHECK (copay_value >= 0);
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_copay_shape;
ALTER TABLE dx_b2b_accounts ADD CONSTRAINT dx_b2b_copay_shape CHECK ((copay_mode = 'none') = (copay_value IS NULL));
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_copay_percent;
ALTER TABLE dx_b2b_accounts ADD CONSTRAINT dx_b2b_copay_percent CHECK (copay_mode <> 'percent' OR copay_value <= 10000);
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_hold_kind_shape;
ALTER TABLE dx_b2b_accounts ADD CONSTRAINT dx_b2b_hold_kind_shape CHECK (hold_kind IS NULL OR status = 'on_hold');
ALTER TABLE dx_b2b_accounts DROP CONSTRAINT IF EXISTS dx_b2b_accounts_rate_card_fk;
ALTER TABLE dx_b2b_accounts ADD CONSTRAINT dx_b2b_accounts_rate_card_fk FOREIGN KEY (clinic_id, rate_card_id)
    REFERENCES dx_rate_cards (clinic_id, id);

ALTER TABLE clinic_admins ADD COLUMN IF NOT EXISTS dx_b2b_account_id UUID NULL;
ALTER TABLE clinic_admins DROP CONSTRAINT IF EXISTS clinic_admins_dx_b2b_account_fk;
ALTER TABLE clinic_admins ADD CONSTRAINT clinic_admins_dx_b2b_account_fk FOREIGN KEY (clinic_id, dx_b2b_account_id)
    REFERENCES dx_b2b_accounts (clinic_id, id);
CREATE INDEX IF NOT EXISTS idx_clinic_admins_dx_b2b_account ON clinic_admins (clinic_id, dx_b2b_account_id)
    WHERE dx_b2b_account_id IS NOT NULL;

-- ── 3. Statements (before the ledger: ledger rows point at them) ──────────
CREATE TABLE IF NOT EXISTS dx_b2b_statements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id           UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    account_id          UUID NOT NULL,
    statement_year      SMALLINT NOT NULL,
    statement_seq       INTEGER NOT NULL CHECK (statement_seq > 0),
    statement_number    TEXT GENERATED ALWAYS AS (opd_format_number('B2B-', statement_year, statement_seq, 5)) STORED,
    period_from         DATE NOT NULL,
    period_to           DATE NOT NULL,
    total_charges_paise BIGINT NOT NULL,
    total_credits_paise BIGINT NOT NULL,
    opening_paise       BIGINT NOT NULL,
    closing_paise       BIGINT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'void')),
    pdf_storage_path    TEXT NULL,
    issued_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    issued_by           UUID NULL,
    issued_by_name      TEXT NULL,
    void_reason         TEXT NULL CHECK (char_length(btrim(void_reason)) BETWEEN 3 AND 300),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_b2b_statements_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_b2b_statements_number_key UNIQUE (clinic_id, statement_year, statement_seq),
    CONSTRAINT dx_b2b_statements_period CHECK (period_to >= period_from),
    CONSTRAINT dx_b2b_statements_closing CHECK (closing_paise = opening_paise + total_charges_paise - total_credits_paise),
    CONSTRAINT dx_b2b_statements_void_shape CHECK ((status = 'void') = (void_reason IS NOT NULL)),
    CONSTRAINT dx_b2b_statements_account_fk FOREIGN KEY (clinic_id, account_id) REFERENCES dx_b2b_accounts (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_b2b_statement_period
    ON dx_b2b_statements (clinic_id, account_id, period_from, period_to) WHERE status <> 'void';

-- ── 4. The ledger (append-only; sign implied by entry_type) ───────────────
CREATE TABLE IF NOT EXISTS dx_b2b_ledger (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id         UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    account_id        UUID NOT NULL,
    entry_type        TEXT NOT NULL CHECK (entry_type IN ('opening_balance', 'charge', 'charge_reversal', 'payment',
                                                          'prepaid_topup', 'credit_note', 'write_off')),
    amount_paise      BIGINT NOT NULL CHECK (amount_paise > 0),
    accession_id      UUID NULL,
    accession_item_id UUID NULL,
    statement_id      UUID NULL,
    payment_mode      TEXT NULL CHECK (payment_mode IN ('neft', 'upi', 'cheque', 'cash', 'card', 'gateway')),
    reference         TEXT NULL CHECK (char_length(reference) <= 80),
    reason            TEXT NULL CHECK (char_length(reason) <= 300),
    idempotency_key   TEXT NULL CHECK (char_length(idempotency_key) <= 120),
    created_by        UUID NULL,
    created_by_name   TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT dx_b2b_ledger_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_b2b_ledger_item_shape CHECK (entry_type NOT IN ('charge', 'charge_reversal')
        OR (accession_id IS NOT NULL AND accession_item_id IS NOT NULL)),
    CONSTRAINT dx_b2b_ledger_payment_shape CHECK ((entry_type IN ('payment', 'prepaid_topup')) = (payment_mode IS NOT NULL)),
    CONSTRAINT dx_b2b_ledger_account_fk FOREIGN KEY (clinic_id, account_id) REFERENCES dx_b2b_accounts (clinic_id, id),
    CONSTRAINT dx_b2b_ledger_accession_fk FOREIGN KEY (clinic_id, accession_id) REFERENCES dx_accessions (clinic_id, id),
    CONSTRAINT dx_b2b_ledger_item_fk FOREIGN KEY (clinic_id, accession_item_id)
        REFERENCES dx_accession_items (clinic_id, id),
    CONSTRAINT dx_b2b_ledger_statement_fk FOREIGN KEY (clinic_id, statement_id)
        REFERENCES dx_b2b_statements (clinic_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_ledger_charge ON dx_b2b_ledger (clinic_id, accession_item_id)
    WHERE entry_type = 'charge';
CREATE UNIQUE INDEX IF NOT EXISTS uq_dx_ledger_idem ON dx_b2b_ledger (clinic_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dx_ledger_account ON dx_b2b_ledger (clinic_id, account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dx_ledger_statement ON dx_b2b_ledger (clinic_id, statement_id) WHERE statement_id IS NOT NULL;

-- Rows are written only by dx_b2b_* functions and never change, except the statement
-- link (set on issue, cleared on void). Deleted only by the clinic purge.
CREATE OR REPLACE FUNCTION dx_guard_b2b_ledger() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_ledger_append_only');
    END IF;
    IF NOT dx_in_tx(NEW.clinic_id) THEN PERFORM dx_raise('dx_managed:dx_b2b_ledger'); END IF;
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - 'statement_id') IS DISTINCT FROM (to_jsonb(OLD) - 'statement_id') THEN
        PERFORM dx_raise('dx_ledger_append_only');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_b2b_ledger_guard ON dx_b2b_ledger;
CREATE TRIGGER trg_dx_b2b_ledger_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_b2b_ledger
    FOR EACH ROW EXECUTE FUNCTION dx_guard_b2b_ledger();

-- A statement is issued and voided only by its functions; the PDF path may be set once afterwards.
CREATE OR REPLACE FUNCTION dx_guard_b2b_statement() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_statement_immutable');
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT dx_in_tx(NEW.clinic_id) THEN PERFORM dx_raise('dx_managed:dx_b2b_statements'); END IF;
        RETURN NEW;
    END IF;
    -- statement_number is generated: not yet computed in NEW of a BEFORE trigger
    IF (to_jsonb(NEW) - 'pdf_storage_path' - 'status' - 'void_reason' - 'statement_number')
           IS DISTINCT FROM (to_jsonb(OLD) - 'pdf_storage_path' - 'status' - 'void_reason' - 'statement_number')
       OR (NEW.status IS DISTINCT FROM OLD.status AND NOT (dx_in_tx(NEW.clinic_id) AND OLD.status = 'issued'))
       OR (NEW.void_reason IS DISTINCT FROM OLD.void_reason AND NOT dx_in_tx(NEW.clinic_id)) THEN
        PERFORM dx_raise('dx_statement_immutable');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_b2b_statements_guard ON dx_b2b_statements;
CREATE TRIGGER trg_dx_b2b_statements_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_b2b_statements
    FOR EACH ROW EXECUTE FUNCTION dx_guard_b2b_statement();

-- ── 5. Pre-printed barcode stock ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dx_barcode_stock (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id  UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    account_id UUID NULL,
    site_id    UUID NULL,
    prefix     TEXT NOT NULL CHECK (prefix ~ '^[A-Z0-9]{0,12}$'),
    range_from BIGINT NOT NULL CHECK (range_from >= 0),
    range_to   BIGINT NOT NULL,
    issued_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    issued_by  UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT dx_barcode_stock_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT dx_barcode_stock_range CHECK (range_to >= range_from AND range_to - range_from < 1000000
                                             AND range_to <= 999999999999999999),
    CONSTRAINT dx_barcode_stock_owner CHECK (num_nonnulls(account_id, site_id) = 1),
    CONSTRAINT dx_barcode_stock_account_fk FOREIGN KEY (clinic_id, account_id) REFERENCES dx_b2b_accounts (clinic_id, id),
    CONSTRAINT dx_barcode_stock_site_fk FOREIGN KEY (clinic_id, site_id) REFERENCES dx_sites (clinic_id, id)
);
CREATE INDEX IF NOT EXISTS idx_dx_barcode_stock_prefix ON dx_barcode_stock (clinic_id, prefix, range_from);

-- Issued ranges never change; a new range may not overlap any range of the same prefix
-- (barcodes are unique per clinic, whoever holds the stock).
CREATE OR REPLACE FUNCTION dx_guard_barcode_stock() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        IF TG_OP = 'DELETE' AND dx_purging(OLD.clinic_id) THEN RETURN OLD; END IF;
        PERFORM dx_raise('dx_barcode_stock_immutable');
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.clinic_id::TEXT || ':dxstock:' || NEW.prefix, 0));
    IF EXISTS (SELECT 1 FROM dx_barcode_stock WHERE clinic_id = NEW.clinic_id AND prefix = NEW.prefix
                                                AND range_from <= NEW.range_to AND range_to >= NEW.range_from) THEN
        PERFORM dx_raise('dx_barcode_stock_overlap');
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_dx_barcode_stock_guard ON dx_barcode_stock;
CREATE TRIGGER trg_dx_barcode_stock_guard BEFORE INSERT OR UPDATE OR DELETE ON dx_barcode_stock
    FOR EACH ROW EXECUTE FUNCTION dx_guard_barcode_stock();

-- A pre-printed label bound to a tube must come from stock issued to the visit's account
-- or the collecting site, when that account / site holds any stock at all.
CREATE OR REPLACE FUNCTION dx_check_barcode_stock(p_clinic_id UUID, p_account_id UUID, p_site_id UUID, p_code TEXT)
RETURNS VOID LANGUAGE plpgsql STABLE AS $$
DECLARE v_code TEXT := upper(btrim(COALESCE(p_code, '')));
BEGIN
    IF NOT EXISTS (SELECT 1 FROM dx_barcode_stock WHERE clinic_id = p_clinic_id
                                                    AND (account_id = p_account_id OR site_id = p_site_id)) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_barcode_stock b
                    WHERE b.clinic_id = p_clinic_id AND (b.account_id = p_account_id OR b.site_id = p_site_id)
                      AND CASE WHEN v_code ~ ('^' || b.prefix || '[0-9]{1,18}$')
                               THEN substr(v_code, char_length(b.prefix) + 1)::BIGINT BETWEEN b.range_from AND b.range_to
                               ELSE false END) THEN
        PERFORM dx_raise('dx_barcode_not_issued');
    END IF;
END $$;

-- ── 6. Derived balance, unpaid items, ageing ──────────────────────────────
-- Debits: opening_balance, charge. Credits: every other entry type. Credits settle the
-- oldest debits first (FIFO); what is left of each debit is "unpaid" from its IST date.
CREATE OR REPLACE FUNCTION dx_b2b_unpaid(p_clinic_id UUID, p_account_id UUID)
RETURNS TABLE (entry_date DATE, unpaid_paise BIGINT) LANGUAGE sql STABLE AS $$
    WITH cr AS (SELECT COALESCE(sum(amount_paise), 0) AS total FROM dx_b2b_ledger
                 WHERE clinic_id = p_clinic_id AND account_id = p_account_id
                   AND entry_type NOT IN ('opening_balance', 'charge')),
         dr AS (SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::DATE AS d, amount_paise,
                       sum(amount_paise) OVER (ORDER BY created_at, id) AS run
                  FROM dx_b2b_ledger
                 WHERE clinic_id = p_clinic_id AND account_id = p_account_id
                   AND entry_type IN ('opening_balance', 'charge'))
    SELECT dr.d, LEAST(dr.amount_paise, dr.run - cr.total)::BIGINT FROM dr, cr WHERE dr.run > cr.total
$$;

-- balance > 0: the account owes the lab; < 0: prepaid money (or overpayment) held.
-- overdue: unpaid debits past their credit_days (none when the account has no credit_days).
CREATE OR REPLACE FUNCTION dx_b2b_balance(p_clinic_id UUID, p_account_id UUID)
RETURNS TABLE (charges_paise BIGINT, credits_paise BIGINT, balance_paise BIGINT, overdue_amount_paise BIGINT,
               oldest_unpaid_date DATE) LANGUAGE sql STABLE AS $$
    WITH t AS (SELECT COALESCE(sum(amount_paise) FILTER (WHERE entry_type IN ('opening_balance', 'charge')), 0)::BIGINT AS dr,
                      COALESCE(sum(amount_paise) FILTER (WHERE entry_type NOT IN ('opening_balance', 'charge')), 0)::BIGINT AS cr
                 FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND account_id = p_account_id),
         u AS (SELECT * FROM dx_b2b_unpaid(p_clinic_id, p_account_id)),
         a AS (SELECT credit_days FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id)
    SELECT t.dr, t.cr, t.dr - t.cr,
           COALESCE((SELECT sum(u.unpaid_paise) FROM u, a
                      WHERE a.credit_days IS NOT NULL
                        AND u.entry_date + a.credit_days < (now() AT TIME ZONE 'Asia/Kolkata')::DATE), 0)::BIGINT,
           (SELECT min(u.entry_date) FROM u)
      FROM t
$$;

-- Receivables ageing (W14): unpaid amounts by days past due (due = entry date + credit_days, 0 when unset).
CREATE OR REPLACE FUNCTION dx_b2b_ageing(p_clinic_id UUID) RETURNS JSONB LANGUAGE sql STABLE AS $$
    SELECT COALESCE(jsonb_agg(x ORDER BY x ->> 'name'), '[]'::jsonb) FROM (
        SELECT jsonb_build_object(
                   'account_id', a.id, 'code', a.code, 'name', a.name, 'kind', a.kind, 'status', a.status,
                   'hold_kind', a.hold_kind, 'credit_days', a.credit_days, 'credit_limit_paise', a.credit_limit_paise,
                   'balance_paise', b.balance_paise,
                   'current_paise', COALESCE(sum(u.unpaid_paise) FILTER (WHERE u.dpd <= 0), 0),
                   'd1_30_paise', COALESCE(sum(u.unpaid_paise) FILTER (WHERE u.dpd BETWEEN 1 AND 30), 0),
                   'd31_60_paise', COALESCE(sum(u.unpaid_paise) FILTER (WHERE u.dpd BETWEEN 31 AND 60), 0),
                   'd61_90_paise', COALESCE(sum(u.unpaid_paise) FILTER (WHERE u.dpd BETWEEN 61 AND 90), 0),
                   'd90_plus_paise', COALESCE(sum(u.unpaid_paise) FILTER (WHERE u.dpd > 90), 0)) AS x
          FROM dx_b2b_accounts a
          CROSS JOIN LATERAL dx_b2b_balance(a.clinic_id, a.id) b
          LEFT JOIN LATERAL (SELECT p.unpaid_paise,
                                    (now() AT TIME ZONE 'Asia/Kolkata')::DATE - p.entry_date - COALESCE(a.credit_days, 0) AS dpd
                               FROM dx_b2b_unpaid(a.clinic_id, a.id) p) u ON true
         WHERE a.clinic_id = p_clinic_id
         GROUP BY a.id, a.code, a.name, a.kind, a.status, a.hold_kind, a.credit_days, a.credit_limit_paise, b.balance_paise
    ) s
$$;

-- ── 7. Postings ────────────────────────────────────────────────────────────
-- Lock order everywhere: the clinic counter row (registration, statements) before the
-- account row, so registration and statement issue never deadlock.
CREATE OR REPLACE FUNCTION dx_b2b_post_charge(p_clinic_id UUID, p_account_id UUID, p_accession_id UUID, p_item_id UUID,
                                              p_amount_paise BIGINT, p_actor_id UUID, p_actor_name TEXT)
RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE acc dx_b2b_accounts; v_mode TEXT; v_held BIGINT; v_id UUID;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF p_amount_paise IS NULL OR p_amount_paise <= 0 THEN PERFORM dx_raise('dx_amount_invalid'); END IF;
    SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_account_not_found'); END IF;
    SELECT billing_mode INTO v_mode FROM dx_accessions
     WHERE clinic_id = p_clinic_id AND id = p_accession_id AND b2b_account_id = acc.id;
    IF v_mode IS NULL OR v_mode NOT LIKE 'b2b%' THEN PERFORM dx_raise('dx_not_b2b_billing'); END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_accession_items WHERE clinic_id = p_clinic_id AND id = p_item_id
                                                      AND accession_id = p_accession_id AND status = 'active') THEN
        PERFORM dx_raise('dx_item_not_found');
    END IF;
    IF acc.status <> 'active' THEN PERFORM dx_raise('dx_b2b_on_hold'); END IF;
    IF v_mode = 'b2b_prepaid' THEN
        SELECT COALESCE(sum(CASE WHEN entry_type IN ('opening_balance', 'charge') THEN -amount_paise ELSE amount_paise END), 0)
          INTO v_held FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND account_id = acc.id;
        IF v_held < p_amount_paise THEN PERFORM dx_raise('dx_b2b_prepaid_insufficient'); END IF;
    END IF;
    INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type, amount_paise, accession_id, accession_item_id,
                               created_by, created_by_name)
    VALUES (p_clinic_id, acc.id, 'charge', p_amount_paise, p_accession_id, p_item_id, p_actor_id,
            left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;

-- Every active item of a B2B visit with a payer share and no charge yet (register, add items).
CREATE OR REPLACE FUNCTION dx_b2b_post_visit_charges(p_clinic_id UUID, p_accession_id UUID, p_actor_id UUID,
                                                     p_actor_name TEXT) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE a dx_accessions; it dx_accession_items; n INTEGER := 0;
BEGIN
    SELECT * INTO a FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = p_accession_id;
    IF NOT FOUND OR a.billing_mode NOT LIKE 'b2b%' THEN RETURN 0; END IF;
    FOR it IN SELECT i.* FROM dx_accession_items i
               WHERE i.clinic_id = p_clinic_id AND i.accession_id = a.id AND i.status = 'active' AND i.payer_share_paise > 0
                 AND NOT EXISTS (SELECT 1 FROM dx_b2b_ledger l WHERE l.clinic_id = i.clinic_id
                                    AND l.accession_item_id = i.id AND l.entry_type = 'charge')
               ORDER BY i.created_at, i.id LOOP
        PERFORM dx_b2b_post_charge(p_clinic_id, a.b2b_account_id, a.id, it.id, it.payer_share_paise, p_actor_id, p_actor_name);
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

-- One reversal per charged item (idempotency key 'reverse:<item>'); no charge, nothing to do.
CREATE OR REPLACE FUNCTION dx_b2b_reverse_charge(p_clinic_id UUID, p_item_id UUID, p_reason TEXT, p_actor_id UUID,
                                                 p_actor_name TEXT) RETURNS UUID LANGUAGE plpgsql AS $$
DECLARE c dx_b2b_ledger; v_id UUID;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    SELECT * INTO c FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND accession_item_id = p_item_id
                                         AND entry_type = 'charge';
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM 1 FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = c.account_id FOR NO KEY UPDATE;
    SELECT id INTO v_id FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND idempotency_key = 'reverse:' || p_item_id;
    IF v_id IS NOT NULL THEN RETURN v_id; END IF;
    INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type, amount_paise, accession_id, accession_item_id, reason,
                               idempotency_key, created_by, created_by_name)
    VALUES (p_clinic_id, c.account_id, 'charge_reversal', c.amount_paise, c.accession_id, p_item_id, left(p_reason, 300),
            'reverse:' || p_item_id, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING id INTO v_id;
    RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION dx_b2b_reverse_visit_charges(p_clinic_id UUID, p_accession_id UUID, p_reason TEXT,
                                                        p_actor_id UUID, p_actor_name TEXT) RETURNS INTEGER
LANGUAGE plpgsql AS $$
DECLARE v_item UUID; n INTEGER := 0;
BEGIN
    FOR v_item IN SELECT l.accession_item_id FROM dx_b2b_ledger l
                    JOIN dx_accession_items i ON i.clinic_id = l.clinic_id AND i.id = l.accession_item_id
                   WHERE l.clinic_id = p_clinic_id AND l.accession_id = p_accession_id AND l.entry_type = 'charge'
                     AND i.status = 'cancelled'
                   ORDER BY l.created_at, l.id LOOP
        PERFORM dx_b2b_reverse_charge(p_clinic_id, v_item, p_reason, p_actor_id, p_actor_name);
        n := n + 1;
    END LOOP;
    RETURN n;
END $$;

-- A payment (credit account) or a top-up (prepaid account). Same key, same entry: replay.
CREATE OR REPLACE FUNCTION dx_b2b_record_payment(p_clinic_id UUID, p_account_id UUID, p_amount_paise BIGINT, p_mode TEXT,
                                                 p_ref TEXT, p_idempotency_key TEXT, p_actor_id UUID, p_actor_name TEXT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE acc dx_b2b_accounts; r dx_b2b_ledger; v_key TEXT := NULLIF(btrim(COALESCE(p_idempotency_key, '')), '');
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF v_key IS NULL THEN PERFORM dx_raise('dx_idempotency_required'); END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_clinic_id::TEXT || ':dxb2bpay:' || v_key, 0));
    SELECT * INTO r FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND idempotency_key = v_key;
    IF FOUND THEN
        IF r.account_id <> p_account_id OR r.amount_paise <> p_amount_paise THEN PERFORM dx_raise('dx_idempotency_reused'); END IF;
        RETURN to_jsonb(r) || jsonb_build_object('replayed', true);
    END IF;
    IF p_amount_paise IS NULL OR p_amount_paise <= 0 THEN PERFORM dx_raise('dx_amount_invalid'); END IF;
    IF COALESCE(p_mode, '') NOT IN ('neft', 'upi', 'cheque', 'cash', 'card', 'gateway') THEN
        PERFORM dx_raise('dx_payment_mode_invalid');
    END IF;
    IF p_mode IN ('neft', 'upi', 'cheque') AND NULLIF(btrim(COALESCE(p_ref, '')), '') IS NULL THEN
        PERFORM dx_raise('dx_payment_reference_required');
    END IF;
    SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_account_not_found'); END IF;
    INSERT INTO dx_b2b_ledger (clinic_id, account_id, entry_type, amount_paise, payment_mode, reference,
                               idempotency_key, created_by, created_by_name)
    VALUES (p_clinic_id, acc.id, CASE WHEN acc.default_billing_mode = 'b2b_prepaid' THEN 'prepaid_topup' ELSE 'payment' END,
            p_amount_paise, p_mode, NULLIF(left(btrim(COALESCE(p_ref, '')), 80), ''), v_key, p_actor_id,
            left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING * INTO r;
    RETURN to_jsonb(r) || jsonb_build_object('replayed', false);
END $$;

-- ── 8. Statements ──────────────────────────────────────────────────────────
-- A statement takes every unbilled ledger row dated (IST) on or before period_to; its
-- opening is the signed sum of rows already on earlier (non-void) statements, so each
-- closing equals the next opening. Periods of one account never overlap.
CREATE OR REPLACE FUNCTION dx_b2b_issue_statement(p_clinic_id UUID, p_account_id UUID, p_from DATE, p_to DATE,
                                                  p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE acc dx_b2b_accounts; v_seq INTEGER; v_open BIGINT; v_dr BIGINT; v_cr BIGINT; st dx_b2b_statements;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF p_from IS NULL OR p_to IS NULL OR p_to < p_from THEN PERFORM dx_raise('dx_statement_period_invalid'); END IF;
    IF p_to > (now() AT TIME ZONE 'Asia/Kolkata')::DATE THEN PERFORM dx_raise('dx_statement_period_future'); END IF;
    IF NOT EXISTS (SELECT 1 FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id) THEN
        PERFORM dx_raise('dx_account_not_found');
    END IF;
    v_seq := opd_next_counter(p_clinic_id, 'dx_b2b:' || EXTRACT(YEAR FROM p_to)::INTEGER);   -- counter row first
    SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id FOR NO KEY UPDATE;
    IF EXISTS (SELECT 1 FROM dx_b2b_statements WHERE clinic_id = p_clinic_id AND account_id = acc.id AND status <> 'void'
                                                 AND period_from <= p_to AND period_to >= p_from) THEN
        PERFORM dx_raise('dx_statement_overlap');
    END IF;
    SELECT COALESCE(sum(CASE WHEN entry_type IN ('opening_balance', 'charge') THEN amount_paise ELSE -amount_paise END)
                        FILTER (WHERE statement_id IS NOT NULL), 0),
           COALESCE(sum(amount_paise) FILTER (WHERE statement_id IS NULL AND d <= p_to
                                                AND entry_type IN ('opening_balance', 'charge')), 0),
           COALESCE(sum(amount_paise) FILTER (WHERE statement_id IS NULL AND d <= p_to
                                                AND entry_type NOT IN ('opening_balance', 'charge')), 0)
      INTO v_open, v_dr, v_cr
      FROM (SELECT entry_type, amount_paise, statement_id, (created_at AT TIME ZONE 'Asia/Kolkata')::DATE AS d
              FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id AND account_id = acc.id) l;
    INSERT INTO dx_b2b_statements (clinic_id, account_id, statement_year, statement_seq, period_from, period_to,
                                   total_charges_paise, total_credits_paise, opening_paise, closing_paise,
                                   issued_by, issued_by_name)
    VALUES (p_clinic_id, acc.id, EXTRACT(YEAR FROM p_to)::SMALLINT, v_seq, p_from, p_to, v_dr, v_cr, v_open,
            v_open + v_dr - v_cr, p_actor_id, left(COALESCE(p_actor_name, 'system'), 100))
    RETURNING * INTO st;
    UPDATE dx_b2b_ledger SET statement_id = st.id
     WHERE clinic_id = p_clinic_id AND account_id = acc.id AND statement_id IS NULL
       AND (created_at AT TIME ZONE 'Asia/Kolkata')::DATE <= p_to;
    RETURN to_jsonb(st);
END $$;

-- Void: the rows go back to "unbilled" and the next statement picks them up. The number stays used.
CREATE OR REPLACE FUNCTION dx_b2b_void_statement(p_clinic_id UUID, p_statement_id UUID, p_reason TEXT, p_actor_id UUID,
                                                 p_actor_name TEXT) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE st dx_b2b_statements;
BEGIN
    PERFORM set_config('kriya.dx_tx', p_clinic_id::TEXT, true);
    IF char_length(btrim(COALESCE(p_reason, ''))) < 3 THEN PERFORM dx_raise('dx_void_reason_required'); END IF;
    SELECT * INTO st FROM dx_b2b_statements WHERE clinic_id = p_clinic_id AND id = p_statement_id;
    IF NOT FOUND THEN PERFORM dx_raise('dx_statement_not_found'); END IF;
    PERFORM 1 FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = st.account_id FOR NO KEY UPDATE;
    SELECT * INTO st FROM dx_b2b_statements WHERE clinic_id = p_clinic_id AND id = p_statement_id;
    IF st.status = 'void' THEN RETURN to_jsonb(st); END IF;
    -- only the newest live statement of an account can be voided (later openings depend on it)
    IF EXISTS (SELECT 1 FROM dx_b2b_statements WHERE clinic_id = p_clinic_id AND account_id = st.account_id
                                                 AND status = 'issued' AND issued_at > st.issued_at) THEN
        PERFORM dx_raise('dx_statement_not_latest');
    END IF;
    UPDATE dx_b2b_ledger SET statement_id = NULL WHERE clinic_id = p_clinic_id AND statement_id = st.id;
    UPDATE dx_b2b_statements SET status = 'void', void_reason = left(btrim(p_reason), 300)
     WHERE clinic_id = p_clinic_id AND id = st.id RETURNING * INTO st;
    RETURN to_jsonb(st);
END $$;

-- ── 9. Holds ───────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION dx_b2b_set_hold(p_clinic_id UUID, p_account_id UUID, p_status TEXT, p_hold_kind TEXT,
                                           p_reason TEXT, p_actor_id UUID, p_actor_name TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE acc dx_b2b_accounts; v_changed BOOLEAN;
BEGIN
    IF COALESCE(p_status, '') NOT IN ('active', 'on_hold') THEN PERFORM dx_raise('dx_hold_status_invalid'); END IF;
    IF p_status = 'on_hold' AND COALESCE(p_hold_kind, '') NOT IN ('manual', 'limit', 'overdue') THEN
        PERFORM dx_raise('dx_hold_kind_invalid');
    END IF;
    SELECT * INTO acc FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id AND id = p_account_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN PERFORM dx_raise('dx_account_not_found'); END IF;
    IF acc.status = 'closed' THEN PERFORM dx_raise('dx_b2b_closed'); END IF;
    v_changed := acc.status <> p_status OR acc.hold_kind IS DISTINCT FROM CASE WHEN p_status = 'on_hold' THEN p_hold_kind END;
    UPDATE dx_b2b_accounts
       SET status = p_status,
           hold_kind = CASE WHEN p_status = 'on_hold' THEN p_hold_kind END,
           hold_reason = CASE WHEN p_status = 'on_hold' THEN left(NULLIF(btrim(COALESCE(p_reason, '')), ''), 300) END,
           updated_at = now()
     WHERE clinic_id = p_clinic_id AND id = acc.id
    RETURNING * INTO acc;
    RETURN to_jsonb(acc) || jsonb_build_object('changed', v_changed);
END $$;

-- Daily job (W14): put auto-hold accounts over their credit limit or with overdue money on
-- hold; lift a 'limit' / 'overdue' hold once the cause is gone. Manual holds are never touched.
CREATE OR REPLACE FUNCTION dx_b2b_evaluate_holds(p_clinic_id UUID) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE acc dx_b2b_accounts; b RECORD; v_kind TEXT; v_out JSONB := '[]'::jsonb;
BEGIN
    FOR acc IN SELECT * FROM dx_b2b_accounts
                WHERE clinic_id = p_clinic_id AND auto_hold AND default_billing_mode <> 'patient'
                  AND (status = 'active' OR (status = 'on_hold' AND hold_kind IN ('limit', 'overdue')))
                ORDER BY id LOOP
        SELECT * INTO b FROM dx_b2b_balance(p_clinic_id, acc.id);
        v_kind := CASE WHEN acc.credit_limit_paise IS NOT NULL AND b.balance_paise > acc.credit_limit_paise THEN 'limit'
                       WHEN b.overdue_amount_paise > 0 THEN 'overdue' END;
        IF v_kind IS NOT NULL AND (acc.status <> 'on_hold' OR acc.hold_kind IS DISTINCT FROM v_kind) THEN
            PERFORM dx_b2b_set_hold(p_clinic_id, acc.id, 'on_hold', v_kind,
                CASE v_kind WHEN 'limit' THEN format('Balance %s paise is over the credit limit of %s paise',
                                                     b.balance_paise, acc.credit_limit_paise)
                            ELSE format('%s paise unpaid beyond %s credit days', b.overdue_amount_paise, acc.credit_days) END,
                NULL, 'system');
            v_out := v_out || jsonb_build_object('account_id', acc.id, 'status', 'on_hold', 'hold_kind', v_kind,
                                                 'balance_paise', b.balance_paise, 'overdue_paise', b.overdue_amount_paise);
        ELSIF v_kind IS NULL AND acc.status = 'on_hold' THEN
            PERFORM dx_b2b_set_hold(p_clinic_id, acc.id, 'active', NULL, NULL, NULL, 'system');
            v_out := v_out || jsonb_build_object('account_id', acc.id, 'status', 'active', 'hold_kind', NULL,
                                                 'balance_paise', b.balance_paise, 'overdue_paise', b.overdue_amount_paise);
        END IF;
    END LOOP;
    RETURN v_out;
END $$;

-- ── 10. Pricing: rate cards and co-pay (replaces the 127 resolver) ─────────
-- p_ctx: {clinic_id, branch_id, gender, billing_mode, discount_limit_percent, b2b_account_id?, accession_id?}
-- A visit with an account prices its tests from the account's live rate card (else MRP).
-- Shares: patient pays all ('patient'); payer pays all (b2b_credit / b2b_prepaid); b2b_split
-- by the account's co-pay: percent = floor(net * bp / 10000) per item (remainder to the payer),
-- fixed = paise per visit taken from the first items first (items already on the visit count).
CREATE OR REPLACE FUNCTION dx_price_items(p_ctx JSONB, p_items JSONB) RETURNS JSONB LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_clinic UUID := (p_ctx ->> 'clinic_id')::UUID;
    v_branch UUID := NULLIF(p_ctx ->> 'branch_id', '')::UUID;
    v_gender TEXT := p_ctx ->> 'gender';
    v_mode   TEXT := COALESCE(NULLIF(p_ctx ->> 'billing_mode', ''), 'patient');
    v_limit  NUMERIC := COALESCE((p_ctx ->> 'discount_limit_percent')::NUMERIC, 0);
    v_acc_id UUID := NULLIF(p_ctx ->> 'b2b_account_id', '')::UUID;
    v_visit  UUID := NULLIF(p_ctx ->> 'accession_id', '')::UUID;
    v_today  DATE := (now() AT TIME ZONE 'Asia/Kolkata')::DATE;
    r JSONB; t lab_tests; m lab_tests; v_out JSONB := '[]'::jsonb; v_tests JSONB; v_price INTEGER; v_disc INTEGER;
    v_net INTEGER; v_member UUID; v_cont dx_containers; v_acc dx_b2b_accounts; v_card UUID; v_rc INTEGER;
    v_src TEXT; v_used BIGINT := 0; v_pat INTEGER; v_booking BOOLEAN;
BEGIN
    IF jsonb_typeof(p_items) IS DISTINCT FROM 'array' OR jsonb_array_length(p_items) = 0 THEN
        PERFORM dx_raise('dx_no_items');
    END IF;
    IF v_mode LIKE 'b2b%' AND v_acc_id IS NULL THEN PERFORM dx_raise('dx_b2b_account_required'); END IF;
    IF v_acc_id IS NOT NULL THEN
        SELECT * INTO v_acc FROM dx_b2b_accounts WHERE clinic_id = v_clinic AND id = v_acc_id;
        IF NOT FOUND THEN PERFORM dx_raise('dx_account_not_found'); END IF;
        SELECT id INTO v_card FROM dx_rate_cards
         WHERE clinic_id = v_clinic AND id = v_acc.rate_card_id AND is_active
           AND valid_from <= v_today AND (valid_to IS NULL OR valid_to >= v_today);
    END IF;
    IF v_mode = 'b2b_split' AND v_acc.copay_mode = 'fixed' AND v_visit IS NOT NULL THEN
        SELECT COALESCE(sum(patient_share_paise), 0) INTO v_used FROM dx_accession_items
         WHERE clinic_id = v_clinic AND accession_id = v_visit AND status = 'active';
    END IF;
    FOR r IN SELECT * FROM jsonb_array_elements(p_items) LOOP
        v_disc := COALESCE((r ->> 'discount_paise')::INTEGER, 0);
        v_booking := r ? 'source_appointment_id' AND r ->> 'source_appointment_id' IS NOT NULL;
        IF r ? 'fee_code' THEN
            v_price := (r ->> 'amount_paise')::INTEGER;
            IF v_price IS NULL OR v_price < 0 THEN PERFORM dx_raise('dx_fee_invalid'); END IF;
            v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price END;
            v_pat := CASE WHEN v_mode IN ('patient', 'internal') THEN v_net
                          WHEN v_mode = 'b2b_split' THEN CASE v_acc.copay_mode
                              WHEN 'percent' THEN floor(v_net::NUMERIC * v_acc.copay_value / 10000)::INTEGER
                              WHEN 'fixed' THEN LEAST(v_net, GREATEST(v_acc.copay_value - v_used, 0))::INTEGER
                              ELSE 0 END
                          ELSE 0 END;
            v_used := v_used + v_pat;
            v_out := v_out || jsonb_build_object(
                'kind', 'fee', 'fee_code', r ->> 'fee_code', 'lab_test_id', NULL,
                'item_name', COALESCE(NULLIF(btrim(r ->> 'item_name'), ''), initcap(replace(r ->> 'fee_code', '_', ' '))),
                'test_code', NULL, 'mrp_paise', v_price, 'price_paise', v_price, 'discount_paise', 0,
                'patient_share_paise', v_pat, 'payer_share_paise', v_net - v_pat, 'price_source', 'fee',
                'rate_card_id', NULL, 'source_appointment_id', r -> 'source_appointment_id', 'tests', '[]'::jsonb);
            CONTINUE;
        END IF;
        t := dx_test_ready(v_clinic, (r ->> 'lab_test_id')::UUID, v_gender);
        -- branch row -> clinic row price; the account's rate card wins (booking lines keep the booked price)
        SELECT COALESCE((SELECT b.price_paise FROM lab_tests b
                          WHERE b.clinic_id = v_clinic AND b.branch_id = v_branch AND b.is_active
                            AND t.test_code IS NOT NULL AND upper(b.test_code) = upper(t.test_code) LIMIT 1),
                        t.price_paise) INTO v_price;
        v_src := CASE WHEN v_booking THEN 'booking' ELSE 'mrp' END;
        IF v_card IS NOT NULL AND NOT v_booking THEN
            SELECT price_paise INTO v_rc FROM dx_rate_card_items
             WHERE clinic_id = v_clinic AND rate_card_id = v_card AND lab_test_id = t.id;
            IF FOUND THEN v_price := v_rc; v_src := 'rate_card'; END IF;
        END IF;
        IF v_disc < 0 OR v_disc > v_price THEN PERFORM dx_raise('dx_discount_invalid'); END IF;
        IF v_disc > floor(v_price * v_limit / 100) THEN PERFORM dx_raise('dx_discount_over_limit'); END IF;
        v_tests := '[]'::jsonb;
        FOR v_member IN SELECT dx_explode(v_clinic, t.id) LOOP
            m := dx_test_ready(v_clinic, v_member, v_gender);
            v_cont := NULL;
            IF m.dx_result_mode = 'parameters' AND m.container_id IS NOT NULL THEN
                SELECT * INTO v_cont FROM dx_containers WHERE clinic_id = v_clinic AND id = m.container_id;
            END IF;
            v_tests := v_tests || jsonb_build_object('lab_test_id', m.id, 'name', m.name, 'test_code', m.test_code,
                'container_id', v_cont.id, 'container_code', v_cont.code, 'sample_type', v_cont.sample_type,
                'result_mode', m.dx_result_mode);
        END LOOP;
        IF jsonb_array_length(v_tests) = 0 THEN PERFORM dx_raise('dx_group_empty:' || t.name); END IF;
        v_net := CASE WHEN v_mode = 'internal' THEN 0 ELSE v_price - v_disc END;
        v_pat := CASE WHEN v_mode IN ('patient', 'internal') THEN v_net
                      WHEN v_mode = 'b2b_split' THEN CASE v_acc.copay_mode
                          WHEN 'percent' THEN floor(v_net::NUMERIC * v_acc.copay_value / 10000)::INTEGER
                          WHEN 'fixed' THEN LEAST(v_net, GREATEST(v_acc.copay_value - v_used, 0))::INTEGER
                          ELSE 0 END
                      ELSE 0 END;
        v_used := v_used + v_pat;
        v_out := v_out || jsonb_build_object(
            'kind', t.kind, 'fee_code', NULL, 'lab_test_id', t.id, 'item_name', t.name, 'test_code', t.test_code,
            'mrp_paise', t.price_paise, 'price_paise', v_price,
            'discount_paise', CASE WHEN v_mode = 'internal' THEN v_price ELSE v_disc END,
            'patient_share_paise', v_pat, 'payer_share_paise', v_net - v_pat, 'price_source', v_src,
            'rate_card_id', CASE WHEN v_src = 'rate_card' THEN v_card END,
            'source_appointment_id', r -> 'source_appointment_id', 'tests', v_tests);
    END LOOP;
    RETURN v_out;
END $$;

-- ── 11. Guarded patches of existing functions ─────────────────────────────
-- Each needle must occur exactly once in the current body; the rollback applies the reverse.
DO $$
DECLARE p TEXT[]; v_def TEXT; n INTEGER;
BEGIN
    FOREACH p SLICE 1 IN ARRAY ARRAY[
        ['dx_register_visit(uuid,jsonb,uuid,text)',
         $s$'billing_mode', v_mode,$s$,
         $s$'billing_mode', v_mode, 'b2b_account_id', v_acc.id,$s$],
        ['dx_register_visit(uuid,jsonb,uuid,text)',
         $s$PERFORM dx_insert_lines(p_clinic_id, v_id, v_lines, p_actor_id, p_actor_name);$s$,
         $s$PERFORM dx_insert_lines(p_clinic_id, v_id, v_lines, p_actor_id, p_actor_name);
    PERFORM dx_b2b_post_visit_charges(p_clinic_id, v_id, p_actor_id, p_actor_name);$s$],
        ['dx_add_items(uuid,uuid,jsonb,text,numeric,uuid,text)',
         $s$'billing_mode', a.billing_mode,$s$,
         $s$'billing_mode', a.billing_mode, 'b2b_account_id', a.b2b_account_id, 'accession_id', a.id,$s$],
        ['dx_add_items(uuid,uuid,jsonb,text,numeric,uuid,text)',
         $s$PERFORM dx_insert_lines(p_clinic_id, a.id, v_lines, p_actor_id, p_actor_name);$s$,
         $s$PERFORM dx_insert_lines(p_clinic_id, a.id, v_lines, p_actor_id, p_actor_name);
    PERFORM dx_b2b_post_visit_charges(p_clinic_id, a.id, p_actor_id, p_actor_name);$s$],
        ['dx_quote(uuid,jsonb)',
         $s$'billing_mode', p_payload ->> 'billing_mode',$s$,
         $s$'billing_mode', p_payload ->> 'billing_mode', 'b2b_account_id', p_payload ->> 'b2b_account_id',$s$],
        ['dx_cancel_item(uuid,uuid,text,uuid,text)',
         $s$PERFORM dx_log(p_clinic_id, a.id, 'item_cancelled'$s$,
         $s$PERFORM dx_b2b_reverse_charge(p_clinic_id, it.id, p_reason, p_actor_id, p_actor_name);
    PERFORM dx_log(p_clinic_id, a.id, 'item_cancelled'$s$],
        ['dx_cancel_visit(uuid,uuid,text,uuid,text)',
         $s$PERFORM dx_log(p_clinic_id, a.id, 'cancelled'$s$,
         $s$PERFORM dx_b2b_reverse_visit_charges(p_clinic_id, a.id, p_reason, p_actor_id, p_actor_name);
    PERFORM dx_log(p_clinic_id, a.id, 'cancelled'$s$],
        ['dx_bill_visit(uuid,uuid,uuid,text)',
         $s$IF a.billing_mode <> 'patient' THEN$s$,
         $s$IF a.billing_mode NOT IN ('patient', 'b2b_split') THEN$s$],
        ['dx_insert_lines(uuid,uuid,jsonb,uuid,text)',
         $s$patient_share_paise, payer_share_paise, price_source)$s$,
         $s$patient_share_paise, payer_share_paise, price_source, rate_card_id)$s$],
        ['dx_insert_lines(uuid,uuid,jsonb,uuid,text)',
         $s$(l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source')$s$,
         $s$(l ->> 'payer_share_paise')::INTEGER, l ->> 'price_source', NULLIF(l ->> 'rate_card_id', '')::UUID)$s$],
        ['dx_bind_barcode(uuid,uuid,text,uuid,text)',
         $s$-- shortcut: no dx_barcode_stock range check (dx_barcode_not_issued) until DX-M8 adds the stock table$s$,
         $s$PERFORM dx_check_barcode_stock(p_clinic_id, (SELECT b2b_account_id FROM dx_accessions WHERE clinic_id = p_clinic_id AND id = s.accession_id), s.collection_site_id, v_code);$s$]
    ] LOOP
        v_def := pg_get_functiondef(p[1]::regprocedure);
        IF position(p[3] IN v_def) > 0 THEN CONTINUE; END IF;                  -- re-run: already patched
        n := (char_length(v_def) - char_length(replace(v_def, p[2], ''))) / char_length(p[2]);
        IF n <> 1 THEN RAISE EXCEPTION '132: % needle found % times: %', p[1], n, p[2]; END IF;
        EXECUTE replace(v_def, p[2], p[3]);
    END LOOP;
END $$;

-- ── 12. Purge v8 (adds the B2B money rows and partner logins) ─────────────
CREATE OR REPLACE FUNCTION dx_purge_clinic(p_clinic_id UUID) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v JSONB := '{}'::jsonb; n INTEGER;
BEGIN
    PERFORM set_config('kriya.dx_purge_clinic', p_clinic_id::TEXT, true);
    PERFORM set_config('kriya.opd_purge_clinic', p_clinic_id::TEXT, true);    -- DX orders live in opd_orders
    DELETE FROM dx_b2b_ledger WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('ledger_rows', n);
    DELETE FROM dx_b2b_statements WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('statements', n);
    DELETE FROM dx_barcode_stock WHERE clinic_id = p_clinic_id;
    -- a partner login is meaningless without its account
    DELETE FROM clinic_admins WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('partner_logins', n);
    DELETE FROM dx_share_links WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_print_log WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_deliveries WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_delivery_overrides WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_release_results WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_report_releases WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('releases', n);
    DELETE FROM dx_outbox WHERE clinic_id = p_clinic_id;
    -- adopted OPD / IPD orders stay with their encounter or admission, unlinked from the bench
    UPDATE opd_orders SET accession_id = NULL, accession_item_id = NULL, dx_sample_id = NULL
     WHERE clinic_id = p_clinic_id AND source <> 'dx' AND accession_id IS NOT NULL; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('adopted_orders_unlinked', n);
    DELETE FROM dx_result_attachments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_critical_events WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('critical_events', n);
    DELETE FROM dx_result_verifications WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_result_value_meta WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_label_prints WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_field_collections WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('field_collections', n);
    DELETE FROM dx_batch_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_batches WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('batches', n);
    DELETE FROM opd_orders WHERE clinic_id = p_clinic_id AND source = 'dx'; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('orders', n);
    DELETE FROM dx_events WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_samples WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_items WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accession_bookings WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_accessions WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('visits', n);
    UPDATE family_members SET dx_b2b_account_id = NULL WHERE clinic_id = p_clinic_id AND dx_b2b_account_id IS NOT NULL;
    DELETE FROM dx_referrers WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_b2b_accounts WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_rate_cards WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;   -- items cascade
    v := v || jsonb_build_object('rate_cards', n);
    DELETE FROM dx_report_templates WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('templates', n);
    DELETE FROM dx_rejection_reasons WHERE clinic_id = p_clinic_id;
    PERFORM set_config('kriya.opd_purge_clinic', '', true);
    DELETE FROM dx_routing_rules WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('routing_rules', n);
    DELETE FROM dx_outsource_partners WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_test_components WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('components', n);
    DELETE FROM dx_signatories WHERE clinic_id = p_clinic_id;
    UPDATE lab_tests SET department_id = NULL, container_id = NULL
     WHERE clinic_id = p_clinic_id AND (department_id IS NOT NULL OR container_id IS NOT NULL);
    DELETE FROM dx_containers  WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_departments WHERE clinic_id = p_clinic_id;
    DELETE FROM dx_sites WHERE clinic_id = p_clinic_id; GET DIAGNOSTICS n = ROW_COUNT;
    v := v || jsonb_build_object('sites', n);
    UPDATE clinics SET dx_state = 'OFF' WHERE id = p_clinic_id;
    PERFORM set_config('kriya.dx_purge_clinic', '', true);
    RETURN v;
END $$;

-- ── 13. RLS parity + API-role lockout ──────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['dx_rate_cards', 'dx_rate_card_items', 'dx_b2b_ledger', 'dx_b2b_statements', 'dx_barcode_stock']
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
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname LIKE 'dx\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
        END IF;
        EXECUTE format('ALTER FUNCTION %s SET search_path = public, pg_temp', r.sig);
    END LOOP;
END $$;

-- ── 14. Verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF (SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relrowsecurity AND relforcerowsecurity
          AND relname IN ('dx_rate_cards', 'dx_rate_card_items', 'dx_b2b_ledger', 'dx_b2b_statements',
                          'dx_barcode_stock')) <> 5 THEN
        RAISE EXCEPTION '132 verify: B2B tables missing or RLS not forced';
    END IF;
    IF (SELECT count(*) FROM pg_indexes WHERE indexname IN ('uq_dx_ledger_charge', 'uq_dx_ledger_idem',
                                                            'uq_dx_b2b_statement_period')) <> 3 THEN
        RAISE EXCEPTION '132 verify: ledger / statement unique indexes missing';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_dx_b2b_ledger_guard', 'trg_dx_b2b_statements_guard',
                                                         'trg_dx_barcode_stock_guard')) <> 3 THEN
        RAISE EXCEPTION '132 verify: B2B guards missing';
    END IF;
    IF to_regprocedure('dx_b2b_balance(uuid,uuid)') IS NULL
       OR to_regprocedure('dx_b2b_post_charge(uuid,uuid,uuid,uuid,bigint,uuid,text)') IS NULL
       OR to_regprocedure('dx_b2b_reverse_charge(uuid,uuid,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_b2b_record_payment(uuid,uuid,bigint,text,text,text,uuid,text)') IS NULL
       OR to_regprocedure('dx_b2b_issue_statement(uuid,uuid,date,date,uuid,text)') IS NULL
       OR to_regprocedure('dx_b2b_set_hold(uuid,uuid,text,text,text,uuid,text)') IS NULL THEN
        RAISE EXCEPTION '132 verify: B2B functions missing';
    END IF;
    IF position('dx_b2b_post_visit_charges' IN pg_get_functiondef('dx_register_visit(uuid,jsonb,uuid,text)'::regprocedure)) = 0
       OR position('dx_b2b_reverse_charge' IN pg_get_functiondef('dx_cancel_item(uuid,uuid,text,uuid,text)'::regprocedure)) = 0
       OR position('dx_check_barcode_stock' IN pg_get_functiondef('dx_bind_barcode(uuid,uuid,text,uuid,text)'::regprocedure)) = 0
       OR position('dx_b2b_not_enabled' IN pg_get_functiondef('dx_price_items(jsonb,jsonb)'::regprocedure)) > 0 THEN
        RAISE EXCEPTION '132 verify: function patches missing';
    END IF;
END $$;
