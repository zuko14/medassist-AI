-- Rollback 082: remove the womenchild plan slug and the treatment service line.
--
-- ORDER: roll the APPLICATION back first. The new build writes service_line
-- when it seeds starter treatments; the previous build never touches it.
--
-- DATA LOSS: only the service-line tag on each treatment (which section --
-- Child Care, Women Care, Fertility Care -- it was filed under). Treatments,
-- their doctors and every booking are untouched. Export first if a clinic has
-- filed its catalogue by hand:
--
--   \copy (SELECT id, name, service_line FROM specialty_treatments WHERE service_line IS NOT NULL) TO 'service_lines.csv' CSV HEADER

DO $$
DECLARE n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM clinics WHERE plan = 'womenchild';
    IF n > 0 THEN
        RAISE EXCEPTION '% clinic(s) are on the womenchild plan — move them to another plan before rolling back 082', n;
    END IF;
END $$;

DELETE FROM plan_tiers WHERE plan_name = 'womenchild';

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_plan_check;
ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
    CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                    'essential', 'polyclinic', 'enterprise',
                    'derma', 'eye', 'dental', 'ivf', 'multispecialty'));

ALTER TABLE plan_tiers DROP CONSTRAINT IF EXISTS plan_tiers_plan_name_check;
ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
    CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                         'essential', 'polyclinic', 'enterprise',
                         'derma', 'eye', 'dental', 'ivf', 'multispecialty'));

ALTER TABLE specialty_treatments
    DROP CONSTRAINT IF EXISTS specialty_treatments_service_line_check;

ALTER TABLE specialty_treatments DROP COLUMN IF EXISTS service_line;
