-- ============================================================================
-- Migration 076: One test name per branch in the diagnostic catalogue
-- ============================================================================
-- lab_tests has carried no uniqueness since migration 038, while the
-- application has always treated the name as the catalogue's identity:
--
--   * the CSV importer decides create-vs-update by name, so two rows sharing
--     a name mean it updates one at random and leaves the other stale;
--   * get_lab_tests() treats a branch row as an OVERRIDE of the all-branches
--     row of the same name, which is only coherent if the name is unique
--     within its scope;
--   * the patient sees the same name twice, at two prices, with nothing to
--     tell the rows apart.
--
-- Duplicates are not hypothetical. Until the paging fix that ships with this
-- change, the importer's duplicate check read existing rows through a single
-- un-ranged select, which PostgREST caps at 1000 rows. A catalogue larger
-- than that (one live centre carries ~1,392 tests) had its tail re-CREATED on
-- every re-import. Any clinic that re-imported a large CSV has duplicates now.
--
-- Scope key: (clinic_id, branch_id, name). branch_id is nullable and Postgres
-- treats every NULL as distinct, so it is COALESCEd to a fixed sentinel
-- exactly as migration 073 does for the lab queue token -- otherwise the index
-- would not constrain all-branches rows at all, which is most of them.
--
-- Name key: lower(btrim(name)), because that is how the application already
-- compares. The importer lowercases, and the override rule strips and
-- lowercases. A DB key on raw `name` would let "CBC" and " cbc " coexist and
-- then collide in Python, which is the worst of both.
--
-- NON-DESTRUCTIVE. Step 1 deletes nothing: history is repointed, and losing
-- rows are renamed and deactivated so an operator can still inspect or
-- restore them. Idempotent and re-runnable.
-- ============================================================================

-- -- Step 1: resolve duplicates already in the table ------------------------
DO $$
DECLARE
    repointed INT := 0;
    retired   INT := 0;
BEGIN
    -- Rank each duplicate group. The KEEPER is the most recently updated row:
    -- after a re-import that is the row carrying current pricing. created_at
    -- then id break ties so the choice is deterministic on a re-run.
    CREATE TEMP TABLE _dup_lab_tests ON COMMIT DROP AS
    SELECT
        id,
        FIRST_VALUE(id) OVER w AS keeper_id,
        ROW_NUMBER()    OVER w AS seq
    FROM lab_tests
    WINDOW w AS (
        PARTITION BY clinic_id,
                     COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
                     lower(btrim(name))
        ORDER BY updated_at DESC, created_at DESC, id
    );

    DELETE FROM _dup_lab_tests WHERE seq = 1;

    IF EXISTS (SELECT 1 FROM _dup_lab_tests) THEN
        -- Keep booking history attached to a real catalogue row. Without this
        -- the rename below would leave old appointments pointing at a row
        -- labelled "[duplicate]", and a later cleanup DELETE would null the
        -- reference outright (migration 039 is ON DELETE SET NULL).
        -- Money is unaffected either way: an appointment stores its own
        -- amount_paise and lab_test_name at booking time.
        UPDATE appointments a
        SET lab_test_id = d.keeper_id
        FROM _dup_lab_tests d
        WHERE a.lab_test_id = d.id;
        GET DIAGNOSTICS repointed = ROW_COUNT;

        -- Renamed rather than deleted: the row, its price and its prep notes
        -- survive for review. Deactivated so no patient is offered it --
        -- get_lab_tests(active_only=True) filters on is_active. The id suffix
        -- makes the new name unique, so this satisfies the index below
        -- without needing a second pass.
        UPDATE lab_tests t
        SET name = left(t.name, 170) || ' [duplicate ' || left(t.id::text, 8) || ']',
            is_active = false,
            updated_at = now()
        FROM _dup_lab_tests d
        WHERE t.id = d.id;
        GET DIAGNOSTICS retired = ROW_COUNT;

        RAISE NOTICE 'lab_tests: retired % duplicate row(s), repointed % appointment(s) onto the surviving test',
            retired, repointed;
        RAISE NOTICE 'Review them with:  SELECT id, clinic_id, branch_id, name, price_paise FROM lab_tests WHERE is_active = false AND name LIKE ''%% [duplicate %%]'';';
    END IF;
END $$;

-- -- Step 2: make the collision impossible from here on ---------------------
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_lab_test_name_per_branch
    ON lab_tests (
        clinic_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        lower(btrim(name))
    );

COMMENT ON INDEX idx_unique_lab_test_name_per_branch IS
    'One test name per branch. branch_id NULL (offered at every branch) is '
    'COALESCEd to a sentinel so those rows are constrained too. Keyed on '
    'lower(btrim(name)) to match how the CSV importer and the branch-override '
    'rule compare names.';

-- Verify
SELECT indexname FROM pg_indexes
WHERE tablename = 'lab_tests' AND indexname = 'idx_unique_lab_test_name_per_branch';
