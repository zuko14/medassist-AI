-- Rollback 083: drop the diagnostic catalogue's details column.
--
-- ORDER: roll the APPLICATION back first; the new build writes this column
-- from the panel and the CSV importer.
--
-- DATA LOSS: the "details / tests included" text a centre entered per test.
-- Export first if anyone has filled it in:
--
--   \copy (SELECT id, name, description FROM lab_tests WHERE description IS NOT NULL) TO 'lab_test_details.csv' CSV HEADER

ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_description_len_check;
ALTER TABLE lab_tests DROP COLUMN IF EXISTS description;
