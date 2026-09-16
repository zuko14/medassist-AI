-- Rollback 077: remove specialty plans and the treatments catalogue.
--
-- ORDER: roll the APPLICATION back first. Code from this release selects
-- appointments.treatment_name; running it against a schema without the
-- column breaks the Payments and Insights pages.
--
-- DATA LOSS WARNING: dropping appointments.treatment_id / treatment_name
-- permanently removes which treatment each booking was for. Export first:
--   COPY (SELECT id, clinic_id, treatment_id, treatment_name FROM appointments
--         WHERE treatment_id IS NOT NULL OR treatment_name IS NOT NULL)
--   TO STDOUT WITH CSV HEADER;
-- The bookings themselves (slots, payments, refunds) are unaffected.

DO $$
DECLARE n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM clinics WHERE plan IN ('derma', 'eye', 'dental', 'ivf');
    IF n > 0 THEN
        RAISE EXCEPTION '% clinic(s) are on a specialty plan — move them to another plan before rolling back 077', n;
    END IF;
END $$;

ALTER TABLE appointments DROP COLUMN IF EXISTS treatment_name;
ALTER TABLE appointments DROP COLUMN IF EXISTS treatment_id;

DROP TABLE IF EXISTS treatment_doctors;
DROP TABLE IF EXISTS specialty_treatments;

DELETE FROM plan_tiers WHERE plan_name IN ('derma', 'eye', 'dental', 'ivf');

ALTER TABLE clinics DROP CONSTRAINT IF EXISTS clinics_plan_check;
ALTER TABLE clinics ADD CONSTRAINT clinics_plan_check
    CHECK (plan IN ('soloclinic', 'diagstream', 'diagbooking',
                    'essential', 'polyclinic', 'enterprise'));

ALTER TABLE plan_tiers DROP CONSTRAINT IF EXISTS plan_tiers_plan_name_check;
ALTER TABLE plan_tiers ADD CONSTRAINT plan_tiers_plan_name_check
    CHECK (plan_name IN ('soloclinic', 'diagstream', 'diagbooking',
                         'essential', 'polyclinic', 'enterprise'));
