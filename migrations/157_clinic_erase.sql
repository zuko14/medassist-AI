-- ============================================================================
-- Migration 157: permanent clinic erasure (owner console "Erase permanently").
--
-- kriya_erase_clinic(p_clinic_id) removes every row of ONE clinic from every public base table that has a
-- clinic_id column, then the clinics row itself (FK cascades catch junction tables without clinic_id).
--
-- Signed records, ledgers and payment_events carry append-only guard triggers, several without a purge flag.
-- For each table that still holds this clinic's rows, the function disables exactly the USER triggers that are
-- enabled right now (never internal FK triggers), deletes, and re-enables those same triggers — all inside the
-- caller's single transaction. Any failure (a guard we cannot pass, an FK we cannot order, a table we do not own)
-- leaves residual rows; the final check then RAISES and the whole erase rolls back: either the clinic is gone
-- completely or nothing changed. Schema-agnostic on purpose: works on a database at any migration level.
--
-- Storage objects (lab-reports/<clinic_id>/…) are removed by the API after this commits.
-- Adds no table, column, FK or trigger. Callable by service_role only.
-- ============================================================================

CREATE OR REPLACE FUNCTION kriya_erase_clinic(p_clinic_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_tables   TEXT[];
    v_where    JSONB := '{}'::jsonb;   -- table -> predicate; clinic_id is UUID on most tables, TEXT on a few
    v_pending  TEXT[];
    v_left     TEXT[];
    t          TEXT;
    trg        TEXT;
    v_trgs     TEXT[];
    n          BIGINT;
    v_has      BOOLEAN;
    v_counts   JSONB := '{}'::jsonb;
    v_errors   JSONB := '{}'::jsonb;
    v_pass     INT := 0;
BEGIN
    IF p_clinic_id IS NULL THEN RAISE EXCEPTION 'erase: clinic id required'; END IF;
    PERFORM 1 FROM clinics WHERE id = p_clinic_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'erase: clinic % not found', p_clinic_id; END IF;

    SELECT array_agg(c.table_name::TEXT ORDER BY c.table_name),
           jsonb_object_agg(c.table_name, CASE WHEN c.data_type = 'uuid' THEN 'clinic_id = $1'
                                               ELSE 'clinic_id::text = $1::text' END)
      INTO v_tables, v_where
      FROM information_schema.columns c
      JOIN information_schema.tables tb
        ON tb.table_schema = c.table_schema AND tb.table_name = c.table_name AND tb.table_type = 'BASE TABLE'
     WHERE c.table_schema = 'public' AND c.column_name = 'clinic_id' AND c.table_name <> 'clinics';

    -- payment_events rows written before migration 054 have clinic_id NULL yet point at this clinic's appointments
    -- (ON DELETE RESTRICT): claim them so the sweep removes them with the rest.
    IF to_regclass('public.payment_events') IS NOT NULL THEN
        SELECT array_agg(tg.tgname::TEXT) INTO v_trgs FROM pg_trigger tg
         WHERE tg.tgrelid = 'public.payment_events'::regclass AND NOT tg.tgisinternal AND tg.tgenabled <> 'D';
        FOREACH trg IN ARRAY COALESCE(v_trgs, ARRAY[]::TEXT[]) LOOP
            EXECUTE format('ALTER TABLE payment_events DISABLE TRIGGER %I', trg);
        END LOOP;
        UPDATE payment_events SET clinic_id = p_clinic_id
         WHERE clinic_id IS NULL AND booking_id IN (SELECT id FROM appointments WHERE clinic_id = p_clinic_id);
        FOREACH trg IN ARRAY COALESCE(v_trgs, ARRAY[]::TEXT[]) LOOP
            EXECUTE format('ALTER TABLE payment_events ENABLE TRIGGER %I', trg);
        END LOOP;
    END IF;

    -- Only tables that actually hold this clinic's rows are touched (no lock on any other table).
    v_pending := ARRAY[]::TEXT[];
    FOREACH t IN ARRAY COALESCE(v_tables, ARRAY[]::TEXT[]) LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE %s)', t, v_where ->> t) INTO v_has USING p_clinic_id;
        IF v_has THEN v_pending := v_pending || t; END IF;
    END LOOP;

    -- Repeat until no table makes progress: an FK child must go before its parent, and the order is not known
    -- statically across 150+ migrations.
    WHILE array_length(v_pending, 1) > 0 AND v_pass < 25 LOOP
        v_pass := v_pass + 1;
        v_left := ARRAY[]::TEXT[];
        FOREACH t IN ARRAY v_pending LOOP
            BEGIN
                SELECT array_agg(tg.tgname::TEXT) INTO v_trgs
                  FROM pg_trigger tg
                 WHERE tg.tgrelid = format('public.%I', t)::regclass
                   AND NOT tg.tgisinternal AND tg.tgenabled <> 'D';
                FOREACH trg IN ARRAY COALESCE(v_trgs, ARRAY[]::TEXT[]) LOOP
                    EXECUTE format('ALTER TABLE %I DISABLE TRIGGER %I', t, trg);
                END LOOP;
                EXECUTE format('DELETE FROM %I WHERE %s', t, v_where ->> t) USING p_clinic_id;
                GET DIAGNOSTICS n = ROW_COUNT;
                FOREACH trg IN ARRAY COALESCE(v_trgs, ARRAY[]::TEXT[]) LOOP
                    EXECUTE format('ALTER TABLE %I ENABLE TRIGGER %I', t, trg);
                END LOOP;
                v_counts := v_counts || jsonb_build_object(t, COALESCE((v_counts ->> t)::BIGINT, 0) + n);
                v_errors := v_errors - t;
            EXCEPTION WHEN OTHERS THEN
                -- The subtransaction rolled back, trigger state included. Retry on the next pass.
                v_left := v_left || t;
                v_errors := v_errors || jsonb_build_object(t, SQLERRM);
            END;
        END LOOP;
        EXIT WHEN array_length(v_left, 1) IS NOT DISTINCT FROM array_length(v_pending, 1);  -- no progress
        v_pending := v_left;
    END LOOP;

    IF array_length(v_left, 1) > 0 THEN
        RAISE EXCEPTION 'erase: could not remove rows from %: %', array_to_string(v_left, ', '), v_errors::TEXT;
    END IF;

    DELETE FROM clinics WHERE id = p_clinic_id;
    v_counts := v_counts || jsonb_build_object('clinics', 1);

    -- Residual check: nothing anywhere still points at this clinic.
    FOREACH t IN ARRAY COALESCE(v_tables, ARRAY[]::TEXT[]) LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I WHERE %s)', t, v_where ->> t) INTO v_has USING p_clinic_id;
        IF v_has THEN RAISE EXCEPTION 'erase: rows remain in %', t; END IF;
    END LOOP;

    RETURN jsonb_build_object('clinic_id', p_clinic_id, 'tables', v_counts,
                              'rows', (SELECT COALESCE(sum(value::BIGINT), 0) FROM jsonb_each_text(v_counts)));
END $$;

REVOKE ALL ON FUNCTION kriya_erase_clinic(UUID) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON FUNCTION kriya_erase_clinic(UUID) FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON FUNCTION kriya_erase_clinic(UUID) FROM authenticated;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT EXECUTE ON FUNCTION kriya_erase_clinic(UUID) TO service_role;
    END IF;
END $$;

-- ── 157 verify ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF to_regprocedure('kriya_erase_clinic(uuid)') IS NULL THEN
        RAISE EXCEPTION '157 verify: kriya_erase_clinic missing';
    END IF;
END $$;
