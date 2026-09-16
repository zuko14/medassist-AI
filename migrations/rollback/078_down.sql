-- Rollback 078: remove the multispecialty plan slug.
--
-- ORDER: roll the APPLICATION back first, as with 077.
--
-- NO DATA LOSS. 078 created no table and no column: it only widened two CHECK
-- constraints and inserted one plan_tiers row. Any treatments a multispecialty
-- hospital created live in specialty_treatments (migration 077) and are left
-- untouched here — they come back intact if the plan is re-added.

DO $$
DECLARE n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM clinics WHERE plan = 'multispecialty';
    IF n > 0 THEN
        RAISE EXCEPTION '% clinic(s) are on the multispecialty plan — move them to another plan before rolling back 078', n;
    END IF;
END $$;

DELETE FROM plan_tiers WHERE plan_name = 'multispecialty';

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_plan_check;
ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
    CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                    'essential', 'polyclinic', 'enterprise',
                    'derma', 'eye', 'dental', 'ivf'));

ALTER TABLE plan_tiers DROP CONSTRAINT IF EXISTS plan_tiers_plan_name_check;
ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
    CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                         'essential', 'polyclinic', 'enterprise',
                         'derma', 'eye', 'dental', 'ivf'));
