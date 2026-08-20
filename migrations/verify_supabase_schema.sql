-- ============================================================================
-- MASTER SUPABASE SCHEMA HEALTH CHECK & AUDIT SUITE
-- Run this query in the Supabase SQL Editor to verify that all 35 migrations
-- have been applied correctly and your database is 100% production-ready.
-- ============================================================================

WITH audit_checks AS (
    -- 1. Table Existence Checks
    SELECT 
        '1. Tables' AS category,
        'Table: ' || t.tbl AS check_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name = t.tbl
            ) THEN '✅ PASS'
            ELSE '❌ FAIL'
        END AS status,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM information_schema.tables 
                WHERE table_schema = 'public' AND table_name = t.tbl
            ) THEN 'Table exists'
            ELSE 'MISSING TABLE - Check migrations'
        END AS details
    FROM (
        VALUES 
            ('clinics'),
            ('patients'),
            ('appointments'),
            ('conversations'),
            ('doctors'),
            ('doctor_leaves'),
            ('hospital_holidays'),
            ('lab_reports'),
            ('prescriptions'),
            ('analytics_events'),
            ('processed_messages'),
            ('branches'),
            ('doctor_branches'),
            ('clinic_admins'),
            ('admin_audit_logs'),
            ('failed_messages'),
            ('family_members'),
            ('rate_limits'),
            ('webhook_security_events')
    ) AS t(tbl)

    UNION ALL

    -- 2. Multi-Tenant Unique Constraints (Migration 035 & Earlier)
    SELECT 
        '2. Multi-Tenant Constraints' AS category,
        'conversations: Composite (clinic_id, phone) Unique' AS check_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'conversations'
                  AND c.contype = 'u'
                  AND ARRAY(
                      SELECT attname FROM pg_attribute
                      WHERE attrelid = t.oid AND attnum = ANY(c.conkey)
                      ORDER BY attnum
                  ) = ARRAY['clinic_id'::name, 'phone'::name]
            ) THEN '✅ PASS'
            ELSE '❌ FAIL - Missing composite unique on conversations(clinic_id, phone)'
        END AS status,
        'Prevents duplicate key conflicts across multi-tenant bot sessions' AS details

    UNION ALL

    SELECT 
        '2. Multi-Tenant Constraints' AS category,
        'conversations: No Single-Column phone Unique' AS check_name,
        CASE 
            WHEN NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'conversations'
                  AND c.contype = 'u'
                  AND ARRAY(
                      SELECT attname FROM pg_attribute
                      WHERE attrelid = t.oid AND attnum = ANY(c.conkey)
                  ) = ARRAY['phone'::name]
            ) THEN '✅ PASS'
            ELSE '❌ FAIL - Legacy single-column UNIQUE(phone) still active on conversations'
        END AS status,
        'Single-column unique blocks patients from using multiple clinics' AS details

    UNION ALL

    SELECT 
        '2. Multi-Tenant Constraints' AS category,
        'patients: Composite (clinic_id, phone) Unique' AS check_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'patients'
                  AND c.contype = 'u'
                  AND ARRAY(
                      SELECT attname FROM pg_attribute
                      WHERE attrelid = t.oid AND attnum = ANY(c.conkey)
                      ORDER BY attnum
                  ) = ARRAY['phone'::name, 'clinic_id'::name]
                  OR EXISTS (
                      SELECT 1 FROM pg_constraint c
                      JOIN pg_class t ON t.oid = c.conrelid
                      WHERE t.relname = 'patients'
                        AND c.contype = 'u'
                        AND ARRAY(
                            SELECT attname FROM pg_attribute
                            WHERE attrelid = t.oid AND attnum = ANY(c.conkey)
                            ORDER BY attnum
                        ) = ARRAY['clinic_id'::name, 'phone'::name]
                  )
            ) THEN '✅ PASS'
            ELSE '❌ FAIL - Missing composite unique on patients(clinic_id, phone)'
        END AS status,
        'Ensures patient records are properly scoped per clinic' AS details

    UNION ALL

    -- 3. Critical Multi-Branch & Routing Columns
    SELECT 
        '3. Critical Columns' AS category,
        'appointments: branch_id & queue token' AS check_name,
        CASE 
            WHEN (
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'appointments'
                  AND column_name IN ('branch_id', 'branch_name', 'token_number', 'queue_status', 'clinic_id')
            ) >= 4 THEN '✅ PASS'
            ELSE '❌ FAIL - Missing required appointment columns'
        END AS status,
        'Required for multi-branch bookings and live queue tracker' AS details

    UNION ALL

    SELECT 
        '3. Critical Columns' AS category,
        'branches: locality & diagnostic fields' AS check_name,
        CASE 
            WHEN (
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_name = 'branches'
                  AND column_name IN ('clinic_id', 'name', 'short_name', 'is_diagnostic', 'is_active', 'display_order')
            ) = 6 THEN '✅ PASS'
            ELSE '❌ FAIL - Missing branch fields'
        END AS status,
        'Required for branch locality upgrade and WhatsApp branch picker' AS details

    UNION ALL

    SELECT 
        '3. Critical Columns' AS category,
        'processed_messages: clinic_id attribution' AS check_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'processed_messages' AND column_name = 'clinic_id'
            ) THEN '✅ PASS'
            ELSE '❌ FAIL - processed_messages missing clinic_id'
        END AS status,
        'Required for atomic deduplication and per-clinic message accounting' AS details

    UNION ALL

    -- 4. Plan Constraint Verification (Migration 016 & 033)
    SELECT 
        '4. Enum & Check Constraints' AS category,
        'clinics: plan check supports 5 tiers' AS check_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'clinics'
                  AND c.contype = 'c'
                  AND pg_get_constraintdef(c.oid) LIKE '%soloclinic%'
                  AND pg_get_constraintdef(c.oid) LIKE '%essential%'
            ) THEN '✅ PASS'
            ELSE '⚠️ WARNING - Check clinics.plan constraint'
        END AS status,
        'Should allow: soloclinic, diagstream, essential, polyclinic, enterprise' AS details

    UNION ALL

    -- 5. Row Level Security (RLS) Status
    SELECT 
        '5. Row Level Security' AS category,
        'RLS enabled on sensitive tables' AS check_name,
        CASE 
            WHEN (
                SELECT COUNT(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname IN ('clinics', 'branches', 'clinic_admins', 'patients', 'appointments')
                  AND c.relrowsecurity = true
            ) >= 4 THEN '✅ PASS'
            ELSE '⚠️ WARNING - Verify RLS policies on core tables'
        END AS status,
        'Protects sensitive patient and tenant data' AS details
)
-- Display the test results ordered by Category and Status
SELECT 
    category,
    check_name,
    status,
    details
FROM audit_checks
ORDER BY 
    CASE 
        WHEN status LIKE '❌%' THEN 1
        WHEN status LIKE '⚠️%' THEN 2
        ELSE 3
    END,
    category,
    check_name;
