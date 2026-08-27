-- Migration 051: Prevent unscoped non-super-admin accounts.
-- Root cause of KRIYA-002. clinic_admins.clinic_id has been nullable for every
-- role since migration 011, so an unscoped 'staff' row could read every tenant
-- via AdminUser.can_access_clinic().
--
-- Safe to run against live data with numInstances: 2. Adds no columns, modifies
-- no rows, and the constraint is NOT VALID so existing rows are untouched.

-- 1. Surface offenders BEFORE constraining. This should return zero in a healthy
--    production database. If it does not, remediate each row manually before
--    running the VALIDATE step in section 3.
DO $$
DECLARE
    offenders INT;
BEGIN
    SELECT count(*) INTO offenders
      FROM clinic_admins
     WHERE clinic_id IS NULL
       AND role IS DISTINCT FROM 'super_admin';

    IF offenders > 0 THEN
        RAISE WARNING
            'MIGRATION 051: % unscoped non-super-admin account(s) found. '
            'Constraint added as NOT VALID, so these rows keep working at the '
            'database layer, but they are DENIED at the application layer once '
            'settings.tenant_scope_enforce = True. Assign a clinic_id or promote '
            'to super_admin, then run: '
            'ALTER TABLE clinic_admins VALIDATE CONSTRAINT chk_admin_scope;',
            offenders;
    END IF;
END $$;

-- 2. NOT VALID: enforced for INSERT and UPDATE immediately; existing rows are
--    not scanned and not rejected. This is what makes it safe mid-rollout.
ALTER TABLE clinic_admins
    ADD CONSTRAINT chk_admin_scope
    CHECK (role = 'super_admin' OR clinic_id IS NOT NULL) NOT VALID;

-- 3. RUN MANUALLY, NOT IN THIS MIGRATION, after remediating any offenders:
--      ALTER TABLE clinic_admins VALIDATE CONSTRAINT chk_admin_scope;
