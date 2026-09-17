-- ============================================================================
-- Migration 080: service category on the diagnostic catalogue
-- ============================================================================
-- A diagnostic centre does not sell one flat list of blood tests. It sells
-- pathology, health packages, radiology/imaging and scans (MRI/CT), and a
-- patient asked to pick one row out of 1,392 is being handed the centre's
-- filing problem. One nullable TEXT column lets the admin panel file each row
-- under a heading, and the WhatsApp catalogue then offers those headings
-- first.
--
-- Deliberately NOT a categories table and NOT an enum CHECK:
--   * the live headings are whatever the centre's own rows carry, so a centre
--     that offers no radiology simply has no radiology rows and the bot shows
--     no radiology heading -- nothing to switch on or off, nothing to keep in
--     sync, no way for the panel and the bot to disagree;
--   * centres name their sections differently ("Imaging", "Radiology &
--     Scans", "Master Health Checkup") and a fixed vocabulary would already
--     be wrong for the second client.
--
-- No index: get_lab_tests() reads the whole catalogue for a clinic and groups
-- it in Python, so nothing ever filters on this column in SQL.
--
-- PURELY ADDITIVE. NULL means "not filed yet", which every existing row is,
-- and a catalogue with one heading behaves exactly as an unfiled one always
-- did. A live centre sees no change until it starts filling this column in.
-- Re-runnable.
-- ============================================================================

ALTER TABLE lab_tests ADD COLUMN IF NOT EXISTS category TEXT;

-- Guard rail matching the API's own cap, so a direct SQL insert cannot store a
-- heading the WhatsApp list and the admin table would both have to truncate.
-- DROP-then-ADD rather than a DO block: idempotent, and it survives the naive
-- statement splitter in run_migrations.py.
ALTER TABLE lab_tests DROP CONSTRAINT IF EXISTS lab_tests_category_len_check;

ALTER TABLE lab_tests ADD CONSTRAINT lab_tests_category_len_check
    CHECK (category IS NULL OR char_length(category) <= 60);

-- Verification
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'lab_tests' AND column_name = 'category';
