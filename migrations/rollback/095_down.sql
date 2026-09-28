-- Rollback Migration 095. Deletes every uploaded corporate report and company.
-- Company-viewer logins (staff_role CORPORATE_VIEWER) remain in clinic_admins;
-- delete them separately. The running app refuses them everything anyway.
ALTER TABLE clinic_admins DROP COLUMN IF EXISTS corporate_client_id;
DROP TABLE IF EXISTS corporate_health_reports;
DROP TABLE IF EXISTS corporate_clients;
