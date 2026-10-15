-- Rollback Migration 109. Restores PRIMARY KEY (holiday_date) only while no two clinics
-- share a holiday date; otherwise the per-clinic key is kept (restoring the old key would
-- fail, and it is the defect 109 fixes, D-27) and a NOTICE says so. No rows are deleted.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM hospital_holidays GROUP BY holiday_date HAVING count(*) > 1) THEN
        RAISE NOTICE '109_down: clinics share holiday dates; keeping PRIMARY KEY (clinic_id, holiday_date)';
        RETURN;
    END IF;
    ALTER TABLE hospital_holidays DROP CONSTRAINT IF EXISTS hospital_holidays_pkey;
    ALTER TABLE hospital_holidays ADD CONSTRAINT hospital_holidays_pkey PRIMARY KEY (holiday_date);
END $$;
