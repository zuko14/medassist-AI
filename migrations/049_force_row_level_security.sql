-- Migration 049: Database-Level Tenant Backstop & Force Row Level Security (W2)
-- Enforces hard tenant isolation directly at the PostgreSQL storage engine level.
-- Even if an application query omits 'clinic_id', PostgreSQL rejects cross-tenant data access.

-- 1. Create dedicated application role without BYPASSRLS
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kriya_app') THEN
        CREATE ROLE kriya_app LOGIN NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO kriya_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO kriya_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO kriya_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO kriya_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO kriya_app;

-- 2. Force Row Level Security and establish tenant isolation policies on all tenant tables

-- appointments
ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_appointments" ON appointments;
CREATE POLICY "service_role_all_appointments" ON appointments FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_appointments" ON appointments;
CREATE POLICY "tenant_isolation_appointments" ON appointments
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- patients
ALTER TABLE patients ENABLE ROW LEVEL SECURITY;
ALTER TABLE patients FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_patients" ON patients;
CREATE POLICY "service_role_all_patients" ON patients FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_patients" ON patients;
CREATE POLICY "tenant_isolation_patients" ON patients
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- lab_reports
ALTER TABLE lab_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_lab_reports" ON lab_reports;
CREATE POLICY "service_role_all_lab_reports" ON lab_reports FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_lab_reports" ON lab_reports;
CREATE POLICY "tenant_isolation_lab_reports" ON lab_reports
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- lab_tests
ALTER TABLE lab_tests ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_tests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_lab_tests" ON lab_tests;
CREATE POLICY "service_role_all_lab_tests" ON lab_tests FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_lab_tests" ON lab_tests;
CREATE POLICY "tenant_isolation_lab_tests" ON lab_tests
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- doctors
ALTER TABLE doctors ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_doctors" ON doctors;
CREATE POLICY "service_role_all_doctors" ON doctors FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_doctors" ON doctors;
CREATE POLICY "tenant_isolation_doctors" ON doctors
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- branches
ALTER TABLE branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE branches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_branches" ON branches;
CREATE POLICY "service_role_all_branches" ON branches FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_branches" ON branches;
CREATE POLICY "tenant_isolation_branches" ON branches
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- doctor_branches
ALTER TABLE doctor_branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_branches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_doctor_branches" ON doctor_branches;
CREATE POLICY "service_role_all_doctor_branches" ON doctor_branches FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_doctor_branches" ON doctor_branches;
CREATE POLICY "tenant_isolation_doctor_branches" ON doctor_branches
    FOR ALL TO kriya_app, authenticated, anon
    USING (
        EXISTS (
            SELECT 1 FROM branches b
            WHERE b.id = doctor_branches.branch_id
            AND b.clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM branches b
            WHERE b.id = doctor_branches.branch_id
            AND b.clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
        )
    );

-- doctor_leaves
ALTER TABLE doctor_leaves ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_leaves FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_doctor_leaves" ON doctor_leaves;
CREATE POLICY "service_role_all_doctor_leaves" ON doctor_leaves FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_doctor_leaves" ON doctor_leaves;
CREATE POLICY "tenant_isolation_doctor_leaves" ON doctor_leaves
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- hospital_holidays
ALTER TABLE hospital_holidays ENABLE ROW LEVEL SECURITY;
ALTER TABLE hospital_holidays FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_hospital_holidays" ON hospital_holidays;
CREATE POLICY "service_role_all_hospital_holidays" ON hospital_holidays FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_hospital_holidays" ON hospital_holidays;
CREATE POLICY "tenant_isolation_hospital_holidays" ON hospital_holidays
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- clinic_admins
ALTER TABLE clinic_admins ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_admins FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_clinic_admins" ON clinic_admins;
CREATE POLICY "service_role_all_clinic_admins" ON clinic_admins FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_clinic_admins" ON clinic_admins;
CREATE POLICY "tenant_isolation_clinic_admins" ON clinic_admins
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- integration_connectors
ALTER TABLE integration_connectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration_connectors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_integration_connectors" ON integration_connectors;
CREATE POLICY "service_role_all_integration_connectors" ON integration_connectors FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_integration_connectors" ON integration_connectors;
CREATE POLICY "tenant_isolation_integration_connectors" ON integration_connectors
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- connector_failed_reports
ALTER TABLE connector_failed_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_failed_reports FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_connector_failed_reports" ON connector_failed_reports;
CREATE POLICY "service_role_all_connector_failed_reports" ON connector_failed_reports FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_connector_failed_reports" ON connector_failed_reports;
CREATE POLICY "tenant_isolation_connector_failed_reports" ON connector_failed_reports
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- conversations
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_conversations" ON conversations;
CREATE POLICY "service_role_all_conversations" ON conversations FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_conversations" ON conversations;
CREATE POLICY "tenant_isolation_conversations" ON conversations
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- inbound_messages
ALTER TABLE inbound_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbound_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_inbound_messages" ON inbound_messages;
CREATE POLICY "service_role_all_inbound_messages" ON inbound_messages FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_inbound_messages" ON inbound_messages;
CREATE POLICY "tenant_isolation_inbound_messages" ON inbound_messages
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- processed_messages
ALTER TABLE processed_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_processed_messages" ON processed_messages;
CREATE POLICY "service_role_all_processed_messages" ON processed_messages FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_processed_messages" ON processed_messages;
CREATE POLICY "tenant_isolation_processed_messages" ON processed_messages
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- payment_events
ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_payment_events" ON payment_events;
CREATE POLICY "service_role_all_payment_events" ON payment_events FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_payment_events" ON payment_events;
CREATE POLICY "tenant_isolation_payment_events" ON payment_events
    FOR ALL TO kriya_app, authenticated, anon
    USING (
        EXISTS (
            SELECT 1 FROM appointments a
            WHERE a.id = payment_events.booking_id
            AND a.clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM appointments a
            WHERE a.id = payment_events.booking_id
            AND a.clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid
        )
    );

-- admin_audit_logs
ALTER TABLE admin_audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE admin_audit_logs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_admin_audit_logs" ON admin_audit_logs;
CREATE POLICY "service_role_all_admin_audit_logs" ON admin_audit_logs FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_admin_audit_logs" ON admin_audit_logs;
CREATE POLICY "tenant_isolation_admin_audit_logs" ON admin_audit_logs
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid)
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id = NULLIF(current_setting('app.clinic_id', true), '')::uuid);

-- family_members
ALTER TABLE family_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE family_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_family_members" ON family_members;
CREATE POLICY "service_role_all_family_members" ON family_members FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "tenant_isolation_family_members" ON family_members;
CREATE POLICY "tenant_isolation_family_members" ON family_members
    FOR ALL TO kriya_app, authenticated, anon
    USING (clinic_id IS NOT NULL AND clinic_id::text = NULLIF(current_setting('app.clinic_id', true), ''))
    WITH CHECK (clinic_id IS NOT NULL AND clinic_id::text = NULLIF(current_setting('app.clinic_id', true), ''));
