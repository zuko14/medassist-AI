-- ============================================================================
-- Migration 104: OPD online-payment exceptions
-- An online payment (payment link) that arrives for more than the invoice's
-- remaining balance, or for a voided invoice, used to write no receipt at all:
-- the patient had paid and the money existed only in the gateway dashboard.
-- Now the part that fits the balance is a normal receipt and the rest is a row
-- here, open until an admin refunds it through the gateway or records how it
-- was settled. Additive: pre-104 code never reads this table.
-- Apply BEFORE deploying code that ships this file (lifespan parity check).
-- ============================================================================

CREATE TABLE IF NOT EXISTS opd_payment_exceptions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id            UUID NOT NULL REFERENCES clinics (id) ON DELETE CASCADE,
    invoice_id           UUID NOT NULL,
    gateway              TEXT NOT NULL CHECK (gateway IN ('razorpay', 'phonepe')),
    gateway_payment_id   TEXT NOT NULL CHECK (char_length(gateway_payment_id) BETWEEN 1 AND 100),
    -- PhonePe refunds by merchant order id, not payment id
    gateway_order_id     TEXT NULL CHECK (char_length(gateway_order_id) <= 100),
    reason               TEXT NOT NULL CHECK (reason IN ('overpaid', 'invoice_settled', 'invoice_void')),
    paid_paise           INTEGER NOT NULL CHECK (paid_paise BETWEEN 1 AND 100000000),
    applied_paise        INTEGER NOT NULL DEFAULT 0 CHECK (applied_paise >= 0),
    excess_paise         INTEGER GENERATED ALWAYS AS (paid_paise - applied_paise) STORED,
    status               TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'refunded', 'settled_offline')),
    resolution_reference TEXT NULL CHECK (char_length(resolution_reference) <= 100),
    resolution_note      TEXT NULL CHECK (char_length(resolution_note) <= 300),
    resolved_by_admin_id UUID NULL,
    resolved_by_name     TEXT NULL,
    resolved_at          TIMESTAMPTZ NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT opd_payment_exceptions_clinic_id_id_key UNIQUE (clinic_id, id),
    CONSTRAINT opd_payment_exceptions_excess_positive CHECK (applied_paise < paid_paise),
    CONSTRAINT opd_payment_exceptions_resolved_complete CHECK (
        (status = 'open') = (resolved_at IS NULL)
        AND (status <> 'refunded' OR resolution_reference IS NOT NULL)
        AND (status <> 'settled_offline' OR char_length(btrim(resolution_note)) >= 5))
);
-- one exception per gateway payment: webhook replays cannot double-count
CREATE UNIQUE INDEX IF NOT EXISTS uq_opd_payment_exceptions_payment
    ON opd_payment_exceptions (clinic_id, gateway, gateway_payment_id);
CREATE INDEX IF NOT EXISTS idx_opd_payment_exceptions_open
    ON opd_payment_exceptions (clinic_id, created_at DESC) WHERE status = 'open';

-- Composite tenant FK. ON DELETE CASCADE so opd_purge_clinic (which deletes
-- invoices) clears these too; the guard below still refuses any other delete.
-- Added conditionally: 103_down drops opd_invoices with CASCADE, which drops
-- this constraint, and a re-apply must restore it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'opd_payment_exceptions_invoice_fk') THEN
        ALTER TABLE opd_payment_exceptions ADD CONSTRAINT opd_payment_exceptions_invoice_fk
            FOREIGN KEY (clinic_id, invoice_id) REFERENCES opd_invoices (clinic_id, id) ON DELETE CASCADE;
    END IF;
END $$;

-- Money facts are immutable; the only change allowed is open -> resolved, once.
CREATE OR REPLACE FUNCTION opd_guard_payment_exception() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF NOT opd_purging(OLD.clinic_id) THEN
            RAISE EXCEPTION 'opd_append_only:opd_payment_exceptions' USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'opd_record_locked:opd_payment_exceptions:%', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.clinic_id <> OLD.clinic_id OR NEW.invoice_id <> OLD.invoice_id OR NEW.gateway <> OLD.gateway
       OR NEW.gateway_payment_id <> OLD.gateway_payment_id OR NEW.gateway_order_id IS DISTINCT FROM OLD.gateway_order_id
       OR NEW.reason <> OLD.reason OR NEW.paid_paise <> OLD.paid_paise OR NEW.applied_paise <> OLD.applied_paise
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'opd_identity_immutable:opd_payment_exceptions' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_opd_payment_exceptions_guard ON opd_payment_exceptions;
CREATE TRIGGER trg_opd_payment_exceptions_guard BEFORE UPDATE OR DELETE ON opd_payment_exceptions
    FOR EACH ROW EXECUTE FUNCTION opd_guard_payment_exception();

-- RLS parity with 103 + API-role lockout
DO $$
BEGIN
    ALTER TABLE opd_payment_exceptions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE opd_payment_exceptions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS service_role_all_opd_payment_exceptions ON opd_payment_exceptions;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE POLICY service_role_all_opd_payment_exceptions ON opd_payment_exceptions
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    DROP POLICY IF EXISTS tenant_isolation_opd_payment_exceptions ON opd_payment_exceptions;
    CREATE POLICY tenant_isolation_opd_payment_exceptions ON opd_payment_exceptions FOR ALL
        USING (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid)
        WITH CHECK (clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON TABLE opd_payment_exceptions FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON TABLE opd_payment_exceptions FROM authenticated;
    END IF;
END $$;

-- Verify
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'opd_payment_exceptions') THEN
        RAISE EXCEPTION '104 verify: opd_payment_exceptions missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'opd_payment_exceptions_invoice_fk') THEN
        RAISE EXCEPTION '104 verify: invoice FK missing';
    END IF;
END $$;
