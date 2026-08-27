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
        'inbound_messages', 'scheduler_locks'
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

