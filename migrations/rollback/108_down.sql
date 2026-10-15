-- Rollback Migration 108. Kriya Ketu run history is lost; domain data and the
-- endpoints' own admin_audit_logs rows are untouched. Export first if an audit request is open.
DROP TABLE IF EXISTS assistant_runs;
