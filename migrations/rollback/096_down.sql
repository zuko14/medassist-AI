-- Rollback Migration 096. Partner rows would otherwise become ordinary tenants
-- (billed, listed), so they are deleted first — which cascades to their
-- companies, uploaded reports and company logins (FK ON DELETE CASCADE).
DELETE FROM clinics WHERE account_type = 'corporate_partner';
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_partner_not_routable;
ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_account_type_check;
ALTER TABLE clinics DROP COLUMN IF EXISTS account_type;
