# KK-P0-15 hardening checklist — catalogue clean-up and import endpoints (KK-P4-09)

Audited 2026-10-15 by reading `app/routers/admin.py` (`get_lab_tests_cleanup_suggestions`,
`apply_lab_tests_cleanup`, `LabCleanupApplyRequest`, `auto_classify_lab_tests`, the three
`/lab-tests/import-*` routes, `_process_catalogue_import_job`), `app/services/catalogue_cleaner.py`,
`app/services/lab_classifier.py` and `app/services/price_list_parser.py`. Same columns as [desk.md](desk.md).
Owner decision (2026-10-15): PB-09 import and the per-row clean-up choices (duplicates, prices) are
hand-offs to the Test Catalogue page.

| Endpoint | Action | Perm | Tenant | Branch | Bounds | Idempotent / CAS | Audit row | 5xx | Off-loop | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| `GET /admin/lab-tests/cleanup-suggestions` | `lab_tests.cleanup` (R0) | LAB_TESTS_MANAGE (dependency) | ✔ `clinic_id` filter | ✘ suggestions cover every branch's tests (the page shows the same; catalogue data, no PHI) | ✔ paged read | — | — | ✔ fixed text | ✔ `asyncio.to_thread` for the scan | Registered |
| `POST /admin/lab-tests/cleanup-apply` (prep fixes only) | `lab_tests.fix_prep` (R3, typed count, ≤ 200) | LAB_TESTS_MANAGE | ✔ | ✔ `enforce_branch_scope` per row — but inside the loop, after earlier rows are written (**D-33**); Kriya sends only rows the user may edit, so it never reaches that 403 | ✔ ≤ 1,000 per list; UUIDs; prep ≤ 500; prices 1..1,00,000 at the model | ✔ sets fixed values, a repeat is harmless; Kriya sends `delete_duplicate_ids: []` and `price_fixes: []` always (CAT-05) | ✔ `cleanup_apply_lab_tests` (only after every row; none on a mid-loop failure, **D-33**) | ✔ | ✔ | Registered |
| `POST /admin/lab-tests/auto-classify` | `lab_tests.auto_classify` (R2) | LAB_TESTS_MANAGE | ✔ `resolve_clinic_id_for_write` | ✘ clinic-wide: files unfiled tests at every branch; the preview tells a pinned user so | ✔ chunks of 200 ids | ✔ `apply=false` writes nothing (Kriya's preview, `registry.read_only_post`); `apply=true` only fills `category IS NULL` in the UPDATE's own filter, so a repeat or a heading chosen meanwhile is never overwritten | ✔ `auto_classify_lab_tests` | ✔ | ✔ | Registered. Deterministic keyword rules, **not an LLM** (registry §4.6 said "existing LLM feature, cost shown": corrected) |
| `POST /admin/lab-tests/import-preview` (+ `GET …/{id}`, `POST import-apply/{id}`) | PB-09 → hand-off (help card "Import Price List") | LAB_TESTS_MANAGE | ✔ | ✔ `resolve_owned_branch`; apply only by the preview's creator | ✔ 10 MB, magic bytes | ✔ apply is an atomic claim `pending → applying` | ✔ | ✘ **D-32**: the background job stores `str(e)[:500]` as `failure_reason`, returned by the GET in a 200 body and shown on the page | ✔ (OCR in a thread; AI call through the gateway) | Not registered (owner) |
| `POST /admin/lab-tests/{id}/generate-details` | hand-off (help card "Add Lab Test") | LAB_TESTS_MANAGE | — | — | — | draft only, saves nothing; uses AI credits | — | — | — | Not registered |

Observations:
- `LabPriceFixItem` already bounds the price at the model (422), so the in-loop range check in
  `apply_lab_tests_cleanup` is unreachable; it is not a defect.
- Import: uploading a file starts OCR and an AI call in a background job (up to 120 s OCR, AI credits)
  and writes a staging row before any confirmation. That is why the owner chose a hand-off.
