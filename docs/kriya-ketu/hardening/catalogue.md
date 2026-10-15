# KK-P0-15 hardening checklist — lab catalogue write endpoints (KK-P4-08)

Audited 2026-10-15 by reading the routes in `app/routers/admin.py` (`LabTestCreate`, `LabTestUpdate`,
`create_lab_test`, `update_lab_test`, `delete_lab_test`, `bulk_delete_lab_tests`, `get_lab_tests_admin`,
`LabCollectionWindowUpdate`, `get/update/clear_lab_collection_window`) and migration 038. Same columns as
[desk.md](desk.md). Gaps found → **D-31**, fixed before registration (bug-remediation-register.md).

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `PUT /admin/lab-tests/{id}` `{price_rupees}` | `lab_test.price` (R2), `lab_test.price_bulk` (R3, ≤ 200, typed count) | LAB_TESTS_MANAGE (dependency) | ✔ `enforce_clinic_access` + `clinic_id` filter | ✔ **D-31**: `restrict_to_branch(_staff_branch(user))` (own branch + shared rows; another branch's row → 404). Was: none | ✔ price 1..21,474,836 (**D-31**: INTEGER paise ceiling, was a DB overflow 500); Kriya refuses out-of-range prices before any write | ✔ sets an absolute value, so a repeat is harmless; no CAS — the plan hash holds each row's old `price_paise`, so a price changed by someone else before the confirm gives a new preview | ✔ `update_lab_test` (field names) | ✔ `_friendly_db_error` (fixed sentences) | ✔ | Registered |
| `PUT /admin/lab-tests/{id}` `{is_active:false}` | `lab_test.deactivate` (R2) | LAB_TESTS_MANAGE | ✔ | ✔ (D-31) | ✔ one id | ✔ repeat harmless; already inactive → refused in the preview | ✔ | ✔ | ✔ | Registered. Delete stays H (registry §4.6) |
| `POST /admin/lab-tests` `{name, price_rupees}` | `lab_test.create` (R2) | LAB_TESTS_MANAGE | ✔ `resolve_clinic_id_for_write` | Kriya never sends `branch_id` (shared test); the endpoint checks one when sent | ✔ name stripped, non-empty; price as above. Name length is unbounded (`TEXT`, no 500); Kriya caps it at 100 | ✘ not idempotent, but migration 076 (one name per branch) turns a repeat into 409; Kriya refuses an existing name in the preview and never retries | ✔ `create_lab_test` | ✔ | ✔ | Registered |
| `PUT /admin/lab-collection-window[?branch_id]` | `lab_collection_window.update` (R2) | LAB_TESTS_MANAGE | ✔ | ✔ `enforce_branch_scope` on a named branch. A pinned user may still set the clinic-wide hours (no `branch_id`), like holidays; Kriya always sends a pinned user's own branch | ✔ `HH:MM` validated; `days` free text — Kriya sends back the days it read | ✘ whole-config rewrite (read, set key, write): a concurrent config save can be lost — **D-13** (open), not made worse: one write per run, never retried | ✔ **D-31** `update_lab_collection_window` (was none) | ✔ | ✔ | Registered |
| `GET /admin/lab-tests`, `GET /admin/lab-collection-window`, `GET /admin/me` | previews + read-back | as their pages | ✔ | ✔ (`resolve_owned_branch` on a named branch) | ✔ | — | — | ✔ | ✔ | Used |

Observations (not defects):
- `GET /admin/lab-tests` without `branch_id` returns every branch's rows to a pinned user (the page does
  the same). Kriya filters to the rows that user can now edit, so its previews never offer a row the
  endpoint would refuse.
- Read-back is one `GET /admin/lab-tests` per changed test (a bulk change of N tests reads the catalogue
  N times). Acceptable for N ≤ 200 in-process; ADR-004 asks for a per-write read-back.
- `turnaround_hours` (INTEGER) is still unbounded on create/update; Kriya never sends it. Not logged as a
  defect for Ketu; worth a bound when the page's form is next touched.
