# Session 13 — Bulk delete for the lab test catalogue

**Date:** 2026-09-18
**Branch:** main
**Migration:** none

## 1. What was reported

Accumax carries 1,392 tests. Removing tests meant one trash-can click and one
confirm per row, which does not work for a catalogue that size.

## 2. What changed

### `POST /admin/lab-tests/bulk-delete` (`app/routers/admin.py`)
* Body `{"ids": [...]}`: 1–1,000 ids per request. **Every id must parse as a
  UUID**, because ids go into a PostgREST `in.(...)` filter. Duplicates are
  collapsed.
* Uses the same rules as `DELETE /lab-tests/{id}`: `LAB_TESTS_MANAGE`, and
  `enforce_clinic_access`. The clinic filter is **in the DELETE itself**, so an
  id from another clinic comes back as "not found", never deleted.
* Rows are read first, for two reasons:
  1. A branch-pinned staff account is held to its branch, the same as create
     and edit. **One foreign-branch test refuses the whole request before
     anything is deleted.**
  2. The audit entry (`bulk_delete_lab_tests`) records the ids and the first
     100 names.
* Reads and deletes run in chunks of 200 ids, so the URL stays small.
* After deleting, it clears the WhatsApp service-type menu cache.
* Returns `{requested, deleted, not_found}`.
* Past bookings are unaffected: `appointments.lab_test_id` is
  `ON DELETE SET NULL` (migration 039), and the booking keeps its test name.

### Admin panel — Test Catalog
* There is a checkbox per row and a select-all box in the header, which
  selects every row currently shown. It respects the service-type tab and the
  search. **Shift-click selects a range.**
* The selection survives tab and search changes, so an admin can search
  "X-ray", tick all, search "USG", tick all, then delete. The bar shows
  "N selected (M not in this view)".
* Before deleting, the confirm names the first five tests and says how many
  more there are. In a branch view it warns when selected tests are offered at
  every branch. **At 20 or more tests, the admin has to type `DELETE`.**
* The panel sends the selection in requests of 1,000. If it fails partway, the
  toast says how many were already deleted, and the list reloads either way.
* The selection is cleared when the clinic changes. On reload, anything that
  no longer exists is dropped from it.
* Checkboxes appear only with `LAB_TESTS_MANAGE`. The per-row delete is
  unchanged.

## 3. Verification

`tests/test_lab_tests_bulk_delete.py` (12 tests) runs the real route against
an in-memory `lab_tests` table. It covers:
* exact deletion;
* another clinic's rows left untouched;
* a 450-test delete in chunks of ≤ 200;
* branch-pinned staff refused all-or-nothing, with nothing deleted and no
  audit entry;
* branch-pinned staff allowed on their own branch plus all-branches tests;
* refusal without the permission;
* non-UUID or empty ids rejected before any query;
* duplicate ids and the request cap;
* no audit entry when nothing is found;
* the menu cache cleared;
* the `ON DELETE SET NULL` foreign key pinned;
* the panel wiring.

The cross-tenant route matrices (`test_admin_super_admin_scope_matrix.py`,
`test_phase2_route_adversarial_matrix.py`) pick up the new route
automatically. The panel's inline JS passes `node --check`.

**Full suite** (`tests/` minus `test_multi_worker_smoke.py`): **2590 passed,
1 skipped, 0 failed** in 359.5s.
