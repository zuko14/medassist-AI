-- ============================================================================
-- KRIYA AI / MediAssist AI — Full Schema Verification Script
-- Run this in the Supabase SQL Editor to verify all 55 migrations (001–055)
-- have been applied correctly.
--
-- Output: A single result set with check_name, status (PASS/FAIL), and detail.
-- Target: ZERO rows with status = 'FAIL'.
-- ============================================================================

DO $$
BEGIN
    -- Create a temporary table for results
    CREATE TEMP TABLE IF NOT EXISTS _verification_results (
        check_id    SERIAL,
        category    TEXT,
        check_name  TEXT,
        status      TEXT,  -- 'PASS' or 'FAIL'
        detail      TEXT
    );
    TRUNCATE _verification_results;
END $$;

-- ============================================================================
-- 1. MIGRATION TRACKING — verify all 55 are recorded
-- ============================================================================
DO $$
DECLARE
    v_count INT;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations') THEN
        SELECT count(*) INTO v_count FROM schema_migrations;
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('MIGRATIONS', 'schema_migrations table exists', 'PASS',
                v_count || ' migrations recorded');

        IF v_count >= 55 THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('MIGRATIONS', 'All 55+ migrations recorded', 'PASS',
                    v_count || ' total migrations');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('MIGRATIONS', 'All 55+ migrations recorded', 'FAIL',
                    'Only ' || v_count || ' recorded, expected >= 55');
        END IF;
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('MIGRATIONS', 'schema_migrations table exists', 'FAIL',
                'Table not found — run scripts/migrate.py or create manually');
    END IF;
END $$;


-- ============================================================================
-- 2. TABLES — verify all 33 tables exist
-- ============================================================================
DO $$
DECLARE
    v_table TEXT;
    v_tables TEXT[] := ARRAY[
        'patients', 'appointments', 'conversations', 'analytics_events',
        'doctors', 'doctor_leaves', 'hospital_holidays', 'prescriptions',
        'lab_reports', 'clinics', 'processed_messages', 'payment_events',
        'integration_connectors', 'branches', 'clinic_admins',
        'connector_failed_reports', 'admin_audit_logs', 'failed_messages',
        'rate_limits', 'family_members', 'webhook_security_events',
        'connector_audit_log', 'integration_processed_reports',
        'callmedex_whatsapp_settings', 'doctor_branches',
        'outbound_message_ledger', 'plan_tiers', 'broadcasts',
        'admin_notifications', 'lab_tests', 'meta_pricing_config',
        'inbound_messages', 'scheduler_locks',
        'specialty_treatments', 'treatment_doctors'
    ];
BEGIN
    FOREACH v_table IN ARRAY v_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = v_table
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('TABLES', 'Table: ' || v_table, 'PASS', 'Exists');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('TABLES', 'Table: ' || v_table, 'FAIL', 'MISSING');
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- 3. FUNCTIONS — verify all 7 functions exist
-- ============================================================================
DO $$
DECLARE
    v_func TEXT;
    v_funcs TEXT[] := ARRAY[
        'acquire_scheduler_lock', 'release_scheduler_lock',
        'check_and_record_rate_limit', 'prevent_payment_event_mutation',
        'update_clinics_updated_at', 'update_plan_tiers_updated_at',
        'update_updated_at_column'
    ];
BEGIN
    FOREACH v_func IN ARRAY v_funcs LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.routines
            WHERE routine_schema = 'public' AND routine_name = v_func
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('FUNCTIONS', 'Function: ' || v_func, 'PASS', 'Exists');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('FUNCTIONS', 'Function: ' || v_func, 'FAIL', 'MISSING');
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- 4. CRITICAL INDEXES — verify the ones that matter for correctness
-- ============================================================================
DO $$
DECLARE
    v_idx TEXT;
    v_indexes TEXT[] := ARRAY[
        'uq_appointment_active_slot',
        'uq_appointment_booking_ref',
        'uq_payment_event_provider_id',
        'idx_unique_queue_token',
        'idx_appointments_stale_holds',
        'idx_inbound_messages_locked_at',
        'idx_inbound_messages_status_retry',
        'idx_scheduler_locks_expires',
        'idx_payment_events_clinic_id',
        'idx_clinics_phone_number_id',
        'idx_patients_clinic_phone',
        'idx_conversations_clinic_phone',
        'idx_lab_reports_clinic_external_report',
        'idx_processed_unique'
    ];
BEGIN
    FOREACH v_idx IN ARRAY v_indexes LOOP
        IF EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE schemaname = 'public' AND indexname = v_idx
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('INDEXES', 'Index: ' || v_idx, 'PASS', 'Exists');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('INDEXES', 'Index: ' || v_idx, 'FAIL', 'MISSING');
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- 5. MIGRATION 051 — chk_admin_scope constraint
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'chk_admin_scope'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M051', 'chk_admin_scope constraint exists', 'PASS',
                'CHECK (role = super_admin OR clinic_id IS NOT NULL)');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M051', 'chk_admin_scope constraint exists', 'FAIL',
                'MISSING — migration 051 not applied');
    END IF;
END $$;

-- ============================================================================
-- 6. MIGRATION 052 — booking_ref widened to VARCHAR(32) + per-tenant index
-- ============================================================================
DO $$
DECLARE
    v_len INT;
BEGIN
    SELECT character_maximum_length INTO v_len
    FROM information_schema.columns
    WHERE table_name = 'appointments' AND column_name = 'booking_ref';

    IF v_len IS NOT NULL AND v_len >= 32 THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M052', 'booking_ref widened to VARCHAR(32)', 'PASS',
                'Current width: ' || v_len);
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M052', 'booking_ref widened to VARCHAR(32)', 'FAIL',
                'Current width: ' || COALESCE(v_len::TEXT, 'NULL'));
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'uq_appointment_booking_ref'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M052', 'Per-tenant booking_ref unique index', 'PASS',
                'uq_appointment_booking_ref exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M052', 'Per-tenant booking_ref unique index', 'FAIL',
                'uq_appointment_booking_ref MISSING');
    END IF;
END $$;

-- ============================================================================
-- 7. MIGRATION 053 — stale holds index
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_appointments_stale_holds'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M053', 'Stale holds partial index', 'PASS', 'exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M053', 'Stale holds partial index', 'FAIL', 'MISSING');
    END IF;
END $$;

-- ============================================================================
-- 8. MIGRATION 054 — payment_events columns + index + slot index drop
-- ============================================================================
DO $$
BEGIN
    -- clinic_id column
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payment_events' AND column_name = 'clinic_id'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'payment_events.clinic_id column', 'PASS', 'Exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'payment_events.clinic_id column', 'FAIL', 'MISSING');
    END IF;

    -- provider_event_id column
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payment_events' AND column_name = 'provider_event_id'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'payment_events.provider_event_id column', 'PASS', 'Exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'payment_events.provider_event_id column', 'FAIL', 'MISSING');
    END IF;

    -- provider_event_id unique index
    IF EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'uq_payment_event_provider_id'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'Payment event idempotency index', 'PASS', 'exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'Payment event idempotency index', 'FAIL', 'MISSING');
    END IF;

    -- Duplicate slot index dropped
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_unique_active_slot'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'Duplicate slot index dropped', 'PASS',
                'idx_unique_active_slot correctly removed');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M054', 'Duplicate slot index dropped', 'FAIL',
                'idx_unique_active_slot still exists');
    END IF;
END $$;

-- ============================================================================
-- 9. MIGRATION 055 — global booking_ref constraint dropped
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'appointments'
          AND constraint_name = 'appointments_booking_ref_key'
          AND constraint_type = 'UNIQUE'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M055', 'Global booking_ref UNIQUE dropped', 'PASS',
                'appointments_booking_ref_key correctly removed');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M055', 'Global booking_ref UNIQUE dropped', 'FAIL',
                'appointments_booking_ref_key still exists');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_appointments_booking_ref'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M055', 'Legacy booking_ref index dropped', 'PASS',
                'idx_appointments_booking_ref correctly removed');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M055', 'Legacy booking_ref index dropped', 'FAIL',
                'idx_appointments_booking_ref still exists');
    END IF;
END $$;

-- ============================================================================
-- 10. TRIGGERS — payment_events append-only trigger intact
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.triggers
        WHERE trigger_name = 'trg_payment_events_no_update'
          AND event_object_table = 'payment_events'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('TRIGGERS', 'payment_events append-only trigger', 'PASS',
                'trg_payment_events_no_update active');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('TRIGGERS', 'payment_events append-only trigger', 'FAIL',
                'trg_payment_events_no_update MISSING or disabled');
    END IF;
END $$;

-- ============================================================================
-- 11. RLS — verify RLS is enabled on critical tables (migration 049)
-- ============================================================================
DO $$
DECLARE
    v_table TEXT;
    v_rls_tables TEXT[] := ARRAY[
        'appointments', 'patients', 'lab_reports', 'doctors',
        'conversations', 'clinic_admins', 'processed_messages',
        'inbound_messages', 'scheduler_locks'
    ];
    v_rls BOOLEAN;
BEGIN
    FOREACH v_table IN ARRAY v_rls_tables LOOP
        SELECT rowsecurity INTO v_rls
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename = v_table;

        IF v_rls THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('RLS', 'RLS enabled: ' || v_table, 'PASS', 'Row-level security ON');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('RLS', 'RLS enabled: ' || v_table, 'FAIL',
                    'Row-level security OFF — migration 049 incomplete');
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- 12. CRITICAL COLUMNS — verify key columns from later migrations
-- ============================================================================
DO $$
DECLARE
    v_check RECORD;
BEGIN
    FOR v_check IN
        SELECT * FROM (VALUES
            ('appointments', 'clinic_id',            '003'),
            ('appointments', 'booking_ref',           '001'),
            ('appointments', 'razorpay_order_id',     '008'),
            ('appointments', 'hold_expires_at',       '008'),
            ('appointments', 'booking_type',          '039'),
            ('appointments', 'refund_id',             '046'),
            ('appointments', 'completed_at',          '063'),
            ('appointments', 'treatment_id',          '077'),
            ('appointments', 'treatment_name',        '077'),
            ('clinics',      'plan',                  '006'),
            ('clinics',      'phone_number_id',       '043'),
            ('clinics',      'config',                '003'),
            ('inbound_messages', 'status',            '047'),
            ('inbound_messages', 'locked_at',         '047'),
            ('inbound_messages', 'payload',           '047'),
            ('inbound_messages', 'attempt_count',     '047'),
            ('inbound_messages', 'retry_at',          '047'),
            ('lab_reports',  'file_path',             '002'),
            ('lab_reports',  'external_report_id',    '026'),
            ('lab_reports',  'ai_summary_sent',       '062'),
            ('lab_reports',  'matched_booking_id',    '040'),
            ('clinic_admins', 'role',                 '011'),
            ('clinic_admins', 'branch_id',            '036'),
            ('payment_events', 'clinic_id',           '054'),
            ('payment_events', 'provider_event_id',   '054')
        ) AS t(tbl, col, migration)
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = v_check.tbl AND column_name = v_check.col
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('COLUMNS', v_check.tbl || '.' || v_check.col, 'PASS',
                    'Added by migration ' || v_check.migration);
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('COLUMNS', v_check.tbl || '.' || v_check.col, 'FAIL',
                    'MISSING — migration ' || v_check.migration || ' not applied');
        END IF;
    END LOOP;
END $$;


-- ============================================================================
-- 13. KEY CONSTRAINT CHECKS
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'appointments_status_check'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('CONSTRAINTS', 'appointments_status_check', 'PASS', 'Exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('CONSTRAINTS', 'appointments_status_check', 'FAIL', 'MISSING');
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'clinics_plan_check'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('CONSTRAINTS', 'clinics_plan_check', 'PASS', 'Exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('CONSTRAINTS', 'clinics_plan_check', 'FAIL', 'MISSING');
    END IF;
END $$;

-- ============================================================================
-- 14. ZERO-OFFENDER CHECK — unscoped non-super-admin accounts (051)
-- ============================================================================
DO $$
DECLARE
    v_offenders INT;
BEGIN
    SELECT count(*) INTO v_offenders
    FROM clinic_admins
    WHERE clinic_id IS NULL
      AND role IS DISTINCT FROM 'super_admin';

    IF v_offenders = 0 THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('DATA_INTEGRITY', 'No unscoped non-super-admin accounts', 'PASS',
                '0 offenders — safe to VALIDATE CONSTRAINT chk_admin_scope');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('DATA_INTEGRITY', 'No unscoped non-super-admin accounts', 'FAIL',
                v_offenders || ' accounts with clinic_id=NULL and role != super_admin');
    END IF;
END $$;

-- ============================================================================
-- 15. MIGRATION 103 — OPD OS Core checks
-- ============================================================================
DO $$
DECLARE
    v_tbl TEXT;
    v_opd_tables TEXT[] := ARRAY[
        'opd_encounters', 'opd_prescriptions', 'opd_prescription_items',
        'opd_invoices', 'opd_invoice_items', 'opd_receipts', 'opd_cashier_shifts',
        'opd_payment_exceptions'  -- migration 104
    ];
    v_func TEXT;
    v_opd_funcs TEXT[] := ARRAY[
        'opd_next_counter', 'opd_format_number', 'opd_assign_mrn',
        'opd_sign_encounter', 'opd_sign_prescription', 'opd_close_shift',
        'opd_purge_clinic'
    ];
    v_slot_rebuilt_count INT;
BEGIN
    -- 15.1 OPD Tables
    FOREACH v_tbl IN ARRAY v_opd_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = v_tbl
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('M103', 'Table: ' || v_tbl, 'PASS', 'Exists');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('M103', 'Table: ' || v_tbl, 'FAIL', 'MISSING');
        END IF;
    END LOOP;

    -- 15.2 Slot Guards Rebuilt
    SELECT count(*) INTO v_slot_rebuilt_count
    FROM pg_indexes
    WHERE tablename = 'appointments'
      AND indexname IN ('uq_appointment_active_slot', 'uq_appointment_active_slot_unassigned')
      AND indexdef ILIKE '%is_walk_in = false%';

    IF v_slot_rebuilt_count = 2 THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M103', 'Slot uniqueness guards rebuilt with is_walk_in = false', 'PASS', 'Both active slot indexes exclude walk-ins');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M103', 'Slot uniqueness guards rebuilt with is_walk_in = false', 'FAIL',
                'Only ' || v_slot_rebuilt_count || ' of 2 rebuilt indexes found with is_walk_in = false');
    END IF;

    -- 15.3 appointments_queue_status_check constraint
    IF EXISTS (
        SELECT 1 FROM information_schema.check_constraints
        WHERE constraint_name = 'appointments_queue_status_check'
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M103', 'appointments_queue_status_check constraint', 'PASS', 'Exists');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M103', 'appointments_queue_status_check constraint', 'FAIL', 'MISSING');
    END IF;

    -- 15.4 OPD Functions
    FOREACH v_func IN ARRAY v_opd_funcs LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.routines
            WHERE routine_schema = 'public' AND routine_name = v_func
        ) THEN
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('M103', 'Function: ' || v_func, 'PASS', 'Exists');
        ELSE
            INSERT INTO _verification_results (category, check_name, status, detail)
            VALUES ('M103', 'Function: ' || v_func, 'FAIL', 'MISSING');
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- 16. MIGRATION 108 — Kriya Ketu assistant_runs
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE relname = 'assistant_runs' AND relkind = 'r' AND relrowsecurity
    ) THEN
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M108', 'Table: assistant_runs', 'PASS', 'Exists, RLS enabled');
    ELSE
        INSERT INTO _verification_results (category, check_name, status, detail)
        VALUES ('M108', 'Table: assistant_runs', 'FAIL', 'MISSING or RLS off');
    END IF;
END $$;

-- ============================================================================
-- 17. MIGRATION 109 — hospital_holidays keyed per clinic (D-27)
-- ============================================================================
DO $$
DECLARE
    v_cols text[];
BEGIN
    SELECT array_agg(a.attname::text ORDER BY k.ord) INTO v_cols
      FROM pg_constraint c
      CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
     WHERE c.conrelid = 'hospital_holidays'::regclass AND c.contype = 'p';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M109', 'PK: hospital_holidays (clinic_id, holiday_date)',
            CASE WHEN v_cols IS NOT DISTINCT FROM ARRAY['clinic_id', 'holiday_date'] THEN 'PASS' ELSE 'FAIL' END,
            'Primary key is ' || coalesce(array_to_string(v_cols, ', '), 'missing'));
END $$;

-- ============================================================================
-- 18. MIGRATION 110 — atomic partial config writes (D-13)
-- ============================================================================
DO $$
BEGIN
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M110', 'Function: kriya_config_apply(text,uuid,uuid,jsonb,jsonb)',
            CASE WHEN to_regprocedure('kriya_config_apply(text,uuid,uuid,jsonb,jsonb)') IS NOT NULL
                 THEN 'PASS' ELSE 'FAIL' END,
            CASE WHEN to_regprocedure('kriya_config_apply(text,uuid,uuid,jsonb,jsonb)') IS NOT NULL
                 THEN 'Exists (settings saves write only changed keys)'
                 ELSE 'MISSING: settings saves fall back to whole-object writes (D-13 lost update)' END);
END $$;

-- ============================================================================
-- 19. MIGRATION 125 — Diagnostic OS foundation (DX-M1)
-- ============================================================================
DO $$
DECLARE
    v_cols int;
    v_rls boolean;
    v_cons int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_cols FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'clinics' AND column_name IN ('dx_state', 'dx_settings');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M125', 'Columns: clinics.dx_state, clinics.dx_settings',
            CASE WHEN v_cols = 2 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 2 present');

    SELECT c.relrowsecurity AND c.relforcerowsecurity INTO v_rls FROM pg_class c
     WHERE c.oid = to_regclass('public.dx_sites');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M125', 'Table: dx_sites (RLS enabled + forced)',
            CASE WHEN v_rls THEN 'PASS' ELSE 'FAIL' END,
            CASE WHEN v_rls IS NULL THEN 'MISSING: dx_sites' WHEN v_rls THEN 'Exists, RLS forced'
                 ELSE 'RLS not enabled + forced' END);

    SELECT count(*) INTO v_cons FROM pg_constraint WHERE conname IN (
        'clinics_dx_state_check', 'dx_sites_clinic_id_id_key', 'dx_sites_clinic_branch_key',
        'dx_sites_clinic_code_key', 'dx_sites_branch_fk');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M125', 'Constraints: dx_state CHECK + dx_sites keys / branch FK',
            CASE WHEN v_cons = 5 THEN 'PASS' ELSE 'FAIL' END, v_cons || ' of 5 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_purging(uuid)', 'dx_purge_clinic(uuid)',
                                                   'dx_update_settings(uuid,jsonb,integer)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M125', 'Functions: dx_purging, dx_purge_clinic, dx_update_settings',
            CASE WHEN v_funcs = 3 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 3 present');
END $$;

-- ============================================================================
-- 20. MIGRATION 126 — Diagnostic OS catalogue (DX-M2)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_cols int;
    v_trg int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_departments', 'dx_containers', 'dx_signatories', 'dx_test_components')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M126', 'Tables: dx_departments, dx_containers, dx_signatories, dx_test_components (RLS forced)',
            CASE WHEN v_tables = 4 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 4 present with RLS forced');

    SELECT count(*) INTO v_cols FROM information_schema.columns WHERE table_schema = 'public' AND (
        (table_name = 'lab_tests' AND column_name IN ('test_code', 'kind', 'department_id', 'container_id',
                                                      'dx_result_mode', 'approved_at', 'approval_digest'))
     OR (table_name = 'lab_test_parameters' AND column_name IN ('parameter_code', 'formula', 'decimal_places',
                                                                'rounding_mode', 'low_exclusive', 'high_exclusive')));
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M126', 'Columns: DX fields on lab_tests / lab_test_parameters',
            CASE WHEN v_cols = 13 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 13 present');

    SELECT count(*) INTO v_trg FROM pg_trigger WHERE tgname IN ('trg_dx_test_components_guard',
        'trg_dx_lab_tests_clear_approval', 'trg_dx_parameters_clear_approval', 'trg_dx_components_clear_approval',
        'trg_dx_parameters_kind');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M126', 'Triggers: component guard + approval clearing',
            CASE WHEN v_trg = 5 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 5 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_test_digest(uuid,uuid)',
                                                   'dx_approve_test(uuid,uuid,text,uuid,text)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M126', 'Functions: dx_test_digest, dx_approve_test',
            CASE WHEN v_funcs = 2 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 2 present');
END $$;

-- ============================================================================
-- 21. MIGRATION 127 — Diagnostic OS visits, items, samples, billing (DX-M3)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_cols int;
    v_trg int;
    v_funcs int;
    v_shape text;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_referrers', 'dx_b2b_accounts', 'dx_accessions', 'dx_accession_bookings',
                       'dx_accession_items', 'dx_samples', 'dx_events')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M127', 'Tables: dx visits, items, samples, events, referrers, B2B accounts (RLS forced)',
            CASE WHEN v_tables = 7 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 7 present with RLS forced');

    SELECT count(*) INTO v_cols FROM information_schema.columns WHERE table_schema = 'public' AND (
        (table_name = 'opd_orders' AND column_name IN ('accession_id', 'accession_item_id', 'dx_sample_id', 'replaces_order_id'))
     OR (table_name = 'opd_invoices' AND column_name = 'accession_id')
     OR (table_name = 'opd_invoice_items' AND column_name = 'dx_accession_item_id')
     OR (table_name = 'family_members' AND column_name = 'dx_b2b_account_id'));
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M127', 'Columns: DX links on opd_orders / opd_invoices / opd_invoice_items / family_members',
            CASE WHEN v_cols = 7 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 7 present');

    SELECT pg_get_constraintdef(oid) INTO v_shape FROM pg_constraint WHERE conname = 'opd_orders_source_check';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M127', 'Constraint: opd_orders.source allows dx',
            CASE WHEN v_shape LIKE '%dx%' THEN 'PASS' ELSE 'FAIL' END, coalesce(v_shape, 'MISSING'));

    SELECT count(*) INTO v_trg FROM pg_trigger WHERE tgname IN ('trg_dx_guard_invoice_item', 'trg_dx_accessions_guard',
        'trg_dx_accession_items_guard', 'trg_dx_samples_guard', 'trg_dx_events_guard', 'trg_dx_accession_bookings_guard',
        'trg_dx_lab_test_delete', 'trg_dx_invoice_accession');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M127', 'Triggers: DX write guards, DX bill-line guard, lab-test delete guard',
            CASE WHEN v_trg = 8 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 8 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_register_visit(uuid,jsonb,uuid,text)', 'dx_quote(uuid,jsonb)',
        'dx_add_items(uuid,uuid,jsonb,text,numeric,uuid,text)', 'dx_cancel_item(uuid,uuid,text,uuid,text)',
        'dx_cancel_visit(uuid,uuid,text,uuid,text)', 'dx_bill_visit(uuid,uuid,uuid,text)',
        'dx_redact_patient(uuid,uuid)']) f WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M127', 'Functions: register, quote, add/cancel items, cancel visit, bill, redact',
            CASE WHEN v_funcs = 7 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 7 present');
END $$;

-- ============================================================================
-- 22. MIGRATION 128 — Diagnostic OS collection and rejection (DX-M4)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_trg int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_rejection_reasons', 'dx_field_collections', 'dx_label_prints')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M128', 'Tables: dx rejection reasons, field collections, label prints (RLS forced)',
            CASE WHEN v_tables = 3 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 3 present with RLS forced');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_dx_guard_sample', 'trg_dx_field_collections_guard', 'trg_dx_label_prints_guard');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M128', 'Triggers: sample guard, field-collection guard, label-print append-only',
            CASE WHEN v_trg = 3 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 3 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_bind_barcode(uuid,uuid,text,uuid,text)',
        'dx_collect_samples(uuid,uuid,jsonb,text,uuid,text,text)',
        'dx_reject_sample(uuid,uuid,text,text,boolean,uuid,text)', 'dx_receive_sample(uuid,text,uuid,uuid,text)',
        'dx_mark_sample_stored(uuid,uuid,text,uuid,text)',
        'dx_record_field_collection(uuid,uuid,integer,text,text,uuid,text,text)',
        'dx_handover_field_collection(uuid,uuid,uuid,uuid,text)', 'dx_seed_rejection_reasons(uuid)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M128', 'Functions: bind, collect, reject, receive, store, field collection, handover, seed reasons',
            CASE WHEN v_funcs = 8 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 8 present');
END $$;

-- ============================================================================
-- 23. MIGRATION 129 — Diagnostic OS logistics and outsourcing (DX-M5)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_idx int;
    v_cols int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_outsource_partners', 'dx_routing_rules', 'dx_batches', 'dx_batch_samples')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M129', 'Tables: dx outsource partners, routing rules, batches, batch samples (RLS forced)',
            CASE WHEN v_tables = 4 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 4 present with RLS forced');

    SELECT count(*) INTO v_idx FROM pg_class WHERE relname IN ('uq_dx_sample_one_live_batch', 'uq_dx_routing_rules_tie');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M129', 'Indexes: one live batch per sample, routing tie refusal',
            CASE WHEN v_idx = 2 THEN 'PASS' ELSE 'FAIL' END, v_idx || ' of 2 present');

    SELECT count(*) INTO v_cols FROM information_schema.columns
     WHERE column_name = 'outsource_partner_id' AND table_name IN ('opd_orders', 'dx_samples');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M129', 'Columns: opd_orders / dx_samples outsource_partner_id',
            CASE WHEN v_cols = 2 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 2 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_resolve_routing(uuid,uuid,uuid,uuid)',
        'dx_create_batch(uuid,uuid,uuid,uuid,text,numeric,uuid,text)', 'dx_batch_add(uuid,uuid,text[],uuid,text)',
        'dx_dispatch_batch(uuid,uuid,numeric,text,uuid,text)',
        'dx_receive_batch(uuid,uuid,text[],numeric,text,uuid,text)', 'dx_batch_json(uuid,uuid)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M129', 'Functions: resolve routing, create / add / dispatch / receive batch, batch read model',
            CASE WHEN v_funcs = 6 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 6 present');
END $$;

-- ============================================================================
-- 24. MIGRATION 130 — Diagnostic OS result workflow (DX-M6)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_trg int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_result_value_meta', 'dx_result_verifications', 'dx_critical_events', 'dx_result_attachments')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M130', 'Tables: dx result value meta, verifications, critical events, attachments (RLS forced)',
            CASE WHEN v_tables = 4 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 4 present with RLS forced');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_dx_lab_results_gate', 'trg_dx_result_attachments_guard', 'trg_dx_result_value_meta_guard',
                      'trg_dx_result_verifications_guard', 'trg_dx_critical_events_guard');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M130', 'Triggers: verification gate on lab_results, attachment / meta / verification / critical guards',
            CASE WHEN v_trg = 5 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 5 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)',
        'dx_verify_result(uuid,uuid,timestamptz,text,text,uuid,text)', 'dx_approve_results(uuid,jsonb,uuid,text,boolean)',
        'dx_log_critical_call(uuid,uuid,text,text,text,boolean,text,uuid,text)', 'dx_critical_pending(uuid,uuid)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M130', 'Functions: record results, verify, approve, critical call, critical pending',
            CASE WHEN v_funcs = 5 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 5 present');
END $$;

-- ============================================================================
-- 25. MIGRATION 131 — Diagnostic OS reports, templates, delivery (DX-M7) + P6b adoption
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_trg int;
    v_funcs int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_report_templates', 'dx_report_releases', 'dx_release_results', 'dx_release_deliveries',
                       'dx_delivery_overrides', 'dx_outbox', 'dx_print_log', 'dx_share_links')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M131', 'Tables: dx report templates, releases, release results, deliveries, overrides, outbox, print log, share links (RLS forced)',
            CASE WHEN v_tables = 8 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 8 present with RLS forced');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_dx_report_templates_guard', 'trg_dx_report_releases_immutable', 'trg_dx_report_releases_guard',
                      'trg_dx_release_deliveries_guard', 'trg_dx_outbox_receipt', 'trg_dx_outbox_invoice',
                      'trg_dx_internal_items');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M131', 'Triggers: template / release guards, delivery guard, outbox on receipts + invoice status, internal visit lines',
            CASE WHEN v_trg = 7 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 7 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_delivery_decision(uuid,uuid,text)',
        'dx_create_release(uuid,uuid,uuid[],text,uuid,text)', 'dx_claim_delivery(uuid,uuid)',
        'dx_mark_delivery_result(uuid,uuid,boolean,uuid,text)', 'dx_reevaluate_deliveries(uuid,uuid)',
        'dx_create_delivery_override(uuid,uuid,text,timestamptz,uuid,text)', 'dx_log_print(uuid,uuid,uuid,text)',
        'dx_create_share_link(uuid,uuid,text,integer,uuid)', 'dx_share_take_attempt(text)', 'dx_outbox_claim(integer)',
        'dx_adopt_orders(uuid,uuid[],uuid,text)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M131', 'Functions: decision, release, claim, mark, re-evaluate, override, print, share link, outbox, adoption',
            CASE WHEN v_funcs = 11 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 11 present');
END $$;

-- ============================================================================
-- 26. MIGRATION 132 — Diagnostic OS B2B money (DX-M8)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_idx int;
    v_trg int;
    v_funcs int;
    v_cols int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_rate_cards', 'dx_rate_card_items', 'dx_b2b_ledger', 'dx_b2b_statements', 'dx_barcode_stock')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M132', 'Tables: dx rate cards, rate card items, B2B ledger, statements, barcode stock (RLS forced)',
            CASE WHEN v_tables = 5 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 5 present with RLS forced');

    SELECT count(*) INTO v_idx FROM pg_indexes
     WHERE indexname IN ('uq_dx_ledger_charge', 'uq_dx_ledger_idem', 'uq_dx_b2b_statement_period');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M132', 'Indexes: one charge per item, ledger idempotency, one live statement per period',
            CASE WHEN v_idx = 3 THEN 'PASS' ELSE 'FAIL' END, v_idx || ' of 3 present');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_dx_b2b_ledger_guard', 'trg_dx_b2b_statements_guard', 'trg_dx_barcode_stock_guard');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M132', 'Triggers: append-only ledger, immutable statements, non-overlapping barcode stock',
            CASE WHEN v_trg = 3 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 3 present');

    SELECT count(*) INTO v_funcs FROM unnest(ARRAY['dx_b2b_balance(uuid,uuid)', 'dx_b2b_ageing(uuid)',
        'dx_b2b_post_charge(uuid,uuid,uuid,uuid,bigint,uuid,text)', 'dx_b2b_reverse_charge(uuid,uuid,text,uuid,text)',
        'dx_b2b_record_payment(uuid,uuid,bigint,text,text,text,uuid,text)',
        'dx_b2b_issue_statement(uuid,uuid,date,date,uuid,text)', 'dx_b2b_void_statement(uuid,uuid,text,uuid,text)',
        'dx_b2b_set_hold(uuid,uuid,text,text,text,uuid,text)', 'dx_b2b_evaluate_holds(uuid)',
        'dx_check_barcode_stock(uuid,uuid,uuid,text)']) f
     WHERE to_regprocedure(f) IS NOT NULL;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M132', 'Functions: balance, ageing, charge, reversal, payment, statement issue / void, holds, stock check',
            CASE WHEN v_funcs = 10 THEN 'PASS' ELSE 'FAIL' END, v_funcs || ' of 10 present');

    SELECT count(*) INTO v_cols FROM information_schema.columns
     WHERE (table_name = 'dx_b2b_accounts' AND column_name IN ('rate_card_id', 'credit_limit_paise', 'credit_days',
                                                              'gstin', 'billing_address', 'statement_cycle', 'auto_hold',
                                                              'hold_kind', 'copay_mode', 'copay_value'))
        OR (table_name = 'clinic_admins' AND column_name = 'dx_b2b_account_id');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M132', 'Columns: B2B account money terms (10) + clinic_admins.dx_b2b_account_id',
            CASE WHEN v_cols = 11 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 11 present');
END $$;

-- ============================================================================
-- 27. MIGRATION 133 — Diagnostic OS instruments and QC (DX-M9)
-- ============================================================================
DO $$
DECLARE
    v_tables int;
    v_trg int;
    v_patch int;
BEGIN
    SELECT count(*) INTO v_tables FROM pg_class
     WHERE relname IN ('dx_bridge_agents', 'dx_instruments', 'dx_instrument_results', 'dx_qc_materials', 'dx_qc_results')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M133', 'Tables: bridge agents, instruments, instrument results, QC materials, QC results (RLS forced)',
            CASE WHEN v_tables = 5 THEN 'PASS' ELSE 'FAIL' END, v_tables || ' of 5 present with RLS forced');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_dx_instrument_results_guard', 'trg_dx_qc_results_append_only');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M133', 'Triggers: raw instrument results immutable, QC run log append-only',
            CASE WHEN v_trg = 2 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 2 present');

    SELECT (position('v9 (133)' IN pg_get_functiondef('dx_purge_clinic(uuid)'::regprocedure)) > 0)::int
         + (position('instrument_result_id UUID' IN pg_get_functiondef(
              'dx_record_results(uuid,uuid,jsonb,jsonb,boolean,text,uuid,text)'::regprocedure)) > 0)::int INTO v_patch;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M133', 'Functions: dx_purge_clinic v9, dx_record_results keeps the instrument result id',
            CASE WHEN v_patch = 2 THEN 'PASS' ELSE 'FAIL' END, v_patch || ' of 2 present');
END $$;

-- ============================================================================
-- 28. MIGRATION 134 — Pharmacy OS foundation (RX-M1)
-- ============================================================================
DO $$
DECLARE
    v_cols int;
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_cols FROM information_schema.columns
     WHERE table_name = 'clinics' AND column_name IN ('rx_state', 'rx_settings');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M134', 'Columns: clinics.rx_state, clinics.rx_settings',
            CASE WHEN v_cols = 2 THEN 'PASS' ELSE 'FAIL' END, v_cols || ' of 2 present');

    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname = 'rx_stores' AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_stores_clinic_id_id_key', 'rx_stores_code_key', 'rx_stores_branch_fk', 'rx_stores_parent_fk',
                       'rx_stores_ward_shape', 'rx_stores_sell_shape', 'rx_stores_no_self_parent');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M134', 'Table: rx_stores (RLS forced, composite branch/parent FKs, 7 constraints)',
            CASE WHEN v_tbl = 1 AND v_con = 7 THEN 'PASS' ELSE 'FAIL' END,
            v_tbl || ' table, ' || v_con || ' of 7 constraints');

    SELECT (to_regprocedure('rx_purging(uuid)') IS NOT NULL)::int + (to_regprocedure('rx_raise(text)') IS NOT NULL)::int
         + (to_regprocedure('rx_purge_clinic(uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_update_settings(uuid,jsonb,integer)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_rx_stores_guard')::int INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M134', 'Functions: rx_purging, rx_raise, rx_purge_clinic, rx_update_settings + store guard trigger',
            CASE WHEN v_fn = 5 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 5 present');
END $$;

-- ============================================================================
-- 29. MIGRATION 135 — Pharmacy OS catalogue (RX-M2)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_manufacturers', 'rx_generics', 'rx_items', 'rx_item_barcodes', 'rx_item_aliases',
                       'rx_item_changes') AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M135', 'Tables: manufacturers, generics, items, barcodes, aliases, item changes (RLS forced)',
            CASE WHEN v_tbl = 6 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 6 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_items_clinic_id_id_key', 'rx_items_code_key', 'rx_items_generic_fk', 'rx_items_mfr_fk',
                       'rx_items_drug_has_generic', 'rx_items_controlled_no_loose', 'rx_generics_class_key',
                       'rx_item_barcodes_barcode_key', 'rx_item_barcodes_item_fk', 'rx_item_aliases_item_fk',
                       'rx_item_changes_item_fk');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M135', 'Constraints: composite FKs, item code / barcode unique per clinic, drug needs generic',
            CASE WHEN v_con = 11 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 11 present');

    SELECT (to_regprocedure('rx_normalize(text)') IS NOT NULL)::int
         + (to_regprocedure('rx_update_item(uuid,uuid,jsonb,uuid,text,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_import_items(uuid,jsonb,boolean,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_resolve_items(uuid,text,integer)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_rx_items_guard', 'trg_rx_item_changes_append_only'))::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M135', 'Functions: rx_normalize, rx_update_item, rx_import_items, rx_resolve_items + item guards',
            CASE WHEN v_fn = 6 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 6 present');
END $$;

-- ============================================================================
-- 30. MIGRATION 136 — Pharmacy OS batches and stock ledger (RX-M3)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_trg int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_batches', 'rx_stock_movements', 'rx_stock_balances') AND relkind = 'r'
       AND relrowsecurity AND relforcerowsecurity;
    SELECT v_tbl + count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_v_batch_stock', 'rx_v_fefo', 'rx_v_item_stock') AND relkind = 'v';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M136', 'Tables: batches, movements, balances (RLS forced) + views batch_stock, fefo, item_stock',
            CASE WHEN v_tbl = 6 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 6 present');

    SELECT count(*) INTO v_trg FROM pg_trigger
     WHERE tgname IN ('trg_rx_batches_guard', 'trg_rx_sm_guard_insert', 'trg_rx_block_expired_dispense',
                      'trg_rx_prevent_negative_stock', 'trg_rx_sm_append_only', 'trg_rx_balances_guard');
    SELECT v_trg + count(*) INTO v_trg FROM pg_constraint WHERE conname IN ('rx_bal_non_negative', 'rx_sm_sign');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M136', 'Ledger walls: function-only, append-only, no negative stock, expired / blocked never dispensed',
            CASE WHEN v_trg = 8 THEN 'PASS' ELSE 'FAIL' END, v_trg || ' of 8 present');

    SELECT (to_regprocedure('rx_in_tx(uuid)') IS NOT NULL)::int + (to_regprocedure('rx_ist_date(timestamptz)') IS NOT NULL)::int
         + (to_regprocedure('rx_post_writeoff(uuid,uuid,uuid,jsonb,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_opening_stock(uuid,uuid,jsonb,boolean,uuid,text)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_stock_movements%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M136', 'Functions: rx_in_tx, rx_ist_date, rx_post_writeoff, rx_opening_stock, rx_purge_clinic v3',
            CASE WHEN v_fn = 5 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 5 present');
END $$;

-- ============================================================================
-- 31. MIGRATION 137 — Pharmacy OS purchase (RX-M4)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_suppliers', 'rx_purchase_orders', 'rx_po_lines', 'rx_grns', 'rx_grn_lines',
                       'rx_supplier_returns', 'rx_supplier_return_lines') AND relkind = 'r'
       AND relrowsecurity AND relforcerowsecurity;
    SELECT v_tbl + count(*) INTO v_tbl FROM pg_class WHERE relname = 'rx_v_item_last_supplier' AND relkind = 'v';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M137', 'Tables: suppliers, POs, PO lines, GRNs, GRN lines, supplier returns + lines (RLS forced) + last-supplier view',
            CASE WHEN v_tbl = 8 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 8 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_grns_invoice_key', 'rx_grns_number_key', 'rx_po_number_key', 'rx_srn_number_key',
                       'rx_grn_lines_grn_fk', 'rx_grn_lines_item_fk', 'rx_grn_lines_batch_fk', 'rx_po_lines_po_fk',
                       'rx_srn_lines_batch_fk', 'rx_grns_supplier_fk', 'rx_suppliers_state_matches_gstin');
    SELECT v_con + count(*) INTO v_con FROM pg_indexes WHERE indexname = 'uq_rx_suppliers_gstin';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M137', 'Constraints: one entry per supplier invoice, gapless numbers, composite FKs, GSTIN unique',
            CASE WHEN v_con = 12 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 12 present');

    SELECT (to_regprocedure('rx_post_grn(uuid,uuid,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_void_grn(uuid,uuid,text,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_send_po(uuid,uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_supplier_return(uuid,uuid,uuid,text)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_rx_grn_line_calc', 'trg_rx_grns_guard',
              'trg_rx_grn_lines_guard', 'trg_rx_srn_line_fill'))::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_supplier_return_lines%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M137', 'Functions: rx_post_grn, rx_void_grn, rx_send_po, rx_supplier_return, line calc + guards, purge v4',
            CASE WHEN v_fn = 9 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 9 present');
END $$;

-- ============================================================================
-- 32. MIGRATION 138 — Pharmacy OS counter sales (RX-M5)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_sales', 'rx_sale_items', 'rx_sale_payments', 'rx_rx_fulfilments', 'rx_returns',
                       'rx_return_lines', 'rx_credit_notes', 'rx_overrides') AND relkind = 'r'
       AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M138', 'Tables: sales, items, payments, fulfilments, returns, return lines, credit notes, overrides (RLS forced)',
            CASE WHEN v_tbl = 8 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 8 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_sales_paid_in_full', 'rx_sales_giveback_le_net', 'rx_sales_discount_le_gross',
                       'rx_sales_rx_fk', 'rx_sale_items_le_mrp', 'rx_sale_items_returned_le_qty',
                       'rx_sale_payments_cash_needs_shift', 'rx_sale_payments_shift_fk', 'rx_overrides_two_people',
                       'rx_overrides_short_lived', 'rx_rx_fulfilments_sale_item_key');
    SELECT v_con + count(*) INTO v_con FROM pg_indexes WHERE indexname IN ('uq_rx_sales_idem', 'uq_rx_sales_one_live_per_rx');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M138', 'Constraints: never above MRP, paid in full, refunds <= paid, cash needs a shift, two-person overrides',
            CASE WHEN v_con = 13 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 13 present');

    SELECT (to_regprocedure('rx_complete_sale(uuid,uuid,jsonb,uuid,text,uuid,text,uuid,boolean,boolean)') IS NOT NULL)::int
         + (to_regprocedure('rx_record_return(uuid,uuid,jsonb,text,text,uuid,text,uuid,text,boolean,uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_void_sale(uuid,uuid,text,uuid,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_gst_split_inclusive(bigint,integer,boolean)') IS NOT NULL)::int
         + (to_regprocedure('rx_unit_price(integer,integer,integer)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'opd_close_shift' AND prosrc LIKE '%rx_sale_payments%')::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_overrides%')::int
         + (SELECT count(*) FROM pg_trigger WHERE tgname IN ('trg_rx_sale_line_calc', 'trg_rx_sales_guard'))::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M138', 'Functions: complete / return / void, GST split, unit price, opd_close_shift + pharmacy cash (D1), purge v5',
            CASE WHEN v_fn = 9 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 9 present');
END $$;

-- ============================================================================
-- 33. MIGRATION 139 — Pharmacy OS inpatient pharmacy (RX-M6)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_ipd_indents', 'rx_indent_lines', 'rx_dispenses', 'rx_dispense_lines', 'rx_mar_links',
                       'rx_empty_returns') AND relkind = 'r'
       AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M139', 'Tables: indents, indent lines, dispenses, dispense lines, MAR links, narcotic empties (RLS forced)',
            CASE WHEN v_tbl = 6 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 6 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_indent_lines_dispensed_le_requested', 'rx_indent_lines_returned_le_dispensed',
                       'rx_indent_lines_order_fk', 'rx_ipd_indents_admission_fk', 'rx_dispense_lines_returned_le_qty',
                       'rx_mar_links_mar_fk', 'rx_empty_returns_two_people', 'rx_indent_lines_indent_fk')
       AND convalidated;
    SELECT v_con + count(*) INTO v_con FROM pg_constraint
     WHERE conname = 'ipd_charges_source_check' AND convalidated
       AND pg_get_constraintdef(oid) LIKE '%pharmacy%' AND pg_get_constraintdef(oid) LIKE '%package%';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M139', 'Constraints: dispense <= requested, return <= dispensed, order / stay FKs, MAR cascade, ipd_charges.source widened',
            CASE WHEN v_con = 9 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 9 present');

    SELECT (to_regprocedure('rx_raise_indent(uuid,uuid,uuid,text,jsonb,text,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_dispense_indent(uuid,uuid,uuid,jsonb,uuid,text,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_cancel_indent(uuid,uuid,text,uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_receive_dispense(uuid,uuid,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_link_mar(uuid,uuid,uuid,uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_ipd_return(uuid,uuid,jsonb,text,uuid,uuid,text,boolean)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_mar_links%')::int
         + (SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
             WHERE t.tgrelid IN ('ipd_charges'::regclass, 'ipd_orders'::regclass, 'ipd_med_administrations'::regclass)
               AND p.proname LIKE 'rx\_%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M139', 'Functions: raise / dispense / cancel / receive / MAR link / ward return, purge v6, no rx trigger on IPD tables (D2/D9)',
            CASE WHEN v_fn = 8 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 8 present');
END $$;

-- ============================================================================
-- 34. MIGRATION 140 — Pharmacy OS transfers, stock audits, par levels (RX-M7)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_transfers', 'rx_transfer_lines', 'rx_stock_audits', 'rx_stock_audit_lines', 'rx_par_levels')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M140', 'Tables: transfers, transfer lines, stock audits, audit lines, par levels (RLS forced)',
            CASE WHEN v_tbl = 5 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 5 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_transfers_distinct_stores', 'rx_transfers_controlled_two_person', 'rx_transfers_number_key',
                       'rx_transfer_lines_received_range', 'rx_transfer_lines_short_reason', 'rx_stock_audits_number_key',
                       'rx_stock_audits_second_person', 'rx_stock_audit_lines_batch_key', 'rx_par_levels_pkey')
       AND convalidated;
    SELECT v_con + count(*) INTO v_con FROM pg_indexes WHERE indexname = 'uq_rx_stock_audits_open';
    SELECT v_con + count(*) INTO v_con FROM information_schema.columns
     WHERE table_name = 'rx_stock_audit_lines' AND column_name = 'variance_units' AND is_generated = 'ALWAYS';
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M140', 'Constraints: distinct stores, two-person controlled receipt, short needs reason, one open audit per store, generated variance',
            CASE WHEN v_con = 11 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 11 present');

    SELECT (to_regprocedure('rx_save_transfer(uuid,uuid,uuid,uuid,jsonb,text,text,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_dispatch_transfer(uuid,uuid,uuid,text,boolean)') IS NOT NULL)::int
         + (to_regprocedure('rx_receive_transfer(uuid,uuid,jsonb,uuid,text,boolean)') IS NOT NULL)::int
         + (to_regprocedure('rx_start_audit(uuid,uuid,text,text,uuid[],boolean,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_record_counts(uuid,uuid,jsonb)') IS NOT NULL)::int
         + (to_regprocedure('rx_post_audit(uuid,uuid,jsonb,uuid,text,uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_set_par_levels(uuid,uuid,jsonb,uuid)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_trigger WHERE tgrelid = 'rx_stock_movements'::regclass
              AND tgname IN ('trg_rx_sm_audit_freeze', 'trg_rx_sm_audit_delta'))::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_par_levels%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M140', 'Functions: transfer save / dispatch / receive, audit start / counts / post, par levels, ledger audit triggers, purge v7',
            CASE WHEN v_fn = 10 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 10 present');
END $$;

-- ============================================================================
-- 35. MIGRATION 141 — Pharmacy OS statutory registers, recall trace, narcotic empties (RX-M8)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname = 'rx_register_entries' AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M141', 'Table: rx_register_entries (RLS forced)',
            CASE WHEN v_tbl = 1 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 1 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_register_entries_clinic_id_id_key', 'rx_register_entries_seq_key',
                       'rx_register_entries_out_prescriber', 'rx_register_entries_out_patient',
                       'rx_register_entries_chain_head', 'rx_register_entries_batch_fk')
       AND convalidated;
    SELECT v_con + count(*) INTO v_con FROM pg_trigger WHERE tgrelid = 'rx_register_entries'::regclass
       AND tgname IN ('trg_rx_register_entries_insert', 'trg_rx_register_entries_append_only');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M141', 'Constraints: gapless seq key, prescriber on issues, patient on H1/X/narcotic issues, chain head, function-only + append-only',
            CASE WHEN v_con = 8 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 8 present');

    SELECT (to_regprocedure('rx_register_verify(uuid,text)') IS NOT NULL)::int
         + (to_regprocedure('rx_register_hash(rx_register_entries)') IS NOT NULL)::int
         + (to_regprocedure('rx_record_empties(uuid,uuid,integer,uuid,uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_batch_trace(uuid,uuid)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_register_append')::int
         + (SELECT count(*) FROM pg_trigger WHERE tgrelid = 'rx_stock_movements'::regclass
              AND tgname = 'trg_rx_sm_register' AND tgdeferrable AND tginitdeferred)::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_register_entries%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M141', 'Functions: verify, hash, append, empties, batch trace, deferred ledger→register trigger, purge v8',
            CASE WHEN v_fn = 7 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 7 present');
END $$;

-- ============================================================================
-- 36. MIGRATION 142 — Pharmacy OS refills, alerts, integrity checks, item metrics (RX-M9)
-- ============================================================================
DO $$
DECLARE
    v_tbl int;
    v_con int;
    v_fn int;
BEGIN
    SELECT count(*) INTO v_tbl FROM pg_class
     WHERE relname IN ('rx_refill_plans', 'rx_refill_requests', 'rx_alerts', 'rx_item_metrics')
       AND relkind = 'r' AND relrowsecurity AND relforcerowsecurity;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M142', 'Tables: refill plans, refill requests, alerts, item metrics (RLS forced)',
            CASE WHEN v_tbl = 4 THEN 'PASS' ELSE 'FAIL' END, v_tbl || ' of 4 present');

    SELECT count(*) INTO v_con FROM pg_constraint
     WHERE conname IN ('rx_refill_plans_sale_item_key', 'rx_refill_plans_dates', 'rx_refill_requests_reject_reason',
                       'rx_refill_requests_packed_sale', 'rx_refill_requests_plan_fk', 'rx_alerts_ack_shape',
                       'rx_item_metrics_pkey')
       AND convalidated;
    SELECT v_con + count(*) INTO v_con FROM pg_indexes WHERE indexname IN ('uq_rx_alerts_dedupe', 'uq_rx_refill_requests_open');
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M142', 'Constraints: one plan per sold line, one open request per plan, reject reason, alert dedupe per day',
            CASE WHEN v_con = 9 THEN 'PASS' ELSE 'FAIL' END, v_con || ' of 9 present');

    SELECT (to_regprocedure('rx_ledger_check(uuid)') IS NOT NULL)::int
         + (to_regprocedure('rx_alerts_scan(uuid,date,integer)') IS NOT NULL)::int
         + (to_regprocedure('rx_expiry_return_plan(uuid,uuid,date)') IS NOT NULL)::int
         + (SELECT count(*) FROM pg_trigger WHERE tgrelid = 'patients'::regclass AND tgname = 'trg_rx_patient_erased')::int
         + (SELECT count(*) FROM pg_proc WHERE proname = 'rx_purge_clinic' AND prosrc LIKE '%rx_refill_plans%')::int
      INTO v_fn;
    INSERT INTO _verification_results (category, check_name, status, detail)
    VALUES ('M142', 'Functions: ledger check, alert scan, expiry-return plan, DPDP erasure trigger, purge v9',
            CASE WHEN v_fn = 5 THEN 'PASS' ELSE 'FAIL' END, v_fn || ' of 5 present');
END $$;

-- ============================================================================
-- FINAL UNIFIED REPORT (Single SELECT for Supabase SQL Editor)
-- Shows FAIL rows first, then the SUMMARY, then PASS rows
-- ============================================================================
SELECT
    category,
    check_name,
    status,
    detail
FROM (
    -- 1. Any FAIL rows (Priority 1)
    SELECT
        1 AS sort_order,
        category,
        check_name,
        '❌ ' || status AS status,
        detail,
        check_id
    FROM _verification_results
    WHERE status = 'FAIL'

    UNION ALL

    -- 2. Summary Verdict Row (Priority 2)
    SELECT
        2 AS sort_order,
        '=== SUMMARY ===' AS category,
        'TOTAL: ' || count(*) || ' checks (' || 
        count(*) FILTER (WHERE status = 'PASS') || ' passed, ' || 
        count(*) FILTER (WHERE status = 'FAIL') || ' failed)' AS check_name,
        CASE
            WHEN count(*) FILTER (WHERE status = 'FAIL') = 0 THEN '✅ ALL PASS'
            ELSE '❌ ' || count(*) FILTER (WHERE status = 'FAIL') || ' FAILED'
        END AS status,
        CASE
            WHEN count(*) FILTER (WHERE status = 'FAIL') = 0 THEN 'Schema is 100% verified.'
            ELSE 'See failed check(s) listed above.'
        END AS detail,
        999999 AS check_id
    FROM _verification_results

    UNION ALL

    -- 3. PASS rows (Priority 3)
    SELECT
        3 AS sort_order,
        category,
        check_name,
        '✅ ' || status AS status,
        detail,
        check_id
    FROM _verification_results
    WHERE status = 'PASS'
) report
ORDER BY sort_order, check_id;

-- Cleanup
DROP TABLE IF EXISTS _verification_results;

