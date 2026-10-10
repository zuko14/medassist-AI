-- ============================================================================
-- Rollback 104: OPD online-payment exceptions
-- Refuses while any exception is still open: those rows are the only record
-- of patient money the clinic still owes. Resolve them first.
-- ============================================================================

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM opd_payment_exceptions WHERE status = 'open') THEN
        RAISE EXCEPTION '104_down: open OPD payment exceptions exist — refund or settle them first';
    END IF;
END $$;

DROP TABLE IF EXISTS opd_payment_exceptions;
DROP FUNCTION IF EXISTS opd_guard_payment_exception();
