-- Rollback 081: drop the treatments catalogue's care pathway column.
--
-- ORDER: roll the APPLICATION back first. The admin panel writes this column
-- and the WhatsApp treatment cards read it; with the previous build deployed
-- nothing touches it and this script is safe to run at any time.
--
-- DATA LOSS: which treatments a clinic marked "doctor decides after
-- examination" lives only here. The treatments, their doctors and every
-- booking are untouched -- only the classification goes. Export first if a
-- clinic has classified its catalogue by hand:
--
--   \copy (SELECT id, name, care_pathway FROM specialty_treatments) TO 'care_pathways.csv' CSV HEADER

ALTER TABLE specialty_treatments
    DROP CONSTRAINT IF EXISTS specialty_treatments_care_pathway_check;

ALTER TABLE specialty_treatments DROP COLUMN IF EXISTS care_pathway;
