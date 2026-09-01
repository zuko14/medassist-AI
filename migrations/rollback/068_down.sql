-- Rollback for 068_subscription_lifecycle_and_daily_limits.sql
--
-- Removing the clinics columns discards every clinic's subscription window and
-- limit tier; there is no way to recover them from elsewhere. Re-applying 068
-- afterwards gives every clinic a fresh 30 days from that moment.
--
-- The application fails OPEN on missing columns (compute_subscription_state
-- treats a clinic with no subscription_end_date as active), so rolling this
-- back does NOT silence a live hospital.

DROP FUNCTION IF EXISTS increment_clinic_daily_usage(UUID, DATE, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER);

DROP INDEX IF EXISTS idx_oml_clinic_source_sent;
DROP INDEX IF EXISTS idx_cdu_clinic_date;
DROP TABLE IF EXISTS clinic_daily_usage;

ALTER TABLE clinics
DROP COLUMN IF EXISTS daily_report_limit,
DROP COLUMN IF EXISTS subscription_start_date,
DROP COLUMN IF EXISTS subscription_end_date,
DROP COLUMN IF EXISTS grace_period_days,
DROP COLUMN IF EXISTS subscription_status,
DROP COLUMN IF EXISTS last_renewed_at;
