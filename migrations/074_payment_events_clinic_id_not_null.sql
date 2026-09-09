-- ============================================================================
-- Migration 074: Enforce tenant ownership on payment_events (KA-A-17)
-- ============================================================================
-- Migration 054 added payment_events.clinic_id and backfilled it once, but the
-- column stayed nullable and 14 of 25 call sites in app/services/payment.py
-- never passed it. Every refund, admin confirm/reject/cancel, hold-expiry and
-- webhook-recovery event since then wrote a NULL-tenant audit row.
--
-- Scope, verified rather than assumed: the RLS policy from migration 049
-- resolves this table's tenant by joining appointments on booking_id, and every
-- shipped read filters by booking_id too, so no live query was returning the
-- wrong rows. What was broken is the column itself — it is indexed and declared
-- tenant-owned, so the first aggregation to group or filter by it would have
-- quietly undercounted.
--
-- The application fix resolves clinic_id inside _log_payment_event_raw(), so no
-- new NULLs can be written. This migration repairs the existing rows and makes
-- the invariant the database's job rather than the caller's.
--
-- Idempotent: safe to re-run. Zero downtime: uses ADD CONSTRAINT NOT VALID +
-- VALIDATE (SHARE UPDATE EXCLUSIVE, concurrent reads and writes keep working)
-- instead of SET NOT NULL, which needs ACCESS EXCLUSIVE and a blocking scan.
-- ============================================================================

DO $$
DECLARE
    orphan_count BIGINT;
    unresolvable BIGINT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = 'public' AND tablename = 'payment_events'
    ) THEN
        RAISE NOTICE 'payment_events absent — nothing to do';
        RETURN;
    END IF;

    SELECT count(*) INTO orphan_count
    FROM payment_events WHERE clinic_id IS NULL;
    RAISE NOTICE 'payment_events rows with NULL clinic_id before backfill: %', orphan_count;

    -- ── Step 1: backfill from the parent appointment ──
    -- payment_events.booking_id is NOT NULL REFERENCES appointments(id)
    -- ON DELETE RESTRICT, so the parent row always exists and names exactly
    -- one clinic. This is a lookup, not a heuristic.
    IF orphan_count > 0 THEN
        -- The append-only trigger rejects UPDATEs; migration 054 does the same.
        IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_payment_events_no_update') THEN
            ALTER TABLE payment_events DISABLE TRIGGER trg_payment_events_no_update;
        END IF;

        -- ponytail: single atomic UPDATE. Batching buys nothing here because
        -- the migration runner wraps each file in one transaction, so locks
        -- are held to commit either way. If payment_events ever grows past a
        -- few million rows, split this into a standalone batched job first.
        UPDATE payment_events pe
        SET clinic_id = a.clinic_id
        FROM appointments a
        WHERE pe.booking_id = a.id
          AND pe.clinic_id IS NULL
          AND a.clinic_id IS NOT NULL;

        IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_payment_events_no_update') THEN
            ALTER TABLE payment_events ENABLE TRIGGER trg_payment_events_no_update;
        END IF;
    END IF;

    -- ── Step 2: declare the invariant ──
    -- NOT VALID is instant and blocks every future NULL insert immediately.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_payment_events_clinic_id_present'
          AND conrelid = 'payment_events'::regclass
    ) THEN
        ALTER TABLE payment_events
            ADD CONSTRAINT chk_payment_events_clinic_id_present
            CHECK (clinic_id IS NOT NULL) NOT VALID;
    END IF;

    -- ── Step 3: validate only if the backfill actually reached every row ──
    -- appointments.clinic_id is itself nullable (004 left the NOT NULL
    -- commented out), so a parent with no clinic leaves a child unresolvable.
    -- Validating anyway would abort the whole migration; leaving the
    -- constraint NOT VALID still stops all new bad rows, which is the part
    -- that matters. The NOTICE tells the operator what to clean up.
    SELECT count(*) INTO unresolvable
    FROM payment_events WHERE clinic_id IS NULL;

    IF unresolvable = 0 THEN
        ALTER TABLE payment_events
            VALIDATE CONSTRAINT chk_payment_events_clinic_id_present;
        RAISE NOTICE 'payment_events.clinic_id backfilled and constraint validated';
    ELSE
        RAISE WARNING
            'payment_events still has % rows whose parent appointment has a NULL clinic_id. '
            'Constraint left NOT VALID (new inserts are still blocked). '
            'Fix the parent appointments, then run: '
            'ALTER TABLE payment_events VALIDATE CONSTRAINT chk_payment_events_clinic_id_present;',
            unresolvable;
    END IF;
END $$;

-- Index: migration 054 already created idx_payment_events_clinic_id on
-- (clinic_id) WHERE clinic_id IS NOT NULL. With the constraint above that
-- predicate now matches every row, so the partial index covers all clinic
-- lookups. A second unconditional index would be pure duplication.
