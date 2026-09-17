-- Rollback 080: drop the diagnostic catalogue's service category column.
--
-- ORDER: roll the APPLICATION back first. The admin panel writes this column
-- and the WhatsApp catalogue reads it; with the previous build deployed
-- nothing touches it and this script is safe to run at any time.
--
-- DATA LOSS: the filing itself (which test is pathology, which is radiology,
-- which is a health package) lives only here and is not reconstructible. The
-- tests, prices and bookings are untouched -- only the headings go. Export
-- first if the filing took real work:
--
--   \copy (SELECT id, name, category FROM lab_tests WHERE category IS NOT NULL) TO 'lab_categories.csv' CSV HEADER

ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_category_len_check;

ALTER TABLE lab_tests DROP COLUMN IF EXISTS category;
